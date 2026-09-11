"""B1 前置验证：同步队列线程 ↔ async agent 桥接、工具白名单、多模型回退链。

全部用桩模型（不发网络请求）。Playwright 线程亲和性用 FakePage 记录调用线程验证：
所有 page 调用必须发生在调用 run_brain 的线程（模拟执行队列线程）。
"""
import threading

import pytest

from app import config
config.set("TD_DB", ":memory:")

from app.db import Base, engine  # noqa
from app import models  # noqa  确保建表
Base.metadata.create_all(engine)

from app.engine import brain_agentscope as B  # noqa
from app.engine.brain_agentscope import (  # noqa
    ALLOWED_ACTIONS, Harness, build_toolkit, make_fallback_model, run_brain,
)


# ---------- 桩页面：记录每个调用发生的线程 ----------

STATE = {"url": "http://t/login", "title": "登录",
         "elements": [{"selector": "#u", "tag": "input", "text": "", "value": ""},
                      {"selector": "#btn", "tag": "button", "text": "登 录", "value": ""}],
         "text": "运营后台 登录", "vw": 1280, "vh": 800}


class FakeMouse:
    def __init__(self, page):
        self.page = page

    def click(self, x, y):
        self.page._rec("mouse_click", x, y)

    def move(self, x, y):
        self.page._rec("mouse_move", x, y)

    def down(self):
        self.page._rec("mouse_down")

    def up(self):
        self.page._rec("mouse_up")


class FakePage:
    def __init__(self, state=None, body="登录成功", fail_selectors=(), shot=b"shot"):
        self.state = state if state is not None else STATE
        self.body = body
        self.fail_selectors = fail_selectors
        self.shot = shot
        self.calls = []
        self.threads = set()
        self.mouse = FakeMouse(self)

    def _rec(self, *c):
        self.calls.append(c)
        self.threads.add(threading.current_thread().ident)

    def evaluate(self, js, arg=None):
        self._rec("evaluate", arg)
        if arg is not None:            # _GAPS_JS：返回缺失清单（默认无缺失）
            return []
        return self.state

    def goto(self, url, timeout=0, wait_until=None):
        self._rec("goto", url)

    def click(self, sel, timeout=0):
        self._rec("click", sel)
        if sel in self.fail_selectors:
            raise TimeoutError(f"Timeout 8000ms exceeded. waiting for {sel}")

    def fill(self, sel, val, timeout=0):
        self._rec("fill", sel, val)
        if sel in self.fail_selectors:
            raise TimeoutError(f"Timeout 8000ms exceeded. waiting for {sel}")

    def inner_text(self, sel, timeout=0):
        self._rec("inner_text", sel)
        return self.body

    def screenshot(self, path="", full_page=False, type=None, quality=None):
        self._rec("screenshot", path)
        return self.shot


# ---------- 桩模型：脚本化 tool_call 序列 ----------

def _stub_model_factory(script, usage=(10, 5)):
    """script: list[ChatResponse content 列表]；每次 _call_api 弹出一轮。"""
    from agentscope.model import ChatModelBase, ChatResponse, OpenAIChatModel
    from agentscope.model._model_response import ChatUsage
    from agentscope.credential import OpenAICredential
    from agentscope.formatter import OpenAIChatFormatter

    class StubModel(ChatModelBase):
        Parameters = OpenAIChatModel.Parameters

        def __init__(self):
            super().__init__(
                credential=OpenAICredential(api_key="stub", base_url="http://stub.local"),
                model="stub-model", parameters=OpenAIChatModel.Parameters(),
                stream=False, max_retries=0)
            self.formatter = OpenAIChatFormatter()
            self.n = 0

        async def _call_api(self, model_name, messages, tools=None, tool_choice=None, **kw):
            content = script[self.n] if self.n < len(script) else []
            self.n += 1
            return ChatResponse(content=list(content), is_last=True,
                                usage=ChatUsage(input_tokens=usage[0],
                                                output_tokens=usage[1], time=0.01))

    return StubModel()


import itertools
_tc_seq = itertools.count(1)


def _tc(tool_name, **inp):
    import json
    from agentscope.message import ToolCallBlock
    return ToolCallBlock(type="tool_call", id=f"call-{next(_tc_seq)}",
                         name=tool_name, input=json.dumps(inp, ensure_ascii=False))


