"""evals（行为级评估）结构测试：任务集/判定键/被测端点齐备（端到端跑法见 evals/runner.py，
fake 模式由 CI 的 evals job 真跑，这里只锁结构防漂移）。"""
import os  # noqa


from app import config
config.set("TD_DB", ":memory:")


def test_tasks_wellformed():
    from evals.tasks import TASKS
    judges = {"ui_logged_in", "inquiry_created", "api_login_ok"}
    names = set()
    for t in TASKS:
        assert t["name"] not in names
        names.add(t["name"])
        assert t["judge"] in judges
        assert t["target"] in ("ui", "api")
        assert t["goal"] and "vars" in t
    assert any(t.get("fake") for t in TASKS)          # fake 模式至少有一个可跑任务
    assert any(not t.get("fake", True) for t in TASKS)  # 也有需要真模型的任务


def test_runner_module_importable():
    import importlib
    mod = importlib.import_module("evals.runner")
    assert callable(mod.run) and callable(mod._judge)
    assert callable(mod._scripted_model_factory)


def test_mock_target_eval_endpoints():
    from fastapi.testclient import TestClient
    from tests import mock_target
    c = TestClient(mock_target.app)
    assert c.post("/__eval/reset").json()["ok"] is True
    assert c.get("/__eval/state").json()["ui_login"] is False
    # 登录接口（API 路径）成功后落服务端状态
    r = c.post("/api/login", json={"username": "admin", "password": "admin123"})
    assert r.json()["code"] == 0
    assert c.get("/__eval/state").json()["api_login"] is True
    # 错误口令不落状态
    c.post("/__eval/reset")
    c.post("/api/login", json={"username": "admin", "password": "wrong"})
    assert c.get("/__eval/state").json()["api_login"] is False
    # 询价创建页与建单接口
    assert "新建询价单" in c.get("/page/inquiry/create").text
    r = c.post("/api/inquiry/create", json={"from": "上海", "to": "北京"})
    iid = r.json()["data"]["id"]
    st = c.get("/__eval/state").json()
    assert st["inquiries"] >= 1 and iid in st["inquiry_ids"]
