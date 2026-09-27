"""新能力回归：录制 AI 代劳的运行中插话（steer）+ AI 用例断点续跑（resume）。"""
import threading
import time

import pytest

from app import config
config.set("TD_DB", ":memory:")

from app.db import Base, engine  # noqa
from app import models  # noqa
Base.metadata.create_all(engine)

from app.engine import brain_agentscope as B
from app.engine.brain_agentscope import run_brain, steer, drain_steers  # noqa
from test_brain_agentscope import FakePage, _stub_model_factory, _tc, stub_llm  # noqa


# ---------- steer：brain 级 ----------

def test_steer_registry():
    assert steer("R-S", "") == 0          # 空文本不入队
    assert steer("R-S", "改用手动") == 1
    assert steer("R-S", "跳过这步") == 2
    assert drain_steers("R-S") == ["改用手动", "跳过这步"]
    assert drain_steers("R-S") == []       # 取走即清空
    assert drain_steers("") == []          # 无 key 的执行（run_id 空）不启用


def test_steer_injected_at_step_boundary(stub_llm, monkeypatch):
    """插话在下一次工具结果里注入并留痕到该步明细；执行结束后残留插话被清掉。"""
    monkeypatch.setattr(B, "build_model_chain", lambda on_usage=None: _stub_model_factory([
        [_tc("browser_click", think="点登录", target="#btn")],
        [_tc("browser_expect_text", think="断言", value="登录成功")],
        [_tc("GenerateStructuredOutput", passed=True, reason="ok")],
    ]))
    steer("R-ST1", "用户名改成 test01")   # 执行前入队：第一个动作的工具结果即注入
    r = run_brain(FakePage(), "登录", {}, max_steps=6, run_id="R-ST1")
    assert r["status"] == "passed"
    steers = [d.get("steer") for d in r["detail"] if d.get("steer")]
    assert steers == ["用户名改成 test01"]      # 注入到第一个动作的明细
    assert drain_steers("R-ST1") == []          # run_brain 结束时清掉残留


# ---------- steer：录制会话层 ----------

def _inject_rec_session(sid, owner="u1", ai_run_id="rec123"):
    from app.engine import ui_recorder
    sess = {"events": [], "start_url": "http://t/", "done": False, "error": "",
            "mode": "remote", "owner": owner, "created": time.time(),
            "last_active": time.time(), "cmd_q": None, "ret_q": None,
            "frame": b"", "page_url": "http://t/", "frame_seq": 0,
            "frame_wh": (1280, 800),
            "ai": {"goal": "g", "state": "running", "run_id": ai_run_id, "summary": ""}}
    with ui_recorder._lock:
        ui_recorder._sessions[sid] = sess
    return sess


def test_steer_ai_session_gate():
    from app.engine import ui_recorder
    _inject_rec_session("rec-st-1")

    class U:
        id, role = "u1", "member"

    r = ui_recorder.steer_ai("rec-st-1", "改去点提交")
    assert r["ok"] and r["queued"] == 1
    assert drain_steers("rec123") == ["改去点提交"]   # 进的是该轮 AI 的 run_id 通道

    with ui_recorder._lock:   # AI 不在执行 → 拒绝
        ui_recorder._sessions["rec-st-1"]["ai"]["state"] = "passed"
    assert not ui_recorder.steer_ai("rec-st-1", "x")["ok"]
    assert not ui_recorder.steer_ai("rec-none", "x")["ok"]

    with ui_recorder._lock:
        ui_recorder._sessions.pop("rec-st-1", None)


def test_steer_endpoint_enforces_ownership():
    from fastapi.testclient import TestClient
    from app.main import app
    client = TestClient(app)
    client.__enter__()   # 进 context 跑 lifespan（admin 种子/建表）
    H = {"Authorization": "Bearer " + client.post(
        "/api/v1/auth/login", json={"username": "admin", "password": "admin123"}).json()["token"]}
    client.post("/api/v1/auth/users", headers=H,
                json={"username": "steal", "password": "pass123", "role": "member"})
    M = {"Authorization": "Bearer " + client.post(
        "/api/v1/auth/login", json={"username": "steal", "password": "pass123"}).json()["token"]}
    _inject_rec_session("rec-st-2", owner="someone-else")
    try:
        # 他人会话 → 404（不泄露存在性）；不存在的会话 → 200 + ok=False（友好错误契约）
        assert client.post("/api/v1/cases/ui-record/rec-st-2/steer-ai",
                           headers=M, json={"text": "x"}).status_code == 404
        r = client.post("/api/v1/cases/ui-record/no-such/steer-ai",
                        headers=M, json={"text": "x"})
        assert r.status_code == 200 and r.json()["ok"] is False
    finally:
        from app.engine import ui_recorder
        with ui_recorder._lock:
            ui_recorder._sessions.pop("rec-st-2", None)


