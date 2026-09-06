"""AI 用例引擎：用假页面 + 脚本化假模型验证决策循环（不启动浏览器）。"""
import os

from app import config
config.set("TD_DB", ":memory:")

from app.engine import ai_runner
from app.engine.ai_runner import ai_drive


class FakeMouse:
    def __init__(self):
        self.ops = []

    def move(self, x, y):
        self.ops.append(("move", x, y))

    def down(self):
        self.ops.append(("down",))

    def up(self):
        self.ops.append(("up",))

    def click(self, x, y):
        self.ops.append(("click", x, y))


class FakePage:
    """实现 ai_drive 用到的页面操作面：evaluate / goto / click / fill / inner_text / screenshot / mouse。"""

    def __init__(self, state, body="", fail_selectors=(), shot=b"shot"):
        self.state = state
        self.body = body
        self.fail_selectors = fail_selectors
        self.shot = shot
        self.shots = 0
        self.calls = []
        self.mouse = FakeMouse()

    def evaluate(self, js):
        return self.state

    def goto(self, url, timeout=0):
        self.calls.append(("goto", url))

    def click(self, sel, timeout=0):
        if sel in self.fail_selectors:
            raise TimeoutError(f"Page.click: Timeout 8000ms exceeded. waiting for {sel}")
        self.calls.append(("click", sel))

    def fill(self, sel, val, timeout=0):
        if sel in self.fail_selectors:
            raise TimeoutError(f"Page.fill: Timeout 8000ms exceeded. waiting for {sel}")
        self.calls.append(("fill", sel, val))

    def inner_text(self, sel, timeout=0):
        return self.body

    def screenshot(self, path="", full_page=False, type=None, quality=None):
        self.shots += 1
        return self.shot


STATE = {"url": "http://t/login", "title": "登录",
         "elements": [{"selector": "#u", "tag": "input", "text": "", "value": ""},
                      {"selector": "#btn", "tag": "button", "text": "登 录", "value": ""}],
         "text": "运营后台 登录"}


def _fake_llm(script, store):
    def fn(system, user, kind="", image_b64=None):
        return script[store["n"]] if store["n"] < len(script) else {"action": "done", "pass": False, "reason": "脚本耗尽"}
    return fn


def _enable(monkeypatch, script):
    store = {"n": 0}
    orig = _fake_llm(script, store)

    def counting(system, user, kind="", image_b64=None):
        r = orig(system, user, kind)
        store["n"] += 1
        return r

    monkeypatch.setattr("app.ai.llm_available", lambda: True)
    monkeypatch.setattr("app.ai.chat_json_sync", counting)
    return store


def test_ai_drive_success_flow(monkeypatch):
    _enable(monkeypatch, [
        {"think": "填账号", "action": "fill", "selector": "#u", "value": "${username}"},
        {"think": "点登录", "action": "click", "selector": "#btn"},
        {"think": "验证", "action": "expect_text", "value": "登录成功"},
        {"think": "存单号", "action": "save", "name": "order_no", "value": "Q001"},
        {"think": "完成", "action": "done", "pass": True, "reason": "登录成功且有单号"},
    ])
    page = FakePage(STATE, body="登录成功，欢迎回来 单号 Q001")
    seen = []
    r = ai_drive(page, "用 ${username} 登录并记住单号", {"username": "sw01"}, max_steps=10,
                 on_step=lambda d: seen.append(len(d)))
    assert r["status"] == "passed" and r["fail_n"] == 0
    assert seen == [1, 2, 3, 4, 5]                       # 每执行一步回调一次（流式进度）
    assert r["saved"] == {"order_no": "Q001"}
    assert ("fill", "#u", "sw01") in page.calls          # ${username} 已替换
    assert ("click", "#btn") in page.calls
    assert [d["action"] for d in r["detail"]] == ["fill", "click", "expect_text", "save", "done"]
    assert r["summary"] == "登录成功且有单号"


def test_ai_drive_same_fail_bail(monkeypatch):
    _enable(monkeypatch, [{"action": "fill", "selector": "#u", "value": "x"}] * 10)
    page = FakePage(STATE, fail_selectors=("#u",))
    r = ai_drive(page, "填表单", {}, max_steps=10)
    assert len(r["detail"]) == 4                          # 3 次失败尝试 + 1 条熔断说明
    assert "连续 3 次失败" in r["detail"][-1]["reason"]
    assert r["status"] == "failed"


