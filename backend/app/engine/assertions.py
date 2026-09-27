"""验收断言原语（测试设计的可执行预期）。

与 AI 动作集解耦：断言由引擎执行、结果客观（通过/未通过 + 实际值），模型无权解释。
原语同时服务两处：
- 测试点验收（brain 的 browser_verify 工具与收尾兜底验证）
- AI 自主断言动作（expect_text 已并入本表，_exec_action 保持兼容）

每条断言 dict 形如 {"type": "expect_text", "value": "登录成功"}，
expect_api 为 {"type": "expect_api", "m": "GET", "url": "/api/inquiry/${bill_no}",
               "field": "status", "expect": "0"}——URL 支持 ${变量}，相对路径拼 api_base。
"""
import time

ASSERT_TYPES = ("expect_text", "expect_not_text", "expect_url", "expect_element",
                "expect_value", "expect_api")

_MAX_API_BODY = 500   # 断言失败时回喂的实际值截断


def _sub(text, variables: dict) -> str:
    from .runner import substitute
    return substitute(str(text or ""), variables or {})


def run_assertion(a: dict, page=None, variables: dict | None = None,
                  api_base: str = "", http=None) -> dict:
    """执行一条断言，返回 {ok, assert, reason}。page/http 由调用方提供（线程亲和性归调用线程）。"""
    variables = variables or {}
    t = (a.get("type") or "").strip()
    try:
        if t == "expect_text":
            val = _sub(a.get("value", ""), variables)
            body = page.inner_text("body", timeout=8000)
            deadline = time.time() + 3   # 慢渲染给 3s 等待窗口（比动作断言短：验收时状态应已稳定）
            while val not in body and time.time() < deadline:
                time.sleep(0.2)
                body = page.inner_text("body", timeout=8000)
            return {"ok": val in body, "assert": f"页面包含「{val}」",
                    "reason": "" if val in body else f"页面未包含「{val}」"}
        if t == "expect_not_text":
            val = _sub(a.get("value", ""), variables)
            body = page.inner_text("body", timeout=8000)
            return {"ok": val not in body, "assert": f"页面不包含「{val}」",
                    "reason": "" if val not in body else f"页面出现了不应出现的「{val}」"}
        if t == "expect_url":
            val = _sub(a.get("value", ""), variables)
            url = page.url or ""
            ok = val in url
            return {"ok": ok, "assert": f"URL 包含 {val}",
                    "reason": "" if ok else f"URL 是 {url[:_MAX_API_BODY]}"}
        if t == "expect_element":
            sel = _sub(a.get("selector", ""), variables)
            el = page.locator(sel).first
            vis = False
            try:
                el.wait_for(state="visible", timeout=3000)
                vis = True
            except Exception:
                pass
            return {"ok": vis, "assert": f"元素可见 {sel}",
                    "reason": "" if vis else "元素不可见或不存在"}
        if t == "expect_value":
            sel = _sub(a.get("selector", ""), variables)
            val = _sub(a.get("value", ""), variables)
            cur = ""
            try:
                cur = el_value(page, sel)
            except Exception:
                pass
            ok = str(cur) == str(val)
            return {"ok": ok, "assert": f"{sel} 的值为 {val}",
                    "reason": "" if ok else f"实际值是 {str(cur)[:_MAX_API_BODY]}"}
        if t == "expect_api":
            if http is None:
                return {"ok": False, "assert": "API 断言", "reason": "无 HTTP 客户端（非执行态）"}
            m = (a.get("m") or "GET").upper()
            url = _sub(a.get("url", ""), variables)
            if url.startswith("/") and api_base:
                url = api_base.rstrip("/") + url
            field, expect = a.get("field", ""), _sub(a.get("expect", ""), variables)
            resp = http.request(m, url)
            try:
                body_json = resp.json()
            except Exception:
                body_json = None
            from .runner import get_field, weak_eq
            actual = get_field(body_json, field) if field else None
            if field:
                ok = resp.status_code == 200 and (str(expect) == "" or weak_eq(actual, expect))
                desc = f"{m} {url} {field}=={expect or '（任意值）'}"
                why = "" if ok else f"HTTP {resp.status_code}，{field} 实际是 {str(actual)[:_MAX_API_BODY]}"
            else:
                ok = resp.status_code == 200
                desc = f"{m} {url} 返回 200"
                why = "" if ok else f"HTTP {resp.status_code}"
            return {"ok": ok, "assert": desc, "reason": why}
        return {"ok": False, "assert": t or "（空）", "reason": f"未知断言类型：{t}"}
    except Exception as e:
        return {"ok": False, "assert": t, "reason": f"{type(e).__name__}: {e}"[:_MAX_API_BODY]}


def el_value(page, sel: str):
    """输入框取 value，其他元素取文本（expect_value 的取值语义）。"""
    tag = page.evaluate("(s) => { const e = document.querySelector(s); return e ? e.tagName.toLowerCase() : ''; }", sel)
    if tag in ("input", "textarea", "select"):
        return page.input_value(sel, timeout=3000)
    return (page.inner_text(sel, timeout=3000) or "").strip()


def verify_assertions(asserts: list, page=None, variables: dict | None = None,
                      api_base: str = "", http=None) -> list[dict]:
    """执行一组断言（遇第一条失败即停，失败原因单一可定位）。"""
    out = []
    for a in asserts or []:
        r = run_assertion(a, page=page, variables=variables, api_base=api_base, http=http)
        out.append(r)
        if not r["ok"]:
            break
    return out
