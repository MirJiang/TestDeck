"""UI 用例录制器：两种模式。

remote（默认，远程/容器部署可用）：后端起无头浏览器，页面以 JPEG 帧串流给
前端；用户在平台页面上点击/滚屏/跳转/输入，指令回传到后端在真实页面上执行，
并同步记录为用例步骤（选择器优先 id，退回 name / 文本）。

local（仅后端与用户同机时）：弹出有头浏览器，注入脚本监听 点击/输入，
用户关闭窗口后把事件编译成步骤。

两种模式产出同一份事件流，compile_steps 编译结果一致。
"""
import queue
import threading
import time

# {session_id: {"events": [...], "start_url": str, "done": bool, "error": str,
#               "mode": "remote"|"local", "created": float, "last_active": float,
#               "cmd_q": Queue, "ret_q": Queue, "frame": bytes, "page_url": str}}
_sessions: dict = {}
_lock = threading.Lock()

SESSION_TTL = 900        # 整场录制最长 15 分钟
IDLE_TTL = 1800          # 会话闲置超过 30 分钟即回收
MAX_ACTIVE = 5           # 同时进行的录制会话上限

_INIT_JS = """
(() => {
  const send = (type, data) => console.log('__TD__' + JSON.stringify({type, ...data}));
  window.addEventListener('click', e => {
    const t = e.target;
    if (!t || !t.getBoundingClientRect) return;
    let sel = t.id ? '#' + t.id
      : (t.name ? `${t.tagName.toLowerCase()}[name="${t.name}"]` : '')
    send('click', {sel, tag: t.tagName.toLowerCase(), text: (t.innerText || t.value || '').slice(0, 20)});
  }, true);
  window.addEventListener('change', e => {
    const t = e.target;
    if (!t) return;
    if (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA') {
      const sel = t.id ? '#' + t.id
        : (t.name ? `${t.tagName.toLowerCase()}[name="${t.name}"]` : '');
      if (sel) send('fill', {sel, value: t.value.slice(0, 100)});
    }
  }, true);
})();
"""

# 在 (x, y) 处取元素信息：选择器优先 id，退回 name；同时告知是否为输入框/是否页面空白
_ELEM_JS = """([x, y]) => {
  const t = document.elementFromPoint(x, y);
  if (!t) return null;
  const tag = t.tagName.toLowerCase();
  const sel = t.id ? '#' + t.id
    : (t.name ? tag + '[name="' + t.name + '"]' : '');
  return { sel, tag, text: (t.innerText || t.value || '').slice(0, 20),
           input: tag === 'input' || tag === 'textarea',
           blank: tag === 'body' || tag === 'html' };
}"""


def start_recording(session_id: str, start_url: str, mode: str = "remote") -> None:
    _cleanup_stale()
    with _lock:
        active = sum(1 for v in _sessions.values() if not v["done"])
        if active >= MAX_ACTIVE:
            raise RuntimeError(f"同时进行的录制已达 {MAX_ACTIVE} 个，请先完成或稍后再试")
        _sessions[session_id] = {
            "events": [], "start_url": start_url, "done": False, "error": "",
            "mode": "remote" if mode != "local" else "local",
            "created": time.time(), "last_active": time.time(),
            "cmd_q": queue.Queue(), "ret_q": queue.Queue(),
            "frame": b"", "page_url": start_url,
        }
    target = _record_local if mode == "local" else _record_remote
    threading.Thread(target=target, args=(session_id, start_url), daemon=True).start()


def _cleanup_stale():
    now = time.time()
    with _lock:
        for sid in [s for s, v in _sessions.items()
                    if now - v.get("last_active", v["created"]) > IDLE_TTL]:
            _sessions.pop(sid, None)


# ---------- remote 模式 ----------

def _record_remote(session_id: str, start_url: str):
    sess = _sessions.get(session_id)
    if sess is None:
        return
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(viewport={"width": 1280, "height": 800})
            page = ctx.new_page()
            try:
                page.goto(start_url, timeout=30000)
                _snap(sess, page)
            except Exception as e:
                sess["error"] = f"打开页面失败：{type(e).__name__}: {e}"[:200]
            deadline = time.time() + SESSION_TTL
            while time.time() < deadline:
                try:
                    cmd = sess["cmd_q"].get(timeout=1.0)
                except queue.Empty:
                    continue
                try:
                    ret = _exec_cmd(sess, page, cmd)
                except Exception as e:
                    ret = {"ok": False, "error": f"{type(e).__name__}: {e}"[:200]}
                _snap(sess, page)
                if cmd.get("op") == "finish":
                    sess["done"] = True  # 先置位再回包，调用方拿到结果时状态已一致
                    sess["ret_q"].put(ret)
                    break
                sess["ret_q"].put(ret)
            try:
                browser.close()
            except Exception:
                pass
    except Exception as e:
        sess["error"] = f"{type(e).__name__}: {e}"[:200]
    finally:
        sess["done"] = True


