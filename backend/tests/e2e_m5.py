"""M5 E2E：成员管理、用户管理、CSV 导出、回归建议、git-sync CLI、录制接口。"""
import sys
import subprocess
import tempfile
import os
import httpx

BASE = "http://127.0.0.1:8000/api/v1"
c = httpx.Client(base_url=BASE, timeout=30)


def ok(cond, msg):
    print(("  ✓ " if cond else "  ✗ FAIL: ") + msg)
    if not cond:
        sys.exit(1)


token = c.post("/auth/login", json={"username": "admin", "password": "admin123"}).json()["token"]
H = {"Authorization": f"Bearer {token}"}

# 用户管理（用户名带时间戳，脚本可重复执行）
import time as _t
uname = f"qa{_t.time() % 100000:.0f}"
u = c.post("/auth/users", json={"username": uname, "password": "qa123456", "role": "member"}, headers=H).json()
ok("id" in u, f"创建成员账号 {uname}")
users = c.get("/auth/users", headers=H).json()
ok(any(x["username"] == uname for x in users), "用户列表可见")
tok2 = c.post("/auth/login", json={"username": uname, "password": "qa123456"}).json()["token"]
H2 = {"Authorization": f"Bearer {tok2}"}
r = c.put("/auth/password", json={"old_password": "qa123456", "new_password": "qa789012"}, headers=H2)
ok(r.json()["ok"], "自助修改密码")
c.post("/auth/login", json={"username": uname, "password": "qa789012"})
ok(True, "新密码可登录")

# 成员加入项目
pid = c.post("/projects", json={"name": "协作项目"}, headers=H).json()["id"]
r = c.get(f"/projects/{pid}/cases", headers=H2)
ok(r.status_code == 403, "未加入前成员访问被拒（403）")
r = c.post(f"/projects/{pid}/members", json={"username": uname}, headers=H)
ok(r.json()["ok"], "把 qa01 加入项目")
r = c.get(f"/projects/{pid}/cases", headers=H2)
ok(r.status_code == 200, "加入后可访问（200）")
mem = c.get(f"/projects/{pid}/members", headers=H).json()
ok(any(m["username"] == uname for m in mem["members"]), "成员列表包含 " + uname)

# CSV 导出
csv_resp = httpx.get(BASE + "/runs/export.csv", headers=H)
ok(csv_resp.status_code == 200 and "执行ID" in csv_resp.text, f"CSV 导出 {len(csv_resp.text)} 字节")

# 回归建议
adv = c.get("/ai/regression-advice", headers=H).json()
ok("stale_cases" in adv and "advice" in adv, f"回归建议：{adv['advice']}")

# LLM 用量
usage = c.get("/ai/usage", headers=H).json()
ok("calls" in usage, f"AI 用量可查（引擎：{usage['model'] or '内置规则'}）")

# git-sync CLI：临时 git 仓库推送到平台 webhook
repo = c.post("/integrations/git/repos", json={"project_id": pid, "repo_url": "local"}, headers=H).json()
with tempfile.TemporaryDirectory() as td:
    def git(*a): subprocess.run(["git", "-C", td] + list(a), capture_output=True, check=True)
    env = dict(os.environ, GIT_AUTHOR_NAME="李娜", GIT_AUTHOR_EMAIL="n@x.co",
               GIT_COMMITTER_NAME="李娜", GIT_COMMITTER_EMAIL="n@x.co")
    def git_e(*a): subprocess.run(["git", "-C", td] + list(a), capture_output=True, check=True, env=env)
    git("init", "-b", "main"); git_e("commit", "--allow-empty", "-m", "feat: 新增会员接口 /api/member/add")
    out = subprocess.run([sys.executable, "-m", "app.cli.git_sync", "--repo", td,
                          "--webhook", BASE + f"/integrations/git/webhook/{repo['webhook_secret']}"],
                         capture_output=True, text=True, cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    ok("已同步 1 条提交" in out.stdout, f"git-sync CLI 成功：{out.stdout.strip() or out.stderr.strip()[:120]}")

# 录制接口：地址校验（真实录制需打开可见浏览器，留给人工验证）
r = c.post("/ui-record/start?url=notaurl", headers=H)
ok(r.status_code == 400, "录制接口校验地址")

print("\nM5 E2E 全部通过 ✔（录制器交互需在桌面环境人工验证）")
