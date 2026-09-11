"""Git 集成：仓库绑定、webhook 接收、提交同步、push 触发 git 计划。"""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session
from ..db import get_db
from ..models import User, GitRepo, CommitSync, TestPlan, Env
from ..auth import current_user
from ..perms import check_project_access, accessible_project_ids

router = APIRouter(prefix="/api/v1/integrations/git", tags=["git"])


class RepoIn(BaseModel):
    project_id: str
    repo_url: str
    provider: str = "github"
    default_branch: str = "main"


@router.get("/repos")
def list_repos(project_id: str = "", db: Session = Depends(get_db), user: User = Depends(current_user)):
    ids = set(accessible_project_ids(user, db))
    q = db.query(GitRepo).filter(GitRepo.project_id.in_(ids))
    if project_id:
        q = q.filter(GitRepo.project_id == project_id)
    return [{"id": r.id, "project_id": r.project_id, "provider": r.provider,
             "repo_url": r.repo_url, "webhook_secret": r.webhook_secret,
             "default_branch": r.default_branch} for r in q.all()]


@router.post("/repos")
def create_repo(body: RepoIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    check_project_access(body.project_id, user, db)
    r = GitRepo(**body.model_dump())
    db.add(r); db.commit()
    return {"id": r.id, "webhook_secret": r.webhook_secret,
            "webhook_url": f"/api/v1/integrations/git/webhook/{r.webhook_secret}"}


@router.delete("/repos/{rid}")
def delete_repo(rid: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    r = db.get(GitRepo, rid)
    if not r:
        raise HTTPException(404, "仓库不存在")
    check_project_access(r.project_id, user, db)
    for c in db.query(CommitSync).filter(CommitSync.repo_id == rid):
        db.delete(c)
    db.delete(r); db.commit()
    return {"ok": True}


@router.get("/commits")
def list_commits(repo_id: str, limit: int = 30, db: Session = Depends(get_db), user: User = Depends(current_user)):
    repo = db.get(GitRepo, repo_id)
    if repo:
        check_project_access(repo.project_id, user, db)
    cs = (db.query(CommitSync).filter(CommitSync.repo_id == repo_id)
          .order_by(CommitSync.created_at.desc()).limit(limit).all())
    return [{"id": c.id, "sha": c.sha[:8], "author": c.author, "message": c.message,
             "branch": c.branch, "files": c.files, "analyzed": c.analyzed,
             "created_at": c.created_at.isoformat()} for c in cs]


async def _ingest_push(db: Session, repo: GitRepo, commits: list[dict], branch: str, ref: str = ""):
    """落库提交；若是默认分支且存在 git 触发的计划，返回待执行的 plan ids。"""
    shas = set()
    for cm in commits:
        sha = cm.get("id") or cm.get("sha") or ""
        if not sha or sha[:8] in shas:
            continue
        shas.add(sha[:8])
        msg = cm.get("message", "").split("\n")[0]
        author = (cm.get("author") or {}).get("name") or cm.get("author_name") or ""
        files = (cm.get("added") or []) + (cm.get("modified") or []) + (cm.get("removed") or []) + (cm.get("files") or [])
        db.add(CommitSync(repo_id=repo.id, sha=sha[:12], author=author, message=msg,
                          branch=branch, files=files[:20]))
    db.commit()
    fired = []
    if branch == repo.default_branch:
        for p in db.query(TestPlan).filter(TestPlan.project_id == repo.project_id,
                                           TestPlan.trigger == "git", TestPlan.enabled == True):  # noqa
            fired.append(p.id)
    return fired


def _verify_hmac(request: Request, raw: bytes) -> None:
    """配置 TD_GIT_WEBHOOK_SECRET 后启用 HMAC 签名校验（GitHub X-Hub-Signature-256）。"""
    from .. import config
    key = config.get("TD_GIT_WEBHOOK_SECRET").strip()
    if not key:
        return
    import hmac as _hmac
    import hashlib
    supplied = request.headers.get("x-hub-signature-256", "")
    expected = "sha256=" + _hmac.new(key.encode(), raw, hashlib.sha256).hexdigest()
    if not _hmac.compare_digest(supplied, expected):
        raise HTTPException(401, "webhook 签名校验失败")


@router.post("/webhook/{secret}")
async def webhook(secret: str, request: Request, db: Session = Depends(get_db)):
    """兼容 GitHub / GitLab push payload；无需 JWT，凭 secret 鉴权（可选 HMAC）。"""
    repo = db.query(GitRepo).filter(GitRepo.webhook_secret == secret).first()
    if not repo:
        raise HTTPException(404, "webhook 不存在")
    raw = await request.body()
    _verify_hmac(request, raw)
    import json as _json
    payload = _json.loads(raw)
    # GitHub: {ref: refs/heads/main, commits: [{id, message, author: {name}, added/modified/removed}]}
    # GitLab: {object_kind: push, ref, commits: [{id, title, author_name}]}
    ref = payload.get("ref") or ""
    branch = payload.get("branch") or ref.split("/")[-1] or repo.default_branch
    commits = payload.get("commits") or []
    for cm in commits:
        cm.setdefault("message", cm.get("title", ""))
    fired = await _ingest_push(db, repo, commits, branch, ref)
    from .runs import execute_plan
    from ..engine.queue import queued
    from ..notify import notify_run
    runs = []
    for pid in fired:
        plan = db.get(TestPlan, pid)
        env = db.get(Env, plan.env_id) if plan and plan.env_id else None
        if plan and env:
            r = await queued(lambda pl=plan, e=env: execute_plan(pl, e, trigger_by=f"git:{branch}"))
            await notify_run(r)
            runs.append({"run_id": r.id, "status": r.status})
    return {"ok": True, "ingested": len(commits), "triggered_plans": runs}