def _login_script():
    """每次新建（ToolCallBlock 会被 agent 更新 state，跨测试复用会被当成已执行）。"""
    return [
        [_tc("browser_fill", think="填账号", selector="#u", value="${username}")],
        [_tc("browser_click", think="点登录", selector="#btn")],
        [_tc("browser_expect_text", think="验证", value="登录成功")],
        [_tc("GenerateStructuredOutput", passed=True, reason="登录成功")],
    ]


@pytest.fixture
def stub_llm(monkeypatch):
    monkeypatch.setattr(B.A, "llm_available", lambda: True)
    monkeypatch.setattr(B.A, "vision_enabled", lambda: False)
    monkeypatch.setattr(B.A, "_log_usage", lambda *a, **k: None)


# ---------- 验证项①：同步队列线程 ↔ async agent 桥接 ----------

def test_bridge_end_to_end(stub_llm, monkeypatch):
    """run_brain 在“队列线程”同步调用；agent 在独立线程跑事件循环；
    所有 Playwright 调用都被泵回队列线程执行；返回结构与 ai_drive 同构。"""
    monkeypatch.setattr(B, "build_model_chain", lambda on_usage=None: _stub_model_factory(_login_script()))
    page = FakePage()
    seen = []
    caller = threading.current_thread().ident

    r = run_brain(page, "用 ${username} 登录", {"username": "sw01"}, max_steps=8,
                  run_id="t1", on_step=lambda d: seen.append(len(d)))

    assert r["status"] == "passed" and r["fail_n"] == 0
    assert r["brain"] == "agentscope"
    assert [d["action"] for d in r["detail"]] == ["fill", "click", "expect_text", "done"]
    assert r["summary"] == "登录成功"
    assert page.threads == {caller}                     # Playwright 线程亲和性保持
    assert ("fill", "#u", "sw01") in page.calls         # ${username} 已替换
    assert seen == [1, 2, 3, 4]                         # on_step 流式回调
    assert r["detail"][0]["think"] == "填账号"           # think 回填
    assert r["detail"][-1]["screenshot"]                # done 留证截图


def test_bridge_save_and_variables(stub_llm, monkeypatch):
    monkeypatch.setattr(B, "build_model_chain", lambda on_usage=None: _stub_model_factory([
        [_tc("browser_save", think="存单号", name="order_no", value="Q001")],
        [_tc("GenerateStructuredOutput", passed=True, reason="已存单号")],
    ]))
    page = FakePage()
    r = run_brain(page, "存单号", {}, max_steps=5)
    assert r["saved"] == {"order_no": "Q001"}
    assert r["detail"][0]["saved"] == "order_no"


def test_bridge_map_warning(stub_llm, monkeypatch):
    """A2 比对信号在新 brain 同样生效：地图按钮缺失 → 明细记 warning。"""
    monkeypatch.setattr(B, "build_model_chain", lambda on_usage=None: _stub_model_factory(_login_script()))
    monkeypatch.setattr("app.engine.app_mapper.map_gaps",
                        lambda pid, url, page: "地图按钮在当前页面缺失：忘记密码（scan）")
    page = FakePage()
    r = run_brain(page, "登录", {}, max_steps=8, project_id="p1")
    assert r["status"] == "passed"                       # 警告不判失败
    assert "忘记密码" in r["detail"][0].get("warning", "")
    assert r["detail"][1].get("warning", "") == ""       # 同一缺失不重复报


def test_bridge_no_model(stub_llm, monkeypatch):
    monkeypatch.setattr(B, "build_model_chain", lambda on_usage=None: None)
    r = run_brain(FakePage(), "目标", {}, max_steps=3)
    assert r["status"] == "failed" and "无可用模型" in r["summary"]


def test_bridge_llm_not_configured(monkeypatch):
    monkeypatch.setattr(B.A, "llm_available", lambda: False)
    r = run_brain(FakePage(), "目标", {}, max_steps=3)
    assert r["status"] == "failed" and "TD_LLM" in r["detail"][0]["reason"]


def test_bridge_circuit_breaker(stub_llm, monkeypatch):
    """断路器：同一动作+selector 连续失败 3 次 → INTERRUPTED 终止 reply。"""
    monkeypatch.setattr(B, "build_model_chain", lambda on_usage=None: _stub_model_factory(
        [[_tc("browser_fill", think="填", selector="#u", value="x")] for _ in range(10)]))
    page = FakePage(fail_selectors=("#u",))
    r = run_brain(page, "填表单", {}, max_steps=10)
    assert r["status"] == "failed"
    assert "连续 3 次失败" in r["detail"][-1]["reason"]
    fills = [c for c in page.calls if c[0] == "fill"]
    assert len(fills) == 3                              # 熔断后不再撞墙


