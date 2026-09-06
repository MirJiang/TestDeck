"""安全回归：XSS 转义、IDOR 拦截、登录限速、改密吊销、执行取消。"""
import os  # noqa: F401

from app import config
config.set("TD_DB", ":memory:")

from fastapi.testclient import TestClient
from app.main import app  # noqa


def _client():
    cm = TestClient(app)
    cm.__enter__()
    return cm


def _login(client, u="admin", p="admin123"):
    r = client.post("/api/v1/auth/login", json={"username": u, "password": p})
    return {"Authorization": "Bearer " + r.json()["token"]}


def _mk_project(client, H, name):
    return client.post("/api/v1/projects", json={"name": name}, headers=H).json()["id"]


def test_export_html_escapes_xss():
    """报告导出对被测系统返回的内容做 HTML 转义。"""
    import json
    client = _client()
    H = _login(client)
    pid = _mk_project(client, H, "XSS")
    eid = client.post(f"/api/v1/projects/{pid}/envs", headers=H,
                      json={"name": "E", "base_url": "http://127.0.0.1:59997"}).json()["id"]
    evil = '<img src=x onerror=alert(1)>'
    cid = client.post(f"/api/v1/projects/{pid}/cases", headers=H, json={
        "project_id": pid, "name": "x", "type": "api",
        "steps": [{"m": "GET", "url": "http://127.0.0.1:59997/x",
                   "check": {"type": "status", "expect": "200"}}]}).json()["id"]
    # 直接构造一条含恶意 reason 的执行记录（模拟被测系统返回注入内容）
    from app.db import SessionLocal
    from app.models import TestRun
    db = SessionLocal()
    run = TestRun(id="R-EVIL", case_id=cid, case_name=evil, env_name="E",
                  detail=[{"idx": 1, "m": "GET", "url": evil, "pass": False, "reason": evil, "ms": 1}])
    db.add(run); db.commit(); db.close()

    r = client.get("/api/v1/runs/R-EVIL/export", headers=H)
    assert r.status_code == 200
    assert "<img src=x" not in r.text          # 原样标签被转义
    assert "&lt;img src=x" in r.text           # 转义后的实体存在
    client.__exit__(None, None, None)


def test_idor_blocked():
    """member 访问他人项目的用例/执行记录/git 仓库均被拒。"""
    client = _client()
    H = _login(client)
    client.post("/api/v1/auth/users", headers=H, json={"username": "zw", "password": "zw12345"})
    H2 = _login(client, "zw", "zw12345")

    pid = _mk_project(client, H, "管理员私有")
    cid = client.post(f"/api/v1/projects/{pid}/cases", headers=H, json={
        "project_id": pid, "name": "c", "type": "ai", "target": "api", "goal": "g"}).json()["id"]
    rid = client.post(f"/api/v1/projects/{pid}/envs", headers=H,
                      json={"name": "E", "base_url": "http://t"}).json()["id"]
    gid = client.post("/api/v1/integrations/git/repos", headers=H, json={
        "project_id": pid, "repo_url": "http://git/x"}).json()["id"]

    assert client.get(f"/api/v1/cases/{cid}", headers=H2).status_code == 403
    assert client.put(f"/api/v1/cases/{cid}", headers=H2, json={
        "project_id": pid, "name": "hack", "type": "ai"}).status_code == 403
    assert client.post(f"/api/v1/projects/{pid}/envs", headers=H2,
                       json={"name": "x"}).status_code == 403
    assert client.put(f"/api/v1/projects/{pid}/envs/{rid}", headers=H2,
                      json={"name": "x"}).status_code == 403
    assert client.post(f"/api/v1/runs/cases/{cid}/run", headers=H2,
                       json={"env_id": rid}).status_code == 403
    assert client.get("/api/v1/integrations/git/repos", headers=H2).json() == []   # 列表只可见自己项目
    assert client.delete(f"/api/v1/integrations/git/repos/{gid}", headers=H2).status_code == 403

    # admin 不受影响
    assert client.get(f"/api/v1/cases/{cid}", headers=H).status_code == 200
    client.__exit__(None, None, None)


def test_login_throttle():
    client = _client()
    for _ in range(5):
        client.post("/api/v1/auth/login", json={"username": "nobody", "password": "wrong"})
    r = client.post("/api/v1/auth/login", json={"username": "nobody", "password": "anypass"})
    assert r.status_code == 429                    # 锁定期间即使密码正确也拒绝
    client.__exit__(None, None, None)


def test_password_change_revokes_token():
    client = _client()
    H_admin = _login(client)
    client.post("/api/v1/auth/users", headers=H_admin,
                json={"username": "tmp1", "password": "tmp12345"})
    H_tmp = _login(client, "tmp1", "tmp12345")
    assert client.get("/api/v1/auth/me", headers=H_tmp).status_code == 200

    # 改密后旧 token 失效
    client.put("/api/v1/auth/password", headers=H_tmp,
               json={"old_password": "tmp12345", "new_password": "new12345"})
    assert client.get("/api/v1/auth/me", headers=H_tmp).status_code == 401
    # 新密码可登录
    H_new = _login(client, "tmp1", "new12345")
    assert client.get("/api/v1/auth/me", headers=H_new).status_code == 200
    client.__exit__(None, None, None)


def test_cancel_flag():
    from app.engine import queue
    queue.register("R-X")
    assert queue.is_cancelled("R-X") is False
    queue.cancel("R-X")
    assert queue.is_cancelled("R-X") is True
    queue.unregister("R-X")
    assert queue.is_cancelled("R-X") is False
