"""浏览器会话 MCP 化（批次 C2）：外部 agent 借平台受控浏览器执行环境干活。

他们出脑子（决定点什么填什么），我们出手：Playwright 页面 + 白名单动作集 +
状态提取 + 截图留档。红线与 Brain 一致——只暴露 goto/click/fill/expect_text/
click_xy/drag/save 动作与只读 state/screenshot，绝无 shell/文件/代码执行能力。

线程模型：每个会话独占一个线程（Playwright sync API 有线程亲和性，page 调用必须
发生在创建它的线程），命令经 SimpleQueue + concurrent.Future 转发；MCP 工具在
事件循环里 `await wrap_future(...)` 拿结果，两个世界互不阻塞。

生命周期：空闲超过 TTL（默认 30 分钟，TD_BROWSER_SESSION_TTL 可配）自动回收；
每用户上限（默认 2）与全局上限（默认 8）防资源耗尽；owner 校验在 MCP 层。
"""
import threading
import time
import uuid
from concurrent.futures import Future
from queue import Empty, SimpleQueue

from .. import config
from .ai_runner import _STATE_JS, _exec_action
from .brain_agentscope import ALLOWED_ACTIONS
from .ui_runner import STATIC_DIR

_TTL = 1800          # 空闲回收秒数（TD_BROWSER_SESSION_TTL 覆盖）
_MAX_PER_USER = 2
_MAX_GLOBAL = 8

_sessions: dict[str, "BrowserSession"] = {}
_lock = threading.Lock()
_sweeper_started = False


class BrowserSession:
    """一个受控浏览器会话：独占线程 + 命令泵 + 会话级变量表。"""

    def __init__(self, sid: str, user_id: str, url: str, engine: str = ""):
        self.id = sid
        self.user_id = user_id
        self.engine = engine
        self.url_target = url
        self.variables: dict = {}
        self.last_used = time.time()
        self.created_at = time.time()
        self.closed = False
        self.error = ""
        self.engine_note = ""
        self.ready = threading.Event()
        self.cmds: SimpleQueue = SimpleQueue()
        self._thread = threading.Thread(target=self._main, daemon=True,
                                        name=f"bsess-{sid[:8]}")

    # ---------- 生命周期 ----------

    def start(self):
        self._thread.start()
        if not self.ready.wait(90):
            raise RuntimeError(self.error or "浏览器会话启动超时")
        if self.error:
            raise RuntimeError(self.error)

    def _main(self):
        """会话线程主体：启动浏览器 → 泵送命令 → 退出时清理。"""
        from playwright.sync_api import sync_playwright
        from .browser import launch_browser
        pw = browser = None
        try:
            pw = sync_playwright().start()
            browser, used, note = launch_browser(pw, self.engine or None)
            self.engine_note = note or ""
            ctx = browser.new_context(viewport={"width": 1280, "height": 800})
            self.page = ctx.new_page()
            if self.url_target:
                self.page.goto(self.url_target, timeout=30000)
            self.ready.set()
            self._pump()
        except Exception as e:
            self.error = f"{type(e).__name__}: {e}"[:300]
            self.ready.set()
        finally:
            self.closed = True
            try:
                if browser:
                    browser.close()
            except Exception:
                pass
            try:
                if pw:
                    pw.stop()
            except Exception:
                pass

    def _pump(self):
        while True:
            try:
                fn, args, fut = self.cmds.get(timeout=0.5)
            except Empty:
                if self.close_requested:
                    return
                continue
            if fn is None:               # 关闭哨兵
                return
            try:
                fut.set_result(fn(*args))
            except Exception as e:
                fut.set_exception(e)

    close_requested = False

    def close(self):
        """请求关闭：投递哨兵，等待线程退出（幂等）。"""
        self.close_requested = True
        try:
            self.cmds.put((None, None, None))
        except Exception:
            pass

    # ---------- 命令投递（任意线程调用） ----------

    def post(self, fn, *args) -> Future:
        if self.closed:
            fut: Future = Future()
            fut.set_exception(RuntimeError("会话已关闭"))
            return fut
        self.last_used = time.time()
        fut = Future()
        self.cmds.put((fn, args, fut))
        return fut

    # ---------- 会话线程侧执行体 ----------

    def do_state(self) -> dict:
        state = self.page.evaluate(_STATE_JS)
        return {"url": state.get("url", ""), "title": state.get("title", ""),
                "elements": state.get("elements", []), "text": state.get("text", ""),
                "variables": dict(self.variables)}

    def do_action(self, action: str, params: dict) -> dict:
        if action not in ALLOWED_ACTIONS:      # 红线：白名单外一律拒绝
            return {"ok": False, "reason": f"动作 {action} 不在白名单内，已拒绝执行",
                    "saved": {}, "state": None}
        act = {"action": action, **(params or {})}
        try:
            ok, reason, saved_kv = _exec_action(self.page, act, self.variables)
        except Exception as e:
            ok, reason, saved_kv = False, f"{type(e).__name__}: {e}"[:200], None
        saved = {}
        if action == "save" and ok and saved_kv:
            self.variables[saved_kv[0]] = saved_kv[1]
            saved = {saved_kv[0]: saved_kv[1]}
        return {"ok": ok, "reason": reason, "saved": saved, "state": self.do_state()}

    def do_screenshot(self) -> dict:
        name = f"bsess-{self.id[:8]}-{int(time.time())}.png"
        path = STATIC_DIR / name
        self.page.screenshot(path=str(path), full_page=False)
        return {"url": f"/static/{name}"}


