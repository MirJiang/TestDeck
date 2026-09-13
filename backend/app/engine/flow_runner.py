"""流程测试引擎：多角色按业务线串行执行。

- 每个角色独立的登录态：API 步骤用各自的 httpx.Client（独立 cookie），
  UI 步骤用各自的浏览器 context/page（独立 cookie/localStorage）
- save 提取的变量进入共享命名空间（业务单据号等），所有角色可读
- 步骤失败即中止（业务线已断）；步骤间检查取消标记，可提前终止
- on_step 回调：每完成一步推送最新明细（流式进度）
- 步骤 type=case：内联展开执行被引用的用例（跑在角色会话里，
  ${变量} 先角色后共享再环境，save 结果直通共享区）
- 截图步骤支持基线对比（视觉回归）：首次通过自动留基线，之后每次比对，
  差异超阈值（TD_SHOT_DIFF_PCT，默认 2%）判失败并生成并排对比图
- UI 使用 Chromium Headless Shell（低资源无头内核）
"""
import time
import httpx
from pathlib import Path

from .runner import get_field, evaluate_check
from .ui_runner import STATIC_DIR
from .ai_runner import ai_drive
from .browser import launch_browser
from .queue import is_cancelled
from .. import config

STATIC_DIR.mkdir(exist_ok=True)


def _resolve(text, role_vars, shared):
    """先查角色变量，再查共享变量。"""
    if not isinstance(text, str):
        return text
    from .runner import VAR_RE
    def rep(m):
        k = m.group(1)
        if k in role_vars:
            return str(role_vars[k])
        if k in shared:
            return str(shared[k])
        return m.group(0)
    return VAR_RE.sub(rep, text)


def _shot_diff_enabled() -> bool:
    return config.get("TD_SHOT_DIFF", "1") != "0"


def _shot_diff_pct(base_path: Path, new_path: Path):
    """两张截图的差异像素占比（0~100）；尺寸不同按 100；读取失败返回 None。"""
    from PIL import Image, ImageChops
    try:
        a = Image.open(base_path).convert("RGB")
        b = Image.open(new_path).convert("RGB")
        if a.size != b.size:
            return 100.0
        hist = ImageChops.difference(a, b).convert("L").histogram()
        changed = sum(hist[1:])
        return round(changed / (a.size[0] * a.size[1]) * 100, 2)
    except Exception:
        return None


def _save_side_by_side(base_path: Path, new_path: Path, out_path: Path):
    """生成「基线 | 本次」并排对比图，失败时静默。"""
    try:
        from PIL import Image
        a = Image.open(base_path).convert("RGB")
        b = Image.open(new_path).convert("RGB")
        h = max(a.height, b.height)
        canvas = Image.new("RGB", (a.width + b.width + 8, h), (255, 255, 255))
        canvas.paste(a, (0, 0))
        canvas.paste(b, (a.width + 8, 0))
        canvas.save(out_path)
    except Exception:
        pass


