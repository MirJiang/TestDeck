"""测试设计先行（用例级验收断言）：断言原语、brain 验证/定性/兜底环、用例 API、AI 起草。"""
from app import config
config.set("TD_DB", ":memory:")

from app.db import Base, engine  # noqa
from app import models  # noqa
Base.metadata.create_all(engine)

from app.engine import brain_agentscope as B
from app.engine.assertions import run_assertion, verify_assertions  # noqa
from test_brain_agentscope import _stub_model_factory, _tc, stub_llm  # noqa


# ---------- 断言原语 ----------

class PPage:
    """够用的页面桩：inner_text/locator/click（body 可被 click 改写，模拟纠正后状态）。"""

    def __init__(self, body="登录页", url="http://t/login", click_body=None):
        self.body, self.url, self.click_body = body, url, click_body
        self.calls = []

    def inner_text(self, sel, timeout=0):
        self.calls.append("inner_text")
        return self.body

    def click(self, sel, timeout=0, force=False):
        self.calls.append(("click", sel))
        if self.click_body is not None:
            self.body = self.click_body

    def locator(self, sel):
        parent = self

        class Loc:
            first = None

            def __init__(s):
                Loc.first = s

            def wait_for(self, state="visible", timeout=0):
                if sel != "#msg":
                    raise TimeoutError("not visible")
        return Loc()


class HttpStub:
    def __init__(self, status=200, json_data=None):
        self.status, self.json_data = status, json_data
        self.requests = []

    def request(self, m, url):
        self.requests.append((m, url))
        resp = type("R", (), {"status_code": self.status,
                              "json": lambda s: self.json_data})()
        return resp


def test_assert_primitives():
    page = PPage(body="登录成功，欢迎回来 IQ-101")
    ok = run_assertion({"type": "expect_text", "value": "登录成功"}, page=page)
    assert ok["ok"] and "登录成功" in ok["assert"]
    bad = run_assertion({"type": "expect_text", "value": "不存在"}, page=page)
    assert not bad["ok"]
    notxt = run_assertion({"type": "expect_not_text", "value": "密码错误"}, page=page)
    assert notxt["ok"]
    u = run_assertion({"type": "expect_url", "value": "/login"}, page=page)
    assert u["ok"] and "URL" in u["assert"]
    el = run_assertion({"type": "expect_element", "selector": "#msg"}, page=page)
    assert el["ok"]
    el_bad = run_assertion({"type": "expect_element", "selector": "#none"}, page=page)
    assert not el_bad["ok"]
    unknown = run_assertion({"type": "magic"}, page=page)
    assert not unknown["ok"] and "未知断言类型" in unknown["reason"]


def test_assert_api_with_variables():
    http = HttpStub(json_data={"status": 0, "data": {"from": "上海"}})
    r = run_assertion({"type": "expect_api", "m": "GET", "url": "/api/inquiry/${bill_no}",
                       "field": "status", "expect": "0"},
                      variables={"bill_no": "IQ-9"}, api_base="http://t", http=http)
    assert r["ok"]
    assert http.requests == [("GET", "http://t/api/inquiry/IQ-9")]   # ${bill_no} 已替换并拼 base
    r2 = run_assertion({"type": "expect_api", "m": "GET", "url": "/api/x",
                        "field": "data.from", "expect": "北京"}, http=http)
    assert not r2["ok"] and "上海" in r2["reason"]
    assert verify_assertions([], page=PPage()) == []


def test_verify_stops_at_first_failure():
    page = PPage(body="只有一半")
    rs = verify_assertions([
        {"type": "expect_text", "value": "只有一半"},
        {"type": "expect_text", "value": "缺的"},
        {"type": "expect_url", "value": "/never"},   # 不应执行（前面已失败）
    ], page=page)
    assert len(rs) == 2 and rs[0]["ok"] and not rs[1]["ok"]


# ---------- brain：测试点验证 / 复核重试 / 定性 / 兜底 ----------

PTS = [{"name": "登录成功", "intent": "登录",
        "asserts": [{"type": "expect_text", "value": "登录成功"},
                    {"type": "expect_url", "value": "/home"}]}]


def _run_points(script, page, pts=PTS):
    return B.run_brain(page, "登录", {}, max_steps=10, run_id="tp", points=pts)


def test_point_pass_via_engine(stub_llm, monkeypatch):
    monkeypatch.setattr(B, "build_model_chain", lambda on_usage=None: _stub_model_factory([
        [_tc("browser_verify", think="验证", point="登录成功")],
        [_tc("GenerateStructuredOutput", passed=True, reason="操作完成")],
    ]))
    page = PPage(body="登录成功，欢迎回来", url="http://t/home")
    r = _run_points(None, page)
    assert r["status"] == "passed"
    assert r["points"][0]["status"] == "passed"
    assert [d["action"] for d in r["detail"]][0] == "verify"
    assert "测试点 1/1 通过" in r["summary"]


