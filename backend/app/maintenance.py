"""维护任务：截图/旧报告清理 + 执行记录与 LLM 日志保留（每日一次 + 启动时）。"""
import time
from pathlib import Path

from . import config

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


def cleanup_screenshots() -> int:
    keep_days = int(config.get("TD_KEEP_DAYS") or 30)
    if not STATIC_DIR.exists():
        return 0
    cutoff = time.time() - keep_days * 86400
    n = 0
    for pattern in ("*.png", "*.webm"):   # 截图与执行录像一起按期清理
        for f in STATIC_DIR.glob(pattern):
            if f.stat().st_mtime < cutoff:
                f.unlink(); n += 1
    return n


def cleanup_runs() -> dict:
    """按保留天数清理执行记录与 LLM 日志（TD_RUN_KEEP_DAYS，默认 90；0=永久保留）。"""
    days = int(config.get("TD_RUN_KEEP_DAYS") or 90)
    if days <= 0:
        return {"runs": 0, "logs": 0}
    cutoff = time.time() - days * 86400
    from datetime import datetime
    cutoff_dt = datetime.utcfromtimestamp(cutoff)
    from .db import SessionLocal
    from .models import TestRun, LLMLog
    db = SessionLocal()
    try:
        runs = db.query(TestRun).filter(TestRun.created_at < cutoff_dt).all()
        for r in runs:
            db.delete(r)
        logs = db.query(LLMLog).filter(LLMLog.created_at < cutoff_dt).all()
        for l in logs:
            db.delete(l)
        db.commit()
        return {"runs": len(runs), "logs": len(logs)}
    except Exception:
        db.rollback()
        return {"runs": 0, "logs": 0}
    finally:
        db.close()


def register_daily():
    from .scheduler import scheduler as _sched
    _sched.add_job(cleanup_screenshots, "cron", hour=3, minute=0, id="cleanup-screenshots",
                   replace_existing=True)
    _sched.add_job(cleanup_runs, "cron", hour=3, minute=10, id="cleanup-runs",
                   replace_existing=True)
