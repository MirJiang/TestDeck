import time
import csv
import io
import html as _html
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session
from ..db import get_db, SessionLocal
from ..models import User, TestCase, TestPlan, TestRun, Env, Flow
from ..auth import current_user
from ..perms import check_project_access
from ..engine.runner import run_case
from ..engine.ui_runner import run_ui_case
from ..engine.ai_runner import run_ai_case
from ..engine.queue import queued, register, cancel, unregister, is_cancelled
from ..notify import notify_run

router = APIRouter(prefix="/api/v1/runs", tags=["runs"])


class RunIn(BaseModel):
    env_id: str


def _new_run_id() -> str:
    return f"R-{int(time.time() * 1000) % 10**9:09d}"


def _push_run_detail(run_id: str, detail: list):
    """把进行中的明细增量写库，供前端轮询实现流式进度。"""
    db = SessionLocal()
    try:
        row = db.get(TestRun, run_id)
        if row:
            row.detail = detail
            db.commit()
    finally:
        db.close()


async def _exec_case_sync(c: TestCase, env: Env, trigger_by: str) -> TestRun:
    """完整执行一个用例（建记录 → 队列执行 → 回写 → 通知）。REST 与 MCP 共用。"""
    run = TestRun(id=_new_run_id(), case_id=c.id, case_name=c.name,
                  env_id=env.id, env_name=env.name, trigger_by=trigger_by)
    db = SessionLocal()
    try:
        db.add(run)
        db.commit()

        def _exec():
            if c.type == "ui":
                return run_ui_case(c, env, run.id)
            if c.type == "ai":
                return run_ai_case(c, env, run.id, on_step=lambda d: _push_run_detail(run.id, d))
            return run_case(c, env)

        register(run.id)
        try:
            r = await queued(_exec)
        finally:
            unregister(run.id)
        run.status, run.pass_n, run.fail_n = r["status"], r["pass_n"], r["fail_n"]
        run.duration, run.detail = r["duration"], r["detail"]
        db.commit()
        await notify_run(run)
        return run
    finally:
        db.close()


async def _exec_flow_sync(f: Flow, env: Env | None, trigger_by: str) -> TestRun:
    """完整执行一个流程（建记录 → 预载引用用例 → 队列执行 → 回写 → 通知）。REST 与 MCP 共用。"""
    from ..engine.flow_runner import run_flow
    run = TestRun(id=_new_run_id(), flow_id=f.id, flow_name=f.name,
                  env_id=env.id if env else None, env_name=env.name if env else "",
                  trigger_by=trigger_by)
    db = SessionLocal()
    try:
        db.add(run)
        db.commit()
        cases = {c.id: c for c in db.query(TestCase).filter(TestCase.project_id == f.project_id)}
        register(run.id)

        def _exec():
            return run_flow(f, env, run.id, on_step=lambda d: _push_run_detail(run.id, d), cases=cases)

        try:
            r = await queued(_exec)
        finally:
            unregister(run.id)
        run.status, run.pass_n, run.fail_n = r["status"], r["pass_n"], r["fail_n"]
        run.duration, run.detail = r["duration"], r["detail"]
        db.commit()
        await notify_run(run)
        return run
    finally:
        db.close()


def execute_plan(plan: TestPlan, env: Env, trigger_by: str = "cron") -> TestRun:
    """同步执行整个计划：先逐条用例、再逐条流程（条目失败不中断批次），写一条 TestRun。

    每条流程另写一条独立执行记录（只带 flow_id，不带 plan_id，
    避免干扰前端对计划记录的轮询），流程历史里也能看到计划触发的执行。
    """
    from ..engine.flow_runner import run_flow
    db = SessionLocal()
    try:
        run = TestRun(id=_new_run_id(), plan_id=plan.id, plan_name=plan.name,
                      env_id=env.id, env_name=env.name, trigger_by=trigger_by)
        db.add(run); db.commit()
        total_p = total_f = 0
        t0 = time.time()
        results = []
        for cid in plan.case_ids or []:
            if is_cancelled(run.id):   # 计划级取消：当前条目完成后不再继续
                break
            c = db.get(TestCase, cid)
            if not c:
                continue
            if c.type == "ui":
                r = run_ui_case(c, env, run.id)
            elif c.type == "ai":
                r = run_ai_case(c, env, run.id,
                                on_step=lambda d, _rid=run.id: _push_run_detail(_rid, d))
            else:
                r = run_case(c, env)
            total_p += r["pass_n"]; total_f += r["fail_n"]
            results.append({"case_id": c.id, "name": c.name, "pass": r["fail_n"] == 0,
                            "pass_n": r["pass_n"], "fail_n": r["fail_n"], "detail": r["detail"]})
            run.detail = list(results)
            db.commit()   # 每完成一条用例就落库，计划执行也能流式看到进度
        for fid in plan.flow_ids or []:
            if is_cancelled(run.id):
                break
            f = db.get(Flow, fid)
            if not f:
                continue
            sub = TestRun(id=_new_run_id(), flow_id=f.id, flow_name=f.name,
                          env_id=env.id, env_name=env.name, trigger_by=trigger_by)
            db.add(sub); db.commit()
            cases = {c.id: c for c in db.query(TestCase).filter(TestCase.project_id == f.project_id)}
            r = run_flow(f, env, sub.id,
                         on_step=lambda d, _sid=sub.id: _push_run_detail(_sid, d),
                         cases=cases, cancel_for=run.id)   # 取消计划也能中断进行中的流程
            sub.status, sub.pass_n, sub.fail_n = r["status"], r["pass_n"], r["fail_n"]
            sub.duration, sub.detail = r["duration"], r["detail"]
            total_p += r["pass_n"]; total_f += r["fail_n"]
            results.append({"flow_id": f.id, "run_id": sub.id, "name": f.name,
                            "pass": r["fail_n"] == 0, "pass_n": r["pass_n"], "fail_n": r["fail_n"],
                            "detail": r["detail"]})
            run.detail = list(results)
            db.commit()
        run.pass_n, run.fail_n = total_p, total_f
        run.duration = round(time.time() - t0, 2)
        run.status = "failed" if total_f else "passed"
        run.detail = results
        db.commit()
        db.refresh(run)
        return run
    finally:
        db.close()


