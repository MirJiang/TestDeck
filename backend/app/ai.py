"""LLM 适配层：OpenAI 兼容接口；未配置 key 时降级为内置启发式，保证开箱可用。

配置来源（优先级）：「模型配置」页数据库配置 > .env 文件（TD_LLM_BASE_URL / TD_LLM_KEY / TD_LLM_MODEL）。
两者都未配置时走启发式规则。
"""
import re
import json
import time
import httpx

from . import config
config.load_dotenv_file()

DEFAULT_MODEL = "glm-4-flash"


def fallback_llm() -> tuple[str, str, str]:
    """LLM 兜底配置（.env），仅在库内「模型配置」无使用中条目时生效。"""
    return (config.get("TD_LLM_BASE_URL").rstrip("/"),
            config.get("TD_LLM_KEY"),
            config.get("TD_LLM_MODEL") or DEFAULT_MODEL)


def _db_cfg():
    """数据库里「使用中」的模型配置；读取失败静默返回 None（退回 .env 兜底）。"""
    try:
        from .db import SessionLocal
        from .models import LLMConfig
        db = SessionLocal()
        try:
            row = db.query(LLMConfig).filter(LLMConfig.is_active == True).first()  # noqa
            if row and row.base_url and row.api_key:
                return row.base_url.rstrip("/"), row.api_key, row.model or DEFAULT_MODEL
        finally:
            db.close()
    except Exception:
        pass
    return None


def _cfg() -> tuple[str, str, str]:
    return _db_cfg() or fallback_llm()


def current_model() -> str:
    return _cfg()[2]


def cfg_source() -> str:
    """db | env | none，标识当前生效的配置来源（env 指 .env 文件兜底）。"""
    if _db_cfg():
        return "db"
    base, key, _ = fallback_llm()
    return "env" if (base and key) else "none"


def llm_available() -> bool:
    base, key, _ = _cfg()
    return bool(base and key)


def vision_enabled() -> bool:
    """「使用中」的模型配置是否勾选了视觉（可接收截图做验证码识别等）。"""
    try:
        from .db import SessionLocal
        from .models import LLMConfig
        db = SessionLocal()
        try:
            row = db.query(LLMConfig).filter(LLMConfig.is_active == True).first()  # noqa
            return bool(row and row.vision)
        finally:
            db.close()
    except Exception:
        return False


def _parse_content_json(text: str):
    """去掉 markdown 围栏并解析 JSON；失败返回 None。"""
    return json.loads(re.sub(r"^```(json)?|```$", "", text.strip(), flags=re.M))


def build_messages(system: str, user: str, image_b64: str | None = None) -> list:
    """构造 OpenAI 兼容消息；带图时 user 内容为数组（视觉格式）。"""
    system = system + "\n只输出 JSON，不要其他文字。"
    if image_b64:
        content = [{"type": "image_url",
                    "image_url": {"url": "data:image/jpeg;base64," + image_b64}},
                   {"type": "text", "text": user}]
        return [{"role": "system", "content": system},
                {"role": "user", "content": content}]
    return [{"role": "system", "content": system},
            {"role": "user", "content": user}]


def _request_body(system: str, user: str, model: str, image_b64: str | None = None) -> dict:
    return {"model": model, "messages": build_messages(system, user, image_b64),
            "temperature": 0.2}


async def chat_json(system: str, user: str, kind: str = "") -> dict | None:
    """调 LLM 并要求返回 JSON；失败/未配置返回 None（调用方降级）。"""
    base, key, model = _cfg()
    if not (base and key):
        return None
    ok, usage = False, (0, 0)
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(f"{base}/chat/completions",
                             headers={"Authorization": f"Bearer {key}"},
                             json=_request_body(system, user, model))
            body = r.json()
            text = body["choices"][0]["message"]["content"]
            u = body.get("usage") or {}
            usage = (u.get("prompt_tokens", 0), u.get("completion_tokens", 0))
            result = _parse_content_json(text)
            ok = True
            return result
    except Exception:
        return None
    finally:
        _log_usage(kind, usage[0], usage[1], ok)


def chat_json_sync(system: str, user: str, kind: str = "", image_b64: str | None = None) -> dict | None:
    """同步版：供执行队列线程里的 AI 用例/流程 AI 步骤调用。image_b64 为页面截图时走视觉格式。"""
    base, key, model = _cfg()
    if not (base and key):
        return None
    ok, usage = False, (0, 0)
    try:
        with httpx.Client(timeout=60) as c:
            r = c.post(f"{base}/chat/completions",
                       headers={"Authorization": f"Bearer {key}"},
                       json=_request_body(system, user, model, image_b64))
            body = r.json()
            text = body["choices"][0]["message"]["content"]
            u = body.get("usage") or {}
            usage = (u.get("prompt_tokens", 0), u.get("completion_tokens", 0))
            result = _parse_content_json(text)
            ok = True
            return result
    except Exception:
        return None
    finally:
        _log_usage(kind, usage[0], usage[1], ok)


