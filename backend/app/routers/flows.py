from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from ..db import get_db
from ..models import User, Project, Flow, TestRun, Env
from ..auth import current_user
from ..perms import check_project_access, accessible_project_ids
from ..engine.queue import queued
from ..engine.flow_runner import run_flow
from ..notify import notify_run

router = APIRouter(prefix="/api/v1/flows", tags=["flows"])


class FlowIn(BaseModel):
    project_id: str
    name: str
    desc: str = ""
    roles: list[dict] = []
    steps: list[dict] = []


@router.get("")
def list_flows(project_id: str = "", db: Session = Depends(get_db), user: User = Depends(current_user)):
    ids = set(accessible_project_ids(user, db))
    q = db.query(Flow).order_by(Flow.updated_at.desc())
    out = []
    for f in q.all():
        if f.project_id not in ids:
            continue
        if project_id and f.project_id != project_id:
            continue
        out.append({"id": f.id, "project_id": f.project_id, "name": f.name, "desc": f.desc,
                    "roles": len(f.roles or []), "steps": len(f.steps or []),
                    "updated_at": f.updated_at.isoformat()})
    return out


@router.get("/{fid}")
def get_flow(fid: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    f = db.get(Flow, fid)
    if not f:
        raise HTTPException(404, "流程不存在")
    check_project_access(f.project_id, user, db)
    return {"id": f.id, "project_id": f.project_id, "name": f.name, "desc": f.desc,
            "roles": f.roles, "steps": f.steps, "updated_at": f.updated_at.isoformat()}


@router.post("")
def create_flow(body: FlowIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    check_project_access(body.project_id, user, db)
    f = Flow(**body.model_dump())
    db.add(f); db.commit()
    return {"id": f.id}


@router.put("/{fid}")
def update_flow(fid: str, body: FlowIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    f = db.get(Flow, fid)
    if not f:
        raise HTTPException(404, "流程不存在")
    check_project_access(f.project_id, user, db)
    for k, v in body.model_dump().items():
        setattr(f, k, v)
    db.commit()
    return {"ok": True}


@router.delete("/{fid}")
def delete_flow(fid: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    f = db.get(Flow, fid)
    if not f:
        raise HTTPException(404, "流程不存在")
    check_project_access(f.project_id, user, db)
    db.delete(f); db.commit()
    return {"ok": True}


class RunIn(BaseModel):
    env_id: str


@router.post("/{fid}/run")
async def run_flow_api(fid: str, body: RunIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    import time as _t
    f = db.get(Flow, fid)
    if not f:
        raise HTTPException(404, "流程不存在")
    check_project_access(f.project_id, user, db)
    env = db.get(Env, body.env_id)
    if not env:
        raise HTTPException(400, "环境不存在")
    run = TestRun(id=f"R-{int(_t.time() * 1000) % 10**9:09d}", flow_id=f.id, flow_name=f.name,
                  env_id=env.id, env_name=env.name, trigger_by=f"user:{user.username}")
    db.add(run); db.commit()
    r = await queued(lambda: run_flow(f, env, run.id))
    run.status, run.pass_n, run.fail_n = r["status"], r["pass_n"], r["fail_n"]
    run.duration, run.detail = r["duration"], r["detail"]
    db.commit()
    await notify_run(run)
    return _out(run)


@router.get("/{fid}/runs")
def list_flow_runs(fid: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    f = db.get(Flow, fid)
    if not f:
        raise HTTPException(404, "流程不存在")
    check_project_access(f.project_id, user, db)
    rows = (db.query(TestRun).filter(TestRun.flow_id == fid)
            .order_by(TestRun.created_at.desc()).limit(20).all())
    return [_out(r, brief=True) for r in rows]


@router.get("/runs/{rid}/detail")
def flow_run_detail(rid: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    r = db.get(TestRun, rid)
    if not r:
        raise HTTPException(404, "执行记录不存在")
    if not r.flow_id:
        raise HTTPException(404, "非流程执行记录")
    f = db.get(Flow, r.flow_id)
    check_project_access(f.project_id, user, db)
    return _out(r)


def _out(r: TestRun, brief: bool = False) -> dict:
    out = {"id": r.id, "flow_id": r.flow_id, "flow_name": r.flow_name, "env_name": r.env_name,
           "status": r.status, "pass_n": r.pass_n, "fail_n": r.fail_n, "duration": r.duration,
           "trigger_by": r.trigger_by, "created_at": r.created_at.isoformat()}
    if not brief:
        out["detail"] = r.detail
    return out