def test_fail_then_retry_then_pass(stub_llm, monkeypatch):
    """断言未通过不立即终判：AI 判断是页面未就绪 → 纠正（click 改写 body）→ 重新验证通过。"""
    monkeypatch.setattr(B, "build_model_chain", lambda on_usage=None: _stub_model_factory([
        [_tc("browser_verify", think="先验证", point="登录成功")],          # 失败（body 还没就绪）
        [_tc("browser_click", think="点登录触发跳转", target="#btn")],      # 纠正：body→登录成功
        [_tc("browser_verify", think="再验证", point="登录成功")],          # 通过（URL 用 /home 的页）
        [_tc("GenerateStructuredOutput", passed=True, reason="操作完成")],
    ]))
    page = PPage(body="加载中", url="http://t/home", click_body="登录成功，欢迎回来")
    r = _run_points(None, page)
    assert r["points"][0]["status"] == "passed" and r["points"][0]["attempts"] == 2
    assert r["status"] == "passed"
    vs = [d for d in r["detail"] if d["action"] == "verify"]
    assert len(vs) == 2 and not vs[0]["pass"] and vs[1]["pass"]


def test_conclude_defect_and_done_cannot_flip(stub_llm, monkeypatch):
    """AI 定性 defect；done(passed=True) 不能把引擎判定翻成通过。"""
    monkeypatch.setattr(B, "build_model_chain", lambda on_usage=None: _stub_model_factory([
        [_tc("browser_verify", think="验证", point="登录成功")],
        [_tc("browser_conclude", think="确认是缺陷", point="登录成功",
             verdict="defect", reason="点击登录后始终停留在登录页")],
        [_tc("GenerateStructuredOutput", passed=True, reason="操作都做了")],
    ]))
    r = _run_points(None, PPage(body="登录页", url="http://t/login"))
    assert r["points"][0]["status"] == "failed"
    assert "被测系统缺陷" in r["summary"] or "未通过" in r["summary"]
    assert r["status"] == "failed"          # 关键不变量：done 不能改判


def test_verify_attempts_exhausted(stub_llm, monkeypatch):
    n = {"i": 0}

    def script_factory():
        pass

    rounds = [[_tc("browser_verify", think="再试", point="登录成功")]
              for _ in range(B.MAX_VERIFY_ATTEMPTS + 1)]
    rounds.append([_tc("browser_conclude", think="定性", point="登录成功",
                       verdict="blocked", reason="登录服务无响应")])
    rounds.append([_tc("GenerateStructuredOutput", passed=False, reason="blocked")])
    monkeypatch.setattr(B, "build_model_chain", lambda on_usage=None: _stub_model_factory(rounds))
    r = _run_points(None, PPage(body="登录页", url="http://t/login"))
    p = r["points"][0]
    assert p["attempts"] == B.MAX_VERIFY_ATTEMPTS and p["status"] == "blocked"
    assert r["status"] == "failed"


def test_finalize_auto_verifies_missing_points(stub_llm, monkeypatch):
    """模型没验证任何测试点就 done：收尾引擎兜底跑断言（判定不依赖模型自觉）。"""
    monkeypatch.setattr(B, "build_model_chain", lambda on_usage=None: _stub_model_factory([
        [_tc("GenerateStructuredOutput", passed=True, reason="我觉得好了")],
    ]))
    page = PPage(body="登录成功，欢迎回来", url="http://t/home")
    r = _run_points(None, page)
    assert r["points"][0]["status"] == "passed"     # 兜底验证真实通过
    assert any(d["action"] == "verify" and d["reason"].startswith("收尾兜底")
               for d in r["detail"])
    # 没到达状态的兜底就是失败：
    monkeypatch.setattr(B, "build_model_chain", lambda on_usage=None: _stub_model_factory([
        [_tc("GenerateStructuredOutput", passed=True, reason="好了")],
    ]))
    r2 = _run_points(None, PPage(body="登录页", url="http://t/login"))
    assert r2["points"][0]["status"] == "failed" and r2["status"] == "failed"


def test_no_points_keeps_legacy_behavior(stub_llm, monkeypatch):
    """无测试点的旧用例：判定仍是 done 自评（兼容存量）。"""
    monkeypatch.setattr(B, "build_model_chain", lambda on_usage=None: _stub_model_factory([
        [_tc("GenerateStructuredOutput", passed=True, reason="done")],
    ]))
    r = B.run_brain(PPage(body="x"), "g", {}, max_steps=5)
    assert r["status"] == "passed" and "points" not in r


# ---------- 用例 API 与 AI 起草 ----------

