from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from ..db import get_db
from ..models import User, Flow, TestRun, Env
from ..auth import current_user
from ..perms import check_project_access, accessible_project_ids

router = APIRouter(prefix="/api/v1/flows", tags=["flows"])

# update_flow 允许覆盖的字段：project_id 不在其中（校验基于原项目，放行改写等于可把流程搬进无权项目）
_FLOW_FIELDS = ("name", "desc", "roles", "steps")


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
    data = body.model_dump()
    for k in _FLOW_FIELDS:   # 白名单覆盖，project_id 等不可经接口改写
        setattr(f, k, data[k])
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
    from .runs import _exec_flow_sync
    f = db.get(Flow, fid)
    if not f:
        raise HTTPException(404, "流程不存在")
    check_project_access(f.project_id, user, db)
    env = db.get(Env, body.env_id)
    if not env or env.project_id != f.project_id:   # 环境必须属于该流程的项目，防跨项目引用
        raise HTTPException(400, "环境不存在")
    run = await _exec_flow_sync(f, env, f"user:{user.username}")
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


# ---------- 截图基线（视觉回归） ----------

def _baseline_files(fid: str):
    from ..engine.ui_runner import STATIC_DIR
    return sorted(STATIC_DIR.glob(f"base-{fid}-*.png"))


@router.get("/{fid}/baselines")
def list_baselines(fid: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    f = db.get(Flow, fid)
    if not f:
        raise HTTPException(404, "流程不存在")
    check_project_access(f.project_id, user, db)
    return [{"file": p.name, "url": f"/static/{p.name}"} for p in _baseline_files(fid)]


@router.delete("/{fid}/baselines")
def clear_baselines(fid: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """清除基线：改了流程步骤导致基线错位时用，下次成功执行会重新留存。"""
    f = db.get(Flow, fid)
    if not f:
        raise HTTPException(404, "流程不存在")
    check_project_access(f.project_id, user, db)
    n = 0
    for p in _baseline_files(fid):
        try:
            p.unlink(); n += 1
        except OSError:
            pass
    return {"ok": True, "removed": n}


@router.get("/runs/{rid}/detail")
def flow_run_detail(rid: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    r = db.get(TestRun, rid)
    if not r:
        raise HTTPException(404, "执行记录不存在")
    if not r.flow_id:
        raise HTTPException(404, "非流程执行记录")
    f = db.get(Flow, r.flow_id)
    if not f:
        raise HTTPException(404, "流程已删除，无法查看该执行记录")
    check_project_access(f.project_id, user, db)
    return _out(r)


def _out(r: TestRun, brief: bool = False) -> dict:
    out = {"id": r.id, "flow_id": r.flow_id, "flow_name": r.flow_name, "env_name": r.env_name,
           "status": r.status, "pass_n": r.pass_n, "fail_n": r.fail_n, "duration": r.duration,
           "trigger_by": r.trigger_by, "created_at": r.created_at.isoformat()}
    if not brief:
        out["detail"] = r.detail
    return out
