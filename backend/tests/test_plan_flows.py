"""计划包含用例+流程：批量执行 + 流程执行记录统一并入 test_runs。"""
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


DEAD = "http://127.0.0.1:59998"   # 无人监听的端口，连接立刻被拒


def _setup(client, H):
    pid = client.post("/api/v1/projects", json={"name": "P"}, headers=H).json()["id"]
    eid = client.post(f"/api/v1/projects/{pid}/envs",
                      json={"name": "E", "base_url": DEAD}, headers=H).json()["id"]
    cid = client.post(f"/api/v1/projects/{pid}/cases",
                      json={"project_id": pid, "name": "用例A", "type": "api",
                            "steps": [{"m": "GET", "url": "/x", "check": {"type": "status", "expect": "200"}}]},
                      headers=H).json()["id"]
    fid = client.post("/api/v1/flows",
                      json={"project_id": pid, "name": "流程B",
                            "roles": [{"key": "u", "name": "用户", "variables": {}}],
                            "steps": [{"role": "u", "type": "api", "m": "GET", "url": "/y",
                                       "check": {"type": "status", "expect": "200"}}]},
                      headers=H).json()["id"]
    return pid, eid, cid, fid


def _wait_run(client, H, rid, timeout=20):
    """触发接口立即返回 running，轮询直到结束。"""
    import time
    t0 = time.time()
    while time.time() - t0 < timeout:
        d = client.get(f"/api/v1/runs/{rid}", headers=H).json()
        if d.get("status") and d["status"] != "running":
            return d
        time.sleep(0.2)
    raise AssertionError("执行轮询超时")


def test_plan_runs_cases_and_flows():
    client = make_client()
    H = {"Authorization": "Bearer " + login(client, "admin", "admin123")}
    pid, eid, cid, fid = _setup(client, H)

    plan = client.post("/api/v1/plans",
                       json={"project_id": pid, "name": "回归", "case_ids": [cid], "flow_ids": [fid],
                             "env_id": eid}, headers=H).json()
    pl = client.get("/api/v1/plans", headers=H).json()[0]
    assert pl["flow_ids"] == [fid], "计划列表应返回 flow_ids"

    run = client.post(f"/api/v1/runs/plans/{plan['id']}/run", json={"env_id": eid}, headers=H).json()
    run = _wait_run(client, H, run["id"])
    assert run["plan_name"] == "回归"
    assert run["fail_n"] == 2, "用例 1 个检查点 + 流程 1 个步骤都失败"
    kinds = {("case" if "case_id" in i else "flow"): i for i in run["detail"]}
    assert set(kinds) == {"case", "flow"} and all(not i["pass"] for i in kinds.values())

    # 流程条目对应一条独立执行记录（不带 plan_id，避免干扰计划轮询）
    flow_item = kinds["flow"]
    sub = client.get(f"/api/v1/runs/{flow_item['run_id']}", headers=H).json()
    assert sub["flow_id"] == fid and sub["flow_name"] == "流程B" and sub["fail_n"] == 1

    # 流程历史里能看到这次计划触发的执行
    hist = client.get(f"/api/v1/flows/{fid}/runs", headers=H).json()
    assert any(r["id"] == flow_item["run_id"] for r in hist)
    det = client.get(f"/api/v1/flows/runs/{flow_item['run_id']}/detail", headers=H).json()
    assert det["detail"] and det["detail"][0]["pass"] is False


def test_flow_run_in_records_and_exports():
    client = make_client()
    H = {"Authorization": "Bearer " + login(client, "admin", "admin123")}
    pid, eid, cid, fid = _setup(client, H)

    run = client.post(f"/api/v1/flows/{fid}/run", json={"env_id": eid}, headers=H).json()
    assert run["status"] == "failed" and run["flow_name"] == "流程B"

    # 统一执行记录里能查到流程执行
    items = client.get("/api/v1/runs?size=50", headers=H).json()["items"]
    assert any(r["id"] == run["id"] and r["flow_id"] == fid for r in items)

    csv = client.get("/api/v1/runs/export.csv", headers=H).text
    lines = csv.splitlines()
    assert "类型" in lines[0]
    assert any(run["id"] in line and "流程" in line for line in lines), "CSV 应包含流程执行记录"

    html = client.get(f"/api/v1/runs/{run['id']}/export", headers=H).text
    assert "流程B" in html and "GET" in html, "HTML 报告应包含流程名与步骤"
