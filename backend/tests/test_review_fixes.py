"""审查修复批次回归测试：cron 校验、AI 端点权限、setattr/case_ids 归属、
/static 项目级鉴权、取消竞态、CSV 公式注入、env 归属、MCP scope 令牌、录制会话属主。"""
import os  # noqa: F401

from app import config
config.set("TD_DB", ":memory:")

from fastapi.testclient import TestClient
from app.main import app, _static  # noqa


def _client():
    cm = TestClient(app)
    cm.__enter__()
    return cm


def _login(client, u="admin", p="admin123"):
    r = client.post("/api/v1/auth/login", json={"username": u, "password": p})
    return {"Authorization": "Bearer " + r.json()["token"]}


def _mk_project(client, H, name):
    return client.post("/api/v1/projects", json={"name": name}, headers=H).json()["id"]


def _mk_user(client, H, username, password="pass123", role="member"):
    r = client.post("/api/v1/auth/users", headers=H,
                    json={"username": username, "password": password, "role": role})
    assert r.status_code == 200, r.text
    return username, password


# ---------- cron 入参校验（非法 cron 曾可让应用无法启动） ----------

def test_invalid_cron_rejected():
    client = _client()
    H = _login(client)
    pid = _mk_project(client, H, "cron")
    for bad in ("每天九点", "0 8 * * * *", "99 * * * *", "* * *"):
        r = client.post("/api/v1/plans", headers=H, json={
            "project_id": pid, "name": "p", "trigger": "cron", "cron": bad})
        assert r.status_code == 400, (bad, r.text)
    r = client.post("/api/v1/plans", headers=H, json={
        "project_id": pid, "name": "ok", "trigger": "cron", "cron": "0 8 * * *"})
    assert r.status_code == 200


def test_validate_cron_unit():
    from app.scheduler import validate_cron
    assert validate_cron("0 8 * * *") is None
    assert validate_cron("*/5 * * * 1-5") is None
    assert validate_cron("bad") is not None
    assert validate_cron("99 * * * *") is not None


def test_scheduler_refresh_survives_bad_cron_row():
    """库里有脏 cron 行时 refresh 只跳过该条，不抛异常（启动链容错）。"""
    from app.db import SessionLocal
    from app.models import Schedule, TestPlan
    db = SessionLocal()
    try:
        p = TestPlan(project_id="x", name="dirty", cron="不是cron", trigger="cron")
        db.add(p); db.commit()
        db.add(Schedule(plan_id=p.id, cron="不是cron", enabled=True)); db.commit()
        from app.scheduler import refresh
        refresh()   # 不应抛异常
    finally:
        db.close()


# ---------- routers/ai.py 权限 ----------

def test_gen_from_commits_requires_project_access():
    client = _client()
    H = _login(client)
    pid = _mk_project(client, H, "repo-p")
    repo = client.post("/api/v1/integrations/git/repos", headers=H, json={
        "project_id": pid, "repo_url": "http://git/x"}).json()
    from app.db import SessionLocal
    from app.models import CommitSync
    db = SessionLocal()
    try:
        db.add(CommitSync(repo_id=repo["id"], sha="abc123def456", author="a",
                          message="feat: 新增接口", branch="main"))
        db.commit()
    finally:
        db.close()
    _mk_user(client, H, "outsider")
    M = _login(client, "outsider", "pass123")
    r = client.post("/api/v1/ai/gen-from-commits", headers=M, json={"repo_id": repo["id"]})
    assert r.status_code == 403


def test_gen_from_text_requires_project_access():
    client = _client()
    H = _login(client)
    pid = _mk_project(client, H, "ai-p")
    _mk_user(client, H, "outsider2")
    M = _login(client, "outsider2", "pass123")
    r = client.post("/api/v1/ai/gen-from-text", headers=M,
                    json={"project_id": pid, "prompt": "生成登录用例"})
    assert r.status_code == 403


def test_regression_advice_scoped_to_accessible_projects():
    client = _client()
    H = _login(client)
    other_pid = _mk_project(client, H, "他人项目")
    client.post(f"/api/v1/projects/{other_pid}/cases", headers=H, json={
        "project_id": other_pid, "name": "他人的机密用例", "type": "ai", "target": "ui"})
    _mk_user(client, H, "adviser")
    M = _login(client, "adviser", "pass123")
    my_pid = client.post("/api/v1/projects", json={"name": "我的项目"}, headers=M).json()["id"]
    client.post(f"/api/v1/projects/{my_pid}/cases", headers=M, json={
        "project_id": my_pid, "name": "我的用例", "type": "ai", "target": "ui"})
    r = client.get("/api/v1/ai/regression-advice", headers=M)
    assert r.status_code == 200
    names = [c["case_name"] for c in r.json()["stale_cases"]]
    assert "我的用例" in names
    assert "他人的机密用例" not in names


def test_ai_usage_admin_only():
    client = _client()
    _mk_user(client, _login(client), "usage_member")
    M = _login(client, "usage_member", "pass123")
    assert client.get("/api/v1/ai/usage", headers=M).status_code == 403


