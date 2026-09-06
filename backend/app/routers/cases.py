from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from ..db import get_db
from ..models import User, Project, TestCase
from ..auth import current_user
from ..perms import check_project_access, accessible_project_ids

router = APIRouter(prefix="/api/v1", tags=["cases"])

VALID_CHECKS = {"status", "contains", "field_eq", "not_empty"}


class StepIn(BaseModel):
    m: str = "GET"
    url: str = ""
    headers: str = ""
    body: str = ""
    check: dict = {}
    save: dict = {}
    continue_on_fail: bool = False
    # UI 用例字段
    action: str = ""
    selector: str = ""
    value: str = ""


class CaseIn(BaseModel):
    project_id: str
    name: str
    type: str = "ai"           # 统一为 ai（api/ui 为历史数据保留类型）
    steps: list[StepIn] = []
    source: str = "manual"
    # AI 用例专用（type=ai 时存为 steps[0]）
    target: str = "ui"         # api | ui，AI 测试的对象
    goal: str = ""
    start_url: str = ""
    max_steps: int = 20
    engine: str = ""
    endpoints: list[dict] = []         # 引用的接口文档端点（喂给 AI 的真实接口清单）
    fixed_steps: list[dict] = []       # UI 固定步骤（录制/固化）：存在且无 goal 时回放（零 token）
    fixed_api_steps: list[dict] = []   # API 固定请求（固化）：存在且无 goal 时回放（零 token）


def _steps_for_store(body: CaseIn) -> list[dict]:
    if body.type == "ai":
        return [{"target": (body.target or "ui").strip().lower(),
                 "goal": body.goal, "start_url": body.start_url,
                 "max_steps": body.max_steps,
                 "engine": (body.engine or "").strip().lower(),
                 "endpoints": body.endpoints or [],
                 "fixed_steps": body.fixed_steps or [],
                 "fixed_api_steps": body.fixed_api_steps or []}]
    return [s.model_dump() for s in body.steps]


def validate_steps(steps, case_type="api"):
    from ..engine.ui_runner import ACTIONS
    if case_type == "ai":
        return
    for s in steps:
        if case_type == "ui":
            if s.action and s.action not in ACTIONS:
                raise HTTPException(400, f"不支持的 UI 动作：{s.action}")
        elif s.check.get("type") and s.check["type"] not in VALID_CHECKS:
            raise HTTPException(400, f"不支持的检查点类型：{s.check['type']}")


@router.get("/projects/{pid}/cases")
def list_cases(pid: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    check_project_access(pid, user, db)
    cs = db.query(TestCase).filter(TestCase.project_id == pid).order_by(TestCase.updated_at.desc()).all()
    out = []
    for c in cs:
        item = {"id": c.id, "name": c.name, "type": c.type, "steps": len(c.steps or []),
                "source": c.source, "updated_at": c.updated_at.isoformat()}
        if c.type == "ai":
            s = (c.steps or [{}])[0] if c.steps else {}
            item["target"] = (s.get("target") or "ui")
            item["fixed"] = bool(s.get("fixed_steps"))
        out.append(item)
    return out


@router.post("/projects/{pid}/cases")
def create_case(pid: str, body: CaseIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    check_project_access(pid, user, db)
    if body.project_id != pid:
        raise HTTPException(400, "项目不匹配")
    validate_steps(body.steps, body.type)
    c = TestCase(project_id=pid, name=body.name, type=body.type,
                steps=_steps_for_store(body), source=body.source, creator_id=user.id)
    db.add(c); db.commit()
    return {"id": c.id}


@router.get("/cases/{cid}")
def get_case(cid: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    c = db.get(TestCase, cid)
    if not c:
        raise HTTPException(404, "用例不存在")
    check_project_access(c.project_id, user, db)
    return {"id": c.id, "project_id": c.project_id, "name": c.name, "type": c.type,
            "steps": c.steps, "source": c.source, "updated_at": c.updated_at.isoformat()}


@router.put("/cases/{cid}")
def update_case(cid: str, body: CaseIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    c = db.get(TestCase, cid)
    if not c:
        raise HTTPException(404, "用例不存在")
    check_project_access(c.project_id, user, db)
    validate_steps(body.steps, body.type)
    c.name, c.type, c.steps = body.name, body.type, _steps_for_store(body)
    db.commit()
    return {"ok": True}


@router.post("/cases/{cid}/copy")
def copy_case(cid: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """复制用例（含 AI 配置与固定步骤）。"""
    c = db.get(TestCase, cid)
    if not c:
        raise HTTPException(404, "用例不存在")
    check_project_access(c.project_id, user, db)
    nc = TestCase(project_id=c.project_id, name=c.name + "（副本）", type=c.type,
                 steps=c.steps, source=c.source, creator_id=user.id)
    db.add(nc); db.commit()
    return {"id": nc.id}


@router.delete("/cases/{cid}")
def delete_case(cid: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    c = db.get(TestCase, cid)
    if not c:
        raise HTTPException(404, "用例不存在")
    check_project_access(c.project_id, user, db)
    db.delete(c); db.commit()
    return {"ok": True}


# ---------- UI 用例录制 ----------

from fastapi import Query, Response


class RecordCmdIn(BaseModel):
    op: str                    # click | fill | goto | scroll | back | finish
    x: int = 0
    y: int = 0
    url: str = ""
    selector: str = ""
    value: str = ""
    dy: int = 0


@router.post("/cases/ui-record/start")
def ui_record_start(url: str = Query(...), mode: str = Query("remote"), user: User = Depends(current_user)):
    if not url.startswith("http"):
        raise HTTPException(400, "请提供完整地址（http(s)://…）")
    from ..engine import ui_recorder
    import time as _t
    sid = f"rec-{int(_t.time() * 1000) % 10**9}"
    try:
        ui_recorder.start_recording(sid, url, mode=mode)
    except RuntimeError as e:
        raise HTTPException(429, str(e))
    return {"session": sid, "mode": "local" if mode == "local" else "remote"}


@router.get("/cases/ui-record/{sid}/frame")
def ui_record_frame(sid: str, user: User = Depends(current_user)):
    """录制画面帧（JPEG），前端轮询拼出实时画面。"""
    from ..engine.ui_recorder import get_frame
    data = get_frame(sid)
    if data is None:
        raise HTTPException(404, "录制会话不存在")
    if not data:
        raise HTTPException(503, "画面尚未就绪")
    return Response(content=data, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@router.post("/cases/ui-record/{sid}/cmd")
def ui_record_cmd(sid: str, body: RecordCmdIn, user: User = Depends(current_user)):
    """在录制中的页面上执行一条用户指令（点击/输入/跳转/滚动/完成）。"""
    from ..engine.ui_recorder import send_cmd
    return send_cmd(sid, body.model_dump())


@router.get("/cases/ui-record/{sid}")
def ui_record_poll(sid: str, user: User = Depends(current_user)):
    from ..engine.ui_recorder import compile_steps
    return compile_steps(sid)
