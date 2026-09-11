from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from ..db import get_db
from ..models import User, TestPlan
from ..auth import current_user
from ..perms import check_project_access, accessible_project_ids

router = APIRouter(prefix="/api/v1/plans", tags=["plans"])


class PlanIn(BaseModel):
    project_id: str
    name: str
    case_ids: list[str] = []
    flow_ids: list[str] = []
    env_id: str = ""
    trigger: str = "manual"  # manual | cron
    cron: str = ""
    enabled: bool = True


@router.get("")
def list_plans(project_id: str = "", db: Session = Depends(get_db), user: User = Depends(current_user)):
    ids = set(accessible_project_ids(user, db))
    q = db.query(TestPlan).order_by(TestPlan.created_at.desc())
    plans = [p for p in q.all() if p.project_id in ids]
    if project_id:
        plans = [p for p in plans if p.project_id == project_id]
    return [{"id": p.id, "project_id": p.project_id, "name": p.name, "case_ids": p.case_ids,
             "flow_ids": p.flow_ids or [], "env_id": p.env_id, "trigger": p.trigger,
             "cron": p.cron, "enabled": p.enabled,
             "created_at": p.created_at.isoformat()} for p in plans]


@router.post("")
def create_plan(body: PlanIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    check_project_access(body.project_id, user, db)
    p = TestPlan(**body.model_dump())
    db.add(p); db.commit()
    sync_schedule(db, p)
    return {"id": p.id}


@router.put("/{pid}")
def update_plan(pid: str, body: PlanIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    p = db.get(TestPlan, pid)
    if not p:
        raise HTTPException(404, "计划不存在")
    check_project_access(p.project_id, user, db)
    for k, v in body.model_dump().items():
        setattr(p, k, v)
    db.commit()
    sync_schedule(db, p)
    return {"ok": True}


@router.delete("/{pid}")
def delete_plan(pid: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    p = db.get(TestPlan, pid)
    if not p:
        raise HTTPException(404, "计划不存在")
    check_project_access(p.project_id, user, db)
    from ..models import Schedule
    for s in db.query(Schedule).filter(Schedule.plan_id == pid):
        db.delete(s)
    db.delete(p); db.commit()
    from ..scheduler import refresh
    refresh()   # 同步移除 APScheduler 中已删计划的任务
    return {"ok": True}


def sync_schedule(db: Session, plan: TestPlan):
    """计划保存后同步 cron 调度（增/改/删）。"""
    from ..models import Schedule
    s = db.query(Schedule).filter(Schedule.plan_id == plan.id).first()
    if plan.trigger == "cron" and plan.cron and plan.enabled:
        if s:
            s.cron, s.enabled = plan.cron, True
        else:
            db.add(Schedule(plan_id=plan.id, cron=plan.cron, enabled=True))
    elif s:
        db.delete(s)
    db.commit()
    from ..scheduler import refresh
    refresh()
