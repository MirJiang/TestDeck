"""失败通知：向钉钉/企微群机器人 webhook 推送文本消息（两种格式相同）。"""
import httpx
from .db import SessionLocal
from .models import NotifyChannel


async def notify_run(run) -> None:
    """执行失败时推送；任何异常静默（通知不能影响主流程）。"""
    try:
        db = SessionLocal()
        try:
            channels = db.query(NotifyChannel).filter(
                NotifyChannel.enabled == True, NotifyChannel.on_fail == True).all()  # noqa
        finally:
            db.close()
        if not channels or run.status != "failed":
            return
        src = run.plan_name or run.flow_name or run.case_name
        text = (f"[TestDeck] 测试失败提醒\n来源：{src}\n环境：{run.env_name}\n"
                f"结果：通过 {run.pass_n} · 失败 {run.fail_n}\n"
                f"触发：{run.trigger_by}\n时间：{run.created_at:%Y-%m-%d %H:%M}\n"
                f"报告：/runs 详情 {run.id}")
        async with httpx.AsyncClient(timeout=10) as c:
            for ch in channels:
                status = ""
                try:
                    r = await c.post(ch.url, json={"msgtype": "text", "text": {"content": text}})
                    status = f"{r.status_code} @ {run.created_at:%m-%d %H:%M}"
                except Exception as e:
                    status = f"失败({type(e).__name__}) @ {run.created_at:%m-%d %H:%M}"
                db = SessionLocal()
                try:
                    row = db.get(NotifyChannel, ch.id)
                    if row:
                        row.last_status = status
                        db.commit()
                finally:
                    db.close()
    except Exception:
        pass