# ---------- resume：断点续跑 ----------

def test_build_resume_from_failed_run():
    from app.routers.runs import _build_resume
    run = models.TestRun(
        id="R-R1", status="failed",
        saved={"bill_no": "IQ-101", "token": "tok"},
        detail=[
            {"idx": 1, "action": "goto", "url": "http://t/login", "target": "http://t/login",
             "pass": True, "reason": "打开"},
            {"idx": 2, "action": "fill", "target": "#u", "pass": True, "reason": "输入"},
            {"idx": 3, "action": "goto", "url": "http://t/inquiry/create", "pass": True, "reason": "打开"},
            {"idx": 4, "action": "dblclick", "target": "#row", "pass": False, "reason": "Timeout"},
            {"idx": 5, "action": "error", "pass": False, "reason": "断路器"},
        ])
    ctx = _build_resume(run)
    assert ctx["start_url"] == "http://t/inquiry/create"   # 最后一个成功的 goto
    assert "✓ fill #u" in ctx["done_summary"]
    assert "✗ dblclick #row" in ctx["done_summary"]
    assert "error" not in ctx["done_summary"]              # note/error 类不计入
    assert ctx["saved"] == {"bill_no": "IQ-101", "token": "tok"}


_JOB_ERRORS = []
_JOB_FUTS = []


def _fake_submit(fn):
    """与 queue.submit 同构的立即执行替身：后台线程跑，返回 Future（留档便于测试等待）。"""
    import concurrent.futures
    fut = concurrent.futures.Future()
    _JOB_FUTS.append(fut)

    def _wrap():
        try:
            fut.set_result(fn())
        except Exception as e:  # noqa
            import traceback
            _JOB_ERRORS.append(traceback.format_exc()[-800:])
            fut.set_exception(e)
    threading.Thread(target=_wrap, daemon=True).start()
    return fut


@pytest.fixture
def resume_env(monkeypatch):
    """搭好 项目/环境/AI用例 + 一条失败执行记录；run_ai_case 换成捕获 resume 的替身。"""
    from fastapi.testclient import TestClient
    from app.main import app
    client = TestClient(app)
    client.__enter__()   # 进 context 跑 lifespan（admin 种子/建表）
    H = {"Authorization": "Bearer " + client.post(
        "/api/v1/auth/login", json={"username": "admin", "password": "admin123"}).json()["token"]}
    pid = client.post("/api/v1/projects", json={"name": "续跑P"}, headers=H).json()["id"]
    client.post(f"/api/v1/projects/{pid}/envs", headers=H,
                json={"name": "E", "base_url": "http://127.0.0.1:59997"})
    cid = client.post(f"/api/v1/projects/{pid}/cases", headers=H, json={
        "project_id": pid, "name": "长流程用例", "type": "ai", "target": "ui",
        "goal": "创建询价单", "start_url": "/page/login"}).json()["id"]
    captured = {}

    def fake_run_ai_case(case, env, run_id, on_step=None, resume=None):
        captured["resume"] = resume
        captured["run_id"] = run_id
        return {"status": "passed", "pass_n": 2, "fail_n": 0, "duration": 1.0,
                "detail": [{"idx": 1, "action": "done", "pass": True, "reason": "续跑完成"}],
                "saved": {"bill_no": "IQ-105"}, "summary": "续跑完成"}

    import app.routers.runs as R
    monkeypatch.setattr(R, "run_ai_case", fake_run_ai_case)
    monkeypatch.setattr(R, "submit", _fake_submit)
    return client, H, pid, cid, captured


