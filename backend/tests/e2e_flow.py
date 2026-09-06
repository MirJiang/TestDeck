"""流程测试 E2E：询价单业务线 —— 货主(API 建单+共享单号) → 物流A/UI 报价 → 物流B/UI 报价 → 货主 UI 查看结果。"""
import sys
import httpx

BASE = "http://127.0.0.1:8000/api/v1"
c = httpx.Client(base_url=BASE, timeout=180)


def ok(cond, msg):
    print(("  ✓ " if cond else "  ✗ FAIL: ") + msg)
    if not cond:
        sys.exit(1)


token = c.post("/auth/login", json={"username": "admin", "password": "admin123"}).json()["token"]
H = {"Authorization": f"Bearer {token}"}

pid = c.post("/projects", json={"name": "物流平台-流程"}, headers=H).json()["id"]
eid = c.post(f"/projects/{pid}/envs", json={"name": "测试", "base_url": "http://127.0.0.1:9001",
            "variables": {}}, headers=H).json()["id"]
ok(True, "创建项目与环境")

roles = [
    {"key": "shipper", "name": "货主", "variables": {"username": "sw01", "password": "******"}},
    {"key": "carrierA", "name": "物流公司A", "variables": {"username": "ck_a", "password": "******"}},
    {"key": "carrierB", "name": "物流公司B", "variables": {"username": "ck_b", "password": "******"}},
]
steps = [
    # 货主调接口创建询价单，单号共享给全流程
    {"role": "shipper", "type": "api", "m": "POST", "url": "/api/inquiry/create",
     "body": '{"from": "上海", "to": "北京"}',
     "check": {"type": "field_eq", "field": "code", "expect": "0"},
     "save": {"name": "inquiry_id", "from": "data.id"}},
    # 物流A 在自己的页面报价（URL 引用共享单号）
    {"role": "carrierA", "type": "ui", "action": "goto",
     "url": "/page/inquiry/quote?id=${inquiry_id}&carrier=A"},
    {"role": "carrierA", "type": "ui", "action": "click", "selector": "#quote"},
    {"role": "carrierA", "type": "ui", "action": "expect_text", "value": "报价成功，已有 1 家报价"},
    # 物流B 同样报价
    {"role": "carrierB", "type": "ui", "action": "goto",
     "url": "/page/inquiry/quote?id=${inquiry_id}&carrier=B"},
    {"role": "carrierB", "type": "ui", "action": "click", "selector": "#quote"},
    {"role": "carrierB", "type": "ui", "action": "expect_text", "value": "报价成功，已有 2 家报价"},
    # 货主查看结果页，应能看到两家报价方
    {"role": "shipper", "type": "ui", "action": "goto", "url": "/page/inquiry/result"},
    {"role": "shipper", "type": "ui", "action": "expect_text", "value": "A、B"},
    {"role": "shipper", "type": "ui", "action": "screenshot"},
]
fid = c.post("/flows", json={"project_id": pid, "name": "询价单全流程",
             "desc": "货主发单，两家物流报价", "roles": roles, "steps": steps}, headers=H).json()["id"]
ok(bool(fid), "创建流程（3 角色 10 步骤，API+UI 混合）")

run = c.post(f"/flows/{fid}/run", json={"env_id": eid}, headers=H).json()
ok(run["status"] == "passed", f"流程跑通：{run['pass_n']}/10 步，{run['duration']}s")
ok(any(s.get("saved") == "inquiry_id" for s in run["detail"]), "货主创建的单号已共享给后续角色")
ok("${inquiry_id}" not in str(run["detail"]), "共享变量在物流公司页面 URL 中正确替换")
ok(any(s.get("screenshot") for s in run["detail"]), "含截图步骤")

# 失败路径：物流公司点报价前先检查一个不存在的文字 → 流程中断
bad = c.post("/flows", json={"project_id": pid, "name": "失败示例", "roles": roles, "steps": [
    steps[0], steps[1],
    {"role": "carrierA", "type": "ui", "action": "expect_text", "value": "这个文字不存在"},
    {"role": "carrierB", "type": "ui", "action": "goto", "url": "/page/inquiry/quote?id=${inquiry_id}"}]}, headers=H).json()["id"]
brun = c.post(f"/flows/{bad}/run", json={"env_id": eid}, headers=H).json()
ok(brun["status"] == "failed" and brun["fail_n"] == 1 and brun["pass_n"] == 2,
   f"失败即中断：2 步通过后第 3 步失败，后续步骤未执行（{brun['detail'][-1]['reason'][:30]}）")

runs = c.get(f"/flows/{fid}/runs", headers=H).json()
ok(len(runs) >= 1, "流程执行历史可查")

print("\n流程测试 E2E 全部通过 ✔（多角色 · API+UI 混合 · 共享变量 · 无头浏览器）")
