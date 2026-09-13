"""AI 驱动的 UI 测试引擎：AgentScope ReAct agent 看页面状态逐步操控浏览器完成自然语言目标。

两种用法：
  1. 独立 AI 用例：TestCase.type == "ai"，steps[0] = {goal, start_url, max_steps}
  2. 流程测试的 AI 步骤：Flow step.type == "ai"，goal/url 按角色变量解析，AI 存的值进共享命名空间

决策循环在 brain_agentscope（B4 起唯一路径）：动作注册为白名单工具、done 走结构化输出、
多模型回退链。本模块保留 AI-API 请求循环（run_ai_api_case）与动作执行层（_exec_action）。
"""
import time
from urllib.parse import urljoin

import httpx

from .. import ai as A
from .api_docs import format_endpoints
from .runner import substitute, evaluate_check, get_field
from .web_interact import HELPERS_JS
from .ui_runner import STATIC_DIR
from .browser import launch_browser
from . import queue as _q

STATIC_DIR.mkdir(exist_ok=True)

DEFAULT_MAX_STEPS = 200

# 提取页面状态：通用可交互元素清单（判定标准见 web_interact.HELPERS_JS，无站点特定规则）。
# 选择器优先级：id > data-testid/name > placeholder/type > aria-label > 精确文本 > 同标签序号。
# checkbox/radio/file 不进清单（模型用不上、费 token）；无名可点元素（纯图标无 aria-label）也不进。
# 弹窗优先：可见 dialog 的内容单独提取（配额 40）——弹窗是当前交互焦点，且其 DOM 追加在
# body 末尾，顺序遍历必被主页面挤出上限（实测选行弹窗会被整块截掉，AI 只能对着截图盲点）；
# 弹窗内零交互信号的表格行降级收录为可点条目（弹窗里的行几乎必然响应点击/双击，如双击选行）。
# 主页面配额 80（管理后台的菜单/页签 chrome 很重，40 会截掉长表单尾部的字段）。
_STATE_JS = "() => {" + HELPERS_JS + """
  // 屏外已渲染元素也算可见（下方表单区块）：点击/填写句柄时浏览器会自动滚过去；
  // 只保留尺寸/样式/aria-hidden 判定，放开 __vis 的视口边界
  const __rendered = el => {
    const r = el.getBoundingClientRect();
    if (r.width <= 2 || r.height <= 2 || r.right <= 0 || r.left >= innerWidth) return false;
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden' || +cs.opacity < 0.1) return false;
    return el.getAttribute('aria-hidden') !== 'true';
  };
  const seen = new Set();
  const selCount = new Map();   // 同名选择器计数：第二个「添加」等同文案元素也能领到唯一句柄
  const grab = (el, allowRow) => {
    const tag = el.tagName;
    const type = (el.type || '').toLowerCase();
    if (tag === 'INPUT' && ['checkbox', 'radio', 'file'].includes(type)) return null;
    if (!__rendered(el)) return null;
    let row = false;
    if (!__inter(el)) {
      if (!(allowRow && (tag === 'TR' || (el.getAttribute('role') || '').toLowerCase() === 'row')
            && (el.innerText || '').trim())) return null;
      row = true;
    }
    const label = __label(el);
    if (!label && !['INPUT', 'SELECT', 'TEXTAREA'].includes(tag)) return null;
    if (label.length > 80) return null;   // 长文本 = 容器，不是单个可点元素
    const clean = row ? label.replace(/[ \\t\\n]+/g, ' ').trim() : label;
    let sel = __sel(el);
    const c = selCount.get(sel) || 0;
    selCount.set(sel, c + 1);
    if (c > 0) sel = sel + ' >> nth=' + c;   // Playwright 原生支持：第 c 个匹配
    if (seen.has(sel)) return null;
    seen.add(sel);
    return { selector: sel, tag: row ? 'row' : tag.toLowerCase(), text: clean.slice(0, 24),
             value: String(el.value || '').slice(0, 40) };
  };
  const DLG_SEL = '.el-dialog__wrapper, .el-drawer__wrapper, [role="dialog"], dialog';
  const inDlg = [];
  for (const d of [...document.querySelectorAll(DLG_SEL)].filter(x => x.getClientRects().length)) {
    for (const el of d.querySelectorAll('*')) {
      if (inDlg.length >= 40) break;
      const it = grab(el, true);
      if (it) inDlg.push(it);
    }
  }
  const out = [];
  for (const el of document.querySelectorAll('body *')) {
    if (out.length >= 80) break;
    if (el.closest(DLG_SEL)) continue;   // 弹窗内已单独提取
    const it = grab(el, false);
    if (it) out.push(it);
  }
  return { url: location.href, title: String(document.title || '').slice(0, 60),
           elements: [...inDlg, ...out], vw: innerWidth, vh: innerHeight,
           text: String(document.body.innerText || '').replace(/[ \\t\\n]+/g, ' ').slice(0, 700) };
}"""

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
    max_steps = int(cfg.get("max_steps") or 200)

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
        pass
        return _fail("AI 测试需要配置大模型：请在「模型配置」页添加并启用，或在 .env 配置 TD_LLM_BASE_URL / TD_LLM_KEY",
                     "未配置 LLM")
    if not base:
        return _fail("AI-API 用例需要环境配置 Base URL（接口域名）", "缺少 Base URL")

    with A.run_context(run_id), httpx.Client(base_url=base, timeout=20, trust_env=False) as client:
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

