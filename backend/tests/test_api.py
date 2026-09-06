import pytest
from fastapi.testclient import TestClient
import os

from app import config
config.set("TD_DB", ":memory:")

from app.main import app  # noqa


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def token(client):
    r = client.post("/api/v1/auth/login", json={"username": "admin", "password": "admin123"})
    assert r.status_code == 200
    return r.json()["token"]


def H(token):
    return {"Authorization": f"Bearer {token}"}


def test_health(client):
    assert client.get("/api/v1/health").json()["ok"] is True


def test_login_bad(client):
    assert client.post("/api/v1/auth/login", json={"username": "admin", "password": "x"}).status_code == 401


def test_project_crud(client, token):
    r = client.post("/api/v1/projects", json={"name": "商城中台", "desc": "核心接口"}, headers=H(token))
    assert r.status_code == 200
    pid = r.json()["id"]
    names = [p["name"] for p in client.get("/api/v1/projects", headers=H(token)).json()]
    assert "商城中台" in names
    r = client.put(f"/api/v1/projects/{pid}", json={"name": "商城中台2", "desc": ""}, headers=H(token))
    assert r.json()["ok"]
    client.delete(f"/api/v1/projects/{pid}", headers=H(token))
    assert pid not in [p["id"] for p in client.get("/api/v1/projects", headers=H(token)).json()]


def test_env_and_case(client, token):
    pid = client.post("/api/v1/projects", json={"name": "P1", "desc": ""}, headers=H(token)).json()["id"]
    eid = client.post(f"/api/v1/projects/{pid}/envs",
                      json={"name": "测试", "base_url": "http://x", "variables": {"a": "1"}},
                      headers=H(token)).json()["id"]
    envs = client.get(f"/api/v1/projects/{pid}/envs", headers=H(token)).json()
    assert envs[0]["variables"] == {"a": "1"}

    step = {"m": "GET", "url": "/api/x", "check": {"type": "status", "expect": "200"}}
    cid = client.post(f"/api/v1/projects/{pid}/cases",
                      json={"project_id": pid, "name": "c1", "type": "api", "steps": [step]}, headers=H(token)).json()["id"]
    c = client.get(f"/api/v1/cases/{cid}", headers=H(token)).json()
    assert c["steps"][0]["check"]["type"] == "status"
    # 非法检查点
    bad = client.post(f"/api/v1/projects/{pid}/cases",
                      json={"project_id": pid, "name": "bad", "type": "api", "steps": [
                          {"m": "GET", "url": "/", "check": {"type": "xxx"}}]}, headers=H(token))
    assert bad.status_code == 400
    client.delete(f"/api/v1/cases/{cid}", headers=H(token))
    client.delete(f"/api/v1/projects/{pid}/envs/{eid}", headers=H(token))


def test_plan_and_schedule(client, token):
    pid = client.post("/api/v1/projects", json={"name": "P2", "desc": ""}, headers=H(token)).json()["id"]
    eid = client.post(f"/api/v1/projects/{pid}/envs",
                      json={"name": "E", "base_url": "", "variables": {}}, headers=H(token)).json()["id"]
    r = client.post("/api/v1/plans", json={"project_id": pid, "name": "冒烟",
                                           "case_ids": [], "env_id": eid,
                                           "trigger": "cron", "cron": "0 8 * * *"}, headers=H(token))
    plan_id = r.json()["id"]
    from app.scheduler import _cron_fields
    assert _cron_fields("0 8 * * *")["hour"] == "8"
    client.put(f"/api/v1/plans/{plan_id}", json={"project_id": pid, "name": "冒烟",
                                                 "case_ids": [], "env_id": eid,
                                                 "trigger": "manual", "cron": ""}, headers=H(token))
    plans = client.get("/api/v1/plans", headers=H(token)).json()
    assert any(p["id"] == plan_id and p["trigger"] == "manual" for p in plans)
