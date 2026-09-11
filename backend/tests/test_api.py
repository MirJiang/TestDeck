import pytest
from fastapi.testclient import TestClient

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


def test_delete_project_cascades(client, token):
    """删除项目级联清理全部从属数据：用例/流程/计划/调度/执行记录/地图/接口库/Git 绑定/账号池/成员/环境，及执行产物文件。"""
    pid = client.post("/api/v1/projects", json={"name": "P-级联", "desc": ""}, headers=H(token)).json()["id"]
    client.post(f"/api/v1/projects/{pid}/envs", json={"name": "E", "base_url": "", "variables": {}}, headers=H(token))
    cid = client.post(f"/api/v1/projects/{pid}/cases",
                      json={"project_id": pid, "name": "c1", "type": "api", "steps": []}, headers=H(token)).json()["id"]
    fid = client.post("/api/v1/flows", json={"project_id": pid, "name": "f1"}, headers=H(token)).json()["id"]
    plan_id = client.post("/api/v1/plans", json={"project_id": pid, "name": "plan1", "case_ids": [],
                                                 "trigger": "cron", "cron": "0 8 * * *"}, headers=H(token)).json()["id"]
    client.post(f"/api/v1/projects/{pid}/users", json={"name": "u", "username": "u1", "password": "p"}, headers=H(token))

    from app.db import SessionLocal
    from app.models import (TestRun, GitRepo, CommitSync, AppPage, AppElement,
                            ApiDoc, ApiEndpoint, Schedule, ProjectMember)
    from app.engine.ui_runner import STATIC_DIR
    db = SessionLocal()
    db.add(ProjectMember(project_id=pid, user_id="someone"))
    repo = GitRepo(project_id=pid, repo_url="https://git.example/x.git")
    db.add(repo); db.commit()
    db.add(CommitSync(repo_id=repo.id, sha="a" * 40))
    db.add(TestRun(id="run-cascade", case_id=cid, status="failed"))
    page = AppPage(project_id=pid, path="/home")
    db.add(page); db.commit()
    db.add(AppElement(page_id=page.id, kind="button", text="提交"))
    doc = ApiDoc(project_id=pid, name="doc")
    db.add(doc); db.commit()
    db.add(ApiEndpoint(project_id=pid, doc_id=doc.id, method="GET", path="/x"))
    db.commit()
    page_id, repo_id = page.id, repo.id   # 删除后对象行不在了，先存普通值
    assert db.query(Schedule).filter_by(plan_id=plan_id).count() == 1
    shot = STATIC_DIR / "run-cascade-1.png"; shot.write_bytes(b"x")
    base = STATIC_DIR / f"base-{fid}-1.png"; base.write_bytes(b"x")

    assert client.delete(f"/api/v1/projects/{pid}", headers=H(token)).status_code == 200
    db.expire_all()
    from app.models import Project, TestCase, Flow, TestPlan, ProjectUser, Env
    assert db.query(Project).filter_by(id=pid).count() == 0
    for m, cond in [(Env, dict(project_id=pid)), (TestCase, dict(project_id=pid)),
                    (Flow, dict(project_id=pid)), (TestPlan, dict(project_id=pid)),
                    (Schedule, dict(plan_id=plan_id)), (TestRun, dict(id="run-cascade")),
                    (ProjectUser, dict(project_id=pid)), (ProjectMember, dict(project_id=pid)),
                    (AppPage, dict(project_id=pid)), (AppElement, dict(page_id=page_id)),
                    (ApiDoc, dict(project_id=pid)), (ApiEndpoint, dict(project_id=pid)),
                    (GitRepo, dict(project_id=pid)), (CommitSync, dict(repo_id=repo_id))]:
        assert db.query(m).filter_by(**cond).count() == 0, m.__tablename__
    assert not shot.exists() and not base.exists()
    db.close()
