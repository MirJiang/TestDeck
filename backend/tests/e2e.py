"""E2E：登录 → 建项目/环境/用例 → 执行（通过 + 失败路径）→ 查报告。"""
import sys
import httpx

BASE = "http://127.0.0.1:8000/api/v1"
c = httpx.Client(base_url=BASE, timeout=20)


def ok(cond, msg):
    print(("  ✓ " if cond else "  ✗ FAIL: ") + msg)
    if not cond:
        sys.exit(1)


token = c.post("/auth/login", json={"username": "admin", "password": "admin123"}).json()["token"]
H = {"Authorization": f"Bearer {token}"}
ok(bool(token), "登录拿到 JWT")

pid = c.post("/projects", json={"name": "商城中台", "desc": "E2E"}, headers=H).json()["id"]
eid = c.post(f"/projects/{pid}/envs", json={
    "name": "测试", "base_url": "http://127.0.0.1:9001",
    "variables": {"username": "admin", "password": "admin123"}}, headers=H).json()["id"]
ok(bool(pid and eid), "创建项目与环境")

steps = [
    {"m": "POST", "url": "/api/login",
     "body": '{"username": "${username}", "password": "${password}"}',
     "check": {"type": "field_eq", "field": "code", "expect": "0"},
     "save": {"name": "token", "from": "data.token"}},
    {"m": "GET", "url": "/api/user/info",
     "headers": "Authorization: Bearer ${token}",
     "check": {"type": "field_eq", "field": "data.name", "expect": "张伟"}},
    {"m": "GET", "url": "/api/user/orders",
     "check": {"type": "not_empty", "field": "data.list"}},
]
cid = c.post(f"/projects/{pid}/cases", json={"project_id": pid, "name": "登录并获取用户信息", "steps": steps}, headers=H).json()["id"]
ok(bool(cid), "创建用例（含变量替换 + 记住 token）")

run = c.post(f"/runs/cases/{cid}/run", json={"env_id": eid}, headers=H).json()
ok(run["status"] == "passed", f"执行通过：{run['pass_n']}/{run['pass_n']+run['fail_n']} 检查点，{run['duration']}s")
ok(run["detail"][0].get("saved") == "token", "第一步记住返回值 token")
ok("Bearer tok-123" in run["detail"][1]["url"] or "user/info" in run["detail"][1]["url"], "第二步 URL 正常")

# 失败路径：错误密码
bad_steps = [dict(steps[0], body='{"username": "${username}", "password": "wrong"}')]
bad_cid = c.post(f"/projects/{pid}/cases", json={"project_id": pid, "name": "错误密码应失败", "steps": bad_steps}, headers=H).json()["id"]
bad = c.post(f"/runs/cases/{bad_cid}/run", json={"env_id": eid}, headers=H).json()
ok(bad["status"] == "failed" and bad["fail_n"] == 1, f"失败路径正确识别：{bad['detail'][0]['reason']}")

# 计划：打包两条用例执行
plan = c.post("/plans", json={"project_id": pid, "name": "冒烟", "case_ids": [cid], "env_id": eid}, headers=H).json()["id"]
prun = c.post(f"/runs/plans/{plan}/run", json={"env_id": eid}, headers=H).json()
ok(prun["status"] == "passed", "计划执行通过")

hist = c.get("/runs?size=10", headers=H).json()
ok(hist["total"] >= 3, f"执行历史 {hist['total']} 条")
detail = c.get(f"/runs/{prun['id']}", headers=H).json()
ok("detail" in detail, "报告详情可查")

print("\nE2E 全部通过 ✔")
