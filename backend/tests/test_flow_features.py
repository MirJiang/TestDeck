"""二期功能：流程引用用例（变量接线）、JSONPath 高级断言、截图基线对比。"""
import time
import threading

from app import config
config.set("TD_DB", ":memory:")

from fastapi.testclient import TestClient
from app.main import app  # noqa


def login(client, u, p):
    r = client.post("/api/v1/auth/login", json={"username": u, "password": p})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def make_client():
    cm = TestClient(app)
    cm.__enter__()
    return cm


# ---------- JSONPath 高级断言 ----------

def test_jsonpath_check():
    from app.engine.runner import evaluate_check
    body = {"data": {"list": [{"id": 1}, {"id": 2}]}}
    ok = evaluate_check({"type": "jsonpath", "field": "$.data.list[*].id", "expect": "2"},
                        200, "{}", body)
    assert ok["pass"], ok["reason"]
    miss = evaluate_check({"type": "jsonpath", "field": "$.data.list[*].id", "expect": "9"},
                          200, "{}", body)
    assert not miss["pass"]
    empty_expect = evaluate_check({"type": "jsonpath", "field": "$.data.list[*].id"},
                                  200, "{}", body)
    assert empty_expect["pass"]   # 只要求匹配到值
    none = evaluate_check({"type": "jsonpath", "field": "$.nope"}, 200, "{}", body)
    assert not none["pass"]


def test_case_api_accepts_jsonpath_check():
    client = make_client()
    H = {"Authorization": "Bearer " + login(client, "admin", "admin123")}
    pid = client.post("/api/v1/projects", json={"name": "P"}, headers=H).json()["id"]
    r = client.post(f"/api/v1/projects/{pid}/cases",
                    json={"project_id": pid, "name": "J", "type": "api",
                          "steps": [{"m": "GET", "url": "/x",
                                     "check": {"type": "jsonpath", "field": "$.code", "expect": "0"}}]},
                    headers=H)
    assert r.status_code == 200, r.text


# ---------- 截图基线对比 ----------

def test_shot_diff_pct(tmp_path):
    from PIL import Image
    from app.engine.flow_runner import _shot_diff_pct
    a = tmp_path / "a.png"
    b = tmp_path / "b.png"
    Image.new("RGB", (50, 50), (200, 200, 200)).save(a)
    Image.new("RGB", (50, 50), (200, 200, 200)).save(b)
    assert _shot_diff_pct(a, b) == 0.0
    Image.new("RGB", (50, 50), (10, 10, 10)).save(b)
    assert _shot_diff_pct(a, b) == 100.0
    Image.new("RGB", (30, 30), (200, 200, 200)).save(b)   # 尺寸不同
    assert _shot_diff_pct(a, b) == 100.0


# ---------- 流程引用用例：角色会话执行 + save 直通共享区 ----------

def _start_mock_server(port: int) -> str:
    import uvicorn
    import httpx
    from tests.mock_target import app as target_app
    server = uvicorn.Server(uvicorn.Config(target_app, host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            httpx.get(base + "/api/user/info", timeout=1)
            return base
        except Exception:
            time.sleep(0.2)
    raise RuntimeError("mock 被测系统启动失败")


def test_flow_case_reference_wiring():
    base = _start_mock_server(9777)
    client = make_client()
    H = {"Authorization": "Bearer " + login(client, "admin", "admin123")}
    pid = client.post("/api/v1/projects", json={"name": "P"}, headers=H).json()["id"]
    eid = client.post(f"/api/v1/projects/{pid}/envs",
                      json={"name": "E", "base_url": base}, headers=H).json()["id"]
    # 被引用用例：创建询价单，save 出 iid
    cid = client.post(f"/api/v1/projects/{pid}/cases",
                      json={"project_id": pid, "name": "创建询价单", "type": "api",
                            "steps": [{"m": "POST", "url": "/api/inquiry/create",
                                       "body": '{"from": "上海", "to": "北京"}',
                                       "check": {"type": "field_eq", "field": "code", "expect": "0"},
                                       "save": {"name": "iid", "from": "data.id"}}]},
                      headers=H).json()["id"]
    # 流程：引用用例（角色会话）→ 用共享变量 iid 打开报价页验证接线
    fid = client.post("/api/v1/flows",
                      json={"project_id": pid, "name": "引用接线",
                            "roles": [{"key": "u", "name": "货主", "variables": {}}],
                            "steps": [
                                {"role": "u", "type": "case", "case_id": cid},
                                {"role": "u", "type": "api", "m": "GET",
                                 "url": "/page/inquiry/quote?id=${iid}&carrier=A",
                                 "check": {"type": "contains", "expect": "报价"}},
                            ]},
                      headers=H).json()["id"]
    run = client.post(f"/api/v1/flows/{fid}/run", json={"env_id": eid}, headers=H).json()
    assert run["status"] == "passed", run.get("detail")
    step1, step2 = run["detail"]
    assert step1["type"] == "case" and step1["pass"]
    assert step1["saved"] == "iid" and step1["actions"][0]["pass"]
    assert "IQ-" in step2["target"], f"共享变量未接线：{step2['target']}"


def test_flow_case_reference_missing_case():
    client = make_client()
    H = {"Authorization": "Bearer " + login(client, "admin", "admin123")}
    pid = client.post("/api/v1/projects", json={"name": "P"}, headers=H).json()["id"]
    eid = client.post(f"/api/v1/projects/{pid}/envs", json={"name": "E"}, headers=H).json()["id"]
    fid = client.post("/api/v1/flows",
                      json={"project_id": pid, "name": "坏引用",
                            "roles": [{"key": "u", "name": "u", "variables": {}}],
                            "steps": [{"role": "u", "type": "case", "case_id": "nope"}]},
                      headers=H).json()["id"]
    run = client.post(f"/api/v1/flows/{fid}/run", json={"env_id": eid}, headers=H).json()
    assert run["status"] == "failed"
    assert "不存在" in run["detail"][0]["reason"]


def test_regression_advice_includes_flows():
    client = make_client()
    H = {"Authorization": "Bearer " + login(client, "admin", "admin123")}
    pid = client.post("/api/v1/projects", json={"name": "P"}, headers=H).json()["id"]
    client.post("/api/v1/flows",
                json={"project_id": pid, "name": "老流程",
                      "roles": [{"key": "u", "name": "u", "variables": {}}], "steps": []},
                headers=H)
    advice = client.get("/api/v1/ai/regression-advice", headers=H).json()
    assert any(f["flow_name"] == "老流程" for f in advice["stale_flows"])
