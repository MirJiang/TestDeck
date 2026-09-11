import pytest
from fastapi.testclient import TestClient

from app import config
config.set("TD_DB", ":memory:")

from app.main import app  # noqa
from app import ai as A


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def ctx(client):
    t = client.post("/api/v1/auth/login", json={"username": "admin", "password": "admin123"}).json()["token"]
    H = {"Authorization": f"Bearer {t}"}
    pid = client.post("/api/v1/projects", json={"name": "P", "desc": ""}, headers=H).json()["id"]
    eid = client.post(f"/api/v1/projects/{pid}/envs",
                      json={"name": "E", "base_url": "http://127.0.0.1:9001", "variables": {}}, headers=H).json()["id"]
    return H, pid, eid


def test_guess_api():
    assert A.guess_api_from_text("feat: 新增退款接口 /api/refund/create") == "/api/refund/create"
    assert A.guess_api_from_text("chore: 升级依赖") is None
    assert A.guess_method("feat: 新增") == "POST"


def test_explain_failure():
    r = A.explain_failure({"reason": "字段 code 应为 0，实际 4001"})
    assert "返回值" in r["cause"] or "预期" in r["cause"]


def test_git_repo_and_webhook(client, ctx):
    H, pid, eid = ctx
    r = client.post("/api/v1/integrations/git/repos",
                    json={"project_id": pid, "repo_url": "http://git.example.com/shop/mall"}, headers=H).json()
    secret = r["webhook_secret"]

    # 已有用例覆盖 /api/login
    client.post(f"/api/v1/projects/{pid}/cases", json={"project_id": pid, "name": "登录", "type": "api",
                "steps": [{"m": "POST", "url": "/api/login", "check": {"type": "status", "expect": "200"}}]}, headers=H)

    # git 触发计划（无 env 指向 9001 的 plan 不会执行；这里建一个带 env 的）
    plan = client.post("/api/v1/plans", json={"project_id": pid, "name": "合并回归",
                       "case_ids": [], "env_id": eid, "trigger": "git"}, headers=H).json()["id"]

    push = {"ref": "refs/heads/main", "commits": [
        {"id": "a3f9c21", "message": "feat: 新增退款接口 /api/refund/create\n\n详细", "author": {"name": "李娜"},
         "added": ["src/refund.py"], "modified": []},
        {"id": "7b2e08d", "message": "fix: 登录接口 /api/login 密码校验 (#88)", "author": {"name": "王强"}}]}
    r = client.post(f"/api/v1/integrations/git/webhook/{secret}", json=push)
    assert r.status_code == 200
    body = r.json()
    assert body["ingested"] == 2
    assert body["triggered_plans"][0]["run_id"]  # main 分支 push 触发 git 计划

    # 提交已同步
    repos = client.get(f"/api/v1/integrations/git/repos?project_id={pid}", headers=H).json()
    cs = client.get(f"/api/v1/integrations/git/commits?repo_id={repos[0]['id']}", headers=H).json()
    assert any("退款" in c["message"] for c in cs)


def test_ai_gen_from_commits(client, ctx):
    H, pid, eid = ctx
    repos = client.get(f"/api/v1/integrations/git/repos?project_id={pid}", headers=H).json()
    rid = repos[0]["id"]
    r = client.post("/api/v1/ai/gen-from-commits", json={"repo_id": rid, "commit_ids": []}, headers=H)
    assert r.status_code == 200
    body = r.json()
    assert body["engine"] in ("builtin",)  # 测试环境未配 LLM key
    # /api/refund/create 未覆盖 → 应有草稿；/api/login 已覆盖 → 进 covered
    assert any("/api/refund/create" in str(d["steps"]) for d in body["drafts"])
    assert any(c["api"] == "/api/login" for c in body["covered"])


def test_ai_gen_from_text(client, ctx):
    H, pid, eid = ctx
    r = client.post("/api/v1/ai/gen-from-text",
                    json={"project_id": pid, "prompt": "测一下登录接口 /api/auth/login，成功返回 code=0"}, headers=H)
    d = r.json()["draft"]
    assert d["steps"] and d["steps"][0]["url"]


def test_ai_analyze_run(client, ctx):
    H, pid, eid = ctx
    # 造一个失败执行
    cid = client.post(f"/api/v1/projects/{pid}/cases", json={"project_id": pid, "name": "必失败", "type": "api",
                      "steps": [{"m": "GET", "url": "/api/none", "check": {"type": "status", "expect": "200"}}]},
                      headers=H).json()["id"]
    run = client.post(f"/api/v1/runs/cases/{cid}/run", json={"env_id": eid}, headers=H).json()
    assert run["status"] == "failed"
    r = client.post(f"/api/v1/ai/analyze-run/{run['id']}", headers=H).json()
    assert r["cause"] and r["suggestion"]
