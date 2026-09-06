"""API 文档解析：Swagger 2.0 / OpenAPI 3.x / Postman Collection v2 → 统一端点列表。

Apifox、ShowDoc 等工具导出的 OpenAPI 格式同样适用；$ref 引用自动展开为字段清单。
"""
import json

try:
    import yaml
except ImportError:  # 仅解析 YAML 文档时需要
    yaml = None

METHODS = ("get", "post", "put", "delete", "patch", "head", "options")


def _load(text: str):
    text = (text or "").strip()
    if not text:
        raise ValueError("文档内容为空")
    if text.startswith("{") or text.startswith("["):
        try:
            return json.loads(text)
        except Exception as e:
            raise ValueError(f"JSON 解析失败：{e}")
    if yaml is None:
        raise ValueError("YAML 文档需要先安装 pyyaml（pip install pyyaml）")
    try:
        return yaml.safe_load(text)
    except Exception as e:
        raise ValueError(f"YAML 解析失败：{e}")


def _deref(ref: str, defs: dict) -> dict:
    return defs.get(ref.split("/")[-1], {}) if isinstance(ref, str) else {}


def _schema_fields(schema, defs: dict, depth: int = 0) -> dict:
    """把 schema 压成 {字段: 类型}；对象/数组下钻（最多 2 层），$ref 自动展开。"""
    if not isinstance(schema, dict) or depth > 2:
        return {}
    if "$ref" in schema:
        return _schema_fields(_deref(schema["$ref"], defs), defs, depth + 1)
    out = {}
    for name, p in (schema.get("properties") or {}).items():
        if not isinstance(p, dict):
            out[str(name)] = "string"
            continue
        sub = {}
        if p.get("type") == "array":
            sub = _schema_fields(p.get("items") or {}, defs, depth + 1)
            if sub:
                out.update({f"{name}.{k}": v for k, v in sub.items()})
                continue
            out[str(name)] = "array"
            continue
        if p.get("type") == "object" or "properties" in p or "$ref" in p:
            sub = _schema_fields(p, defs, depth + 1)
            if sub:
                out.update({f"{name}.{k}": v for k, v in sub.items()})
                continue
            out[str(name)] = "object"
            continue
        out[str(name)] = p.get("type") or "string"
    return out


def _params_of(op_params, extra_params, body_schema, defs) -> dict:
    params = {"query": [], "header": [], "path": [], "body": {}}
    for prm in list(op_params or []) + list(extra_params or []):
        if not isinstance(prm, dict):
            continue
        loc = prm.get("in")
        if loc in ("query", "header", "path"):
            t = prm.get("type") or ((prm.get("schema") or {}).get("type")) or "string"
            params[loc].append({"name": prm.get("name", ""), "type": t})
        elif loc == "body" and isinstance(prm.get("schema"), dict):
            params["body"] = _schema_fields(prm["schema"], defs)
    if body_schema:
        params["body"] = _schema_fields(body_schema, defs)
    return params


def _parse_oas3(obj: dict) -> dict:
    info = obj.get("info") or {}
    defs = (obj.get("components") or {}).get("schemas") or {}
    servers = obj.get("servers") or []
    base_url = (servers[0].get("url") if servers and isinstance(servers[0], dict) else "") or ""
    eps = []
    for path, item in (obj.get("paths") or {}).items():
        if not isinstance(item, dict):
            continue
        common = item.get("parameters") or []
        for m in METHODS:
            op = item.get(m)
            if not isinstance(op, dict):
                continue
            rb = (((op.get("requestBody") or {}).get("content") or {}).get("application/json") or {})
            eps.append({"method": m.upper(), "path": path,
                        "summary": str(op.get("summary") or op.get("description") or "")[:80],
                        "params": _params_of(op.get("parameters"), common, rb.get("schema"), defs)})
    return {"format": "openapi3", "title": str(info.get("title") or ""),
            "base_url": str(base_url), "endpoints": eps}


