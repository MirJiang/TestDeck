"""项目级权限：admin 全可见；member 可见自己创建的 + 被加入的项目。"""
from fastapi import Depends, HTTPException
from sqlalchemy.orm import Session
from .db import get_db
from .models import User, Project, ProjectMember
from .auth import current_user


def accessible_project_ids(user: User, db: Session) -> list[str]:
    if user.role == "admin":
        return [p.id for p in db.query(Project).all()]
    owned = {p.id for p in db.query(Project).filter(Project.owner_id == user.id)}
    joined = {m.project_id for m in db.query(ProjectMember).filter(ProjectMember.user_id == user.id)}
    return list(owned | joined)


def check_project_access(pid: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> Project:
    p = db.get(Project, pid)
    if not p:
        raise HTTPException(404, "项目不存在")
    if user.role != "admin" and pid not in accessible_project_ids(user, db):
        raise HTTPException(403, "无权访问该项目")
    return p
