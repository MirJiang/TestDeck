"""APScheduler 定时调度：启动时从 Schedule 表装载 cron 任务。"""
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from . import config
from .db import SessionLocal
from .models import Schedule, TestPlan, Env

scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")


def _no_sched() -> bool:
    return bool(config.get("TD_NO_SCHEDULER"))  # 测试环境禁用，避免跨事件循环串扰


async def _fire(plan_id: str):
    db = SessionLocal()
    try:
        plan = db.get(TestPlan, plan_id)
        env = db.get(Env, plan.env_id) if plan and plan.env_id else None
        if not plan or not env:
            return
        from .routers.runs import execute_plan
        from .engine.queue import queued
        await queued(lambda: execute_plan(plan, env, trigger_by="cron"))
        s = db.query(Schedule).filter(Schedule.plan_id == plan_id).first()
        from .models import now
        if s:
            s.last_run_at = now()
            db.commit()
    finally:
        db.close()


def refresh():
    """重建调度表（计划增删改后调用）。"""
    if _no_sched():
        return
    scheduler.remove_all_jobs()
    db = SessionLocal()
    try:
        for s in db.query(Schedule).filter(Schedule.enabled == True):  # noqa
            plan = db.get(TestPlan, s.plan_id)
            if plan:
                scheduler.add_job(_fire, "cron", args=[plan.id], id=f"plan-{plan.id}",
                                  **_cron_fields(s.cron))
    finally:
        db.close()


def _cron_fields(expr: str) -> dict:
    parts = expr.split()
    if len(parts) != 5:
        raise ValueError(f"cron 表达式不合法: {expr}")
    return {"minute": parts[0], "hour": parts[1], "day": parts[2],
            "month": parts[3], "day_of_week": parts[4]}


def start():
    if _no_sched():
        return
    if not scheduler.running:
        refresh()
        scheduler.start()