@router.post("/cases/{cid}/run")
async def run_single(cid: str, body: RunIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    c = db.get(TestCase, cid)
    if not c:
        raise HTTPException(404, "用例不存在")
    check_project_access(c.project_id, user, db)
    env = db.get(Env, body.env_id)
    if not env:
        raise HTTPException(400, "环境不存在")
    run = await _exec_case_sync(c, env, f"user:{user.username}")
    return _run_out(run)


@router.post("/plans/{pid}/run")
async def run_plan(pid: str, body: RunIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    plan = db.get(TestPlan, pid)
    if not plan:
        raise HTTPException(404, "计划不存在")
    check_project_access(plan.project_id, user, db)
    env = db.get(Env, body.env_id or plan.env_id)
    if not env:
        raise HTTPException(400, "环境不存在")
    result = await queued(lambda: execute_plan(plan, env, trigger_by=f"user:{user.username}"))
    await notify_run(result)
    return _run_out(result)


@router.post("/{rid}/cancel")
def cancel_run(rid: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """取消正在执行的 AI 用例/计划（在当前步骤完成后生效）。"""
    r = db.get(TestRun, rid)
    if not r:
        raise HTTPException(404, "执行记录不存在")
    _authorize_run(r, user, db)
    cancel(rid)
    return {"ok": True}


@router.get("/export.csv")
def export_runs_csv(db: Session = Depends(get_db), user: User = Depends(current_user)):
    from ..perms import accessible_project_ids
    ids = set(accessible_project_ids(user, db))
    allowed_case = {c.id for c in db.query(TestCase).filter(TestCase.project_id.in_(ids))}
    allowed_plan = {pl.id for pl in db.query(TestPlan).filter(TestPlan.project_id.in_(ids))}
    allowed_flow = {f.id for f in db.query(Flow).filter(Flow.project_id.in_(ids))}
    q = db.query(TestRun).order_by(TestRun.created_at.desc()).limit(2000)
    rows = [r for r in q.all()
            if (r.case_id and r.case_id in allowed_case) or (r.plan_id and r.plan_id in allowed_plan)
            or (r.flow_id and r.flow_id in allowed_flow)]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["执行ID", "类型", "来源", "环境", "状态", "通过", "失败", "耗时s", "触发", "时间"])
    for r in rows:
        kind = "计划" if r.plan_id else ("流程" if r.flow_id else "用例")
        w.writerow([r.id, kind, r.plan_name or r.flow_name or r.case_name, r.env_name, r.status,
                    r.pass_n, r.fail_n, r.duration, r.trigger_by,
                    r.created_at.strftime("%Y-%m-%d %H:%M")])
    return Response(content="\ufeff" + buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=runs.csv"})


@router.get("")
def list_runs(plan: str = "", case: str = "", flow: str = "", page: int = 1, size: int = 20,
              db: Session = Depends(get_db), user: User = Depends(current_user)):
    q = db.query(TestRun).order_by(TestRun.created_at.desc(), TestRun.id.desc())
    if plan:
        q = q.filter(TestRun.plan_id == plan)
    if case:
        q = q.filter(TestRun.case_id == case)
    if flow:
        q = q.filter(TestRun.flow_id == flow)
    if user.role != "admin":  # member 只见自己项目的执行记录
        from ..perms import accessible_project_ids
        ids = set(accessible_project_ids(user, db))
        allowed_case = {c.id for c in db.query(TestCase).filter(TestCase.project_id.in_(ids))}
        allowed_plan = {pl.id for pl in db.query(TestPlan).filter(TestPlan.project_id.in_(ids))}
        allowed_flow = {f.id for f in db.query(Flow).filter(Flow.project_id.in_(ids))}
        q = q.filter((TestRun.case_id.in_(allowed_case) | TestRun.plan_id.in_(allowed_plan)
                      | TestRun.flow_id.in_(allowed_flow)))
    total = q.count()
    rows = q.offset((page - 1) * size).limit(size).all()
    return {"total": total, "items": [_run_out(r, brief=True) for r in rows]}


@router.get("/{rid}/export")
def export_run(rid: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """导出独立 HTML 报告（可直接发给同事或存档）。"""
    r = db.get(TestRun, rid)
    if not r:
        raise HTTPException(404, "执行记录不存在")
    _authorize_run(r, user, db)
    esc = lambda x: _html.escape(str(x or ""))  # 被测系统返回的内容不可信，全部转义防 XSS
    rows = ""
    for item in r.detail or []:
        if item.get("case_id") or item.get("flow_id"):  # 计划：每项是一条用例或流程
            kind = "流程" if item.get("flow_id") else "用例"
            rows += (f"<tr><td>{kind} · {esc(item['name'])}</td><td>{'✓ 通过' if item['pass'] else '✗ 失败'}</td>"
                     f"<td>{item['pass_n']}/{item['pass_n'] + item['fail_n']}</td></tr>")
    # 单用例 / 单流程：detail 即步骤
    step_rows = ""
    for s in (r.detail or []) if (r.case_id or r.flow_id) else []:
        warn = f"<div style='color:#b8860b'>⚠ {esc(s.get('warning'))}</div>" if s.get("warning") else ""
        step_rows += (f"<tr><td class='m'>{esc(s.get('m') or s.get('action') or s.get('type'))} {esc(s.get('url') or s.get('target', ''))}</td>"
                      f"<td>{'✓' if s.get('pass') else '✗'} {esc(s.get('reason'))}{warn}</td></tr>")
    html = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8"><title>TestDeck 报告 {r.id}</title>
<style>body{{font-family:sans-serif;margin:40px;color:#333}}table{{border-collapse:collapse;width:100%;margin:16px 0}}
td,th{{border:1px solid #ddd;padding:8px 12px;font-size:14px;text-align:left}}.m{{font-family:monospace}}
h1{{font-size:20px}}.meta{{color:#888;font-size:13px}}</style></head><body>
<h1>执行报告 {esc(r.id)} · {'全部通过 ✓' if r.status == 'passed' else '存在失败 ✗'}</h1>
<div class="meta">来源：{esc(r.plan_name or r.flow_name or r.case_name)} · 环境：{esc(r.env_name)} · 触发：{esc(r.trigger_by)}
· 通过 {r.pass_n} / 失败 {r.fail_n} · 耗时 {r.duration}s · {r.created_at:%Y-%m-%d %H:%M}</div>
{('<table><tr><th>用例</th><th>结果</th><th>检查点</th></tr>' + rows + '</table>') if rows else ''}
{('<table><tr><th>步骤</th><th>结果</th></tr>' + step_rows + '</table>') if step_rows else ''}
<p class="meta">由 TestDeck 生成</p></body></html>"""
    return Response(content=html, media_type="text/html",
                    headers={"Content-Disposition": f"attachment; filename=report-{r.id}.html"})


@router.get("/{rid}")
def get_run(rid: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    r = db.get(TestRun, rid)
    if not r:
        raise HTTPException(404, "执行记录不存在")
    _authorize_run(r, user, db)
    return _run_out(r)


def _authorize_run(r: TestRun, user, db) -> None:
    """执行记录的项目归属校验：admin 放行，member 只能看自己项目的记录。"""
    from ..perms import accessible_project_ids
    from ..models import TestCase, TestPlan
    if user.role == "admin":
        return
    ids = set(accessible_project_ids(user, db))
    pid = None
    if r.plan_id and db.get(TestPlan, r.plan_id):
        pid = db.get(TestPlan, r.plan_id).project_id
    elif r.flow_id and db.get(Flow, r.flow_id):
        pid = db.get(Flow, r.flow_id).project_id
    elif r.case_id and db.get(TestCase, r.case_id):
        pid = db.get(TestCase, r.case_id).project_id
    if pid not in ids:
        raise HTTPException(403, "无权访问该执行记录")


def _run_out(r: TestRun, brief: bool = False) -> dict:
    out = {"id": r.id, "plan_id": r.plan_id, "plan_name": r.plan_name, "case_id": r.case_id,
           "case_name": r.case_name, "flow_id": r.flow_id, "flow_name": r.flow_name,
           "env_name": r.env_name, "status": r.status,
           "pass_n": r.pass_n, "fail_n": r.fail_n, "duration": r.duration,
           "trigger_by": r.trigger_by, "created_at": r.created_at.isoformat()}
    if not brief:
        out["detail"] = r.detail
    return out