# ---------- setattr 白名单与 case_ids/flow_ids/env 归属 ----------

def test_plan_case_ids_must_belong_to_project():
    client = _client()
    H = _login(client)
    other = _mk_project(client, H, "他P")
    other_case = client.post(f"/api/v1/projects/{other}/cases", headers=H, json={
        "project_id": other, "name": "c", "type": "ai", "target": "ui"}).json()["id"]
    _mk_user(client, H, "plan_member")
    M = _login(client, "plan_member", "pass123")
    mine = client.post("/api/v1/projects", json={"name": "我P"}, headers=M).json()["id"]
    r = client.post("/api/v1/plans", headers=M, json={
        "project_id": mine, "name": "越权计划", "case_ids": [other_case]})
    assert r.status_code == 400


def test_update_plan_cannot_move_project():
    client = _client()
    H = _login(client)
    _mk_user(client, H, "plan_mover")
    M = _login(client, "plan_mover", "pass123")
    mine = client.post("/api/v1/projects", json={"name": "原项目"}, headers=M).json()["id"]
    pid = client.post("/api/v1/plans", headers=M, json={
        "project_id": mine, "name": "p"}).json()["id"]
    target = client.post("/api/v1/projects", json={"name": "目标项目"}, headers=M).json()["id"]
    r = client.put(f"/api/v1/plans/{pid}", headers=M, json={
        "project_id": target, "name": "p2"})
    assert r.status_code == 200
    from app.db import SessionLocal
    from app.models import TestPlan
    db = SessionLocal()
    try:
        assert db.get(TestPlan, pid).project_id == mine   # project_id 不随 update 改写
    finally:
        db.close()


def test_update_flow_cannot_move_project():
    client = _client()
    _mk_user(client, _login(client), "flow_mover")
    M = _login(client, "flow_mover", "pass123")
    mine = client.post("/api/v1/projects", json={"name": "流原"}, headers=M).json()["id"]
    fid = client.post("/api/v1/flows", headers=M, json={
        "project_id": mine, "name": "f"}).json()["id"]
    target = client.post("/api/v1/projects", json={"name": "流目标"}, headers=M).json()["id"]
    r = client.put(f"/api/v1/flows/{fid}", headers=M, json={
        "project_id": target, "name": "f2"})
    assert r.status_code == 200
    from app.db import SessionLocal
    from app.models import Flow
    db = SessionLocal()
    try:
        assert db.get(Flow, fid).project_id == mine
    finally:
        db.close()


def test_run_rejects_foreign_env():
    client = _client()
    H = _login(client)
    other = _mk_project(client, H, "env他P")
    other_env = client.post(f"/api/v1/projects/{other}/envs", headers=H,
                            json={"name": "他环境", "base_url": "http://127.0.0.1:59998"}).json()["id"]
    _mk_user(client, H, "env_member")
    M = _login(client, "env_member", "pass123")
    mine = client.post("/api/v1/projects", json={"name": "env我P"}, headers=M).json()["id"]
    my_case = client.post(f"/api/v1/projects/{mine}/cases", headers=M, json={
        "project_id": mine, "name": "c", "type": "ai", "target": "ui"}).json()["id"]
    r = client.post(f"/api/v1/runs/cases/{my_case}/run", headers=M, json={"env_id": other_env})
    assert r.status_code == 400


# ---------- /static 项目级鉴权 ----------

def test_static_screenshots_project_scoped():
    client = _client()
    H = _login(client)
    other = _mk_project(client, H, "shot他P")
    other_case = client.post(f"/api/v1/projects/{other}/cases", headers=H, json={
        "project_id": other, "name": "c", "type": "ai", "target": "ui"}).json()["id"]
    _mk_user(client, H, "shot_member")
    M = _login(client, "shot_member", "pass123")
    mine = client.post("/api/v1/projects", json={"name": "shot我P"}, headers=M).json()["id"]
    my_case = client.post(f"/api/v1/projects/{mine}/cases", headers=M, json={
        "project_id": mine, "name": "c2", "type": "ai", "target": "ui"}).json()["id"]
    from app.db import SessionLocal
    from app.models import TestRun
    db = SessionLocal()
    try:
        r1 = TestRun(id="R-aaaaaaa001", case_id=other_case, case_name="c", status="passed")
        r2 = TestRun(id="R-bbbbbbb002", case_id=my_case, case_name="c2", status="passed")
        db.add_all([r1, r2]); db.commit()
    finally:
        db.close()
    f1, f2 = _static / "R-aaaaaaa001-1.png", _static / "R-bbbbbbb002-1.png"
    f1.write_bytes(b"\x89PNG\r\n\x1a\n")
    f2.write_bytes(b"\x89PNG\r\n\x1a\n")
    try:
        assert client.get("/static/R-bbbbbbb002-1.png", headers=M).status_code == 200   # 自己项目
        assert client.get("/static/R-aaaaaaa001-1.png", headers=M).status_code == 403   # 他人项目
        assert client.get("/static/R-aaaaaaa001-1.png", headers=H).status_code == 200   # admin 放行
        assert client.get("/static/R-ccccccc003-1.png", headers=M).status_code == 403   # 无法判定归属 → 拒绝
    finally:
        f1.unlink(missing_ok=True); f2.unlink(missing_ok=True)


