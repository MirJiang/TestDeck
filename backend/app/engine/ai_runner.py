"""AI 驱动的 UI 测试引擎：把页面状态交给大模型决策，逐步操控浏览器完成自然语言目标。

两种用法：
  1. 独立 AI 用例：TestCase.type == "ai"，steps[0] = {goal, start_url, max_steps}
  2. 流程测试的 AI 步骤：Flow step.type == "ai"，goal/url 按角色变量解析，AI 存的值进共享命名空间

循环：提取页面状态（可见交互元素 + 文字摘要）→ LLM 输出下一步动作 → 执行 → 直到
模型输出 done 或达到步数上限。每个动作记录为一条明细（含模型理由），结束时留截图。
需要配置 LLM（TD_LLM_BASE_URL / TD_LLM_KEY），纯规则无法驱动浏览器。
"""
import base64
import time
from pathlib import Path

import httpx

from .. import ai as A
from .. import config
from .runner import substitute, evaluate_check, get_field
from .ui_runner import STATIC_DIR
from .browser import launch_browser
from . import queue as _q

STATIC_DIR.mkdir(exist_ok=True)

DEFAULT_MAX_STEPS = 30

# 提取页面状态：可见的交互元素（选择器/文字/当前值）+ 标题 + 文字摘要
# 选择器兜底顺序：id > name > placeholder 属性 > type 属性 > 同标签可见序号。
# 注意不能用 :has-text 兜底输入框——它只匹配内部文字，input 永远不匹配。
_STATE_JS = """
() => {
  const visible = el => { const r = el.getBoundingClientRect(); return r.width && r.height && r.bottom >= 0 && r.top <= innerHeight; };
  const out = [];
  const seen = new Set();
  for (const t of document.querySelectorAll('a, button, input, select, textarea, [onclick]')) {
    if (!visible(t)) continue;
    const tag = t.tagName.toLowerCase();
    if (tag === 'input' && ['hidden', 'checkbox', 'radio', 'file'].includes(t.type)) continue;
    let sel = '';
    if (t.id) sel = '#' + t.id;
    else if (t.name) sel = tag + '[name="' + t.name + '"]';
    else if ((tag === 'input' || tag === 'textarea') && t.placeholder) sel = tag + '[placeholder="' + t.placeholder.slice(0, 30) + '"]';
    else if (tag === 'input' && t.type && t.type !== 'text') sel = tag + '[type="' + t.type + '"]';
    else if (tag !== 'input' && tag !== 'textarea') {
      const text = String(t.innerText || '').trim().slice(0, 24);
      if (text) sel = tag + ':has-text("' + text.replace(/"/g, '\\\\"') + '")';
    }
    if (!sel) {  // 同标签可见序号，Playwright 稳定可执行
      const peers = [...document.querySelectorAll(tag)].filter(visible);
      sel = tag + ' >> nth=' + peers.indexOf(t);
    }
    const label = t.innerText || t.value || t.placeholder || t.title || '';
    const text = String(label).trim().slice(0, 24);
    const key = sel;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push({ selector: sel, tag: tag, text: text, value: String(t.value || '').slice(0, 40) });
    if (out.length >= 40) break;
  }
  return { url: location.href, title: String(document.title || '').slice(0, 60),
           elements: out, vw: innerWidth, vh: innerHeight,
           text: String(document.body.innerText || '').replace(/\\s+/g, ' ').slice(0, 700) };
}
"""

# 视觉模式附加指令（随截图一并发给模型）
_SYSTEM_VISION = (
    "\n本轮会附上当前页面的截图。涉及验证码或图形操作时："
    "滑块验证码输出 drag 动作（x,y=按住滑块手柄的起点像素坐标，x2,y2=拖到缺口/终点的坐标，拖动会自动分步模拟人手）；"
    "点选验证码按顺序输出多次 click_xy（x,y=每个目标中心的像素坐标，一次一个，多轮完成）；"
    "图片文字/数字/算式验证码直接读出内容，用 fill 填进对应输入框。"
    "坐标以截图左上角为原点，不要超出截图范围。"
)