# ---------- 会话管理器 ----------

def _ttl() -> int:
    try:
        return max(60, int(config.get("TD_BROWSER_SESSION_TTL") or _TTL))
    except ValueError:
        return _TTL


def _start_sweeper():
    global _sweeper_started
    if _sweeper_started:
        return
    _sweeper_started = True

    def _sweep():
        while True:
            time.sleep(60)
            deadline = time.time() - _ttl()
            with _lock:
                stale = [s for s in _sessions.values() if s.last_used < deadline]
            for s in stale:
                s.close()
                with _lock:
                    _sessions.pop(s.id, None)

    threading.Thread(target=_sweep, daemon=True, name="bsess-sweeper").start()


def open_session(user_id: str, url: str, engine: str = "") -> dict:
    """开会话（同步，内部起线程）。超限抛 RuntimeError。返回会话信息 dict。"""
    _start_sweeper()
    with _lock:
        alive = [s for s in _sessions.values() if not s.closed]
        if len(alive) >= _MAX_GLOBAL:
            raise RuntimeError(f"浏览器会话已达全局上限 {_MAX_GLOBAL}，请先关闭闲置会话")
        if len([s for s in alive if s.user_id == user_id]) >= _MAX_PER_USER:
            raise RuntimeError(f"每个用户最多 {_MAX_PER_USER} 个并发浏览器会话，请先关闭闲置会话")
        sid = uuid.uuid4().hex[:16]
        sess = BrowserSession(sid, user_id, url, engine)
        _sessions[sid] = sess
    try:
        sess.start()
    except Exception:
        with _lock:
            _sessions.pop(sid, None)
        sess.close()
        raise
    return _info(sess)


def _info(sess: BrowserSession) -> dict:
    return {"session_id": sess.id, "engine_note": sess.engine_note,
            "created_at": int(sess.created_at), "variables": dict(sess.variables)}


def get_session(session_id: str, user_id: str, is_admin: bool = False) -> BrowserSession:
    """按 id 取会话并校验归属。不存在/无权/已关闭抛 KeyError（调用方转错误文案）。"""
    sess = _sessions.get(session_id)
    if not sess or sess.closed:
        raise KeyError("会话不存在或已关闭（空闲超时会自动回收）")
    if sess.user_id != user_id and not is_admin:
        raise KeyError("无权操作该会话")
    return sess


def list_sessions(user_id: str, is_admin: bool = False) -> list[dict]:
    with _lock:
        rows = [s for s in _sessions.values() if not s.closed
                and (is_admin or s.user_id == user_id)]
    return [{"session_id": s.id, "owner": s.user_id, "created_at": int(s.created_at),
             "idle_seconds": int(time.time() - s.last_used)} for s in rows]


def close_session(session_id: str, user_id: str, is_admin: bool = False) -> None:
    sess = get_session(session_id, user_id, is_admin)
    sess.close()
    with _lock:
        _sessions.pop(session_id, None)