def test_bridge_cancel(stub_llm, monkeypatch):
    """取消：queue registry 标记取消后，下一个工具执行点终止。"""
    from app.engine import queue as _q
    monkeypatch.setattr(B, "build_model_chain", lambda on_usage=None: _stub_model_factory(
        [[_tc("browser_click", think="点", selector="#btn")] for _ in range(10)]))
    _q.register("cancel-run")
    page = FakePage()
    orig_execute = Harness.execute

    def execute_then_cancel(self, action, think, params):
        out = orig_execute(self, action, think, params)
        _q.cancel("cancel-run")                         # 第一步后用户点了取消
        return out
    monkeypatch.setattr(Harness, "execute", execute_then_cancel)

    r = run_brain(page, "一直点", {}, max_steps=10, run_id="cancel-run")
    assert r["status"] == "failed" and r["summary"] == "用户取消"
    clicks = [c for c in page.calls if c[0] == "click"]
    assert len(clicks) == 1                             # 取消即停


def test_bridge_max_steps(stub_llm, monkeypatch):
    monkeypatch.setattr(B, "build_model_chain", lambda on_usage=None: _stub_model_factory(
        [[_tc("browser_click", think="点", selector="#btn")] for _ in range(30)]))
    page = FakePage()
    r = run_brain(page, "无限点击", {}, max_steps=4)
    assert r["status"] == "failed" and "最大步数" in r["detail"][-1]["reason"]
    clicks = [c for c in page.calls if c[0] == "click"]
    assert len(clicks) == 4


# ---------- 验证项②：工具白名单 ----------

def test_toolkit_whitelist():
    """toolkit 只含浏览器测试动作（+视觉模式 look）；无任何 shell/文件/代码执行工具。"""
    h = Harness(FakePage(), "g", {}, "", "ai", None, "", "", "", vision=False, max_actions=10)
    tk = build_toolkit(h)
    import asyncio
    schemas = asyncio.run(tk.get_tool_schemas())
    names = {s["function"]["name"] if "function" in s else s.get("name") for s in schemas}
    assert names == {f"browser_{a}" for a in ALLOWED_ACTIONS}

    h_v = Harness(FakePage(), "g", {}, "", "ai", None, "", "", "", vision=True, max_actions=10)
    names_v = {s["function"]["name"] if "function" in s else s.get("name")
               for s in asyncio.run(build_toolkit(h_v).get_tool_schemas())}
    assert names_v == names | {"browser_look"}

    forbidden = {"Bash", "Read", "Write", "Edit", "PowerShell", "Glob", "Grep",
                 "execute_python_code", "run_shell"}
    assert not (names_v & forbidden)


def test_harness_rejects_non_whitelist():
    """红线双保险：白名单外的动作即使被构造出来也拒绝执行。"""
    h = Harness(FakePage(), "g", {}, "", "ai", None, "", "", "", vision=False, max_actions=10)
    ok, reason = h.execute("run_shell", "", {"cmd": "rm -rf /"})
    assert not ok and "白名单" in reason
    ok2, reason2 = h.execute("read_file", "", {"path": "/etc/passwd"})
    assert not ok2 and "白名单" in reason2


# ---------- 验证项③：多模型回退链 ----------

class _StubAPI:
    """make_fallback_model 需要的最小模型面：身份字段 + _call_api。"""

    def __init__(self, name, fail=False):
        from agentscope.credential import OpenAICredential
        from agentscope.model import OpenAIChatModel
        self.credential = OpenAICredential(api_key="k", base_url=f"http://{name}.local")
        self.model = name
        self.parameters = OpenAIChatModel.Parameters()
        self.max_retries = 0
        self.context_size = 128000
        self.fail = fail
        self.called = 0

    async def _call_api(self, model_name, messages, tools=None, tool_choice=None, **kw):
        import asyncio
        self.called += 1
        if self.fail:
            await asyncio.sleep(0)
            raise RuntimeError(f"{model_name} 挂了")
        from agentscope.model import ChatResponse
        from agentscope.model._model_response import ChatUsage
        from agentscope.message import TextBlock
        return ChatResponse(content=[TextBlock(type="text", text="ok")], is_last=True,
                            usage=ChatUsage(input_tokens=7, output_tokens=3, time=0.01))


