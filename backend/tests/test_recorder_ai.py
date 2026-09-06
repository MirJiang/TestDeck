"""录制 × AI：混合录制的事件转换 + 录完增强端点。"""
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


def test_ai_actions_to_events_and_compile():
    """AI 的动作明细转录制事件后，compile_steps 能编译出含断言的步骤。"""
    from app.engine import ui_recorder as R
    sess = {"events": [], "start_url": "http://x/login", "done": True, "error": "",
            "mode": "remote", "created": 0, "last_active": 0, "cmd_q": None, "ret_q": None,
            "frame": b"", "page_url": ""}
    actions = [
        {"action": "goto", "url": "http://x/home"},
        {"action": "fill", "selector": "#u", "value": "sw01"},
        {"action": "click", "selector": "#btn"},
        {"action": "expect_text", "value": "欢迎回来"},
        {"action": "click_xy", "x": 1, "y": 2},          # 坐标动作不转换
        {"action": "done"},                                # 控制指令不转换
    ]
    n = R._append_ai_events(sess, actions)
    assert n == 4, "只转换有稳定选择器/内容的 4 个动作"

    R._sessions["t-ai"] = sess
    out = R.compile_steps("t-ai")
    kinds = [s["action"] for s in out["steps"]]
    assert kinds == ["goto", "goto", "fill", "click", "expect_text"]
    assert out["steps"][-1]["value"] == "欢迎回来"
    del R._sessions["t-ai"]


def test_enhance_steps_fallback_without_llm():
    """未配置大模型时：原样返回步骤 + enhanced=False 引导直接使用。"""
    client = make_client()
    H = {"Authorization": "Bearer " + login(client, "admin", "admin123")}
    steps = [{"action": "goto", "url": "http://x/login", "selector": "", "value": ""},
             {"action": "fill", "url": "", "selector": "#u", "value": "sw01"},
             {"action": "click", "url": "", "selector": "#btn", "value": ""}]
    r = client.post("/api/v1/ai/enhance-steps",
                    json={"steps": steps, "url": "http://x/login"}, headers=H)
    assert r.status_code == 200
    body = r.json()
    assert body["enhanced"] is False and body["steps"] == steps
    assert "系统设置" in body["note"]

    # 空步骤直接 400
    assert client.post("/api/v1/ai/enhance-steps", json={"steps": []}, headers=H).status_code == 400


def test_record_cmd_accepts_ai_fields():
    """指令模型接受 goal/vars/max_steps（AI 代劳参数），非法会话返回友好错误。"""
    client = make_client()
    H = {"Authorization": "Bearer " + login(client, "admin", "admin123")}
    r = client.post("/api/v1/cases/ui-record/no-such/cmd",
                    json={"op": "ai", "goal": "登录", "vars": {"username": "a"}, "max_steps": 8}, headers=H)
    assert r.status_code == 200 and r.json()["ok"] is False