# ---------- API 目标循环 ----------

_SYSTEM_API = (
    "你是接口自动化测试助手，根据测试目标自主设计并发送 HTTP 请求，逐步完成测试。"
    '每轮输出一个动作，JSON 格式：{"think":"一句话理由","action":"request|done",'
    '"request":{"m":"GET|POST|PUT|DELETE","url":"/api/xxx（相对路径，Base URL 自动拼接）",'
    '"headers":{"K":"V"},"body":"JSON 字符串"},'
    '"check":{"type":"status|contains|field_eq|not_empty","field":"data.code","expect":"0"},'
    '"save":{"name":"token","from":"data.token"},"pass":true,"reason":""}。\n'
    "规则：url 只填路径；请求体用 JSON，字段值可用 ${变量} 引用可用变量；"
    "登录/创建类请求成功后用 save 记住 token 或单据号，后续请求在 headers 里带 ${token} 或直接引用；"
    "check 描述本请求的通过条件；收到失败响应时分析原因并调整（换参数/换路径），不要原样重试；"
    "目标达成或确认无法继续时输出 done，用 pass 与 reason 给出测试结论。只输出 JSON。"
)


def _api_prompt(goal: str, variables: dict, history: list, base: str, endpoints: list | None = None) -> str:
    var = "; ".join(f"${k}={v}" for k, v in variables.items()) or "无"
    hist = "\n".join(history[-10:]) or "无"
    p = (f"测试目标：{goal}\nBase URL：{base}（url 只填路径）\n可用变量：{var}\n"
         f"已发送请求与结果：\n{hist}")
    if endpoints:
        p += ("\n\n项目接口文档中的接口（优先使用这些真实接口与参数，不要臆造路径）：\n"
              + format_endpoints(endpoints))
    return p


def _parse_headers(hdrs) -> dict:
    """兼容 dict 与 'K: V' 多行文本两种形式。"""
    out = {}
    if isinstance(hdrs, dict):
        for k, v in hdrs.items():
            out[str(k)] = substitute(str(v), {})
    elif isinstance(hdrs, str):
        for line in hdrs.splitlines():
            if ":" in line:
                k, _, v = line.partition(":")
                out[k.strip()] = v.strip()
    return out


def _do_request(client, req: dict, check: dict, variables: dict):
    """执行单个请求（url/头/体先做变量替换）。返回 (ok, reason, status, text, body_json)。"""
    m = (req.get("m") or "GET").upper()
    url = substitute(req.get("url", ""), variables)
    headers = _parse_headers(req.get("headers"))
    for k, v in list(headers.items()):
        headers[k] = substitute(v, variables)
    body = substitute(str(req.get("body") or ""), variables)
    if body.strip():
        headers.setdefault("Content-Type", "application/json")
    resp = client.request(m, url, headers=headers, content=body or None)
    text = resp.text
    try:
        bj = resp.json()
    except Exception:
        bj = None
    chk = evaluate_check(check or {"type": "status", "expect": "200"}, resp.status_code, text, bj)
    return chk["pass"], chk["reason"], resp.status_code, text, bj


def _case_vars(case, env) -> dict:
    """执行变量 = 环境变量 + 用例绑定的测试账号（username/password，从项目用户列表带出）。"""
    variables = dict(env.variables or {}) if env else {}
    if getattr(case, "username", ""):
        variables["username"] = case.username
        variables["password"] = getattr(case, "password", "") or ""
    return {k: str(v) for k, v in variables.items()}