def _parse_swagger2(obj: dict) -> dict:
    info = obj.get("info") or {}
    defs = obj.get("definitions") or {}
    host, base_path = obj.get("host") or "", obj.get("basePath") or ""
    scheme = (obj.get("schemes") or ["https"])[0] if obj.get("schemes") else "https"
    base_url = f"{scheme}://{host}{base_path}" if host else str(base_path)
    eps = []
    for path, item in (obj.get("paths") or {}).items():
        if not isinstance(item, dict):
            continue
        common = item.get("parameters") or []
        for m in METHODS:
            op = item.get(m)
            if not isinstance(op, dict):
                continue
            body_schema = next((p.get("schema") for p in (op.get("parameters") or []) + common
                                if isinstance(p, dict) and p.get("in") == "body"), None)
            eps.append({"method": m.upper(), "path": path,
                        "summary": str(op.get("summary") or op.get("description") or "")[:80],
                        "params": _params_of(op.get("parameters"), common, body_schema, defs)})
    return {"format": "swagger2", "title": str(info.get("title") or ""),
            "base_url": str(base_url), "endpoints": eps}


def _parse_postman(obj: dict) -> dict:
    info = obj.get("info") or {}

    def walk(items, eps):
        for it in items or []:
            if it.get("item"):
                walk(it.get("item"), eps)
                continue
            req = it.get("request")
            if not isinstance(req, dict):
                continue
            url = req.get("url")
            raw = str((url.get("raw") if isinstance(url, dict) else url) or "")
            path = raw
            if "://" in raw:
                rest = raw.split("://", 1)[1]
                path = "/" + rest.split("/", 1)[1] if "/" in rest else "/"
            query = url.get("query") if isinstance(url, dict) else None
            if not query and "?" in path:  # raw 携带查询串而 query 数组缺失时，从路径解析
                query = [{"key": kv.split("=", 1)[0]} for kv in path.split("?", 1)[1].split("&") if kv]
            path = path.split("?", 1)[0]
            params = {"query": [{"name": q.get("key", ""), "type": "string"} for q in (query or [])][:15],
                      "header": [], "path": [], "body": {}}
            body = req.get("body") or {}
            raw_body = str(body.get("raw") or "")
            if raw_body.strip().startswith("{"):
                try:
                    params["body"] = {k: type(v).__name__ for k, v in json.loads(raw_body).items()}
                except Exception:
                    pass
            elif body.get("urlencoded"):
                params["body"] = {p.get("key", ""): "string" for p in body.get("urlencoded") or []}
            eps.append({"method": (req.get("method") or "GET").upper(), "path": path,
                        "summary": str(it.get("name") or "")[:80], "params": params})

    eps = []
    walk(obj.get("item"), eps)

    def first_raw(items):
        for it in items or []:
            if it.get("item"):
                r = first_raw(it.get("item"))
                if r:
                    return r
            req = it.get("request")
            if isinstance(req, dict):
                u = req.get("url")
                return str((u.get("raw") if isinstance(u, dict) else u) or "")
        return None

    from urllib.parse import urlparse
    fr = first_raw(obj.get("item")) or ""
    base_url = ""
    if "://" in fr:
        u = urlparse(fr)
        base_url = f"{u.scheme}://{u.netloc}"
    return {"format": "postman", "title": str(info.get("name") or ""),
            "base_url": base_url, "endpoints": eps}


def parse_spec(text: str) -> dict:
    """识别格式并解析。返回 {format, title, base_url, endpoints:[{method,path,summary,params}]}。"""
    obj = _load(text)
    if not isinstance(obj, dict):
        raise ValueError("无法识别的文档结构")
    if obj.get("openapi"):
        return _parse_oas3(obj)
    if obj.get("swagger"):
        return _parse_swagger2(obj)
    if isinstance(obj.get("item"), list) or (obj.get("info") or {}).get("_postman_id"):
        return _parse_postman(obj)
    raise ValueError("无法识别的格式：支持 Swagger 2.0 / OpenAPI 3.x / Postman Collection"
                     "（Apifox 请导出 OpenAPI 格式）")


def format_endpoints(eps: list, limit: int = 30) -> str:
    """把端点列表压成给大模型看的紧凑清单。"""
    lines = []
    for e in eps[:limit]:
        p = e.get("params") or {}
        bits = []
        for k in ("query", "header", "path"):
            if p.get(k):
                bits.append(f"{k}: " + ", ".join(f"{x.get('name')}({x.get('type', 'string')})" for x in p[k][:8]))
        if p.get("body"):
            bits.append("body: " + ", ".join(f"{n}({t})" for n, t in list(p["body"].items())[:12]))
        line = f"- {e.get('method', 'GET')} {e.get('path', '')}"
        if e.get("summary"):
            line += f" — {e['summary']}"
        if bits:
            line += f"（{'; '.join(bits)}）"
        lines.append(line)
    return "\n".join(lines)
