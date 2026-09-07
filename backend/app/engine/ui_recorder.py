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

SESSION_TTL = 3600       # 整场录制最长 1 小时
IDLE_TTL = 7200          # 会话闲置超过 2 小时才回收
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
            "frame_seq": 0, "frame_wh": (1280, 800),
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
            # Screencast：页面重绘即产生 JPEG 帧（Playwright 内部走 CDP startScreencast 并自动应答）。
            # 注意：同步 API 下回调只在有 playwright 调用进行时派发，等待循环里用 wait_for_timeout 泵。
            page.screencast.start(quality=65, size={"width": 1280, "height": 800},
                                  on_frame=lambda f: _store_frame(sess, f))
            try:
                page.goto(start_url, timeout=30000)
            except Exception as e:
                sess["error"] = f"打开页面失败：{type(e).__name__}: {e}"[:200]
            deadline = time.time() + SESSION_TTL
            while time.time() < deadline:
                try:
                    cmd = sess["cmd_q"].get(timeout=0.02)
                except queue.Empty:
                    # 泊在 playwright 调用上派发 screencast 帧：空闲时画面也随页面重绘实时刷新。
                    # 周期 ~50ms（0.02 阻塞 + 30ms 泵）：screencast 按重绘产出，泵决定观看帧率上限
                    page.wait_for_timeout(30)
                    continue
                try:
                    ret = _exec_cmd(sess, page, cmd)
                except Exception as e:
                    ret = {"ok": False, "error": f"{type(e).__name__}: {e}"[:200]}
                if cmd.get("op") == "finish":
                    sess["done"] = True  # 先置位再回包，调用方拿到结果时状态已一致
                    sess["ret_q"].put(ret)
                    break
                sess["ret_q"].put(ret)
            try:
                page.screencast.stop()
            except Exception:
                pass
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
        # 混合录制：让 AI 在当前页面上完成一个目标（登录/过验证码/填长表单…）。
        # 平台主打 AI 主导测试：达成目标后整轮记为一个「AI 代劳」步骤（只存目标），
        # 回放时 AI 现场重新执行——验证码等动态内容每次重新识别，不做死坐标脚本。
        # 进度经会话状态轮询（sess["ai"]），启动即返回；录制时步数上限放宽（人工盯守 + 可随时取消）
        goal = (cmd.get("goal") or "").strip()
        if not goal:
            return {"ok": False, "error": "请先告诉 AI 这一步要做什么"}
        from .ai_runner import ai_drive
        from . import queue as _q
        ai_run_id = f"rec{int(time.time() * 1000) % 10**9:09d}"
        _q.register(ai_run_id)
        with _lock:
            sess["ai"] = {"goal": goal, "state": "running", "run_id": ai_run_id, "summary": ""}
        r = ai_drive(page, goal, cmd.get("vars") or {},
                     max_steps=int(cmd.get("max_steps") or 200),
                     run_id=ai_run_id,
                     shot_tag=f"ai{len(sess['events'])}",
                     on_step=lambda _d: _snap(sess, page))   # AI 每走一步刷一帧，画面不静止
        ok = r.get("status") == "passed"
        cancelled = (r.get("summary") == "用户取消")
        if ok:
            with _lock:
                sess["events"].append({"type": "ai", "goal": goal})
        with _lock:
            sess["ai"] = {"goal": goal, "state": "cancelled" if cancelled else ("passed" if ok else "failed"),
                          "run_id": ai_run_id, "summary": (r.get("summary") or "")[:200]}
        _q.unregister(ai_run_id)
        return {"ok": ok and not cancelled,
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


def _snap(sess: dict, page):
    try:
        data = page.screenshot(type="jpeg", quality=65)   # 锁外截图：慢页面截图可达数秒，不能锁住会话
    except Exception:
        return  # 页面跳转中截图可能失败，保留上一帧
    with _lock:
        sess["frame"] = data
        sess["frame_seq"] = sess.get("frame_seq", 0) + 1
        sess["page_url"] = page.url
        sess["last_active"] = time.time()


def _store_frame(sess: dict, f: dict):
    """screencast 帧落地：重绘即覆盖最新帧并递增序号（stream_frames 按序号推送）。"""
    with _lock:
        sess["frame"] = f.get("data") or b""
        if f.get("viewportWidth"):
            sess["frame_wh"] = (f.get("viewportWidth"), f.get("viewportHeight"))
        sess["frame_seq"] = sess.get("frame_seq", 0) + 1


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


def start_ai_cmd(session_id: str, cmd: dict) -> dict:
    """AI 代劳：入队即返回（进度与结果经会话状态轮询），避免长时间挂起 HTTP 请求。"""
    with _lock:
        sess = _sessions.get(session_id)
        sess and sess.update(last_active=time.time())
    if not sess:
        return {"ok": False, "error": "录制会话不存在或已过期"}
    if sess["done"]:
        return {"ok": False, "error": "录制已结束"}
    if (sess.get("ai") or {}).get("state") == "running":
        return {"ok": False, "error": "AI 正在执行，请先停止或等它完成"}
    if not (cmd.get("goal") or "").strip():
        return {"ok": False, "error": "请先告诉 AI 这一步要做什么"}
    sess["cmd_q"].put(dict(cmd))
    return {"ok": True, "started": True}


def cancel_ai(session_id: str) -> dict:
    """中止当前 AI 代劳轮（录制线程在 ai_drive 步骤间检查取消标记后退出）。"""
    from . import queue as _q
    with _lock:
        sess = _sessions.get(session_id)
        ai = sess.get("ai") if sess else None
        run_id = ai.get("run_id") if ai and ai.get("state") == "running" else None
    if not run_id:
        return {"ok": False, "error": "AI 当前没有在执行"}
    _q.cancel(run_id)
    return {"ok": True}


def get_frame(session_id: str) -> bytes | None:
    with _lock:
        sess = _sessions.get(session_id)
        if not sess:
            return None
        sess["last_active"] = time.time()
        return sess["frame"]


def _peek_stream(session_id: str, last_seq: int):
    """线程池里安全地窥视会话帧状态（拿锁必须避开事件循环线程，见 stream_frames）。"""
    with _lock:
        sess = _sessions.get(session_id)
        if not sess:
            return None
        seq = sess.get("frame_seq", 0)
        data = sess["frame"] if (seq != last_seq and sess["frame"]) else None
        sess["last_active"] = time.time()
        return seq, data, sess["done"]


async def stream_frames(session_id: str):
    """screencast 帧推流（异步生成器，供 WebSocket 端点消费）。

    新帧（frame_seq 变化）即 yield 二进制；会话结束 yield {"done": true}；会话消失 yield {"gone": true}。
    取锁窥视放线程池执行：_lock 可能被录制线程在截图时短暂持有，
    若在事件循环上直接等锁会冻结整个后端。
    """
    import asyncio
    last_seq = -1
    while True:
        info = await asyncio.to_thread(_peek_stream, session_id, last_seq)
        if info is None:
            yield {"gone": True}
            return
        seq, data, done = info
        if data is not None:
            last_seq = seq
            yield data
        if done:
            yield {"done": True}
            return
        await asyncio.sleep(0.03)


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
        ai_state = dict(sess["ai"]) if sess.get("ai") else None

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
        elif t == "ai":  # AI 代劳：整轮目标记一步，回放时 AI 现场重新执行
            if ev.get("goal"):
                event_steps.append({"action": "ai", "url": "", "selector": "",
                                    "value": ev["goal"]})
        elif t == "click_xy":  # 验证码点选：比例坐标，回放按视口换算
            xf, yf = ev.get("xf", 0), ev.get("yf", 0)
            event_steps.append({"action": "click_xy", "url": "", "selector": "",
                                "value": f"({xf:.0%}, {yf:.0%})", "x": round(xf, 4), "y": round(yf, 4)})
        elif t == "drag":  # 滑块拖拽：比例坐标
            xf, yf, x2f, y2f = ev.get("xf", 0), ev.get("yf", 0), ev.get("x2f", 0), ev.get("y2f", 0)
            event_steps.append({"action": "drag", "url": "", "selector": "",
                                "value": f"({xf:.0%}, {yf:.0%})→({x2f:.0%}, {y2f:.0%})",
                                "x": round(xf, 4), "y": round(yf, 4),
                                "x2": round(x2f, 4), "y2": round(y2f, 4)})
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
    return {"done": done, "steps": merged, "url": start_url, "error": error, "ai": ai_state}