def run_ai_api_case(case, env, run_id: str, on_step=None) -> dict:
    """AI-API 用例：模型设计请求 → httpx 执行 → 检查/存变量 → 循环。由执行队列线程调用。"""
    cfg = (case.steps or [{}])[0] if case.steps else {}
    variables = _case_vars(case, env)
    goal = substitute(cfg.get("goal", ""), variables)
    base = (env.base_url or "").rstrip("/") if env else ""
    max_steps = int(cfg.get("max_steps") or 12)

    def _emit():
        if on_step:
            try:
                on_step([dict(d) for d in detail])
            except Exception:
                pass

    def _fail(reason: str, summary: str):
        detail.append({"idx": len(detail) + 1, "action": "error", "target": "",
                       "pass": False, "reason": reason[:300], "ms": 0})
        _emit()
        return {"status": "failed", "pass_n": 0, "fail_n": 1, "duration": round(time.time() - t0, 2),
                "detail": detail, "saved": {}, "summary": summary}

    detail, saved, history = [], {}, []
    pass_n = fail_n = bad = 0
    t0 = time.time()
    summary = ""
    last_fail_key, same_fail = None, 0

    if not A.llm_available():
        return _fail("AI 测试需要配置大模型：请在「模型配置」页添加并启用，或在 .env 配置 TD_LLM_BASE_URL / TD_LLM_KEY",
                     "未配置 LLM")
    if not base:
        return _fail("AI-API 用例需要环境配置 Base URL（接口域名）", "缺少 Base URL")

    with httpx.Client(base_url=base, timeout=20, trust_env=False) as client:
        endpoints = (cfg.get("endpoints") or [])[:30]
        for i in range(1, max_steps + 1):
            if _q.is_cancelled(run_id):
                return _fail("已手动取消", "用户取消")
            act = A.chat_json_sync(_SYSTEM_API, _api_prompt(goal, variables, history, base, endpoints), kind="agent-api")
            if not act or not act.get("action"):
                bad += 1
                if bad >= 3:
                    return _fail("模型连续 3 次未返回有效动作", "模型输出无效")
                continue
            bad = 0
            op = act.get("action")
            entry = {"idx": i, "action": op, "think": (act.get("think") or "")[:120], "ms": 0,
                     "url": "", "selector": "", "value": ""}

            if op == "done":
                ok = bool(act.get("pass", True))
                entry.update(target="", **{"pass": ok},
                             reason=(act.get("reason") or "模型判定完成")[:300])
                detail.append(entry); _emit()
                pass_n, fail_n = pass_n + (1 if ok else 0), fail_n + (0 if ok else 1)
                summary = act.get("reason") or ("目标达成" if ok else "模型判定未达成")
                break

            if op != "request":
                entry.update(target="", **{"pass": False}, reason=f"不支持的动作：{op}")
                detail.append(entry); _emit(); fail_n += 1
                history.append(f"无效动作 {op}")
                continue

            req = act.get("request") or {}
            m = (req.get("m") or "GET").upper()
            url = substitute(req.get("url", ""), variables)
            entry["target"] = f"{m} {url}"
            entry["request"] = {"m": m, "url": req.get("url", ""),
                                "headers": req.get("headers") or {}, "body": req.get("body") or ""}
            entry["check"] = act.get("check") or {"type": "status", "expect": "200"}
            entry["save"] = act.get("save") or {}

            t_step = time.time()
            try:
                ok, chk_reason, status, text, body_json = _do_request(client, req, entry["check"], variables)
                reason = f"{status} · {chk_reason}"
                save = act.get("save") or {}
                if ok and save.get("name") and save.get("from"):
                    saved_val = get_field(body_json, substitute(str(save.get("from", "")), {}))
                    if saved_val is not None:
                        saved[save["name"]] = saved_val
                        variables[save["name"]] = str(saved_val)
                        entry["saved"] = save["name"]
            except Exception as e:
                status, ok, reason = None, False, f"{type(e).__name__}: {e}"[:200]
            entry["ms"] = int((time.time() - t_step) * 1000)
            entry.update(**{"pass": ok}, reason=reason)
            if status is not None:
                entry["status"] = status
                entry["response"] = text[:300]
            detail.append(entry); _emit()
            pass_n, fail_n = pass_n + (1 if ok else 0), fail_n + (0 if ok else 1)
            history.append(f"{m} {url} → {status} {'通过' if ok else '失败:' + reason}")

            if not ok:  # 同一请求连续失败 3 次熔断
                key = (m, url)
                same_fail = same_fail + 1 if key == last_fail_key else 1
                last_fail_key = key
                if same_fail >= 3:
                    return _fail(f"请求 {m} {url} 连续 {same_fail} 次失败，已提前终止", "重复失败终止")
            else:
                last_fail_key, same_fail = None, 0
        else:
            return _fail(f"达到最大步数（{max_steps}）仍未完成目标", f"超过最大步数 {max_steps}")

    if not summary:
        summary = "执行结束"
    return {"status": "failed" if fail_n else "passed", "pass_n": pass_n, "fail_n": fail_n,
            "duration": round(time.time() - t0, 2), "detail": detail, "saved": saved, "summary": summary}


