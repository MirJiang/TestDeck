"""AI-API 用例引擎：假 HTTP 客户端 + 脚本化模型，验证请求循环 / 固化回放 / 分派。"""
import os  # noqa: F401

from app import config
config.set("TD_DB", ":memory:")

from types import SimpleNamespace

from app.engine import ai_runner, ui_runner
from app.engine.ai_runner import run_ai_case, run_ai_api_case


class FakeResp:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = payload
        self.text = text or (str(payload) if payload is not None else "")

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeClient:
    """httpx.Client 替身：记录请求并按队列返回响应。"""

    last = None

    def __init__(self, responses=None, **kw):
        self.base_url = kw.get("base_url")
        self.calls = []
        self.responses = list(responses or [])

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def request(self, m, url, headers=None, content=None):
        FakeClient.last = self
        self.calls.append({"m": m, "url": url, "headers": headers, "content": content})
        return self.responses.pop(0) if self.responses else FakeResp(200, {"code": 0})


def _enable(monkeypatch, script, vision=False):
    monkeypatch.setattr("app.ai.llm_available", lambda: True)
    monkeypatch.setattr("app.ai.vision_enabled", lambda: vision)
    it = iter(script)
    monkeypatch.setattr("app.ai.chat_json_sync", lambda *a, **k: next(it))


ENV = SimpleNamespace(variables={"username": "sw01"}, base_url="http://t:9001")


def test_api_loop_request_save_done(monkeypatch):
    _enable(monkeypatch, [
        {"think": "创建单据", "action": "request",
         "request": {"m": "POST", "url": "/api/inquiry/create",
                     "headers": {"X-User": "${username}"}, "body": '{"from":"上海"}'},
         "check": {"type": "field_eq", "field": "code", "expect": "0"},
         "save": {"name": "inq", "from": "data.id"}},
        {"think": "完成", "action": "done", "pass": True, "reason": "单据已创建"},
    ])
    responses = [FakeResp(200, {"code": 0, "data": {"id": "Q1"}})]
    monkeypatch.setattr(ai_runner.httpx, "Client", lambda **kw: FakeClient(responses, **kw))

    case = SimpleNamespace(steps=[{"target": "api", "goal": "创建询价单", "max_steps": 5}])
    r = run_ai_case(case, ENV, "R-TEST")
    assert r["status"] == "passed" and r["saved"] == {"inq": "Q1"}
    assert r["detail"][0]["action"] == "request" and r["detail"][0]["pass"] is True
    assert r["detail"][0]["request"]["m"] == "POST"          # 固化所需全量已记录
    assert FakeClient.last.calls[0]["url"] == "/api/inquiry/create"
    assert FakeClient.last.calls[0]["headers"]["X-User"] == "sw01"     # ${username} 已替换
    assert FakeClient.last.calls[0]["content"] == '{"from":"上海"}'


def test_api_loop_check_fail(monkeypatch):
    _enable(monkeypatch, [
        {"action": "request", "request": {"m": "GET", "url": "/api/x"},
         "check": {"type": "field_eq", "field": "code", "expect": "0"}},
        {"action": "done", "pass": False, "reason": "接口返回异常"},
    ])
    monkeypatch.setattr(ai_runner.httpx, "Client",
                        lambda **kw: FakeClient([FakeResp(500, {"code": 1})], **kw))
    case = SimpleNamespace(steps=[{"target": "api", "goal": "g", "max_steps": 4}])
    r = run_ai_api_case(case, ENV, "R-T2")
    assert r["status"] == "failed" and r["fail_n"] == 2   # 请求失败 + done 判未达成
    assert r["detail"][0]["status"] == 500


def test_api_fixed_replay(monkeypatch):
    """固化产物回放：不调模型，按存下的 request/check 原样重发。"""
    called = {"n": 0}
    monkeypatch.setattr("app.ai.chat_json_sync", lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    monkeypatch.setattr(ai_runner.httpx, "Client",
                        lambda **kw: FakeClient([FakeResp(200, {"code": 0, "data": {"id": "Q9"}})], **kw))
    fixed = [{"request": {"m": "POST", "url": "/api/inquiry/create", "headers": {}, "body": '{"from":"上海"}'},
              "check": {"type": "field_eq", "field": "code", "expect": "0"},
              "save": {"name": "inq", "from": "data.id"}}]
    case = SimpleNamespace(steps=[{"target": "api", "goal": "", "fixed_api_steps": fixed}])
    r = run_ai_case(case, ENV, "R-T3")
    assert r["status"] == "passed" and "固定请求回放" in r["summary"]
    assert called["n"] == 0 and r["saved"] == {"inq": "Q9"}   # 零 token


def test_dispatch_fixed_ui_replay(monkeypatch):
    """UI 固定步骤分派：转交 ui_runner._sync_run 并透传引擎。"""
    got = {}
    def fake_sync(case, env, run_id, engine=None):
        got["engine"] = engine
        got["steps"] = case.steps
        return {"status": "passed", "pass_n": 2, "fail_n": 0, "detail": [], "summary": ""}
    monkeypatch.setattr(ui_runner, "_sync_run", fake_sync)
    case = SimpleNamespace(steps=[{"target": "ui", "goal": "", "engine": "chromium",
                                   "fixed_steps": [{"action": "goto", "url": "/x"}]}])
    r = run_ai_case(case, ENV, "R-T4")
    assert r["status"] == "passed"
    assert got["engine"] == "chromium" and got["steps"][0]["action"] == "goto"


def test_build_messages_with_image():
    """多模态消息构造：无图纯文本 content，有图转 image_url 块。"""
    from app.ai import build_messages
    text_only = build_messages("sys", "hello")
    assert isinstance(text_only[1]["content"], str)
    with_img = build_messages("sys", "hello", image_b64="QUJD")
    parts = with_img[1]["content"]
    assert parts[0]["type"] == "image_url" and parts[0]["image_url"]["url"].endswith("QUJD")
    assert parts[1]["type"] == "text"
