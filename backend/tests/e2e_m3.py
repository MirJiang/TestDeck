"""M3 E2E：UI 用例（Playwright 无头浏览器）→ 打开页面 → 输入 → 点击 → 检查文字 → 截图。"""
import sys
import httpx

BASE = "http://127.0.0.1:8000/api/v1"
c = httpx.Client(base_url=BASE, timeout=120)


def ok(cond, msg):
    print(("  ✓ " if cond else "  ✗ FAIL: ") + msg)
    if not cond:
        sys.exit(1)


token = c.post("/auth/login", json={"username": "admin", "password": "admin123"}).json()["token"]
H = {"Authorization": f"Bearer {token}"}

pid = c.post("/projects", json={"name": "运营后台-M3"}, headers=H).json()["id"]
eid = c.post(f"/projects/{pid}/envs", json={"name": "测试", "base_url": "http://127.0.0.1:9001", "variables": {}}, headers=H).json()["id"]

steps = [
    {"action": "goto", "url": "/page/login", "selector": "", "value": ""},
    {"action": "fill", "url": "", "selector": "#u", "value": "admin"},
    {"action": "fill", "url": "", "selector": "#p", "value": "admin123"},
    {"action": "click", "url": "", "selector": "#btn", "value": ""},
    {"action": "expect_text", "url": "", "selector": "", "value": "登录成功，欢迎回来"},
    {"action": "screenshot", "url": "", "selector": "", "value": ""},
]
cid = c.post(f"/projects/{pid}/cases", json={"project_id": pid, "name": "后台登录流程", "type": "ui", "steps": steps}, headers=H).json()["id"]
ok(bool(cid), "创建 UI 用例（6 个动作步骤）")

run = c.post(f"/runs/cases/{cid}/run", json={"env_id": eid}, headers=H).json()
ok(run["status"] == "passed", f"浏览器执行通过：{run['pass_n']}/6 步，{run['duration']}s")
ok(any(s.get("screenshot") for s in run["detail"]), "最后一步截图留档")
shot = next(s["screenshot"] for s in run["detail"] if s.get("screenshot"))
img = httpx.get("http://127.0.0.1:8000" + shot)
ok(img.status_code == 200 and img.content[:8].startswith(b"\x89PNG"), f"截图可访问：{shot} ({len(img.content)} bytes)")

# 失败路径：期望不存在的文字
bad = c.post(f"/projects/{pid}/cases", json={"project_id": pid, "name": "登录失败文案", "type": "ui", "steps": [
    steps[0], {**steps[4], "value": "这个文字不存在"}]}, headers=H).json()["id"]
brun = c.post(f"/runs/cases/{bad}/run", json={"env_id": eid}, headers=H).json()
ok(brun["status"] == "failed" and "未包含" in brun["detail"][-1]["reason"], "失败路径正确：页面未包含预期文字")

# 非法动作校验
r = c.post(f"/projects/{pid}/cases", json={"project_id": pid, "name": "bad", "type": "ui",
          "steps": [{"action": "hack", "url": "/"}]}, headers=H)
ok(r.status_code == 400, "非法动作被拒绝")

print("\nM3 E2E 全部通过 ✔")
