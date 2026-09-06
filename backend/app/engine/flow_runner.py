"""流程测试引擎：多角色按业务线串行执行。

- 每个角色独立的登录态：API 步骤用各自的 httpx.Client（独立 cookie），
  UI 步骤用各自的浏览器 context/page（独立 cookie/localStorage）
- save 提取的变量进入共享命名空间（业务单据号等），所有角色可读
- 步骤失败即中止（业务线已断）
- UI 使用 Chromium Headless Shell（低资源无头内核）
"""
import time
import httpx
from pathlib import Path

from .runner import substitute, get_field, evaluate_check
from .ui_runner import STATIC_DIR
from .ai_runner import ai_drive
from .browser import launch_browser

STATIC_DIR.mkdir(exist_ok=True)


def _resolve(text, role_vars, shared):
    """先查角色变量，再查共享变量。"""
    if not isinstance(text, str):
        return text
    import re
    from .runner import VAR_RE
    def rep(m):
        k = m.group(1)
        if k in role_vars:
            return str(role_vars[k])
        if k in shared:
            return str(shared[k])
        return m.group(0)
    return VAR_RE.sub(rep, text)


def run_flow(flow, env, run_id: str, timeout: float = 15.0) -> dict:
    base = (env.base_url or "").rstrip("/") if env else ""
    env_vars = dict(env.variables or {}) if env else {}
    roles = {r["key"]: r for r in (flow.roles or []) if r.get("key")}
    role_vars = {k: {**env_vars, **(r.get("variables") or {})} for k, r in roles.items()}
    shared = {}

    detail, pass_n, fail_n = [], 0, 0
    t0 = time.time()
    api_clients = {}   # role -> httpx.Client
    browser = pages = contexts = None
    _pw = None

    def role_api(role):
        if role not in api_clients:
            api_clients[role] = httpx.Client(timeout=timeout, verify=False, trust_env=False,
                                             base_url=base or None)
        return api_clients[role]

    def ensure_browser():
        nonlocal browser, contexts, pages, _pw
        if browser is None:
            from playwright.sync_api import sync_playwright
            _pw = sync_playwright().start()
            browser, _, _ = launch_browser(_pw)  # 引擎由全局配置决定
            contexts, pages = {}, {}
        return browser

    def role_page(role):
        ensure_browser()
        if role not in pages:
            contexts[role] = browser.new_context()   # 每角色独立会话
            pages[role] = contexts[role].new_page()
        return pages[role]

    def close_all():
        for c in api_clients.values():
            try: c.close()
            except Exception: pass
        if browser is not None:
            try: browser.close()
            except Exception: pass
        if _pw is not None:
            try: _pw.stop()
            except Exception: pass

    try:
        for i, step in enumerate(flow.steps or [], 1):
            role = step.get("role", "")
            if role not in roles:
                res = {"idx": i, "role": role, "pass": False, "reason": f"未定义的角色：{role}"}
                detail.append(res); fail_n += 1; break
            stype = step.get("type", "ui")
            res = {"idx": i, "role": role, "role_name": roles[role].get("name", role), "type": stype,
                   "pass": False, "reason": "", "ms": 0}
            t_step = time.time()
            try:
                if stype == "api":
                    method = step.get("m", "GET").upper()
                    url = _resolve(step.get("url", ""), role_vars[role], shared)
                    headers = {}
                    for line in (step.get("headers") or "").splitlines():
                        if ":" in line:
                            k, v = line.split(":", 1)
                            headers[k.strip()] = _resolve(v.strip(), role_vars[role], shared)
                    raw_body = _resolve(step.get("body") or "", role_vars[role], shared)
                    kwargs = {"headers": headers}
                    if raw_body.strip():
                        kwargs["content"] = raw_body
                        headers.setdefault("Content-Type", "application/json")
                    res["target"] = f"{method} {url}"   # 先记录目标，连接失败时报告里也能看到请求了什么
                    resp = role_api(role).request(method, url, **kwargs)
                    try:
                        body_json = resp.json()
                    except Exception:
                        body_json = None
                    res.update(status=resp.status_code, response=resp.text[:1500])
                    chk = evaluate_check(step.get("check"), resp.status_code, resp.text, body_json)
                    res.update(**{"pass": chk["pass"], "reason": chk["reason"]})
                    save = step.get("save") or {}
                    if chk["pass"] and save.get("name") and save.get("from"):
                        shared[save["name"]] = get_field(body_json, save["from"])
                        res["saved"] = save["name"]
                elif stype == "ai":
                    goal = _resolve(step.get("goal", ""), role_vars[role], shared)
                    surl = _resolve(step.get("url", ""), role_vars[role], shared)
                    if not goal:
                        res.update(**{"pass": False, "reason": "AI 步骤缺少目标描述"})
                    else:
                        page = role_page(role)
                        if surl:
                            if surl.startswith("/"):
                                surl = base + surl
                            page.goto(surl, timeout=20000)
                        ai_vars = {**shared, **role_vars[role]}
                        r_ai = ai_drive(page, goal, ai_vars,
                                        max_steps=int(step.get("max_steps") or 15),
                                        run_id=run_id, shot_tag=f"flow{i}")
                        shared.update(r_ai["saved"])
                        res.update(target=f"[AI] {goal[:60]}",
                                   **{"pass": r_ai["status"] == "passed"},
                                   reason=r_ai["summary"][:200])
                        if r_ai["saved"]:
                            res["saved"] = ",".join(r_ai["saved"].keys())
                        res["actions"] = r_ai["detail"]
                else:
                    action = step.get("action", "goto")
                    page = role_page(role)
                    url = _resolve(step.get("url", ""), role_vars[role], shared)
                    if url.startswith("/"):
                        url = base + url
                    sel = step.get("selector", "")
                    val = _resolve(step.get("value", ""), role_vars[role], shared)
                    res["target"] = f"[{action}] {url or sel}"
                    if action == "goto":
                        page.goto(url, timeout=15000)
                        res.update(**{"pass": True, "reason": f"打开 {url}"})
                    elif action == "click":
                        page.click(sel, timeout=8000)
                        res.update(**{"pass": True, "reason": f"点击 {sel}"})
                    elif action == "fill":
                        page.fill(sel, val, timeout=8000)
                        res.update(**{"pass": True, "reason": f"在 {sel} 输入 {val}"})
                    elif action == "expect_text":
                        page.wait_for_load_state("domcontentloaded")
                        deadline = time.time() + 5
                        body = page.inner_text("body", timeout=8000)
                        while val not in body and time.time() < deadline:
                            time.sleep(0.2)
                            body = page.inner_text("body", timeout=8000)
                        ok = val in body
                        res.update(**{"pass": ok, "reason": f"页面未包含「{val}」（应显示）" if not ok else f"包含「{val}」"})
                    elif action == "screenshot":
                        path = STATIC_DIR / f"{run_id}-{i}.png"
                        page.screenshot(path=str(path), full_page=True)
                        res.update(**{"pass": True, "reason": "已截图", "screenshot": f"/static/{path.name}"})
                    else:
                        res["reason"] = f"不支持的动作：{action}"
            except Exception as e:
                res["reason"] = f"{type(e).__name__}: {e}"[:200]
            res["ms"] = int((time.time() - t_step) * 1000)
            detail.append(res)
            if res["pass"]:
                pass_n += 1
            else:
                fail_n += 1
                break
    finally:
        close_all()

    return {"status": "failed" if fail_n else "passed", "pass_n": pass_n, "fail_n": fail_n,
            "duration": round(time.time() - t0, 2), "detail": detail, "shared": shared}