def _exec_cmd(sess: dict, page, cmd: dict) -> dict:
    op = cmd.get("op")
    if op == "click":
        x, y = int(cmd.get("x", 0)), int(cmd.get("y", 0))
        info = None
        try:  # 先取元素信息再点击：点击可能触发跳转导致元素消失
            info = page.evaluate(_ELEM_JS, [x, y])
        except Exception:
            pass
        page.mouse.click(x, y)
        if info:
            if not info.get("blank"):  # 空白处点击照常执行，但不记为步骤（多为误点）
                with _lock:
                    sess["events"].append({"type": "click", "sel": info.get("sel", ""),
                                           "tag": info.get("tag", ""), "text": info.get("text", "")})
            return {"ok": True, "sel": info.get("sel", ""), "tag": info.get("tag", ""),
                    "text": info.get("text", ""), "input": bool(info.get("input")),
                    "blank": bool(info.get("blank"))}
        return {"ok": True, "sel": "", "input": False}
    if op == "fill":
        sel, value = cmd.get("selector", ""), cmd.get("value", "")
        if not sel:
            return {"ok": False, "error": "请先点击要输入的输入框"}
        page.fill(sel, value, timeout=8000)
        with _lock:
            sess["events"].append({"type": "fill", "sel": sel, "value": value})
        return {"ok": True}
    if op == "goto":
        url = (cmd.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            return {"ok": False, "error": "地址需以 http(s://) 开头"}
        page.goto(url, timeout=30000)
        with _lock:
            sess["events"].append({"type": "goto", "url": url})
        sess["page_url"] = url
        return {"ok": True}
    if op == "ai":
        # 混合录制：让 AI 在当前页面上完成一个目标（登录/过验证码/填长表单…），
        # 它的动作转成普通录制事件——回放零 token
        goal = (cmd.get("goal") or "").strip()
        if not goal:
            return {"ok": False, "error": "请先告诉 AI 这一步要做什么"}
        from .ai_runner import ai_drive
        r = ai_drive(page, goal, cmd.get("vars") or {},
                     max_steps=int(cmd.get("max_steps") or 12),
                     run_id=f"rec{int(time.time() * 1000) % 10**9:09d}",
                     shot_tag=f"ai{len(sess['events'])}",
                     on_step=lambda _d: _snap(sess, page))   # AI 每走一步刷一帧，画面不静止
        n = _append_ai_events(sess, r.get("detail") or [])
        ok = r.get("status") == "passed"
        return {"ok": ok, "ai_steps": n,
                "error": "" if ok else (r.get("summary") or "AI 未完成目标")[:200],
                "summary": (r.get("summary") or "")[:200]}
    if op == "scroll":
        page.mouse.wheel(0, int(cmd.get("dy", 300)))
        return {"ok": True}
    if op == "back":
        page.go_back(timeout=10000)
        return {"ok": True}
    if op == "finish":
        return {"ok": True}
    return {"ok": False, "error": f"不支持的操作：{op}"}


def _append_ai_events(sess: dict, actions: list) -> int:
    """把 AI 执行的动作明细转成录制事件（与人工录制同构），返回转换条数。

    坐标类动作（click_xy/drag）没有稳定选择器，不转换；done/save 是控制指令，不转换。
    """
    n = 0
    with _lock:
        for a in actions:
            act = a.get("action")
            if act == "goto" and a.get("url"):
                sess["events"].append({"type": "goto", "url": a["url"]}); n += 1
            elif act == "click" and a.get("selector"):
                sess["events"].append({"type": "click", "sel": a["selector"],
                                       "tag": "", "text": ""}); n += 1
            elif act == "fill" and a.get("selector"):
                sess["events"].append({"type": "fill", "sel": a["selector"],
                                       "value": str(a.get("value", ""))}); n += 1
            elif act == "expect_text" and a.get("value"):
                sess["events"].append({"type": "expect_text", "value": a["value"]}); n += 1
    return n


def _snap(sess: dict, page):
    try:
        with _lock:
            sess["frame"] = page.screenshot(type="jpeg", quality=55)
            sess["page_url"] = page.url
            sess["last_active"] = time.time()
    except Exception:
        pass  # 页面跳转中截图可能失败，保留上一帧


def send_cmd(session_id: str, cmd: dict, timeout: float = 35.0) -> dict:
    """把一条指令交给录制线程在页面上执行，等待并返回执行结果。"""
    with _lock:
        sess = _sessions.get(session_id)
        sess and sess.update(last_active=time.time())
    if not sess:
        return {"ok": False, "error": "录制会话不存在或已过期"}
    if sess["done"]:
        return {"ok": False, "error": "录制已结束"}
    try:  # 丢弃上一条超时指令迟到返回的结果，避免错位
        while True:
            sess["ret_q"].get_nowait()
    except queue.Empty:
        pass
    sess["cmd_q"].put(dict(cmd))
    try:
        return sess["ret_q"].get(timeout=timeout)
    except queue.Empty:
        return {"ok": False, "error": "指令执行超时（页面可能无响应）"}


def get_frame(session_id: str) -> bytes | None:
    with _lock:
        sess = _sessions.get(session_id)
        if not sess:
            return None
        sess["last_active"] = time.time()
        return sess["frame"]


# ---------- local 模式（保留：后端与操作者同机时体验更好） ----------

def _record_local(session_id: str, start_url: str):
    sess = _sessions.get(session_id)
    if sess is None:
        return
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False)
            ctx = browser.new_context()
            ctx.add_init_script(_INIT_JS)
            page = ctx.new_page()

            def on_console(msg):
                if msg.type == "log" and msg.text.startswith("__TD__"):
                    import json
                    try:
                        with _lock:
                            sess["events"].append(json.loads(msg.text[6:]))
                    except Exception:
                        pass

            page.on("console", on_console)
            try:
                page.goto(start_url, timeout=30000)
                page.wait_for_event("close", timeout=0)
            except Exception:
                pass  # 浏览器被用户关闭
            try:
                browser.close()
            except Exception:
                pass
    except Exception as e:
        with _lock:
            sess["events"].append({"type": "error", "value": str(e)[:200]})
    finally:
        sess["done"] = True