def _loc_visible_first(page, sel: str):
    """选择器命中多个元素时优先可见的那个（下拉选项常与已选值/隐藏节点同文案，
    按 DOM 序点第一个会落在隐藏元素上等到超时）。
    宿主对象不支持 locator（测试桩）时返回 None，调用方走 page.click/fill 旧路径。"""
    try:
        loc = page.locator(sel)
        n = loc.count()
        if n > 1:
            for i in range(n):
                cand = loc.nth(i)
                if cand.is_visible():
                    return cand
        return loc.first
    except Exception:
        return None


def _exec_action(page, act: dict, variables: dict):
    """在页面上执行一个模型动作。返回 (ok, reason, saved|None)。"""
    op = act.get("action", "")
    sel = substitute(act.get("selector", ""), variables)
    val = substitute(str(act.get("value", "")), variables)
    url = substitute(act.get("url", ""), variables)
    if op == "goto":
        # 模型常给相对路径（如 "/"）：按当前页解析成绝对地址，否则 Playwright 直接报 invalid URL
        target = url if "://" in url else urljoin(getattr(page, "url", "") or "http://localhost/", url)
        page.goto(target, timeout=20000)
        return True, f"打开 {target}", None
    if op == "click":
        loc = _loc_visible_first(page, sel)
        try:
            if loc is None:
                page.click(sel, timeout=5000)
            else:
                loc.click(timeout=5000)
        except Exception:
            # 常规点击超时多为遮罩/动画/未进视口；强制再点一次（绕过可点性检查），
            # 仍失败才抛给上层记失败——省掉模型"看截图改坐标"的两步回退
            if loc is None:
                page.click(sel, timeout=5000, force=True)
            else:
                loc.click(timeout=5000, force=True)
        return True, f"点击 {sel}", None
    if op == "dblclick":
        loc = _loc_visible_first(page, sel)
        try:
            if loc is None:
                page.dblclick(sel, timeout=5000)
            else:
                loc.dblclick(timeout=5000)
        except Exception:
            # 与 click 同策略：超时多为遮罩/动画，强制再试一次
            if loc is None:
                page.dblclick(sel, timeout=5000, force=True)
            else:
                loc.dblclick(timeout=5000, force=True)
        return True, f"双击 {sel}", None
    if op == "click_xy":  # 点选验证码：按截图坐标点击
        x, y = int(act.get("x", 0)), int(act.get("y", 0))
        page.mouse.click(x, y)
        return True, f"点击坐标 ({x},{y})", None
    if op == "scroll":
        dy = max(-2000, min(2000, int(act.get("dy", 600) or 600)))
        try:
            page.mouse.wheel(0, dy)          # 滚轮：不依赖滚动条位置
        except Exception:                    # 引擎不支持滚轮时退回 window.scrollBy
            page.evaluate(f"window.scrollBy(0, {dy})")
        return True, f"滚动页面{'向下' if dy > 0 else '向上'} {abs(dy)}px", None
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
        loc = _loc_visible_first(page, sel)
        if loc is None:
            page.fill(sel, val, timeout=8000)
        else:
            loc.fill(val, timeout=8000)
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
             page_map: str = "", project_id: str = "", map_keys: set | None = None) -> dict:
    """同步驱动（B4 起唯一路径）：委托 AgentScope ReAct agent（brain_agentscope.run_brain）。
    返回 {status, pass_n, fail_n, detail, saved, summary}，须在执行队列线程调用。

    on_step(detail_list)：每执行完一步回调一次，调用方可把进度增量写入数据库实现流式展示。
    engine 会被标注到首个步骤明细，供前端展示本次用的浏览器引擎。
    project_id 非空时启用执行比对信号：每步把当前页与应用地图（期望基线）比对，地图记录的
    按钮缺失就在该步明细记 warning——不判失败、不计入 pass_n/fail_n、不回写地图。
    """
    from .brain_agentscope import run_brain   # 惰性导入：模块反向依赖 ai_runner 的动作层
    with A.run_context(run_id):   # 本执行的 LLM 调用都归属到 llm_logs.run_id
        r = run_brain(page, goal, variables, max_steps=max_steps, run_id=run_id,
                      shot_tag=shot_tag, on_step=on_step, engine=engine,
                      page_map=page_map, project_id=project_id, map_keys=map_keys)
    r.pop("brain", None)     # 内部标注不进执行明细契约
    r.pop("usage", None)     # 用量已经 on_usage 实时写入 llm_logs
    return r


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
        from .app_mapper import app_map_brief, map_page_keys
        pid = getattr(case, "project_id", "") or ""
        r = ai_drive(page, goal, variables, max_steps=max_steps, run_id=run_id,
                     on_step=on_step, engine=used,
                     page_map=app_map_brief(pid),
                     project_id=pid,
                     map_keys=map_page_keys(pid) if pid else set())
        if detail:  # 把 note 并进结果明细头部（idx 保持 ai_drive 的编号可读性）
            r["detail"] = detail + r["detail"]
            r["pass_n"] += 1
        finish_run_video(page, video_url, r["detail"])
        browser.close()
    return r
