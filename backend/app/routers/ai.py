"""AI 接口：从提交生成用例草稿、自然语言生成、失败分析。AI 只产草稿，保存走普通用例接口。"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from ..db import get_db
from ..models import User, Project, TestCase, TestRun, GitRepo, CommitSync
from ..auth import current_user
from .. import ai as A

router = APIRouter(prefix="/api/v1/ai", tags=["ai"])


class CommitsIn(BaseModel):
    repo_id: str
    commit_ids: list[str] = []   # CommitSync.id 列表


class TextIn(BaseModel):
    project_id: str
    prompt: str


def _steps_valid(steps) -> bool:
    return isinstance(steps, list) and all(isinstance(s, dict) and s.get("url") for s in steps)


async def _gen_from_commits(commits: list[CommitSync], existing: list[TestCase]) -> dict:
    """优先 LLM；降级启发式。返回 {drafts:[], covered:[]}"""
    # 已有用例覆盖的接口集合
    covered_urls = [(c, s.get("url", "")) for c in existing for s in (c.steps or []) if s.get("url")]

    llm_in = [{"sha": c.sha, "message": c.message, "files": c.files} for c in commits]
    out = await A.chat_json(
        "你是测试用例生成助手。根据 Git 提交找出受影响的 HTTP 接口，"
        '生成用例草稿。输出格式：{"drafts":[{"name":str,"reason":str,'
        '"steps":[{"m":"GET|POST|PUT|DELETE","url":str,"headers":"","body":"",'
        '"check":{"type":"status|contains|field_eq|not_empty","field":str,"expect":str},'
        '"save":{"name":str,"from":str}}]}]}。只分析提交说明与文件路径即可。',
        str(llm_in), kind="gen-commits")

    drafts = []
    if _steps_valid((out or {}).get("drafts") and out["drafts"][0].get("steps")):
        for d in out["drafts"]:
            if _steps_valid(d.get("steps")):
                drafts.append({"name": d.get("name", "AI 草稿"), "reason": d.get("reason", ""),
                               "steps": d["steps"], "by": "llm"})
    if not drafts:  # 降级启发式
        for c in commits:
            api = A.guess_api_from_text(c.message + " " + " ".join(c.files or []))
            hit = next((tc for tc, u in covered_urls if api and api in u), None)
            if api and hit:
                continue  # 已覆盖 → 放入 covered
            if api or any(k in c.message for k in ("feat", "新增", "接口")):
                d = A.draft_from_commit(c.message + " " + " ".join(c.files or []))
                if d["steps"]:
                    d["by"] = "heuristic"
                    drafts.append(d)

    covered = []
    for c in commits:
        api = A.guess_api_from_text(c.message + " " + " ".join(c.files or []))
        if not api:
            continue
        hit = next((tc for tc, u in covered_urls if api in u), None)
        if hit:
            covered.append({"case_id": hit.id, "case_name": hit.name, "api": api, "commit": c.message})
    return {"drafts": drafts, "covered": covered, "engine": A.current_model() if A.llm_available() else "builtin"}


@router.post("/gen-from-commits")
async def gen_from_commits(body: CommitsIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    repo = db.get(GitRepo, body.repo_id)
    if not repo:
        raise HTTPException(404, "仓库不存在")
    q = db.query(CommitSync).filter(CommitSync.repo_id == repo.id, CommitSync.analyzed == 0)
    if body.commit_ids:
        q = q.filter(CommitSync.id.in_(body.commit_ids))
    commits = q.order_by(CommitSync.created_at.desc()).limit(20).all()
    if not commits:
        raise HTTPException(400, "没有待分析的提交")
    existing = db.query(TestCase).filter(TestCase.project_id == repo.project_id).all()
    result = await _gen_from_commits(commits, existing)
    for c in commits:
        c.analyzed = 1
    db.commit()
    return result


@router.post("/gen-from-text")
async def gen_from_text(body: TextIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    if not db.get(Project, body.project_id):
        raise HTTPException(404, "项目不存在")
    out = await A.chat_json(
        "你是测试用例生成助手。根据需求描述生成一条 API 用例草稿。"
        '输出 {"name":str,"reason":str,"steps":[{"m":str,"url":str,"headers":"","body":"",'
        '"check":{"type":str,"field":str,"expect":str},"save":{"name":str,"from":str}}]}',
        body.prompt, kind="gen-text")
    if out and _steps_valid(out.get("steps")):
        return {"draft": out, "engine": A.current_model()}
    return {"draft": A.draft_from_prompt(body.prompt), "engine": "builtin"}


@router.post("/analyze-run/{run_id}")
async def analyze_run(run_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    run = db.get(TestRun, run_id)
    if not run:
        raise HTTPException(404, "执行记录不存在")
    from ..perms import accessible_project_ids
    from ..models import TestCase, TestPlan, Flow
    if user.role != "admin":
        ids = set(accessible_project_ids(user, db))
        pid = None
        if run.plan_id and db.get(TestPlan, run.plan_id):
            pid = db.get(TestPlan, run.plan_id).project_id
        elif run.flow_id and db.get(Flow, run.flow_id):
            pid = db.get(Flow, run.flow_id).project_id
        elif run.case_id and db.get(TestCase, run.case_id):
            pid = db.get(TestCase, run.case_id).project_id
        if pid not in ids:
            raise HTTPException(403, "无权访问该执行记录")
    if run.status != "failed":
        return {"cause": "本次执行全部通过，无需分析", "suggestion": "", "engine": "builtin"}

    # 收集失败步骤：计划明细的条目带 case_id/flow_id（步骤在其 detail 里），
    # 单用例/单流程的 detail 本身就是步骤数组
    failed = []
    detail = run.detail or []
    is_plan_detail = bool(detail) and isinstance(detail[0], dict) and (
        "case_id" in detail[0] or "flow_id" in detail[0])
    if is_plan_detail:
        for item in detail:
            for s in item.get("detail") or []:
                if not s.get("pass"):
                    failed.append(s)
    else:
        failed = [s for s in detail if not s.get("pass")]

    out = await A.chat_json(
        "你是测试失败分析助手。根据失败的 HTTP 测试步骤给出原因和建议。"
        '输出 {"cause":str,"suggestion":str}，用中文、面向非专业测试人员。',
        str(failed[:5]), kind="analyze")
    if out and out.get("cause"):
        return {**out, "engine": A.current_model()}
    return {**A.explain_failure(failed[0] if failed else {}), "engine": "builtin"}


@router.get("/usage")
def get_usage(user: User = Depends(current_user)):
    from ..ai import usage_summary
    return usage_summary()


@router.get("/regression-advice")
def regression_advice(db: Session = Depends(get_db), user: User = Depends(current_user)):
    """回归建议：超过 14 天未执行的用例与流程 + 近 7 天有提交但未跑过的项目。"""
    from datetime import datetime, timedelta
    from ..models import TestRun, Flow
    threshold = datetime.utcnow() - timedelta(days=14)
    stale = []
    for c in db.query(TestCase).all():
        last = (db.query(TestRun).filter(TestRun.case_id == c.id)
                .order_by(TestRun.created_at.desc()).first())
        if last is None or last.created_at < threshold:
            stale.append({"case_id": c.id, "case_name": c.name,
                          "last_run": last.created_at.isoformat() if last else "从未执行"})
    stale_flows = []
    for f in db.query(Flow).all():
        last = (db.query(TestRun).filter(TestRun.flow_id == f.id)
                .order_by(TestRun.created_at.desc()).first())
        if last is None or last.created_at < threshold:
            stale_flows.append({"flow_id": f.id, "flow_name": f.name,
                                "last_run": last.created_at.isoformat() if last else "从未执行"})
    week_ago = datetime.utcnow() - timedelta(days=7)
    hot = []
    from ..models import CommitSync, Project
    for cm in (db.query(CommitSync).filter(CommitSync.created_at > week_ago)
               .order_by(CommitSync.created_at.desc()).limit(50).all()):
        proj = db.get(Project, cm.repo_id)  # repo→project 映射在列表页拼
        hot.append({"message": cm.message, "author": cm.author, "sha": cm.sha[:8],
                    "created_at": cm.created_at.isoformat()})
    return {"stale_cases": stale[:10], "stale_flows": stale_flows[:10], "recent_commits": hot[:10],
            "advice": (f"有 {len(stale)} 条用例、{len(stale_flows)} 条流程超过 14 天未执行，建议安排一次回归。"
                       if stale or stale_flows else "用例执行情况良好。") +
                      (f" 近 7 天有 {len(hot)} 条新提交。" if hot else "")}