def test_ai_drive_expect_fail_marks_failed(monkeypatch):
    _enable(monkeypatch, [
        {"think": "验证", "action": "expect_text", "value": "不存在的文字"},
        {"think": "完成", "action": "done", "pass": True, "reason": "ok"},
    ])
    page = FakePage(STATE, body="登录成功")
    r = ai_drive(page, "检查文案", {}, max_steps=5)
    assert r["status"] == "failed" and r["fail_n"] == 1


def test_ai_drive_invalid_output_gives_up(monkeypatch):
    _enable(monkeypatch, [None, None, None])
    page = FakePage(STATE)
    r = ai_drive(page, "随便", {}, max_steps=6)
    assert r["status"] == "failed"
    assert "未返回有效动作" in r["detail"][-1]["reason"]


def test_ai_drive_max_steps(monkeypatch):
    _enable(monkeypatch, [{"action": "click", "selector": "#btn"}] * 30)
    page = FakePage(STATE)
    r = ai_drive(page, "无限点击", {}, max_steps=4)
    assert r["status"] == "failed"
    assert "最大步数" in r["detail"][-1]["reason"]


def test_ai_drive_llm_not_configured(monkeypatch):
    monkeypatch.setattr("app.ai.llm_available", lambda: False)
    r = ai_drive(FakePage(STATE), "任何目标", {})
    assert r["status"] == "failed"
    assert "TD_LLM" in r["detail"][0]["reason"]


def test_build_messages_with_image():
    from app.ai import build_messages
    text_only = build_messages("sys", "hello")
    assert isinstance(text_only[1]["content"], str)
    with_img = build_messages("sys", "hello", image_b64="QUJD")
    parts = with_img[1]["content"]
    assert parts[0]["type"] == "image_url" and parts[0]["image_url"]["url"].endswith("QUJD")
    assert parts[1]["type"] == "text"


def test_ai_drive_vision_slider(monkeypatch):
    """视觉模式：滑块场景下模型输出 drag，执行时按轨迹分步拖拽。"""
    monkeypatch.setattr("app.ai.llm_available", lambda: True)
    monkeypatch.setattr("app.ai.vision_enabled", lambda: True)
    script = iter([
        {"think": "拖滑块到缺口", "action": "drag", "x": 50, "y": 300, "x2": 280, "y2": 300},
        {"think": "完成", "action": "done", "pass": True, "reason": "滑块已拼合"},
    ])

    def fake_chat(system, user, kind="", image_b64=None):
        assert image_b64 == "c2hvdA=="          # 截图已转 base64 附带
        assert "附有当前页面截图" in user and "坐标原点为左上角" in user
        return next(script)

    monkeypatch.setattr("app.ai.chat_json_sync", lambda *a, **k: fake_chat(*a, **k))
    page = FakePage(STATE)
    r = ai_drive(page, "通过滑块验证码", {}, max_steps=5)
    assert r["status"] == "passed" and page.shots >= 1
    downs = [i for i, o in enumerate(page.mouse.ops) if o == ("down",)]
    ups = [i for i, o in enumerate(page.mouse.ops) if o == ("up",)]
    assert downs and ups and downs[0] < ups[0]           # down 在 up 之前
    assert ("move", 280, 300) in page.mouse.ops          # 终点到位


def test_ai_driver_click_xy(monkeypatch):
    monkeypatch.setattr("app.ai.llm_available", lambda: True)
    monkeypatch.setattr("app.ai.vision_enabled", lambda: True)
    script = iter([
        {"think": "点第一个字", "action": "click_xy", "x": 120, "y": 200},
        {"think": "完成", "action": "done", "pass": True, "reason": "点选完成"},
    ])

    def fake_chat(system, user, kind="", image_b64=None):
        return next(script)

    monkeypatch.setattr("app.ai.chat_json_sync", lambda *a, **k: fake_chat(*a, **k))
    page = FakePage(STATE)
    r = ai_drive(page, "点选验证码", {}, max_steps=4)
    assert r["status"] == "passed"
    assert ("click", 120, 200) in page.mouse.ops
    assert r["detail"][0]["action"] == "click_xy"


def test_ai_no_vision_no_screenshot(monkeypatch):
    monkeypatch.setattr("app.ai.llm_available", lambda: True)
    monkeypatch.setattr("app.ai.vision_enabled", lambda: False)   # 未勾选视觉
    seen_imgs = []
    def chat(system, user, kind="", image_b64=None):
        seen_imgs.append(image_b64)
        return {"action": "done", "pass": True, "reason": "ok"}
    monkeypatch.setattr("app.ai.chat_json_sync", chat)
    page = FakePage(STATE)
    r = ai_drive(page, "g", {}, max_steps=3)
    assert seen_imgs == [None]                            # 决策全程不带截图（省 token）
    assert page.shots == 1                                # 仅 done 时留证截图
