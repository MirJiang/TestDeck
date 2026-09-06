"""执行引擎：变量替换、检查点、记住返回值。纯同步实现，便于在专用线程串行执行。"""
import re
import time
import httpx

VAR_RE = re.compile(r"\$\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


def substitute(text: str, variables: dict) -> str:
    """把 ${name} 替换为变量值；未知变量原样保留。"""
    if not isinstance(text, str):
        return text
    return VAR_RE.sub(lambda m: str(variables.get(m.group(1), m.group(0))), text)


def get_field(data, path: str):
    """按 a.b.c 取 JSON 字段；支持数组下标 a.list.0.id。"""
    cur = data
    for part in path.split("."):
        if part == "":
            continue
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
                continue
            except (ValueError, IndexError):
                return None
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def weak_eq(a, b) -> bool:
    """弱类型比较：'0' == 0 为真。"""
    try:
        return str(a) == str(b)
    except Exception:
        return False


def evaluate_check(check: dict, status: int, body_text: str, body_json) -> dict:
    """返回 {pass, reason}。check.type: status|contains|field_eq|not_empty|jsonpath"""
    ctype = (check or {}).get("type", "status")
    expect = (check or {}).get("expect", "")
    field = (check or {}).get("field", "")

    if ctype == "status":
        want = int(expect or 200)
        ok = status == want
        return {"pass": ok, "reason": f"状态码应为 {want}，实际 {status}" if not ok else f"状态码 {status}"}
    if ctype == "contains":
        ok = str(expect) in (body_text or "")
        return {"pass": ok, "reason": f"返回应包含「{expect}」" if not ok else f"包含「{expect}」"}
    if ctype == "field_eq":
        actual = get_field(body_json, field)
        ok = weak_eq(actual, expect)
        return {"pass": ok, "reason": f"字段 {field} 应为 {expect}，实际 {actual}" if not ok else f"{field} == {expect}"}
    if ctype == "not_empty":
        actual = get_field(body_json, field) if field else body_json
        ok = actual is not None and actual != "" and actual != [] and actual != {}
        return {"pass": ok, "reason": f"字段 {field} 应不为空" if not ok else f"{field or '返回'} 非空"}
    if ctype == "jsonpath":
        # 高级断言：field 写 JSONPath 表达式（如 $.data.items[*].sku）
        # expect 留空 → 匹配到任意值即通过；expect 有值 → 任一匹配值等于它（弱类型）即通过
        return _check_jsonpath(field, expect, body_json)
    return {"pass": True, "reason": "无检查点"}


def _check_jsonpath(expr: str, expect: str, body_json) -> dict:
    if not expr:
        return {"pass": False, "reason": "JSONPath 表达式为空"}
    try:
        from jsonpath_ng import parse
        matches = [m.value for m in parse(expr).find(body_json)]
    except Exception as e:
        return {"pass": False, "reason": f"JSONPath 解析失败：{e}"[:160]}
    if not matches:
        return {"pass": False, "reason": f"{expr} 未匹配到任何值"}
    if not str(expect):
        return {"pass": True, "reason": f"{expr} 匹配到 {len(matches)} 个值"}
    ok = any(weak_eq(m, expect) for m in matches)
    shown = ", ".join(str(m) for m in matches[:5])
    return {"pass": ok,
            "reason": f"{expr} 应含 {expect}，实际匹配 [{shown}]" if not ok else f"{expr} 匹配到 {expect}"}


def run_case(case, env, timeout: float = 15.0) -> dict:
    """同步执行一条 API 用例。步骤失败默认中止（step.continue_on_fail 可覆盖）。"""
    base = (env.base_url or "").rstrip("/") if env else ""
    variables = dict(env.variables or {}) if env else {}
    detail, pass_n, fail_n = [], 0, 0
    t0 = time.time()

    with httpx.Client(timeout=timeout, verify=False, trust_env=False) as client:
        for i, step in enumerate(case.steps or [], 1):
            url = substitute(step.get("url", ""), variables)
            if base and url.startswith("/"):
                url = base + url
            method = step.get("m", "GET").upper()
            headers = {}
            for line in (step.get("headers") or "").splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    headers[k.strip()] = substitute(v.strip(), variables)
            raw_body = substitute(step.get("body") or "", variables)
            kwargs = {"headers": headers}
            if raw_body.strip():
                kwargs["content"] = raw_body
                headers.setdefault("Content-Type", "application/json")

            t_step = time.time()
            step_res = {"idx": i, "m": method, "url": url, "pass": False, "reason": "", "ms": 0}
            try:
                resp = client.request(method, url, **kwargs)
                status, text = resp.status_code, resp.text
                try:
                    body_json = resp.json()
                except Exception:
                    body_json = None
                step_res["status"] = status
                step_res["response"] = text[:2000]
                chk = evaluate_check(step.get("check"), status, text, body_json)
                step_res.update(**{"pass": chk["pass"], "reason": chk["reason"]})
                save = step.get("save") or {}
                if chk["pass"] and save.get("name") and save.get("from"):
                    variables[save["name"]] = get_field(body_json, save["from"])
                    step_res["saved"] = save["name"]
            except Exception as e:
                step_res["reason"] = f"请求失败：{type(e).__name__}: {e}"
            step_res["ms"] = int((time.time() - t_step) * 1000)
            detail.append(step_res)
            if step_res["pass"]:
                pass_n += 1
            else:
                fail_n += 1
                if not step.get("continue_on_fail"):
                    break

    return {
        "status": "failed" if fail_n else "passed",
        "pass_n": pass_n,
        "fail_n": fail_n,
        "duration": round(time.time() - t0, 2),
        "detail": detail,
        "variables": variables,
    }