def test_fallback_chain_switches_model():
    import asyncio
    from agentscope.message import UserMsg
    a, b = _StubAPI("flash", fail=True), _StubAPI("plus")
    usage_log = []
    fb = make_fallback_model([a, b], on_usage=lambda m, pt, ct, ok: usage_log.append((m, pt, ct, ok)))
    msgs = [UserMsg(name="u", content="hi")]
    resp = asyncio.run(fb._call_api(fb.model, msgs))
    assert a.called == 1 and b.called == 1
    assert resp.content[0].text == "ok"
    assert usage_log == [("flash", 0, 0, False), ("plus", 7, 3, True)]   # llm_logs 记账口径


def test_fallback_chain_all_fail_raises():
    import asyncio
    from agentscope.message import UserMsg
    a, b = _StubAPI("m1", fail=True), _StubAPI("m2", fail=True)
    fb = make_fallback_model([a, b])
    with pytest.raises(RuntimeError):
        asyncio.run(fb._call_api(fb.model, [UserMsg(name="u", content="hi")]))


def test_chain_entries_from_llm_configs(stub_llm):
    """链来源 = llm_configs 使用中条目 + .env 兜底（去重）。"""
    from app.db import SessionLocal
    from app.models import LLMConfig
    db = SessionLocal()
    db.add(LLMConfig(name="主力", vendor="qwen", base_url="http://db.local/v1",
                     api_key="dbkey", model="qwen-flash", is_active=True))
    db.commit(); db.close()
    monkey_base, monkey_key = "http://env.local/v1", "envkey"
    import app.ai as A
    orig = A.fallback_llm
    A.fallback_llm = lambda: (monkey_base, monkey_key, "qwen-plus")
    try:
        entries = B._chain_entries()
    finally:
        A.fallback_llm = orig
    assert ("http://db.local/v1", "dbkey", "qwen-flash") in entries
    assert (monkey_base, monkey_key, "qwen-plus") in entries
    db = SessionLocal()
    db.query(LLMConfig).delete(); db.commit(); db.close()


# ---------- B4：ai_drive 门面（AgentScope 唯一路径） ----------

def test_ai_drive_facade_delegates(monkeypatch):
    """ai_drive 委托 run_brain，返回结构去掉内部标注，参数原样透传。"""
    from app.engine import ai_runner

    captured = {}

    def fake_run_brain(page, goal, variables, **kw):
        captured["kw"] = kw
        return {"status": "passed", "pass_n": 2, "fail_n": 0, "duration": 0.5,
                "detail": [{"idx": 1, "action": "done", "pass": True, "reason": "ok"}],
                "saved": {"k": "v"}, "summary": "完成",
                "brain": "agentscope", "usage": [{"model": "m", "input_tokens": 1}]}
    monkeypatch.setattr("app.engine.brain_agentscope.run_brain", fake_run_brain)

    r = ai_runner.ai_drive(FakePage(), "目标", {"username": "a"}, max_steps=9,
                           run_id="r1", project_id="p9", page_map="地图")
    assert r["status"] == "passed" and r["saved"] == {"k": "v"}
    assert "brain" not in r and "usage" not in r          # 内部标注不外泄
    assert captured["kw"]["project_id"] == "p9"           # 参数原样透传
    assert captured["kw"]["max_steps"] == 9
    assert captured["kw"]["page_map"] == "地图"


# ---------- 视觉（B4 前半：截图进消息） ----------

def test_vision_look_tool(stub_llm, monkeypatch):
    """视觉模式注册 look 工具；模型 look 后拿到截图 DataBlock 再决策坐标。"""
    monkeypatch.setattr(B.A, "vision_enabled", lambda: True)
    script = [
        [_tc("browser_look", think="先看截图")],
        [_tc("browser_click_xy", think="点滑块", x=50, y=300)],
        [_tc("GenerateStructuredOutput", passed=True, reason="完成")],
    ]
    monkeypatch.setattr(B, "build_model_chain", lambda on_usage=None: _stub_model_factory(script))
    page = FakePage()
    r = run_brain(page, "过验证码", {}, max_steps=6)
    assert r["status"] == "passed"
    assert [d["action"] for d in r["detail"]] == ["click_xy", "done"]   # look 不记为动作步骤
    assert ("mouse_click", 50, 300) in page.calls                       # 坐标点击落地