def _replay_api(fixed: list, env, on_step=None) -> dict:
    """API 固定请求回放（固化产物，零 token）：按存下的 request/check 原样重发。"""
    variables = {k: str(v) for k, v in (env.variables or {}).items()} if env else {}
    base = (env.base_url or "").rstrip("/") if env else ""
    detail, saved = [], {}
    pass_n = fail_n = 0
    t0 = time.time()

    def _emit():
        if on_step:
            try:
                on_step([dict(d) for d in detail])
            except Exception:
                pass

    if not base:
        return {"status": "failed", "pass_n": 0, "fail_n": 1, "duration": 0.0,
                "detail": [{"idx": 1, "action": "error", "target": "", "pass": False,
                            "reason": "AI-API 用例需要环境配置 Base URL", "ms": 0}],
                "saved": {}, "summary": "缺少 Base URL"}

    with httpx.Client(base_url=base, timeout=20, trust_env=False) as client:
        for i, st in enumerate(fixed, 1):
            req = st.get("request") or {}
            check = st.get("check") or {"type": "status", "expect": "200"}
            m = (req.get("m") or "GET").upper()
            entry = {"idx": i, "action": "request", "target": f"{m} {req.get('url', '')}", "ms": 0,
                     "url": "", "selector": "", "value": ""}
            t_step = time.time()
            try:
                ok, chk_reason, status, text, bj = _do_request(client, req, check, variables)
                reason = f"{status} · {chk_reason}"
                save = st.get("save") or {}
                if ok and save.get("name") and save.get("from"):
                    saved_val = get_field(bj, substitute(str(save.get("from", "")), {}))
                    if saved_val is not None:
                        saved[save["name"]] = saved_val
                        variables[save["name"]] = str(saved_val)
                        entry["saved"] = save["name"]
            except Exception as e:
                status, ok, reason = None, False, f"{type(e).__name__}: {e}"[:200]
            entry["ms"] = int((time.time() - t_step) * 1000)
            entry.update(**{"pass": ok}, reason=reason)
            if status is not None:
                entry["status"] = status
                entry["response"] = text[:300]
            detail.append(entry); _emit()
            pass_n, fail_n = pass_n + (1 if ok else 0), fail_n + (0 if ok else 1)

    return {"status": "failed" if fail_n else "passed", "pass_n": pass_n, "fail_n": fail_n,
            "duration": round(time.time() - t0, 2), "detail": detail, "saved": saved,
            "summary": f"固定请求回放 · {'通过' if not fail_n else '未通过'}"}

_SYSTEM = (
    "你是浏览器自动化测试助手，根据目标一步步操控网页完成测试。每轮给你当前页面状态，"
    '你输出下一个动作，格式：{"think":"一句话理由","action":"goto|click|fill|expect_text|save|done",'
    '"url":"","selector":"","value":"","name":"","pass":true,"reason":""}。\n'
    "规则：click/fill 必须原样使用页面状态中给出的 selector；fill 需要给 value（可用 ${变量} 引用可用变量）；"
    "expect_text 检查页面是否包含 value 文字；save 把页面上看到的业务单号等值存为变量（给 name 和 value）；"
    "goto 仅在需要跳转到全新地址时使用（相对路径以 / 开头）。"
    "某个动作失败（尤其超时）后，千万不要重复同一个 selector，改用状态列表里的其他元素或其它定位方式；"
    "连续失败说明该元素状态不对，先重新观察页面再行动。"
    "目标达成或确认无法继续时输出 done，"
    "用 pass（布尔）与 reason 总结测试结论。不要输出 JSON 以外的任何文字。"
)


