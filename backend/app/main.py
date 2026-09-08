from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from . import config
from .db import engine, SessionLocal, Base
from . import models  # noqa 确保建表
from .auth import hash_pw
from .models import User
from .routers import auth, projects, cases, plans, runs, git, ai, settings, flows
from .routers import api_docs
from . import scheduler


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(engine)
    _migrate()
    _seed()
    scheduler.start()
    from .maintenance import register_daily, cleanup_screenshots
    register_daily()
    cleanup_screenshots()
    if (config.get("TD_MCP") or "1") != "0":
        from .mcp_server import mcp
        async with mcp.session_manager.run():   # MCP 会话管理器随应用启停
            yield
    else:
        yield


def _migrate():
    """轻量迁移：为已有库补齐后加的列（create_all 不会改表）。"""
    from sqlalchemy import inspect, text
    insp = inspect(engine)
    if "llm_configs" in insp.get_table_names():
        cols = {c["name"] for c in insp.get_columns("llm_configs")}
        with engine.begin() as conn:
            if "vision" not in cols:
                if engine.dialect.name == "postgresql":
                    conn.execute(text("ALTER TABLE llm_configs ADD COLUMN vision BOOLEAN DEFAULT FALSE"))
                else:  # sqlite / mysql
                    conn.execute(text("ALTER TABLE llm_configs ADD COLUMN vision BOOLEAN DEFAULT 0"))
    # 计划支持包含流程；流程执行记录统一并入 test_runs；用例绑定测试账号；
    # 应用地图加来源/角色/状态备注（地图=期望基线，多来源写入）
    for table, column, ddl in [
        ("test_plans", "flow_ids", "JSON"),
        ("test_runs", "flow_id", "VARCHAR(64)"),
        ("test_runs", "flow_name", "VARCHAR(255)"),
        ("test_cases", "username", "VARCHAR(255) DEFAULT ''"),
        ("test_cases", "password", "VARCHAR(255) DEFAULT ''"),
        ("app_pages", "source", "VARCHAR(16) DEFAULT 'scan'"),
        ("app_pages", "roles", "VARCHAR(255) DEFAULT ''"),
        ("app_elements", "source", "VARCHAR(16) DEFAULT 'scan'"),
        ("app_elements", "state_note", "VARCHAR(255) DEFAULT ''"),
        ("app_elements", "roles", "VARCHAR(255) DEFAULT ''"),
    ]:
        if table in insp.get_table_names():
            cols = {c["name"] for c in insp.get_columns(table)}
            if column not in cols:
                with engine.begin() as conn:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
    _migrate_flow_runs()


def _migrate_flow_runs():
    """一次性迁移：旧 flow_runs 表数据搬进 test_runs，然后删表（幂等，表不存在即跳过）。"""
    import json as _json
    from datetime import datetime
    from sqlalchemy import inspect, text
    from .models import TestRun
    if "flow_runs" not in inspect(engine).get_table_names():
        return
    with engine.begin() as conn:
        rows = conn.execute(text(
            "SELECT id, flow_id, flow_name, env_id, env_name, status, pass_n, fail_n, "
            "duration, detail, trigger_by, created_at FROM flow_runs")).mappings().all()
    db = SessionLocal()
    try:
        for r in rows:
            if db.get(TestRun, r["id"]):
                continue
            detail = r["detail"]
            if isinstance(detail, str):
                try:
                    detail = _json.loads(detail)
                except Exception:
                    detail = []
            created = r["created_at"]
            if isinstance(created, str):
                try:
                    created = datetime.fromisoformat(created)
                except Exception:
                    created = datetime.utcnow()
            db.add(TestRun(id=r["id"], flow_id=r["flow_id"], flow_name=r["flow_name"] or "",
                           env_id=r["env_id"], env_name=r["env_name"] or "",
                           status=r["status"] or "failed", pass_n=r["pass_n"] or 0,
                           fail_n=r["fail_n"] or 0, duration=r["duration"] or 0.0,
                           detail=detail, trigger_by=r["trigger_by"] or "user", created_at=created))
        db.commit()
        with engine.begin() as conn:
            conn.execute(text("DROP TABLE flow_runs"))
    finally:
        db.close()


def _seed():
    db = SessionLocal()
    try:
        if not db.query(User).filter(User.username == "admin").first():
            db.add(User(username="admin", password_hash=hash_pw(config.get("TD_ADMIN_PASSWORD") or "admin123"), role="admin"))
            db.commit()
        if config.get("TD_SEED_DEMO", "1") != "0":
            _seed_demo(db)
    finally:
        db.close()