# ---------- 取消竞态与 run_id ----------

def test_register_no_longer_swallows_cancel():
    """排队期间发起的取消，任务开跑（register）后必须仍然有效。"""
    from app.engine import queue
    queue.cancel("R-QUEUED")
    queue.register("R-QUEUED")
    assert queue.is_cancelled("R-QUEUED") is True
    queue.unregister("R-QUEUED")
    assert queue.is_cancelled("R-QUEUED") is False


def test_run_ids_unique_and_random():
    from app.routers.runs import _new_run_id
    ids = {_new_run_id() for _ in range(200)}
    assert len(ids) == 200   # 无碰撞
    assert all(i.startswith("R-") for i in ids)


# ---------- CSV 公式注入 ----------

def test_csv_export_sanitizes_formula():
    client = _client()
    H = _login(client)
    pid = _mk_project(client, H, "csvP")
    evil = "=HYPERLINK(\"http://evil\",\"点我\")"
    cid = client.post(f"/api/v1/projects/{pid}/cases", headers=H, json={
        "project_id": pid, "name": evil, "type": "ai", "target": "ui"}).json()["id"]
    from app.db import SessionLocal
    from app.models import TestRun
    db = SessionLocal()
    try:
        db.add(TestRun(id="R-csv12345", case_id=cid, case_name=evil, status="passed"))
        db.commit()
    finally:
        db.close()
    r = client.get("/api/v1/runs/export.csv", headers=H)
    assert r.status_code == 200
    body = r.text
    assert "'=HYPERLINK" in body      # 公式开头已加单引号前缀
    assert "\n=HYPERLINK" not in body  # 不存在未转义的行首公式


# ---------- MCP scope 令牌 ----------

def test_mcp_token_rejected_on_rest():
    client = _client()
    H = _login(client)
    r = client.get("/api/v1/settings/mcp", headers=H)
    mcp_token = r.json()["token"]
    M = {"Authorization": "Bearer " + mcp_token}
    assert client.get("/api/v1/auth/me", headers=M).status_code == 401   # 不能当登录令牌用
    assert client.get("/api/v1/projects", headers=M).status_code == 401


# ---------- 录制会话属主 ----------

def test_recording_session_ownership():
    from app.engine import ui_recorder

    class _U:
        id, role = "u1", "member"

    class _U2:
        id, role = "u2", "member"

    class _Admin:
        id, role = "u9", "admin"

    ui_recorder.start_recording("rec-own-test", "http://127.0.0.1:59999/", mode="remote",
                                owner_id="u1")   # 线程内起浏览器会失败，但会话记录已建立
    try:
        assert ui_recorder.session_exists("rec-own-test")
        assert ui_recorder.session_owned_by("rec-own-test", _U()) is True
        assert ui_recorder.session_owned_by("rec-own-test", _U2()) is False   # 他人 → 拒绝
        assert ui_recorder.session_owned_by("rec-own-test", _Admin()) is True  # admin 放行
        assert ui_recorder.session_owned_by("rec-none", _U()) is False
    finally:
        with ui_recorder._lock:
            ui_recorder._sessions.pop("rec-own-test", None)


# ---------- brain 行为修正 ----------

def test_thinking_safe_choice_keeps_forced():
    """强制结构化输出不再被无条件降级为 auto（交给 AgentScope 梯队按 400 降级）。"""
    from app.engine.brain_agentscope import _thinking_safe_choice

    class _TC:
        def __init__(self, mode):
            self.mode = mode

    forced = _TC("generate_structured_output")
    tc, tools = _thinking_safe_choice(forced, ["t"])
    assert tc is forced and tools == ["t"]          # 原样透传
    tc, tools = _thinking_safe_choice(_TC("none"), ["t"])
    assert tc is None and tools is None             # none → 纯文本补全
    assert _thinking_safe_choice(None, ["t"]) == (None, ["t"])


def test_fallback_model_exposes_openai_exceptions():
    """FallbackChatModel 必须暴露 OpenAI 的重试/结构化降级异常集，否则两套梯队全部失效。"""
    from agentscope.credential import OpenAICredential
    from agentscope.model import OpenAIChatModel
    from app.engine.brain_agentscope import make_fallback_model
    m = OpenAIChatModel(credential=OpenAICredential(api_key="k", base_url="http://127.0.0.1:1"),
                        model="test-model", stream=False)
    fb = make_fallback_model([m])
    assert len(fb._get_retryable_exceptions()) > 0
    assert len(fb._get_structured_output_fallback_exceptions()) > 0