def _build_prompt(goal: str, variables: dict, history: list, state: dict,
                  vision: bool = False, page_map: str = "") -> str:
    els = "; ".join(f"{e['selector']}({e['tag']}「{e['text']}」)" for e in state.get("elements", [])[:40])
    var = "; ".join(f"${k}={v}" for k, v in variables.items()) or "无"
    hist = "; ".join(history[-12:]) or "无"
    p = (f"测试目标：{goal}\n可用变量：{var}\n已执行动作：{hist}\n"
         f"当前页面：{state.get('url', '')}「{state.get('title', '')}」\n"
         f"可交互元素：{els or '无'}\n页面文字：{state.get('text', '')}")
    if page_map:
        p += f"\n\n{page_map}"   # 应用地图先验知识：页面关系与按钮清单
    if vision:
        p += f"\n附有当前页面截图，尺寸 {state.get('vw', 0)}x{state.get('vh', 0)}，坐标原点为左上角。"
    return p


def _exec_action(page, act: dict, variables: dict):
    """在页面上执行一个模型动作。返回 (ok, reason, saved|None)。"""
    op = act.get("action", "")
    sel = substitute(act.get("selector", ""), variables)
    val = substitute(str(act.get("value", "")), variables)
    url = substitute(act.get("url", ""), variables)
    if op == "goto":
        page.goto(url, timeout=20000)
        return True, f"打开 {url}", None
    if op == "click":
        page.click(sel, timeout=8000)
        return True, f"点击 {sel}", None
    if op == "click_xy":  # 点选验证码：按截图坐标点击
        x, y = int(act.get("x", 0)), int(act.get("y", 0))
        page.mouse.click(x, y)
        return True, f"点击坐标 ({x},{y})", None
    if op == "drag":  # 滑块验证码：按住起点分步拖到终点，模拟人手轨迹
        x1, y1 = int(act.get("x", 0)), int(act.get("y", 0))
        x2, y2 = int(act.get("x2", 0)), int(act.get("y2", 0))
        page.mouse.move(x1, y1)
        page.mouse.down()
        n = 14
        for i in range(1, n + 1):
            page.mouse.move(x1 + (x2 - x1) * i / n, y1 + (y2 - y1) * i / n)
            time.sleep(0.03)
        time.sleep(0.1)
        page.mouse.up()
        return True, f"拖拽 ({x1},{y1})→({x2},{y2})", None
    if op == "fill":
        page.fill(sel, val, timeout=8000)
        return True, f"在 {sel} 输入 {val}", None
    if op == "expect_text":
        body = page.inner_text("body", timeout=8000)
        ok = val in body
        return ok, (f"页面包含「{val}」" if ok else f"页面未包含「{val}」"), None
    if op == "save":
        name, value = (act.get("name") or "").strip(), act.get("value", "")
        if not name:
            return False, "save 缺少变量名", None
        return True, f"已记住 {name} = {value}", (name, value)
    return False, f"不支持的动作：{op}", None


_FAST_MODEL_MARKS = ("flash", "turbo", "lite", "mini", "air", "instant", "speed", "haiku")


def _model_hint() -> str:
    """失败时的模型分档建议：验证码/复杂定位等高难度场景对模型能力敏感。"""
    try:
        model = A.current_model()
        if not model:
            return ""
        if not A.vision_enabled():
            return "当前未开启视觉（截图辅助）——验证码、复杂页面建议在「模型配置」勾选支持视觉"
        if any(m in model.lower() for m in _FAST_MODEL_MARKS):
            return f"当前模型 {model} 为快速档，验证码等高难度场景建议在「模型配置」切换更强档模型"
    except Exception:
        pass
    return ""


