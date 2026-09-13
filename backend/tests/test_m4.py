"""M4：项目权限隔离 + 通知渠道 + 报告导出。"""

import time

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


def test_member_isolation():
    client = make_client()
    tok = login(client, "admin", "admin123")
    H = {"Authorization": f"Bearer {tok}"}
    # 建两个用户：管理员建项目，成员建自己的项目
    member = client.post("/api/v1/auth/users", json={"username": "zw", "password": "zw12345"},
                         headers=H).json()
    tok2 = login(client, "zw", "zw12345")
    H2 = {"Authorization": f"Bearer {tok2}"}

    p_admin = client.post("/api/v1/projects", json={"name": "管理员私有"}, headers=H).json()["id"]
    p_member = client.post("/api/v1/projects", json={"name": "成员自己"}, headers=H2).json()["id"]

    names1 = [p["name"] for p in client.get("/api/v1/projects", headers=H2).json()]
    assert "管理员私有" not in names1 and "成员自己" in names1, "成员看不到别人的项目"

    # 成员不能访问别人的项目资源
    assert client.get(f"/api/v1/projects/{p_admin}/cases", headers=H2).status_code == 403
    assert client.get(f"/api/v1/projects/{p_admin}/envs", headers=H2).status_code == 403
    # 管理员全可见
    names2 = [p["name"] for p in client.get("/api/v1/projects", headers=H).json()]
    assert {"管理员私有", "成员自己"} <= set(names2)

    # 计划列表同样隔离
    client.post("/api/v1/plans", json={"project_id": p_admin, "name": "P1"}, headers=H)
    client.post("/api/v1/plans", json={"project_id": p_member, "name": "P2"}, headers=H2)
    plan_names = [p["name"] for p in client.get("/api/v1/plans", headers=H2).json()]
    assert "P1" not in plan_names and "P2" in plan_names


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


def test_notify_and_export():
    client = make_client()
    tok = login(client, "admin", "admin123")
    H = {"Authorization": f"Bearer {tok}"}
    # 通知渠道（指向不存在的地址也不影响主流程）
    ch = client.post("/api/v1/settings/notify",
                     json={"name": "测试群", "url": "http://127.0.0.1:59999/hook"}, headers=H).json()
    assert "id" in ch
    assert len(client.get("/api/v1/settings/notify", headers=H).json()) == 1

    # 失败执行 → 触发通知（不抛错），渠道记录推送状态
    pid = client.post("/api/v1/projects", json={"name": "N"}, headers=H).json()["id"]
    eid = client.post(f"/api/v1/projects/{pid}/envs", json={"name": "E"}, headers=H).json()["id"]
    cid = client.post(f"/projects/{pid}/cases".replace("/projects", "/api/v1/projects"),
                      json={"project_id": pid, "name": "必失败", "type": "api",
                            "steps": [{"m": "GET", "url": "http://127.0.0.1:59998/x",
                                       "check": {"type": "status", "expect": "200"}}]}, headers=H).json()["id"]
    run = client.post(f"/api/v1/runs/cases/{cid}/run", json={"env_id": eid}, headers=H).json()
    run = _wait_run(client, H, run["id"])
    assert run["status"] == "failed"
    last = ""
    for _ in range(20):   # 通知在执行回写后异步发出，短暂轮询
        last = client.get("/api/v1/settings/notify", headers=H).json()[0]["last_status"]
        if last:
            break
        time.sleep(0.25)
    assert last  # 有推送记录（连接失败也会记录）

    # 报告导出
    exp = client.get(f"/api/v1/runs/{run['id']}/export", headers=H)
    assert exp.status_code == 200 and "执行报告" in exp.text and "attachment" in exp.headers["content-disposition"]


def login(client, u, p):
    r = client.post("/api/v1/auth/login", json={"username": u, "password": p})
    assert r.status_code == 200, r.text
    return r.json()["token"]
