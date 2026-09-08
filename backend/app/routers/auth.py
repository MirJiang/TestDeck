from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from ..db import get_db
from ..models import User
from ..auth import (hash_pw, verify_pw, make_token, current_user, require_admin,
                    login_throttled, record_login_fail)

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


class LoginIn(BaseModel):
    username: str
    password: str


class UserIn(BaseModel):
    username: str
    password: str
    role: str = "member"


@router.post("/login")
def login(body: LoginIn, db: Session = Depends(get_db)):
    if login_throttled(body.username):
        raise HTTPException(429, "失败次数过多，请 1 分钟后再试")
    user = db.query(User).filter(User.username == body.username).first()
    if not user or not verify_pw(body.password, user.password_hash):
        record_login_fail(body.username)
        raise HTTPException(401, "用户名或密码错误")
    from fastapi.responses import JSONResponse
    from ..auth import TOKEN_TTL
    token = make_token(user)
    resp = JSONResponse({"token": token, "user": {"id": user.id, "username": user.username, "role": user.role}})
    # 截图/录像等 <img>/<video> 资源带不了 Authorization 头：同步下发 Cookie 供 /static 鉴权
    resp.set_cookie("td_token", token, max_age=TOKEN_TTL, samesite="lax", path="/")
    return resp


@router.post("/users")
def create_user(body: UserIn, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    if db.query(User).filter(User.username == body.username).first():
        raise HTTPException(400, "用户名已存在")
    u = User(username=body.username, password_hash=hash_pw(body.password),
             role="admin" if body.role == "admin" else "member")
    db.add(u); db.commit()
    return {"id": u.id, "username": u.username, "role": u.role}


@router.get("/me")
def me(user: User = Depends(current_user)):
    return {"id": user.id, "username": user.username, "role": user.role}


class PasswordIn(BaseModel):
    old_password: str
    new_password: str


class ResetIn(BaseModel):
    new_password: str


@router.get("/users")
def list_users(db: Session = Depends(get_db), user: User = Depends(require_admin)):
    return [{"id": u.id, "username": u.username, "role": u.role,
             "created_at": u.created_at.isoformat()} for u in db.query(User).all()]


@router.put("/password")
def change_password(body: PasswordIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    if not verify_pw(body.old_password, user.password_hash):
        raise HTTPException(400, "原密码不正确")
    if len(body.new_password) < 6:
        raise HTTPException(400, "新密码至少 6 位")
    user.password_hash = hash_pw(body.new_password); db.commit()
    from ..auth import bump_token_version
    bump_token_version()  # 旧 token 全部失效
    return {"ok": True}


@router.put("/users/{uid}/password")
def reset_password(uid: str, body: ResetIn, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    u = db.get(User, uid)
    if not u:
        raise HTTPException(404, "用户不存在")
    if len(body.new_password) < 6:
        raise HTTPException(400, "新密码至少 6 位")
    u.password_hash = hash_pw(body.new_password); db.commit()
    from ..auth import bump_token_version
    bump_token_version()
    return {"ok": True}