# ---------- 事件 → 步骤 ----------

def _selector(tag: str, sel: str, text: str) -> str:
    if sel:
        return sel
    return f"{tag}:has-text(\"{text}\")" if text else tag


def compile_steps(session_id: str) -> dict:
    """把录制事件编译成 UI 步骤；返回 {done, steps, url, error}。"""
    with _lock:
        sess = _sessions.get(session_id)
        if not sess:
            return {"done": False, "steps": [], "url": "", "error": "录制会话不存在"}
        events, done = list(sess["events"]), sess["done"]
        start_url, error = sess["start_url"], sess["error"]

    event_steps = []
    for ev in events:
        t = ev.get("type")
        if t == "click":
            sel = _selector(ev.get("tag", ""), ev.get("sel", ""), ev.get("text", ""))
            event_steps.append({"action": "click", "url": "", "selector": sel, "value": ""})
        elif t == "fill":
            if ev.get("sel"):
                event_steps.append({"action": "fill", "url": "", "selector": ev["sel"], "value": ev.get("value", "")})
        elif t == "expect_text":
            event_steps.append({"action": "expect_text", "url": "", "selector": "", "value": ev.get("value", "")})
        elif t == "goto":
            event_steps.append({"action": "goto", "url": ev.get("url", ""), "selector": "", "value": ""})
    # 首步固定为打开起始地址；合并对同一输入框的连续 fill；连续点同一元素只记一次；
    # 开头与起始地址相同的 goto 事件去掉
    merged = [{"action": "goto", "url": start_url, "selector": "", "value": ""}]
    for s in event_steps:
        if s["action"] == "fill" and merged[-1]["action"] == "fill" \
                and merged[-1]["selector"] == s["selector"]:
            merged[-1]["value"] = s["value"]
        elif s["action"] == "click" and merged[-1]["action"] == "click" \
                and merged[-1]["selector"] == s["selector"]:
            continue
        elif s["action"] == "goto" and s["url"] == start_url \
                and all(m["action"] == "goto" for m in merged):
            continue
        else:
            merged.append(s)
    return {"done": done, "steps": merged, "url": start_url, "error": error}
