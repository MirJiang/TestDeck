"""UI 录制器：事件编译与指令通道（不启动浏览器）。"""
import os

from app import config
config.set("TD_DB", ":memory:")

from app.engine import ui_recorder as R


def _seed(sid, events, start_url="http://t/", done=True, error=""):
    with R._lock:
        R._sessions[sid] = {
            "events": list(events), "start_url": start_url, "done": done, "error": error,
            "mode": "remote", "created": 0.0, "last_active": 0.0,
            "cmd_q": None, "ret_q": None, "frame": b"", "page_url": start_url,
        }


def test_compile_click_fill_merge():
    _seed("s1", [
        {"type": "click", "sel": "#user", "tag": "input", "text": ""},
        {"type": "fill", "sel": "#user", "value": "a"},
        {"type": "fill", "sel": "#user", "value": "ab"},      # 连续输入合并为一步
        {"type": "click", "sel": "", "tag": "button", "text": "登录"},
    ])
    out = R.compile_steps("s1")
    assert [s["action"] for s in out["steps"]] == ["goto", "click", "fill", "click"]
    assert out["steps"][2]["value"] == "ab"
    assert out["steps"][3]["selector"] == 'button:has-text("登录")'
    assert out["done"] is True


def test_compile_dup_clicks_dedup():
    _seed("s3", [
        {"type": "click", "sel": "#btn", "tag": "button", "text": "登 录"},
        {"type": "click", "sel": "#btn", "tag": "button", "text": "登 录"},   # 手抖连点
        {"type": "click", "sel": "#other", "tag": "a", "text": ""},
        {"type": "click", "sel": "#btn", "tag": "button", "text": "登 录"},   # 非连续，保留
    ])
    out = R.compile_steps("s3")
    assert [s["selector"] for s in out["steps"]] == ["", "#btn", "#other", "#btn"]


def test_compile_goto_events_dedup_start():
    _seed("s2", [
        {"type": "goto", "url": "http://t/"},                 # 与起始地址相同 → 去掉
        {"type": "click", "sel": "#a", "tag": "a", "text": ""},
        {"type": "goto", "url": "http://t/page2"},            # 真实跳转 → 保留
        {"type": "click", "sel": "#b", "tag": "button", "text": ""},
    ])
    out = R.compile_steps("s2")
    assert [(s["action"], s["url"]) for s in out["steps"]] == [
        ("goto", "http://t/"), ("click", ""), ("goto", "http://t/page2"), ("click", "")]


def test_unknown_session():
    assert R.send_cmd("nope", {"op": "click"})["ok"] is False
    assert R.get_frame("nope") is None
    out = R.compile_steps("nope")
    assert out["done"] is False and out["steps"] == []