def _client_and_headers():
    from fastapi.testclient import TestClient
    from app.main import app
    client = TestClient(app)
    client.__enter__()
    H = {"Authorization": "Bearer " + client.post(
        "/api/v1/auth/login", json={"username": "admin", "password": "admin123"}).json()["token"]}
    return client, H


def test_case_test_points_roundtrip():
    client, H = _client_and_headers()
    pid = client.post("/api/v1/projects", json={"name": "设计P"}, headers=H).json()["id"]
    pts = [{"name": "登录成功", "intent": "登录",
            "asserts": [{"type": "expect_text", "value": "登录成功"},
                        {"type": "expect_api", "m": "GET", "url": "/api/me",
                         "field": "status", "expect": "0"}]}]
    cid = client.post(f"/api/v1/projects/{pid}/cases", headers=H, json={
        "project_id": pid, "name": "带设计的用例", "type": "ai", "target": "ui",
        "goal": "登录", "test_points": pts}).json()["id"]
    got = client.get(f"/api/v1/cases/{cid}", headers=H).json()
    assert got["steps"][0]["test_points"] == pts
    # 未知断言类型拒绝（引擎执行不了的预期不放行）
    r = client.post(f"/api/v1/projects/{pid}/cases", headers=H, json={
        "project_id": pid, "name": "坏设计", "type": "ai", "target": "ui", "goal": "x",
        "test_points": [{"name": "p", "asserts": [{"type": "看起来对", "value": "1"}]}]})
    assert r.status_code == 400


def test_draft_design_endpoint(monkeypatch):
    client, H = _client_and_headers()
    from app import ai as A
    monkeypatch.setattr(A, "chat_json", None)   # 占位：下一行真正替换（async）
    async def fake_chat(sys_prompt, payload, kind=""):
        assert "测试设计师" in sys_prompt and "expect_api" in sys_prompt
        return {"points": [{"name": "登录成功", "intent": "登录",
                            "asserts": [{"type": "expect_text", "value": "登录成功"}]}],
                "note": "selector 请人工核对"}
    monkeypatch.setattr("app.routers.ai.A.chat_json", fake_chat)
    r = client.post("/api/v1/ai/draft-design", headers=H,
                    json={"goal": "测货主创建询价单"})
    assert r.status_code == 200
    body = r.json()
    assert body["points"][0]["asserts"][0]["type"] == "expect_text"
    assert "人工核对" in body["note"]

    async def bad_chat(sys_prompt, payload, kind=""):
        return {"points": [{"name": "p", "asserts": [{"type": "瞎写的断言"}]}]}
    monkeypatch.setattr("app.routers.ai.A.chat_json", bad_chat)
    r2 = client.post("/api/v1/ai/draft-design", headers=H, json={"goal": "x"})
    assert r2.json()["points"] == []   # 非法原语被 _validated_points 拦掉，回空并提示手动添加


def test_run_ai_case_passes_points(monkeypatch):
    """run_ai_case 把测试点与环境地址传进 ai_drive（断言里的 expect_api 用得到）。"""
    from types import SimpleNamespace
    import app.engine.ai_runner as AR
    got = {}

    def fake_drive(page, goal, variables, max_steps=200, run_id="", shot_tag="ai",
                   on_step=None, engine="", page_map="", project_id="", map_keys=None,
                   resume=None, points=None, api_base=""):
        got["points"], got["api_base"] = points, api_base
        return {"status": "passed", "pass_n": 1, "fail_n": 0, "duration": 0.1,
                "detail": [], "saved": {}, "summary": "ok"}

    class FB:
        def new_context(self, **kw):
            class Ctx:
                def new_page(s):
                    class P:
                        url = "http://t"

                        def goto(self, u, timeout=0):
                            pass
                    return P()
            return Ctx()

        def close(self):
            pass

    monkeypatch.setattr(AR, "launch_browser", lambda p, e=None: (FB(), "chromium", ""))
    monkeypatch.setattr(AR, "ai_drive", fake_drive)
    monkeypatch.setattr("app.engine.ui_runner.start_run_video", lambda page, rid, tag=None: None)
    monkeypatch.setattr("app.engine.ui_runner.finish_run_video", lambda page, v, d: None)

    class FakePW:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr("playwright.sync_api.sync_playwright", lambda: FakePW())
    pts = [{"name": "p1", "intent": "", "asserts": []}]
    case = SimpleNamespace(id="c", name="n", type="ai", project_id="",
                           username="", password="",
                           steps=[{"target": "ui", "goal": "g", "start_url": "/login",
                                   "test_points": pts}])
    env = SimpleNamespace(id="e", name="E", base_url="http://t", variables={})
    AR.run_ai_case(case, env, "R-TP")
    assert got["points"] == pts and got["api_base"] == "http://t"
