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
    """重建调度表（计划增删改后调用）。单条装载体失败只跳过该条，不影响其余任务。"""
    if _no_sched():
        return
    scheduler.remove_all_jobs()
    db = SessionLocal()
    try:
        for s in db.query(Schedule).filter(Schedule.enabled == True):  # noqa
            plan = db.get(TestPlan, s.plan_id)
            if plan:
                try:
                    scheduler.add_job(_fire, "cron", args=[plan.id], id=f"plan-{plan.id}",
                                      **_cron_fields(s.cron))
                except Exception as e:   # 单条非法 cron 只跳过，绝不让 refresh 整体失败
                    print(f"[scheduler] 计划 {plan.id} 的 cron「{s.cron}」无法装载，已跳过：{e}")
    finally:
        db.close()


def validate_cron(expr: str) -> str | None:
    """校验 5 段 cron 表达式，合法返回 None，非法返回原因（供入参校验复用 APScheduler 的解析器）。"""
    try:
        from apscheduler.triggers.cron import CronTrigger
        CronTrigger.from_crontab(expr)
        return None
    except Exception as e:
        return f"cron 表达式不合法：{e}"[:120]


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
        try:
            refresh()
        except Exception as e:   # 调度装载失败不能阻断应用启动（执行等核心功能照常可用）
            print(f"[scheduler] 启动装载调度失败：{e}")
        scheduler.start()
