from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from ..db import get_db
from ..models import User, Project, Env, ProjectMember
from ..auth import current_user
from ..perms import check_project_access, accessible_project_ids

router = APIRouter(prefix="/api/v1/projects", tags=["projects"])


class ProjectIn(BaseModel):
    name: str
    desc: str = ""


def get_project(pid: str, db: Session) -> Project:
    p = db.get(Project, pid)
    if not p:
        raise HTTPException(404, "项目不存在")
    return p


@router.get("")
def list_projects(db: Session = Depends(get_db), user: User = Depends(current_user)):
    ids = set(accessible_project_ids(user, db))
    ps = [p for p in db.query(Project).order_by(Project.created_at.desc()).all() if p.id in ids]
    out = []
    for p in ps:
        env_n = db.query(Env).filter(Env.project_id == p.id).count()
        out.append({"id": p.id, "name": p.name, "desc": p.desc, "owner_id": p.owner_id,
                    "envs": env_n, "created_at": p.created_at.isoformat()})
    return out


@router.post("")
def create_project(body: ProjectIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    p = Project(name=body.name, desc=body.desc, owner_id=user.id)
    db.add(p); db.commit()
    return {"id": p.id, "name": p.name}


@router.put("/{pid}")
def update_project(pid: str, body: ProjectIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    check_project_access(pid, user, db)
    p = get_project(pid, db)
    p.name, p.desc = body.name, body.desc
    db.commit()
    return {"ok": True}


@router.delete("/{pid}")
def delete_project(pid: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    check_project_access(pid, user, db)
    p = get_project(pid, db)
    for e in db.query(Env).filter(Env.project_id == pid):
        db.delete(e)
    db.delete(p); db.commit()
    return {"ok": True}


# ---------- 环境 ----------

class EnvIn(BaseModel):
    name: str
    base_url: str = ""
    variables: dict = {}


@router.get("/{pid}/envs")
def list_envs(pid: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    check_project_access(pid, user, db)
    return [{"id": e.id, "name": e.name, "base_url": e.base_url, "variables": e.variables}
            for e in db.query(Env).filter(Env.project_id == pid)]


@router.post("/{pid}/envs")
def create_env(pid: str, body: EnvIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    check_project_access(pid, user, db)
    e = Env(project_id=pid, name=body.name, base_url=body.base_url, variables=body.variables)
    db.add(e); db.commit()
    return {"id": e.id}


@router.put("/{pid}/envs/{eid}")
def update_env(pid: str, eid: str, body: EnvIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    check_project_access(pid, user, db)
    e = db.get(Env, eid)
    if not e or e.project_id != pid:
        raise HTTPException(404, "环境不存在")
    e.name, e.base_url, e.variables = body.name, body.base_url, body.variables
    db.commit()
    return {"ok": True}


@router.delete("/{pid}/envs/{eid}")
def delete_env(pid: str, eid: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    check_project_access(pid, user, db)
    e = db.get(Env, eid)
    if not e or e.project_id != pid:
        raise HTTPException(404, "环境不存在")
    db.delete(e); db.commit()
    return {"ok": True}


# ---------- 成员 ----------

class MemberIn(BaseModel):
    username: str


def _can_manage(p: Project, user: User):
    if user.role != "admin" and p.owner_id != user.id:
        raise HTTPException(403, "只有管理员或项目创建人可以管理成员")


@router.get("/{pid}/members")
def list_members(pid: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    check_project_access(pid, user, db)
    p = get_project(pid, db)
    rows = [{"user_id": m.user_id, "username": (db.get(User, m.user_id).username if db.get(User, m.user_id) else "?")}
            for m in db.query(ProjectMember).filter(ProjectMember.project_id == pid)]
    owner = db.get(User, p.owner_id)
    return {"owner": owner.username if owner else "?", "members": rows}


@router.post("/{pid}/members")
def add_member(pid: str, body: MemberIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    p = get_project(pid, db)
    _can_manage(p, user)
    u = db.query(User).filter(User.username == body.username).first()
    if not u:
        raise HTTPException(404, f"用户 {body.username} 不存在")
    exists = db.query(ProjectMember).filter_by(project_id=pid, user_id=u.id).first()
    if not exists:
        db.add(ProjectMember(project_id=pid, user_id=u.id)); db.commit()
    return {"ok": True}


@router.delete("/{pid}/members/{uid}")
def remove_member(pid: str, uid: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    p = get_project(pid, db)
    _can_manage(p, user)
    m = db.query(ProjectMember).filter_by(project_id=pid, user_id=uid).first()
    if m:
        db.delete(m); db.commit()
    return {"ok": True}
