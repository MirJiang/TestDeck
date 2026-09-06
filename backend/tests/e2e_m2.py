"""M2 E2E：绑定仓库 → git 计划 → push 触发自动执行 → AI 生成草稿/覆盖匹配 → 失败分析。"""
import sys
import httpx

BASE = "http://127.0.0.1:8000/api/v1"
c = httpx.Client(base_url=BASE, timeout=30)


def ok(cond, msg):
    print(("  ✓ " if cond else "  ✗ FAIL: ") + msg)
    if not cond:
        sys.exit(1)


token = c.post("/auth/login", json={"username": "admin", "password": "admin123"}).json()["token"]
H = {"Authorization": f"Bearer {token}"}

pid = c.post("/projects", json={"name": "支付网关-M2"}, headers=H).json()["id"]
eid = c.post(f"/projects/{pid}/envs", json={"name": "测试", "base_url": "http://127.0.0.1:9001", "variables": {}}, headers=H).json()["id"]
ok(True, "创建项目与环境")

repo = c.post("/integrations/git/repos", json={"project_id": pid, "repo_url": "https://git.example.com/pay/gw"}, headers=H).json()
ok("webhook_secret" in repo, f"绑定仓库，webhook: .../{repo['webhook_secret'][:8]}...")

# 已覆盖接口的用例 + git 触发计划
cid = c.post(f"/projects/{pid}/cases", json={"project_id": pid, "name": "登录冒烟", "steps": [
    {"m": "POST", "url": "/api/login", "body": '{"username":"admin","password":"admin123"}',
     "check": {"type": "field_eq", "field": "code", "expect": "0"}}]}, headers=H).json()["id"]
plan = c.post("/plans", json={"project_id": pid, "name": "合并自动回归", "case_ids": [cid], "env_id": eid, "trigger": "git"}, headers=H).json()["id"]

push = {"ref": "refs/heads/main", "commits": [
    {"id": "f10001", "message": "feat: 新增退款接口 /api/refund/create", "author": {"name": "李娜"}, "added": ["src/refund.py"]},
    {"id": "f10002", "message": "fix: 登录 /api/login 密码校验 (#90)", "author": {"name": "王强"}},
]}
wh = c.post(f"/integrations/git/webhook/{repo['webhook_secret']}", json=push).json()
ok(wh["ingested"] == 2, "webhook 同步 2 条提交")
ok(len(wh["triggered_plans"]) == 1 and wh["triggered_plans"][0]["status"] == "passed",
   "push 到 main 自动执行 git 计划并通过")

# 非默认分支不触发
wh2 = c.post(f"/integrations/git/webhook/{repo['webhook_secret']}",
             json={"ref": "refs/heads/dev", "commits": [{"id": "f10003", "message": "chore: dev 分支改动"}]}).json()
ok(len(wh2["triggered_plans"]) == 0, "非默认分支不触发计划")

ai = c.post("/ai/gen-from-commits", json={"repo_id": repo["id"]}, headers=H).json()
ok(any("/api/refund/create" in str(d["steps"]) for d in ai["drafts"]), f"AI 生成 {len(ai['drafts'])} 条草稿，含新接口 /api/refund/create")
ok(any(x["api"] == "/api/login" for x in ai["covered"]), "已覆盖接口 /api/login 识别为「建议执行」")

# 保存草稿 → source=ai
saved = c.post(f"/projects/{pid}/cases", json={"project_id": pid, "name": ai["drafts"][0]["name"],
               "steps": ai["drafts"][0]["steps"], "source": "ai"}, headers=H).json()
ok("id" in saved, "草稿可保存为用例")

# 失败分析
bad = c.post(f"/projects/{pid}/cases", json={"project_id": pid, "name": "必失败", "steps": [
    {"m": "GET", "url": "/api/none", "check": {"type": "status", "expect": "200"}}]}, headers=H).json()["id"]
run = c.post(f"/runs/cases/{bad}/run", json={"env_id": eid}, headers=H).json()
tips = c.post(f"/ai/analyze-run/{run['id']}", headers=H).json()
ok(bool(tips["cause"] and tips["suggestion"]), f"失败分析：{tips['cause']}")

print("\nM2 E2E 全部通过 ✔")
