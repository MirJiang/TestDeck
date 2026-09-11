"""C2 浏览器会话 MCP 化：命令泵、白名单动作、会话变量、归属与限额（不起真浏览器）。"""
import pytest

from app import config
config.set("TD_DB", ":memory:")
config.set("TD_MCP", "0")

from app.engine import browser_sessions as BS  # noqa
from app.engine.browser_sessions import (  # noqa
    BrowserSession, open_session, get_session, list_sessions, close_session,
)

STATE = {"url": "http://t/orders", "title": "订单",
         "elements": [{"selector": "#btn", "tag": "button", "text": "查询", "value": ""}],
         "text": "订单列表", "vw": 1280, "vh": 800}


class FakeMouse:
    def __init__(self, page):
        self.page = page

    def click(self, x, y):
        self.page.calls.append(("mouse_click", x, y))


class FakePage:
    def __init__(self):
        self.calls = []
        self.mouse = FakeMouse(self)

    def evaluate(self, js, arg=None):
        return STATE

    def goto(self, url, timeout=0, wait_until=None):
        self.calls.append(("goto", url))

    def click(self, sel, timeout=0):
        self.calls.append(("click", sel))

    def fill(self, sel, val, timeout=0):
        self.calls.append(("fill", sel, val))

    def inner_text(self, sel, timeout=0):
        return "订单列表 Q001"

    def screenshot(self, path="", full_page=False, type=None, quality=None):
        self.calls.append(("screenshot", path, type))
        return b"shot"


def _fake_main(self):
    """替换真实浏览器启动：假页面 + 正常泵送（线程模型不变，含 open 时初始导航）。"""
    self.page = FakePage()
    if self.url_target:
        self.page.goto(self.url_target, timeout=30000)
    self.ready.set()
    self._pump()
    self.closed = True


@pytest.fixture(autouse=True)
def fake_browser(monkeypatch):
    monkeypatch.setattr(BrowserSession, "_main", _fake_main)
    yield
    # 清场：关掉测试开的所有会话
    for sid in list(BS._sessions):
        BS._sessions.pop(sid, None)


def test_open_state_act_close():
    info = open_session("u1", "http://t/orders")
    sid = info["session_id"]
    sess = get_session(sid, "u1")
    st = sess.post(sess.do_state).result(timeout=5)
    assert st["url"] == "http://t/orders" and st["elements"][0]["selector"] == "#btn"
    assert ("goto", "http://t/orders") in sess.page.calls      # open 时已导航

    r = sess.post(sess.do_action, "click", {"selector": "#btn"}).result(timeout=5)
    assert r["ok"] and ("click", "#btn") in sess.page.calls
    assert r["state"]["url"] == "http://t/orders"              # 动作后回带最新状态

    close_session(sid, "u1")
    assert sid not in BS._sessions
    with pytest.raises(KeyError):
        get_session(sid, "u1")


def test_whitelist_enforced():
    """红线：白名单外动作（shell/文件/代码执行）一律拒绝，页面不被触碰。"""
    sess_id = open_session("u1", "http://t/")["session_id"]
    sess = get_session(sess_id, "u1")
    for action in ("run_shell", "read_file", "execute_python", "Bash"):
        r = sess.post(sess.do_action, action, {"cmd": "rm -rf /"}).result(timeout=5)
        assert not r["ok"] and "白名单" in r["reason"]
    assert sess.page.calls == [] or all(c[0] == "goto" for c in sess.page.calls)


def test_session_variables_save_and_substitute():
    """save 存会话变量，后续动作 ${var} 自动替换（与平台执行语义一致）。"""
    sess_id = open_session("u1", "http://t/")["session_id"]
    sess = get_session(sess_id, "u1")
    r = sess.post(sess.do_action, "save", {"name": "kw", "value": "Q001"}).result(timeout=5)
    assert r["ok"] and r["saved"] == {"kw": "Q001"}
    r2 = sess.post(sess.do_action, "fill", {"selector": "#kw", "value": "${kw}"}).result(timeout=5)
    assert r2["ok"] and ("fill", "#kw", "Q001") in sess.page.calls


def test_expect_text_and_screenshot():
    sess_id = open_session("u1", "http://t/")["session_id"]
    sess = get_session(sess_id, "u1")
    r = sess.post(sess.do_action, "expect_text", {"value": "Q001"}).result(timeout=5)
    assert r["ok"] and "包含" in r["reason"]
    r2 = sess.post(sess.do_action, "expect_text", {"value": "不存在的字"}).result(timeout=5)
    assert not r2["ok"]
    shot = sess.post(sess.do_screenshot).result(timeout=5)
    assert shot["url"].startswith("/static/")


def test_ownership_and_admin():
    sess_id = open_session("u1", "http://t/")["session_id"]
    with pytest.raises(KeyError):
        get_session(sess_id, "u2")                    # 别人拿不到
    assert get_session(sess_id, "u2", is_admin=True)  # 管理员可以
    rows = list_sessions("u1")
    assert [r["session_id"] for r in rows] == [sess_id]
    assert list_sessions("u2") == []


def test_limits(monkeypatch):
    monkeypatch.setattr(BS, "_MAX_PER_USER", 2)
    monkeypatch.setattr(BS, "_MAX_GLOBAL", 3)
    open_session("u1", "http://t/")
    open_session("u1", "http://t/")
    with pytest.raises(RuntimeError, match="每个用户"):
        open_session("u1", "http://t/")
    open_session("u2", "http://t/")
    with pytest.raises(RuntimeError, match="全局上限"):
        open_session("u3", "http://t/")


def test_closed_session_post_raises():
    sess_id = open_session("u1", "http://t/")["session_id"]
    sess = get_session(sess_id, "u1")
    sess.close()
    sess.closed = True
    with pytest.raises(RuntimeError):
        sess.post(sess.do_state).result(timeout=2)


def test_mcp_server_builds_with_browser_tools():
    """MCP 服务可构建（工具签名/注册无误），浏览器工具组在列。"""
    import asyncio
    from app.mcp_server import _build
    m = _build()

    async def _names():
        tools = await m.list_tools()
        return {t.name for t in tools}
    names = asyncio.run(_names())
    assert {"browser_open", "browser_state", "browser_act", "browser_screenshot",
            "browser_sessions", "browser_close", "app_map_upsert"} <= names


def test_settings_mcp_returns_tools_catalog():
    """设置页「MCP 接入」返回的配置附带完整工具清单（名称+用途说明），
    外部 AI 未连接也能知道平台暴露了哪些接口、各自干嘛。"""
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c:
        tok = c.post("/api/v1/auth/login",
                     json={"username": "admin", "password": "admin123"}).json()["token"]
        r = c.get("/api/v1/settings/mcp", headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 200
        data = r.json()
        assert "mcpServers" in data["config_json"]
        descs = {t["name"]: t["description"] for t in data["tools"]}
        assert {"projects_list", "case_run", "app_map_upsert",
                "browser_open", "browser_act"} <= set(descs)
        assert descs["browser_act"] and "白名单" in descs["browser_act"]   # 用途说明含关键约束
        assert all(d.strip() for d in descs.values())                      # 每个工具都有说明
