"""API 文档解析器：Swagger2 / OpenAPI3 / Postman + $ref 展开 + 导入接口。"""
import os  # noqa: F401

from app import config
config.set("TD_DB", ":memory:")

from fastapi.testclient import TestClient

from app.engine.api_docs import parse_spec, format_endpoints

OAS3 = {
    "openapi": "3.0.1",
    "info": {"title": "询价服务"},
    "servers": [{"url": "https://api.example.com/v1"}],
    "components": {"schemas": {"InquiryCreate": {"type": "object", "properties": {
        "from": {"type": "string"}, "to": {"type": "string"},
        "cargo": {"type": "object", "properties": {"weight": {"type": "number"}}}}}}},
    "paths": {"/api/inquiry/create": {"post": {
        "summary": "创建询价单",
        "requestBody": {"content": {"application/json": {"schema": {
            "$ref": "#/components/schemas/InquiryCreate"}}}},
        "responses": {"200": {"description": "ok"}}},
        "get": {"summary": "查询列表", "parameters": [
            {"name": "page", "in": "query", "schema": {"type": "integer"}},
            {"name": "X-Token", "in": "header", "schema": {"type": "string"}}]}}},
}

SWAGGER2 = {
    "swagger": "2.0", "info": {"title": "老服务"},
    "host": "old.example.com", "basePath": "/api",
    "paths": {"/user/login": {"post": {"summary": "登录",
        "parameters": [{"name": "body", "in": "body", "schema": {"type": "object",
            "properties": {"username": {"type": "string"}, "password": {"type": "string"}}}}]}}},
}

POSTMAN = {"info": {"name": "冒烟集"},
    "item": [{"name": "登录", "request": {"method": "POST",
        "url": {"raw": "https://api.example.com/api/login?debug=1"},
        "body": {"mode": "raw", "raw": '{"username":"a","password":"b"}'}}}]}


def test_oas3_parse_and_ref():
    r = parse_spec(__import__("json").dumps(OAS3))
    assert r["format"] == "openapi3" and r["title"] == "询价服务"
    assert r["base_url"] == "https://api.example.com/v1"
    by = {(e["method"], e["path"]): e for e in r["endpoints"]}
    post = by[("POST", "/api/inquiry/create")]
    assert post["summary"] == "创建询价单"
    assert post["params"]["body"]["from"] == "string"
    assert post["params"]["body"]["cargo.weight"] == "number"      # $ref + 嵌套展开
    get = by[("GET", "/api/inquiry/create")]
    assert get["params"]["query"][0]["name"] == "page"
    assert get["params"]["header"][0]["name"] == "X-Token"


def test_swagger2_parse():
    import json as j
    r = parse_spec(j.dumps(SWAGGER2))
    assert r["format"] == "swagger2" and r["base_url"] == "https://old.example.com/api"
    ep = r["endpoints"][0]
    assert ep["params"]["body"]["username"] == "string"


def test_postman_parse():
    import json as j
    r = parse_spec(j.dumps(POSTMAN))
    assert r["format"] == "postman" and r["base_url"] == "https://api.example.com"
    ep = r["endpoints"][0]
    assert ep["path"] == "/api/login" and ep["params"]["query"][0]["name"] == "debug"
    assert ep["params"]["body"]["username"] == "str"


def test_yaml_and_unknown():
    r = parse_spec("openapi: 3.0.1\ninfo:\n  title: Y\npaths: {}\n")
    assert r["format"] == "openapi3"
    try:
        parse_spec("hello world")
        assert False
    except ValueError as e:
        assert "无法识别" in str(e)


def test_format_endpoints_compact():
    s = format_endpoints([{"method": "POST", "path": "/api/a", "summary": "创建",
                           "params": {"query": [{"name": "page", "type": "integer"}],
                                      "body": {"from": "string"}}}])
    assert "POST /api/a — 创建" in s and "page(integer)" in s and "from(string)" in s


def test_import_from_url(monkeypatch):
    import json as j
    from app.routers import api_docs as R

    def fake_get(url, **kw):
        assert url.endswith("/v3/api-docs")
        class Resp:
            status_code = 200
            text = j.dumps(OAS3)
        return Resp()
    monkeypatch.setattr(R.httpx, "get", fake_get)

    cm = TestClient(__import__("app.main", fromlist=["app"]).app)
    cm.__enter__()
    tok = cm.post("/api/v1/auth/login", json={"username": "admin", "password": "admin123"}).json()["token"]
    H = {"Authorization": f"Bearer {tok}"}
    pid = cm.post("/api/v1/projects", json={"name": "URL 导入项目"}, headers=H).json()["id"]

    r = cm.post(f"/api/v1/projects/{pid}/api-docs/import", headers=H,
                json={"url": "http://demo/v3/api-docs", "name": "从URL导入"}).json()
    assert r["count"] == 2 and r["name"] == "从URL导入"

    # 坏 URL 给出可读错误
    r2 = cm.post(f"/api/v1/projects/{pid}/api-docs/import", headers=H,
                 json={"url": "ftp://bad"}).json()
    assert "http(s)://" in r2.get("detail", "")

    cm.delete(f"/api/v1/projects/{pid}/api-docs/{r['id']}", headers=H)
    cm.delete(f"/api/v1/projects/{pid}", headers=H)
    cm.__exit__(None, None, None)


def test_import_flow():
    import json as j
    cm = TestClient(__import__("app.main", fromlist=["app"]).app)
    cm.__enter__()
    tok = cm.post("/api/v1/auth/login", json={"username": "admin", "password": "admin123"}).json()["token"]
    H = {"Authorization": f"Bearer {tok}"}
    pid = cm.post("/api/v1/projects", json={"name": "文档项目"}, headers=H).json()["id"]

    r = cm.post(f"/api/v1/projects/{pid}/api-docs/import", headers=H,
                json={"name": "询价", "content": j.dumps(OAS3)}).json()
    assert r["count"] == 2 and r["format"] == "openapi3"

    docs = cm.get(f"/api/v1/projects/{pid}/api-docs", headers=H).json()
    assert len(docs) == 1 and docs[0]["count"] == 2

    eps = cm.get(f"/api/v1/projects/{pid}/api-docs/{r['id']}/endpoints", headers=H).json()
    assert {e["method"] for e in eps} == {"GET", "POST"}

    assert cm.delete(f"/api/v1/projects/{pid}/api-docs/{r['id']}", headers=H).json()["ok"]
    assert cm.get(f"/api/v1/projects/{pid}/api-docs", headers=H).json() == []
    cm.__exit__(None, None, None)