def ai_drive(page, goal: str, variables: dict, max_steps: int = DEFAULT_MAX_STEPS,
             run_id: str = "", shot_tag: str = "ai", on_step=None, engine: str = "",
             page_map: str = "", project_id: str = "") -> dict:
    """同步驱动：返回 {status, pass_n, fail_n, detail, saved, summary}。须在执行队列线程调用。

    对外门面（批次 B）：.env 配 TD_BRAIN=agentscope 时委托 AgentScope ReAct agent
    （brain_agentscope.run_brain），否则走下方手搓循环（B4 基准对比期间保留，对比完成后删除）。
    两条路径返回结构完全一致，所有调用方零改动。

    on_step(detail_list)：每执行完一步回调一次，调用方可把进度增量写入数据库实现流式展示。
    engine 会被标注到首个步骤明细，供前端展示本次用的浏览器引擎。
    project_id 非空时启用执行比对信号：每步把当前页与应用地图（期望基线）比对，地图记录的
    按钮缺失就在该步明细记 warning——不判失败、不计入 pass_n/fail_n、不回写地图。
    """
    if (config.get("TD_BRAIN") or "legacy").strip().lower() in ("agentscope", "as"):
        from .brain_agentscope import run_brain
        r = run_brain(page, goal, variables, max_steps=max_steps, run_id=run_id,
                      shot_tag=shot_tag, on_step=on_step, engine=engine,
                      page_map=page_map, project_id=project_id)
        r.pop("brain", None)     # 内部标注不进执行明细契约
        r.pop("usage", None)     # 用量已经 on_usage 实时写入 llm_logs
        return r

    detail, saved, history = [], {}, []
    pass_n = fail_n = bad = 0
    t0 = time.time()
    summary = ""
    last_fail_key, same_fail = None, 0
    last_warn = ""   # 同一缺失集合只在变化时报一次，避免每步刷屏
    use_vision = A.vision_enabled()   # 使用中的模型勾选了视觉才发截图（省 token）

    def _emit():
        if on_step:
            try:
                on_step([dict(d) for d in detail])
            except Exception:
                pass

    def _mark(entry: dict) -> dict:
        if engine and detail and not detail[0].get("engine"):
            detail[0]["engine"] = engine
        return entry

    if not A.llm_available():
        detail.append({"idx": 1, "action": "error", "target": "", "pass": False,
                       "reason": "AI 测试需要配置大模型：请在「模型配置」页添加并启用，或在 .env 配置 TD_LLM_BASE_URL / TD_LLM_KEY", "ms": 0})
        return {"status": "failed", "pass_n": 0, "fail_n": 1, "detail": detail, "saved": {},
                "summary": "未配置 LLM"}

    def _shot(entry):
        try:
            path = STATIC_DIR / f"{run_id or 'ai'}-{shot_tag}-{int(time.time())}.png"
            page.screenshot(path=str(path), full_page=False)
            entry["screenshot"] = f"/static/{path.name}"
        except Exception:
            pass

    i = 0
    for i in range(1, max_steps + 1):
        if run_id and _q.is_cancelled(run_id):
            detail.append({"idx": i, "action": "note", "target": "", "pass": False,
                           "reason": "已手动取消", "ms": 0})
            _emit()
            fail_n += 1
            summary = "用户取消"
            break
        try:
            state = page.evaluate(_STATE_JS)
        except Exception as e:
            state = {"url": "", "title": "", "elements": [], "text": f"页面状态读取失败：{e}"}
        # 执行比对信号：地图（期望基线）记录的按钮在当前页 DOM 缺失 → 本步记警告（元素级回归检测）
        step_warn = ""
        if project_id:
            try:
                from .app_mapper import map_gaps
                w = map_gaps(project_id, state.get("url", ""), page)
            except Exception:
                w = ""
            if w != last_warn:
                step_warn = w
            last_warn = w
        image_b64 = None
        system = _SYSTEM
        if use_vision:
            try:  # 视觉模式：视口截图（坐标与截图一一对应）
                image_b64 = base64.b64encode(page.screenshot(type="jpeg", quality=80)).decode()
                system = _SYSTEM + _SYSTEM_VISION
            except Exception:
                image_b64 = None  # 截图失败（如 Lightpanda 不支持）退回纯文本决策
        act = A.chat_json_sync(system, _build_prompt(goal, variables, history, state, bool(image_b64), page_map),
                               kind="agent-step", image_b64=image_b64)
        if not act or not act.get("action"):
            bad += 1
            if bad >= 3:
                e = {"idx": i, "action": "error", "target": "", "pass": False,
                     "reason": "模型连续 3 次未返回有效动作", "ms": 0}
                _shot(e)
                detail.append(e); _emit(); fail_n += 1
                summary = "模型输出无效"
                break
            continue
        bad = 0
        op = act["action"]
        entry = {"idx": i, "action": op, "think": (act.get("think") or "")[:120], "ms": 0}
        if step_warn:
            entry["warning"] = step_warn
        if op == "done":
            ok = bool(act.get("pass", True))
            entry.update(target="", **{"pass": ok}, reason=(act.get("reason") or "模型判定完成")[:300])
            entry["url"], entry["selector"], entry["value"] = "", "", ""
            _shot(entry)
            detail.append(_mark(entry)); _emit()
            pass_n, fail_n = pass_n + (1 if ok else 0), fail_n + (0 if ok else 1)
            summary = act.get("reason") or ("目标达成" if ok else "模型判定未达成")
            break
        t_step = time.time()
        try:
            ok, reason, saved_kv = _exec_action(page, act, variables)
        except Exception as e:
            ok, reason, saved_kv = False, f"{type(e).__name__}: {e}"[:200], None
        entry["ms"] = int((time.time() - t_step) * 1000)
        entry.update(target=reason.split("，")[0][:80], **{"pass": ok}, reason=reason)
        entry["url"], entry["selector"] = act.get("url", ""), act.get("selector", "")
        entry["value"] = str(act.get("value", "")) if op in ("fill", "expect_text", "save") else ""
        if op == "save" and ok and saved_kv:
            saved[saved_kv[0]] = saved_kv[1]
            entry["saved"] = saved_kv[0]
        detail.append(_mark(entry)); _emit()
        pass_n, fail_n = pass_n + (1 if ok else 0), fail_n + (0 if ok else 1)
        history.append(f"{op}({entry['target']}) {'成功' if ok else '失败:' + reason}")
        if not ok:  # 同一动作连续失败 3 次提前熔断，避免模型反复撞墙浪费几分钟
            key = (op, act.get("selector", ""))
            same_fail = same_fail + 1 if key == last_fail_key else 1
            last_fail_key = key
            if same_fail >= 3:
                e = {"idx": i + 1, "action": "error", "target": "", "pass": False,
                     "reason": f"动作 {op}({act.get('selector', '')}) 连续 {same_fail} 次失败，已提前终止", "ms": 0}
                _shot(e)
                detail.append(e); _emit(); fail_n += 1
                summary = "重复失败终止"
                break
        else:
            last_fail_key, same_fail = None, 0
    else:
        e = {"idx": i + 1, "action": "error", "target": "", "pass": False,
             "reason": f"达到最大步数（{max_steps}）仍未完成目标", "ms": 0}
        _shot(e)
        detail.append(e); _emit(); fail_n += 1
        summary = f"超过最大步数 {max_steps}"

    if not summary:
        summary = "执行结束"
    if fail_n and summary != "用户取消":
        hint = _model_hint()
        if hint:
            summary += f"；建议：{hint}"
    return {"status": "failed" if fail_n else "passed", "pass_n": pass_n, "fail_n": fail_n,
            "duration": round(time.time() - t0, 2), "detail": detail, "saved": saved, "summary": summary}


