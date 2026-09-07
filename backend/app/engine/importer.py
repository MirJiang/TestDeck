"""存量测试资产导入：HAR（浏览器导出的请求日志）/ Postman Collection v2.x → 平台 API 用例。

只迁移请求定义（方法 / URL / 头 / 体）并配基础状态码检查；
Postman 的测试脚本（event.test）无法语义化转换，导入后可在用例里自行补检查点。
"""
import json
from urllib.parse import urlparse

# 静态资源后缀与资源类型：HAR 里这些请求对接口测试无意义，直接过滤
_STATIC_EXT = (".js", ".css", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico",
               ".woff", ".woff2", ".ttf", ".otf", ".map", ".mp4", ".webp", ".webm")
_SKIP_RES_TYPES = {"image", "stylesheet", "script", "font", "media"}
# 浏览器自动附加的头：重放时由 httpx 自行处理，留着反而干扰（cookie 除外场景由用户补）
_SKIP_HEADERS = {"cookie", "accept-encoding", "content-length", "host", "user-agent",
                 "connection", "referer", "origin", "sec-fetch-mode", "sec-fetch-site",
                 "sec-fetch-dest", "sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform",
                 "accept-language", "priority"}


def _rel(url: str, base: str) -> str:
    """与项目环境地址同源的 URL 转相对路径，执行时由环境补回域名。"""
    if base and url.startswith(base):
        return url[len(base):] or "/"
    return url


def _headers_text(hs) -> str:
    if isinstance(hs, dict):
        pairs = hs.items()
    else:
        pairs = ((h.get("name", ""), h.get("value", "")) for h in hs or [])
    return "\n".join(f"{k}: {v}" for k, v in pairs
                     if k and k.lower() not in _SKIP_HEADERS)


def _step(method: str, url: str, headers: str, body: str, base: str) -> dict:
    return {"m": method, "url": _rel(url, base), "headers": headers,
            "body": body or "", "check": {"type": "status", "expect": "200"},
            "save": {}, "continue_on_fail": False}


def parse_har(text: str, base: str = "") -> dict:
    """HAR → 一个 API 用例（多步骤，按发生顺序）。返回 {name, steps, skipped}。"""
    data = json.loads(text)
    entries = (data.get("log") or {}).get("entries") or []
    steps, skipped = [], 0
    for en in entries:
        req = en.get("request") or {}
        url, method = req.get("url") or "", (req.get("method") or "GET").upper()
        if not url.startswith(("http://", "https://")):
            continue
        rt = (en.get("_resourceType") or "").lower()
        if urlparse(url).path.lower().endswith(_STATIC_EXT) or rt in _SKIP_RES_TYPES:
            skipped += 1
            continue
        body = ((req.get("postData") or {}).get("text") or "")
        steps.append(_step(method, url, _headers_text(req.get("headers")), body, base))
    if not steps:
        raise ValueError("HAR 里没有可用的接口请求（可能全是静态资源）")
    return {"steps": steps, "skipped": skipped}


def parse_postman(text: str, base: str = "") -> list:
    """Postman Collection v2.x → 每个请求一个 API 用例（文件夹层级拼进名称）。返回 [{name, steps}]。"""
    data = json.loads(text)
    schema = (data.get("info") or {}).get("schema") or ""
    if "schema.getpostman.com" not in schema:
        raise ValueError("不是 Postman Collection 文件（缺少 info.schema）")
    out = []

    def walk(items, prefix):
        for it in items or []:
            if "item" in it:                     # 文件夹
                walk(it["item"], f"{prefix}{it.get('name') or ''} · ")
                continue
            req = it.get("request") or {}
            url = req.get("url")
            if isinstance(url, dict):
                url = url.get("raw") or (url.get("protocol") or "https") + "://" + "".join(
                    h.get("raw", "") + ("." if i < len(url.get("host") or []) - 1 else "")
                    for i, h in enumerate(url.get("host") or [])) + (url.get("path") and "/" + "/".join(url["path"]) or "")
            url, method = str(url or ""), (req.get("method") or "GET").upper()
            if not url.startswith(("http://", "https://")):
                continue                          # 变量占位的 URL（{{base}}）无法直接执行，跳过
            body = ""
            bd = req.get("body") or {}
            if bd.get("mode") == "raw":
                body = bd.get("raw") or ""
            out.append({"name": f"{prefix}{it.get('name') or method + ' ' + url}".strip(),
                        "steps": [_step(method, url, _headers_text(req.get("header")), body, base)]})

    walk(data.get("item"), "")
    if not out:
        raise ValueError("Collection 里没有可直接执行的请求（{{变量}} 地址需先在 Postman 里解析）")
    return out