def test_connection(base_url: str = "", api_key: str = "", model: str = "") -> dict:
    """连接测试：参数给全用参数（弹窗测未保存配置），否则回落到当前使用中的配置。"""
    base = (base_url or "").strip().rstrip("/") or None
    key = (api_key or "").strip() or None
    mdl = (model or "").strip() or None
    if not (base and key and mdl):
        cur = _cfg()
        base, key, mdl = base or cur[0], key or cur[1], mdl or cur[2]
    if not (base and key and mdl):
        return {"ok": False, "error": "尚未配置"}
    t0 = time.time()
    try:
        with httpx.Client(timeout=20, trust_env=False) as c:
            r = c.post(f"{base}/chat/completions",
                       headers={"Authorization": f"Bearer {key}"},
                       json={"model": mdl,
                             "messages": [{"role": "user", "content": "只回复两个字母：OK"}],
                             "max_tokens": 8})
            body = r.json()
            if r.status_code != 200 or not isinstance(body, dict) or "choices" not in body:
                return {"ok": False, "error": f"HTTP {r.status_code}：{str(body)[:120]}",
                        "ms": int((time.time() - t0) * 1000)}
            return {"ok": True, "reply": str(body["choices"][0]["message"]["content"])[:40],
                    "model": mdl, "ms": int((time.time() - t0) * 1000)}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"[:200],
                "ms": int((time.time() - t0) * 1000)}


def _log_usage(kind, pt, ct, ok, model=""):
    from .db import SessionLocal
    from .models import LLMLog
    db = SessionLocal()
    try:
        db.add(LLMLog(kind=kind, model=model or current_model(),
                      prompt_tokens=pt, completion_tokens=ct, ok=ok))
        db.commit()
    except Exception:
        pass
    finally:
        db.close()


def usage_summary() -> dict:
    from .db import SessionLocal
    from .models import LLMLog
    db = SessionLocal()
    try:
        rows = db.query(LLMLog).all()
        return {"calls": len(rows),
                "prompt_tokens": sum(r.prompt_tokens for r in rows),
                "completion_tokens": sum(r.completion_tokens for r in rows),
                "failed": sum(1 for r in rows if not r.ok),
                "model": current_model() if llm_available() else "",
                "source": cfg_source()}
    finally:
        db.close()


# ---------- 启发式（离线降级 & LLM 结果的兜底校验） ----------

API_IN_MSG = re.compile(r"/[a-zA-Z0-9_\-]+(?:/[a-zA-Z0-9_\-{}]+)+")


def guess_api_from_text(text: str) -> str | None:
    """从提交说明/需求描述里猜接口路径，如 /api/refund/create。"""
    m = API_IN_MSG.search(text or "")
    return m.group(0) if m else None


def guess_method(text: str) -> str:
    t = (text or "").lower()
    if any(k in t for k in ("新增", "创建", "添加", "feat", "create", "add", "登录", "提交")):
        return "POST"
    if any(k in t for k in ("删除", "del", "remove")):
        return "DELETE"
    if any(k in t for k in ("更新", "修改", "update", "fix", "edit")):
        return "PUT"
    return "GET"


def draft_from_commit(msg: str) -> dict:
    """从一条提交说明生成用例草稿（启发式）。"""
    api = guess_api_from_text(msg)
    name = re.sub(r"^(feat|fix|chore|refactor|test|docs)(\([^)]*\))?!?:\s*", "", msg).strip() or "新接口测试"
    if api:
        return {"name": f"{name}", "reason": f"提交提到接口 {api}，平台上没有覆盖它的用例",
                "steps": [
                    {"m": guess_method(msg), "url": api,
                     "headers": "", "body": "",
                     "check": {"type": "status", "expect": "200", "field": ""},
                     "save": {"name": "", "from": ""}},
                    {"m": "GET", "url": api, "headers": "", "body": "",
                     "check": {"type": "not_empty", "expect": "", "field": ""},
                     "save": {"name": "", "from": ""}}]}
    return {"name": name, "reason": "提交未提及明确接口，建议人工确认测试范围", "steps": []}


def draft_from_prompt(prompt: str) -> dict:
    api = guess_api_from_text(prompt) or "/api/"
    return {"name": prompt[:30], "reason": f"根据描述定位到接口 {api}",
            "steps": [{"m": guess_method(prompt), "url": api, "headers": "", "body": "",
                       "check": {"type": "status", "expect": "200", "field": ""},
                       "save": {"name": "", "from": ""}}]}


def explain_failure(step: dict) -> dict:
    """失败步骤的启发式归因。"""
    reason = step.get("reason", "")
    if "请求失败" in reason:
        return {"cause": "网络/服务不可达", "suggestion": "先确认被测环境服务是否在线、Base URL 是否正确，可在环境页检查。"}
    if "状态码应为" in reason:
        return {"cause": "接口返回异常状态码", "suggestion": "可能是服务内部错误或权限不足，建议把响应内容转给开发查看。"}
    if "应为" in reason and "实际" in reason:
        return {"cause": "返回值与预期不符", "suggestion": "检查测试数据是否被其他操作改变，或预期值是否需要更新。"}
    if "包含" in reason:
        return {"cause": "返回内容缺少预期关键字", "suggestion": "确认接口行为是否变更；若属正常变更，请更新检查点的期望值。"}
    return {"cause": "未知", "suggestion": "建议查看完整响应，或转给开发排查。"}