def run_ai_case(case, env, run_id: str, on_step=None) -> dict:
    """AI 用例统一入口，按 steps[0] 分派。由执行队列线程调用。

    cfg = {target: "ui"|"api", goal, start_url, max_steps, engine, fixed_steps?}
      - target=api            → run_ai_api_case（模型设计请求，httpx 执行）
      - fixed_steps 且无 goal → 固定步骤回放（录制/固化产物，零 token）
      - 其余                  → 浏览器 + ai_drive（模型看页面操作）
    """
    cfg = (case.steps or [{}])[0] if case.steps else {}
    variables = _case_vars(case, env)
    goal = substitute(cfg.get("goal", ""), variables)
    target = (cfg.get("target") or "ui").strip().lower()
    engine = (cfg.get("engine") or "").strip().lower() or None

    if target == "api":
        fixed_api = cfg.get("fixed_api_steps") or []
        if fixed_api and not goal:
            return _replay_api(fixed_api, env, on_step=on_step)
        return run_ai_api_case(case, env, run_id, on_step=on_step)

    fixed = cfg.get("fixed_steps") or []
    if fixed and not goal:  # 固定步骤回放：录制 / 固化产物
        from types import SimpleNamespace
        from . import ui_runner
        r = ui_runner._sync_run(SimpleNamespace(steps=fixed), env, run_id, engine=engine)
        r["summary"] = f"固定步骤回放 · {'通过' if r['status'] == 'passed' else '未通过'}"
        return r

    start = substitute(cfg.get("start_url", ""), variables)
    base = (env.base_url or "").rstrip("/") if env else ""
    if start.startswith("/") and base:
        start = base + start
    max_steps = int(cfg.get("max_steps") or DEFAULT_MAX_STEPS)

    from playwright.sync_api import sync_playwright
    t0 = time.time()
    with sync_playwright() as p:
        try:
            browser, used, note = launch_browser(p, engine)
        except RuntimeError as e:  # 引擎与回退都不可用：给出部署指引而不是 500
            return {"status": "failed", "pass_n": 0, "fail_n": 1, "duration": 0.0,
                    "detail": [{"idx": 1, "action": "error", "target": "", "pass": False,
                                "reason": str(e)[:300], "ms": 0}],
                    "saved": {}, "summary": "浏览器引擎不可用"}
        ctx = browser.new_context(viewport={"width": 1280, "height": 800})
        page = ctx.new_page()
        from .ui_runner import start_run_video, finish_run_video
        video_url = start_run_video(page, run_id)
        note_entry = None
        if note:  # 引擎回退等信息带进结果，用户在执行明细里能看到
            note_entry = {"idx": 1, "action": "note", "target": note, "pass": True,
                          "reason": note, "ms": 0, "engine": used}
        try:
            page.goto(start, timeout=25000)
        except Exception as e:
            detail = ([note_entry] if note_entry else []) + [
                {"idx": 2 if note_entry else 1, "action": "goto", "target": start, "pass": False,
                 "reason": f"打开起始页失败：{type(e).__name__}: {e}"[:200], "ms": 0}]
            finish_run_video(page, video_url, detail)
            browser.close()
            return {"status": "failed", "pass_n": 0, "fail_n": len(detail),
                    "duration": round(time.time() - t0, 2), "detail": detail,
                    "saved": {}, "summary": "起始页打开失败"}
        if note_entry:
            detail = [note_entry]
            if on_step:
                on_step([dict(note_entry)])
        else:
            detail = []
        from .app_mapper import app_map_brief
        r = ai_drive(page, goal, variables, max_steps=max_steps, run_id=run_id,
                     on_step=on_step, engine=used,
                     page_map=app_map_brief(getattr(case, "project_id", "")),
                     project_id=getattr(case, "project_id", "") or "")
        if detail:  # 把 note 并进结果明细头部（idx 保持 ai_drive 的编号可读性）
            r["detail"] = detail + r["detail"]
            r["pass_n"] += 1
        finish_run_video(page, video_url, r["detail"])
        browser.close()
    return r
