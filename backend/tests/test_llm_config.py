"""模型配置（多条列表）：新增/编辑/激活/删除、key 保留、权限。"""

from app import config
config.set("TD_DB", ":memory:")

from fastapi.testclient import TestClient
from app.main import app  # noqa
from app import ai


def _client():
    cm = TestClient(app)
    cm.__enter__()
    return cm


def _login(client, u="admin", p="admin123"):
    return {"Authorization": "Bearer " + client.post(
        "/api/v1/auth/login", json={"username": u, "password": p}).json()["token"]}


def _cleanup():
    from app.db import SessionLocal
    from app.models import LLMConfig
    db = SessionLocal()
    try:
        for r in db.query(LLMConfig).all():
            db.delete(r)
        db.commit()
    except Exception:
        pass  # 表尚未创建（单跑本文件时）无需清理
    finally:
        db.close()


def _add(client, H, name, model, key="sk-test1234567890", url_type="api", base="http://127.0.0.1:9/v1"):
    return client.post("/api/v1/settings/llm", headers=H, json={
        "name": name, "vendor": "测试厂商", "url_type": url_type,
        "base_url": base, "api_key": key, "model": model})


def test_llm_list_crud_and_activate():
    _cleanup()
    client = _client()
    H = _login(client)

    a = _add(client, H, "主力", "m-a").json()
    assert a["is_active"] is True                      # 首条自动使用中
    b = _add(client, H, "备用", "m-b", url_type="plan").json()
    assert b["is_active"] is False
    c3 = _add(client, H, "海外", "m-c", url_type="plan2").json()   # 第二种套餐类型
    assert c3["is_active"] is False

    items = client.get("/api/v1/settings/llm", headers=H).json()["items"]
    assert len(items) == 3 and items[0]["is_active"] is True
    assert all("sk-test1234567890" not in i["api_key_masked"] for i in items)
    assert {i["url_type"] for i in items} == {"api", "plan", "plan2"}
    assert ai.current_model() == "m-a"

    # 编辑留空 key → 保留原值
    client.put(f"/api/v1/settings/llm/{b['id']}", headers=H, json={
        "name": "备用2", "vendor": "测试厂商", "url_type": "plan",
        "base_url": "http://127.0.0.1:9/v2", "api_key": "", "model": "m-b"})
    assert ai.current_model() == "m-a"                 # 使用中未变

    # 激活 B → 全局切换
    client.post(f"/api/v1/settings/llm/{b['id']}/activate", headers=H)
    assert ai.current_model() == "m-b"
    assert ai._cfg()[0] == "http://127.0.0.1:9/v2"

    # 删除使用中的 B → 回到无使用中状态
    client.delete(f"/api/v1/settings/llm/{b['id']}", headers=H)
    assert ai.llm_available() is False

    # 新增校验：无 key 拒绝、坏地址拒绝、坏 url_type 拒绝
    assert _add(client, H, "x", "m", key="").status_code == 400
    assert _add(client, H, "x", "m", base="no-url").status_code == 400
    assert _add(client, H, "x", "m", url_type="weird").status_code == 400
    _cleanup()


def test_llm_admin_only():
    _cleanup()
    client = _client()
    H = _login(client)
    client.post("/api/v1/auth/users", headers=H, json={"username": "zw", "password": "zw12345"})
    H2 = _login(client, "zw", "zw12345")
    assert client.get("/api/v1/settings/llm", headers=H2).status_code == 403
    assert _add(client, H2, "x", "m").status_code == 403
    _cleanup()


def test_llm_log_run_attribution():
    """llm_logs.run_id：run_context 内的调用归属到执行记录，外部调用为空。"""
    from app import ai as A
    from app.db import SessionLocal
    from app.models import LLMLog
    db = SessionLocal()
    db.query(LLMLog).delete(); db.commit()
    with A.run_context("R-attr-1"):
        A._log_usage("agent-step", 100, 50, True, model="m1")
    A._log_usage("agent-step", 10, 5, True, model="m1")
    rows = db.query(LLMLog).all()
    by_run = {r.run_id: r for r in rows}
    assert by_run["R-attr-1"].prompt_tokens == 100
    assert by_run[""].completion_tokens == 5
    db.close()


def test_usage_daily_breakdown():
    """usage_summary 的 daily：最近 14 天按天聚合（日期倒序）。"""
    from datetime import datetime, timedelta
    from app import ai as A
    from app.db import SessionLocal
    from app.models import LLMLog
    db = SessionLocal()
    db.query(LLMLog).delete(); db.commit()
    now = datetime.utcnow()
    db.add(LLMLog(kind="agent-step", model="m", prompt_tokens=500, completion_tokens=100,
                  ok=True, created_at=now))
    db.add(LLMLog(kind="agent-step", model="m", prompt_tokens=200, completion_tokens=30,
                  ok=False, created_at=now - timedelta(days=2)))
    db.commit(); db.close()
    u = A.usage_summary(days=14)
    assert u["calls"] == 2 and u["prompt_tokens"] == 700
    dates = {d["date"]: d for d in u["daily"]}
    today = now.date().isoformat()
    d2 = (now - timedelta(days=2)).date().isoformat()
    assert dates[today]["calls"] == 1 and dates[today]["completion_tokens"] == 100
    assert dates[d2]["calls"] == 1 and dates[d2]["failed"] == 1
    assert [d["date"] for d in u["daily"]] == sorted([today, d2], reverse=True)


def test_run_detail_tokens():
    """GET /runs/{rid} 与列表带 tokens（llm_logs 按 run_id 汇总）。"""
    from app.db import SessionLocal
    from app.models import TestRun
    client = _client()
    H = _login(client)
    pid = client.post("/api/v1/projects", json={"name": "P-tok"}, headers=H).json()["id"]
    db = SessionLocal()
    run = TestRun(id="R-tok-1", case_id=None, status="failed", pass_n=0, fail_n=1)
    db.add(run); db.commit(); db.close()
    from app import ai as A
    with A.run_context("R-tok-1"):
        A._log_usage("agent-step", 300, 60, True, model="m")
        A._log_usage("agent-step", 40, 10, True, model="m")
    d = client.get("/api/v1/runs/R-tok-1", headers=H).json()
    assert d["tokens"] == 410
    items = client.get("/api/v1/runs", headers=H).json()["items"]
    assert any(i["id"] == "R-tok-1" and i["tokens"] == 410 for i in items)
    client.delete("/api/v1/projects/" + pid, headers=H)
    client.__exit__(None, None, None)