def run_flow(flow, env, run_id: str, timeout: float = 15.0,
             on_step=None, cases: dict | None = None, cancel_for: str = "") -> dict:
    """cases: {case_id: TestCase}，供 type=case 步骤内联执行（调用方预载）。

    cancel_for：父级执行记录 id（如计划执行时传计划 run id），
    父级被取消时本流程也在步骤间提前终止。
    """
    def _cancelled() -> bool:
        return is_cancelled(run_id) or (cancel_for and is_cancelled(cancel_for))

    base = (env.base_url or "").rstrip("/") if env else ""
    env_vars = dict(env.variables or {}) if env else {}
    roles = {r["key"]: r for r in (flow.roles or []) if r.get("key")}
    role_vars = {k: {**env_vars, **(r.get("variables") or {})} for k, r in roles.items()}
    shared = {}

    detail, pass_n, fail_n = [], 0, 0
    t0 = time.time()
    api_clients = {}   # role -> httpx.Client
    browser = pages = contexts = None
    engine_used = "chromium"   # launch_browser 实际使用的引擎；截图类步骤据此优雅降级
    _pw = None
    videos = {}        # role -> 视频静态路径（角色会话的执行录像）
    role_names = {k: (r.get("name") or k) for k, r in roles.items()}
    diff_pct = float(config.get("TD_SHOT_DIFF_PCT") or 2)

    def role_api(role):
        if role not in api_clients:
            api_clients[role] = httpx.Client(timeout=timeout, verify=False, trust_env=False,
                                             base_url=base or None)
        return api_clients[role]

    def ensure_browser():
        nonlocal browser, contexts, pages, _pw, engine_used
        if browser is None:
            from playwright.sync_api import sync_playwright
            _pw = sync_playwright().start()
            browser, engine_used, _ = launch_browser(_pw)  # 引擎由全局配置决定
            contexts, pages = {}, {}
        return browser

    def role_page(role):
        ensure_browser()
        if role not in pages:
            contexts[role] = browser.new_context()   # 每角色独立会话
            pages[role] = contexts[role].new_page()
            from .ui_runner import start_run_video
            videos[role] = start_run_video(pages[role], run_id, tag=role)
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

    # ---- 内联执行器：流程步骤与「引用用例」展开的步骤共用 ----

    def do_api(step: dict, role: str, res: dict):
        """在 role 会话上执行一个 API 步骤（m/url/headers/body/check/save）。"""
        method = step.get("m", "GET").upper()
        url = _resolve(step.get("url", ""), role_vars[role], shared)
        headers = {}
        raw_headers = step.get("headers") or ""
        if isinstance(raw_headers, dict):   # AI-API 固化步骤的 headers 是 dict
            raw_headers = "\n".join(f"{k}: {v}" for k, v in raw_headers.items())
        for line in raw_headers.splitlines():
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

    def do_ui(step: dict, role: str, res: dict, tag: str):
        """在 role 浏览器会话上执行一个 UI 动作；tag 用于截图文件命名。"""
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
        elif action == "click_xy":  # 坐标类动作（验证码）：比例坐标按角色视口换算
            vs = page.viewport_size or {"width": 1280, "height": 800}
            x, y = int(step.get("x", 0) * vs["width"]), int(step.get("y", 0) * vs["height"])
            page.mouse.click(x, y)
            res.update(**{"pass": True, "reason": f"点击坐标 ({x},{y})"})
        elif action == "drag":
            vs = page.viewport_size or {"width": 1280, "height": 800}
            x1, y1 = int(step.get("x", 0) * vs["width"]), int(step.get("y", 0) * vs["height"])
            x2, y2 = int(step.get("x2", 0) * vs["width"]), int(step.get("y2", 0) * vs["height"])
            page.mouse.move(x1, y1)
            page.mouse.down()
            n = 14
            for i in range(1, n + 1):
                page.mouse.move(x1 + (x2 - x1) * i / n, y1 + (y2 - y1) * i / n)
                time.sleep(0.03)
            time.sleep(0.1)
            page.mouse.up()
            res.update(**{"pass": True, "reason": f"拖拽 ({x1},{y1})→({x2},{y2})"})
        elif action == "ai":  # AI 代劳步骤：在角色会话内现场重新执行目标，失败自动重试
            from .ai_runner import ai_drive
            from .app_mapper import app_map_brief
            retries = max(0, min(3, int(step.get("retries", 1))) if str(step.get("retries", "1")).strip() != "" else 1)
            r, attempt = {}, 0
            for attempt in range(retries + 1):
                r = ai_drive(page, val, {**role_vars.get(role, {}), **shared},
                             max_steps=200, run_id=run_id, shot_tag=f"{run_id}-{tag}",
                             page_map=app_map_brief(getattr(flow, "project_id", "")),
                             project_id=getattr(flow, "project_id", "") or "")
                if r.get("status") == "passed":
                    break
            ok = r.get("status") == "passed"
            reason = (r.get("summary") or ("AI 完成目标" if ok else "AI 未完成目标"))[:120]
            if attempt:
                reason += f"（自动重试 {attempt} 次后{'成功' if ok else '仍失败'}）"
            res.update(**{"pass": ok, "reason": reason})
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
            try:
                path = STATIC_DIR / f"{run_id}-{tag}.png"
                page.screenshot(path=str(path), full_page=True)
                res.update(**{"pass": True, "reason": "已截图", "screenshot": f"/static/{path.name}"})
                if _shot_diff_enabled():
                    baseline = STATIC_DIR / f"base-{flow.id}-{tag}.png"
                    if baseline.exists():
                        pct = _shot_diff_pct(baseline, path)
                        if pct is not None:
                            if pct > diff_pct:
                                diff_img = STATIC_DIR / f"{run_id}-{tag}-diff.png"
                                _save_side_by_side(baseline, path, diff_img)
                                res.update(**{
                                    "pass": False,
                                    "reason": f"页面与基线差异 {pct}%（阈值 {diff_pct}%）",
                                    "baseline": f"/static/{baseline.name}",
                                    "diff": f"/static/{diff_img.name}"})
                            else:
                                res["reason"] = f"已截图，与基线差异 {pct}%（阈值 {diff_pct}% 内）"
                    else:
                        import shutil
                        shutil.copyfile(path, baseline)   # 首次通过自动留存基线
                        res["reason"] = "已截图（已留存基线）"
            except Exception:
                if engine_used == "lightpanda":   # 视觉相关能力缺失不影响执行链路
                    res.update(**{"pass": True, "reason": "Lightpanda 不支持截图，已跳过留档（基线对比同跳过）"})
                else:
                    raise
        else:
            res["reason"] = f"不支持的动作：{action}"

    def do_case(case, role: str, res: dict, tag: str):
        """内联展开执行被引用用例：跑在 role 会话里，save 结果直通共享区。

        用例步骤失败即中止（与单用例执行语义一致）；子步骤明细进 res["actions"]。
        """
        sub = []
        ok_n = bad_n = 0

        def note_cancelled(i):
            sub.append({"idx": i, "pass": False, "target": "", "reason": "已取消", "ms": 0})

        def run_seq(steps, kind, step_tag):
            """顺序执行一组同构步骤（api / ui），共用取消与计数。"""
            nonlocal ok_n, bad_n
            for j, st in enumerate(steps, 1):
                if _cancelled():
                    note_cancelled(j)
                    bad_n += 1
                    return
                r = {"idx": j, "type": kind, "pass": False, "reason": "", "ms": 0}
                t_s = time.time()
                try:
                    if kind == "api":
                        do_api(st, role, r)
                    else:
                        do_ui(st, role, r, f"{tag}-{j}")
                except Exception as e:
                    r["reason"] = f"{type(e).__name__}: {e}"[:200]
                r["ms"] = int((time.time() - t_s) * 1000)
                sub.append(r)
                if r["pass"]:
                    ok_n += 1
                else:
                    bad_n += 1
                    return   # 用例内失败即停

        ctype = (case.type or "api").lower()
        if ctype == "api":
            run_seq(case.steps or [], "api", tag)
        elif ctype == "ui":
            run_seq(case.steps or [], "ui", tag)
        else:   # ai 用例：配置打包在 steps[0]
            cfg = (case.steps or [{}])[0] if case.steps else {}
            goal = _resolve(cfg.get("goal", ""), role_vars[role], shared)
            target = (cfg.get("target") or "ui").strip().lower()
            fixed_api = cfg.get("fixed_api_steps") or []
            fixed_ui = cfg.get("fixed_steps") or []
            if fixed_api and not goal:   # 固化 API 步骤回放（零 token）
                steps = [{"m": (st.get("request") or {}).get("m", "GET"),
                          "url": (st.get("request") or {}).get("url", ""),
                          "headers": (st.get("request") or {}).get("headers") or "",
                          "body": (st.get("request") or {}).get("body") or "",
                          "check": st.get("check") or {"type": "status", "expect": "200"},
                          "save": st.get("save") or {}} for st in fixed_api]
                run_seq(steps, "api", tag)
            elif fixed_ui and not goal:  # 固定 UI 步骤回放（录制/固化产物）
                run_seq(fixed_ui, "ui", tag)
            elif target == "api":
                # 目标式 API AI 用例：交给 AI 引擎独立执行（模型自主设计请求），
                # save 的结果并入共享区
                from .ai_runner import run_ai_api_case
                r = run_ai_api_case(case, env, run_id)
                ok_n, bad_n = r["pass_n"], r["fail_n"]
                sub.extend(r.get("detail") or [])
                for k, v in (r.get("saved") or {}).items():
                    shared[k] = v
            else:
                # 目标式 UI AI 用例：在该角色的页面上由模型驱动（与流程 AI 步骤一致）
                page = role_page(role)
                surl = _resolve(cfg.get("start_url", ""), role_vars[role], shared)
                if surl:
                    if surl.startswith("/"):
                        surl = base + surl
                    page.goto(surl, timeout=20000)
                ai_vars = {**shared, **role_vars[role]}
                r_ai = ai_drive(page, goal, ai_vars,
                                max_steps=int(cfg.get("max_steps") or 200),
                                run_id=run_id, shot_tag=tag,
                                project_id=getattr(flow, "project_id", "") or "")
                shared.update(r_ai.get("saved") or {})
                sub.append({"idx": 1, "type": "ai", "target": f"[AI] {goal[:60]}",
                            "pass": r_ai["status"] == "passed",
                            "reason": (r_ai.get("summary") or "")[:200],
                            "actions": r_ai.get("detail"), "ms": 0})
                if r_ai["status"] == "passed":
                    ok_n += 1
                else:
                    bad_n += 1

        saved_names = [d.get("saved") for d in sub if d.get("saved")]
        if saved_names:
            res["saved"] = ",".join(saved_names)
        res["actions"] = sub
        res["pass"] = bad_n == 0
        total = ok_n + bad_n
        res["reason"] = (f"通过 {total} 步" if total else "用例没有可执行的步骤") if not bad_n \
            else f"第 {ok_n + 1} 步失败（前 {ok_n} 步已通过）"

    try:
        for i, step in enumerate(flow.steps or [], 1):
            if _cancelled():
                detail.append({"idx": i, "role": step.get("role", ""), "type": step.get("type", ""),
                               "pass": False, "reason": "已取消", "ms": 0})
                fail_n += 1
                break
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
                    do_api(step, role, res)
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
                                        max_steps=int(step.get("max_steps") or 200),
                                        run_id=run_id, shot_tag=f"flow{i}",
                                        project_id=getattr(flow, "project_id", "") or "")
                        shared.update(r_ai["saved"])
                        res.update(target=f"[AI] {goal[:60]}",
                                   **{"pass": r_ai["status"] == "passed"},
                                   reason=r_ai["summary"][:200])
                        if r_ai["saved"]:
                            res["saved"] = ",".join(r_ai["saved"].keys())
                        res["actions"] = r_ai["detail"]
                elif stype == "case":
                    c = (cases or {}).get(step.get("case_id"))
                    if c is None:
                        res.update(**{"pass": False, "reason": "引用的用例不存在或已删除"})
                    else:
                        res["target"] = f"[用例] {c.name}"
                        do_case(c, role, res, tag=f"case{i}")
                else:
                    do_ui(step, role, res, tag=f"s{i}")
            except Exception as e:
                res["reason"] = f"{type(e).__name__}: {e}"[:200]
            res["ms"] = int((time.time() - t_step) * 1000)
            detail.append(res)
            if on_step:
                try:
                    on_step([dict(d) for d in detail])
                except Exception:
                    pass
            if res["pass"]:
                pass_n += 1
            else:
                fail_n += 1
                break
    finally:
        for role, pg in list((pages or {}).items()):   # 角色会话录像落盘并挂到明细尾部
            from .ui_runner import finish_run_video
            finish_run_video(pg, videos.get(role), detail, label=f"「{role_names.get(role, role)}」执行录像")
        close_all()

    return {"status": "failed" if fail_n else "passed", "pass_n": pass_n, "fail_n": fail_n,
            "duration": round(time.time() - t0, 2), "detail": detail, "shared": shared}