def test_resume_endpoint_runs_with_context(resume_env):
    client, H, pid, cid, captured = resume_env
    from app.db import SessionLocal
    db = SessionLocal()
    try:
        env = db.query(models.Env).filter(models.Env.project_id == pid).first()
        failed = models.TestRun(id="R-RES1", case_id=cid, case_name="长流程用例",
                                env_id=env.id, status="failed",
                                saved={"bill_no": "IQ-101"},
                                detail=[{"idx": 1, "action": "goto", "url": "http://t/login",
                                         "target": "http://t/login", "pass": True, "reason": "打开"}])
        db.add(failed); db.commit()
    finally:
        db.close()

    r = client.post("/api/v1/runs/R-RES1/resume", headers=H)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["resumed_from"] == "R-RES1" and body["status"] in ("running", "passed")
    new_id = body["id"]
    assert new_id != "R-RES1"

    # 等后台执行完成（future 比 DB 轮询确定：内存库单连接下轮询可能与后台写串扰）
    _JOB_FUTS[-1].result(timeout=15)
    db = SessionLocal()
    try:
        row = db.get(models.TestRun, new_id)
    finally:
        db.close()
    assert row.status == "passed"
    assert not _JOB_ERRORS, _JOB_ERRORS[0]
    assert row.saved == {"bill_no": "IQ-105"}     # save 的变量终值已随执行落库
    assert captured["run_id"] == new_id
    assert "✓ goto http://t/login" in captured["resume"]["done_summary"]
    assert captured["resume"]["saved"] == {"bill_no": "IQ-101"}
    assert captured["resume"]["start_url"] == "http://t/login"


def test_resume_endpoint_guards(resume_env):
    client, H, pid, cid, captured = resume_env
    from app.db import SessionLocal
    db = SessionLocal()
    try:
        env = db.query(models.Env).filter(models.Env.project_id == pid).first()
        db.add(models.TestRun(id="R-OK1", case_id=cid, status="passed",
                              env_id=env.id, detail=[]))
        db.commit()
    finally:
        db.close()
    assert client.post("/api/v1/runs/R-OK1/resume", headers=H).status_code == 400   # 只有失败可续
    assert client.post("/api/v1/runs/R-NOSUCH/resume", headers=H).status_code == 404
    assert captured.get("resume") is None


def test_run_ai_case_resume_start_url(monkeypatch):
    """run_ai_case 浏览器路径：resume 带合法起始页时覆盖用例 start_url。"""
    from types import SimpleNamespace
    got = {}

    class FakePW:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_run_brain(page, goal, variables, max_steps=200, run_id="", shot_tag="ai",
                       on_step=None, engine="", page_map="", project_id="",
                       map_keys=None, resume=None, points=None, api_base=""):
        got["start"] = page.url
        got["resume"] = resume
        return {"status": "passed", "pass_n": 1, "fail_n": 0, "duration": 0.1,
                "detail": [], "saved": {}, "summary": "ok"}

    opened = {}

    def fake_goto(url, timeout=0):
        opened["url"] = url

    class FakePage:
        url = "http://t/login"

        def goto(self, u, timeout=0):
            fake_goto(u, timeout)

    class FakeBrowser:
        def new_context(self, **kw):
            return FakeCtx()

        def close(self):
            pass

    class FakeCtx:
        def new_page(self):
            return FakePage()

    import app.engine.ai_runner as AR
    monkeypatch.setattr(AR, "launch_browser", lambda p, e=None: (FakeBrowser(), "chromium", ""))
    monkeypatch.setattr(AR, "ai_drive", fake_run_brain)
    # 连 playwright 驱动一起换掉：本测试只验 run_ai_case 的接线，不起真 node 驱动进程
    monkeypatch.setattr("playwright.sync_api.sync_playwright", lambda: FakePW())
    monkeypatch.setattr("app.engine.ui_runner.start_run_video", lambda page, rid, tag=None: None)
    monkeypatch.setattr("app.engine.ui_runner.finish_run_video", lambda page, v, d: None)
    case = SimpleNamespace(id="c1", name="n", type="ai", project_id="",
                           username="", password="",
                           steps=[{"target": "ui", "goal": "g", "start_url": "/login",
                                   "max_steps": 10}])
    env = SimpleNamespace(id="e1", name="E", base_url="http://t", variables={})
    r = AR.run_ai_case(case, env, "R-R2",
                       resume={"start_url": "http://t/inquiry/create",
                               "done_summary": "✓ 登录", "saved": {"bill_no": "IQ-1"}})
    assert r["status"] == "passed"
    assert opened["url"] == "http://t/inquiry/create"    # 从断点页起跑
    assert got["resume"]["saved"] == {"bill_no": "IQ-1"}