def _seed_demo(db):
    """预置演示：询价单全流程（货主 + 两家物流公司），配合 tests/mock_target.py 体验。"""
    from .models import Project, Env, Flow
    if db.query(Flow).count() or db.query(Project).filter(Project.name == "演示 · 物流平台").count():
        return
    p = Project(name="演示 · 物流平台", desc="内置演示：询价单业务线（需启动 mock 被测系统）", owner_id="demo")
    db.add(p); db.commit()
    e = Env(project_id=p.id, name="测试", base_url="http://127.0.0.1:9001", variables={})
    db.add(e); db.commit()
    roles = [
        {"key": "shipper", "name": "货主", "variables": {"username": "sw01", "password": "******"}},
        {"key": "carrierA", "name": "物流公司A", "variables": {"username": "ck_a", "password": "******"}},
        {"key": "carrierB", "name": "物流公司B", "variables": {"username": "ck_b", "password": "******"}},
    ]
    steps = [
        {"role": "shipper", "type": "api", "m": "POST", "url": "/api/inquiry/create",
         "body": '{"from": "上海", "to": "北京"}', "check": {"type": "field_eq", "field": "code", "expect": "0"},
         "save": {"name": "inquiry_id", "from": "data.id"}},
        {"role": "carrierA", "type": "ui", "action": "goto", "url": "/page/inquiry/quote?id=${inquiry_id}&carrier=A", "selector": "", "value": ""},
        {"role": "carrierA", "type": "ui", "action": "click", "selector": "#quote", "url": "", "value": ""},
        {"role": "carrierA", "type": "ui", "action": "expect_text", "value": "报价成功", "url": "", "selector": ""},
        {"role": "carrierB", "type": "ui", "action": "goto", "url": "/page/inquiry/quote?id=${inquiry_id}&carrier=B", "selector": "", "value": ""},
        {"role": "carrierB", "type": "ui", "action": "click", "selector": "#quote", "url": "", "value": ""},
        {"role": "carrierB", "type": "ui", "action": "expect_text", "value": "报价成功", "url": "", "selector": ""},
        {"role": "shipper", "type": "ui", "action": "goto", "url": "/page/inquiry/result", "selector": "", "value": ""},
        {"role": "shipper", "type": "ui", "action": "expect_text", "value": "A、B", "url": "", "selector": ""},
        {"role": "shipper", "type": "ui", "action": "screenshot", "url": "", "selector": "", "value": ""},
    ]
    db.add(Flow(project_id=p.id, name="询价单全流程", desc="货主发单 → 两家物流各自登录报价 → 货主查看结果", roles=roles, steps=steps))
    db.commit()


app = FastAPI(title="TestDeck", version="0.1.0", lifespan=lifespan)
_cors = [o.strip() for o in config.get("TD_CORS_ORIGINS", "*").split(",") if o.strip()]
app.add_middleware(CORSMiddleware, allow_origins=_cors, allow_methods=["*"], allow_headers=["*"])
from fastapi.staticfiles import StaticFiles
from fastapi.responses import PlainTextResponse
from pathlib import Path as _P
_static = _P(__file__).resolve().parent.parent / "static"
_static.mkdir(exist_ok=True)


class _AuthedStaticFiles(StaticFiles):
    """执行截图/录像可能含被测系统页面与业务数据：仅登录用户可访问。

    鉴权双通道：Authorization: Bearer（API/程序访问）或登录时下发的 td_token Cookie
    （<img>/<video> 标签带不了请求头，靠浏览器自动带 Cookie）。"""
    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            from .auth import resolve_token
            from .db import SessionLocal
            headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                       for k, v in scope.get("headers", [])}
            token = ""
            auth = headers.get("authorization", "")
            if auth.lower().startswith("bearer "):
                token = auth[7:]
            if not token:
                for part in headers.get("cookie", "").split(";"):
                    k, _, v = part.strip().partition("=")
                    if k == "td_token":
                        token = v
                        break
            db = SessionLocal()
            try:
                user = resolve_token(token, db) if token else None
            except Exception:
                user = None
            finally:
                db.close()
            if user is None:
                resp = PlainTextResponse("需要登录后访问", status_code=403)
                await resp(scope, receive, send)
                return
        await super().__call__(scope, receive, send)


app.mount("/static", _AuthedStaticFiles(directory=str(_static)), name="static")
app.include_router(auth.router)
app.include_router(projects.router)
app.include_router(cases.router)
app.include_router(plans.router)
app.include_router(runs.router)
app.include_router(git.router)
app.include_router(ai.router)
app.include_router(settings.router)
app.include_router(flows.router)
app.include_router(api_docs.router)

# MCP 服务（Streamable HTTP）：外部 AI agent 连 http://<host>:8000/mcp，
# 请求头 Authorization: Bearer <平台登录 token> 鉴权
if (config.get("TD_MCP") or "1") != "0":
    from .mcp_server import mcp_app
    app.mount("/mcp", mcp_app)


@app.get("/api/v1/health")
def health():
    return {"ok": True, "sched": scheduler.scheduler.running}
