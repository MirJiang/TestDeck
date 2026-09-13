from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from ..db import get_db
from ..models import (User, Project, Env, ProjectMember, ProjectUser, AppPage, AppElement, AppMenu,
                      TestCase, TestPlan, Schedule, TestRun, Flow, GitRepo, CommitSync,
                      ApiDoc, ApiEndpoint)
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
    run_ids, flow_ids = _purge_project_data(db, pid)
    db.delete(p); db.commit()
    _remove_static_files(run_ids, flow_ids)
    from ..scheduler import refresh
    refresh()   # 反注册已删计划的 cron 任务
    return {"ok": True}


def _purge_project_data(db: Session, pid: str):
    """级联删除项目全部从属数据：用例、流程、计划（含调度行）、执行记录、应用地图、
    接口文档库、Git 绑定与提交、测试账号池、成员、环境。
    执行记录无 project_id，经 case/flow/plan 间接关联；返回 run/flow id 供清理静态文件。"""
    case_ids = [c.id for c in db.query(TestCase).filter(TestCase.project_id == pid)]
    flow_ids = [f.id for f in db.query(Flow).filter(Flow.project_id == pid)]
    plan_ids = [p.id for p in db.query(TestPlan).filter(TestPlan.project_id == pid)]
    repo_ids = [r.id for r in db.query(GitRepo).filter(GitRepo.project_id == pid)]
    page_ids = [pg.id for pg in db.query(AppPage).filter(AppPage.project_id == pid)]

    runs = db.query(TestRun).filter(
        TestRun.case_id.in_(case_ids) | TestRun.flow_id.in_(flow_ids) | TestRun.plan_id.in_(plan_ids))
    run_ids = [r.id for r in runs]
    for r in runs:
        db.delete(r)

    def drop(model, **cond):
        for row in db.query(model).filter_by(**cond):
            db.delete(row)

    for pg_id in page_ids:
        drop(AppElement, page_id=pg_id)
    drop(AppPage, project_id=pid)
    for plan_id in plan_ids:
        drop(Schedule, plan_id=plan_id)
    for repo_id in repo_ids:
        drop(CommitSync, repo_id=repo_id)
    drop(GitRepo, project_id=pid)
    drop(ApiEndpoint, project_id=pid)
    drop(ApiDoc, project_id=pid)
    drop(TestCase, project_id=pid)
    drop(Flow, project_id=pid)
    drop(TestPlan, project_id=pid)
    drop(ProjectUser, project_id=pid)
    drop(ProjectMember, project_id=pid)
    drop(Env, project_id=pid)
    return run_ids, flow_ids


def _remove_static_files(run_ids, flow_ids):
    """删除执行产物（截图/录像/差异图，run_id 前缀）与流程截图基线（base-{flow_id}-*）。"""
    from ..engine.ui_runner import STATIC_DIR
    if not STATIC_DIR.exists():
        return
    patterns = [f"{rid}*" for rid in run_ids] + [f"base-{fid}-*.png" for fid in flow_ids]
    for pattern in patterns:
        for f in STATIC_DIR.glob(pattern):
            try:
                f.unlink()
            except OSError:
                pass


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
    if db.query(Env).filter(Env.project_id == pid).count():
        raise HTTPException(400, "一个项目只保留一个环境地址，请直接编辑现有环境")
    e = Env(project_id=pid, name=body.name or "default", base_url=body.base_url, variables=body.variables)
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


# ---------- 项目测试用户（账号密码池：用例/流程角色选择带出） ----------

class ProjectUserIn(BaseModel):
    name: str = ""
    username: str
    password: str = ""
    remark: str = ""


class ImportIn(BaseModel):
    text: str


@router.get("/{pid}/users")
def list_project_users(pid: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    check_project_access(pid, user, db)
    return [{"id": u.id, "name": u.name, "username": u.username, "password": u.password,
             "remark": u.remark} for u in
            db.query(ProjectUser).filter(ProjectUser.project_id == pid).order_by(ProjectUser.created_at)]


@router.post("/{pid}/users")
def add_project_user(pid: str, body: ProjectUserIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    check_project_access(pid, user, db)
    u = ProjectUser(project_id=pid, name=body.name, username=body.username,
                    password=body.password, remark=body.remark)
    db.add(u); db.commit()
    return {"id": u.id}


@router.put("/{pid}/users/{uid}")
def update_project_user(pid: str, uid: str, body: ProjectUserIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    check_project_access(pid, user, db)
    u = db.get(ProjectUser, uid)
    if not u or u.project_id != pid:
        raise HTTPException(404, "用户不存在")
    u.name, u.username, u.password, u.remark = body.name, body.username, body.password, body.remark
    db.commit()
    return {"ok": True}


@router.delete("/{pid}/users/{uid}")
def del_project_user(pid: str, uid: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    check_project_access(pid, user, db)
    u = db.get(ProjectUser, uid)
    if not u or u.project_id != pid:
        raise HTTPException(404, "用户不存在")
    db.delete(u); db.commit()
    return {"ok": True}


@router.post("/{pid}/users/import")
def import_project_users(pid: str, body: ImportIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """批量导入：每行一个用户，逗号/制表符分隔。两列=账号,密码；三列=名称,账号,密码。
    账号已存在则更新（幂等）。返回 {added, updated, skipped}。"""
    check_project_access(pid, user, db)
    added = updated = skipped = 0
    exist = {u.username: u for u in db.query(ProjectUser).filter(ProjectUser.project_id == pid)}
    for ln in (body.text or "").splitlines():
        parts = [p.strip() for p in ln.replace("，", ",").replace("\t", ",").split(",")]
        parts = [p for p in parts if p]
        if not parts:
            continue
        if len(parts) == 1:
            skipped += 1
            continue
        if len(parts) == 2:
            name, uname, pwd = "", parts[0], parts[1]
        else:
            name, uname, pwd = parts[0], parts[1], parts[2]
        if uname in exist:
            u = exist[uname]
            u.name, u.password = name or u.name, pwd
            updated += 1
        else:
            u = ProjectUser(project_id=pid, name=name, username=uname, password=pwd)
            db.add(u)
            exist[uname] = u
            added += 1
    db.commit()
    return {"added": added, "updated": updated, "skipped": skipped}


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


# ---------- 应用地图（页面/按钮/关系：期望基线，多来源写入，执行观察不回写） ----------

class ScanIn(BaseModel):
    start_path: str = ""
    user_id: str = ""            # 兼容旧调用：单个测试账号 AI 代劳登录后爬取
    user_ids: list[str] = []     # 多角色扫描：逐个 AI 登录爬取后按 path 合并
    max_pages: int = 25


class MapElementIn(BaseModel):
    kind: str = "button"          # button | link
    text: str = ""
    selector: str = ""
    href: str = ""
    disabled: bool | None = None
    state_note: str = ""          # 出现条件备注（非空 = 条件性元素，执行比对跳过）


class MapPageIn(BaseModel):
    path: str
    title: str = ""
    depth: int | None = None
    elements: list[MapElementIn] = []


class UpsertIn(BaseModel):
    source: str = "manual"        # code=源码分析 | manual=人工；非法值按 manual 处理
    pages: list[MapPageIn] = []


@router.post("/{pid}/app-map/scan")
async def scan_app_map(pid: str, body: ScanIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """扫描被测系统生成应用地图（同源 BFS，静态资源过滤；可勾选多个测试账号逐角色 AI 代劳登录后合并）。"""
    import asyncio
    from ..engine.app_mapper import scan_app
    check_project_access(pid, user, db)
    env = db.query(Env).filter(Env.project_id == pid).first()
    if not env or not (env.base_url or "").startswith(("http://", "https://")):
        raise HTTPException(400, "项目未配置有效的环境地址")
    ids = list(dict.fromkeys(i for i in [*body.user_ids, body.user_id] if i))
    users = []
    for uid in ids:
        u = db.get(ProjectUser, uid)
        if u and u.project_id == pid:
            users.append({"name": u.name or u.username, "username": u.username, "password": u.password})
    if ids and not users:
        raise HTTPException(400, "所选测试账号不存在或不属于该项目")
    result = await asyncio.to_thread(
        scan_app, env.base_url, pid, body.start_path, users,
        max(1, min(50, body.max_pages)))
    return result


@router.post("/{pid}/app-map/upsert")
def upsert_app_map(pid: str, body: UpsertIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """合并写入地图（源码分析 Skill / 人工维护通道）：页面按 path 匹配，元素同键 upsert，不删已有数据。"""
    from ..engine.app_mapper import upsert_map
    check_project_access(pid, user, db)
    get_project(pid, db)
    if not body.pages:
        raise HTTPException(400, "pages 不能为空")
    return upsert_map(pid, [p.model_dump() for p in body.pages], body.source)


@router.get("/{pid}/app-map")
def get_app_map(pid: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """应用地图：页面清单（含按钮与链接关系、来源与角色标注）。"""
    check_project_access(pid, user, db)
    menus = [{"parent": m.parent, "text": m.text}
             for m in db.query(AppMenu).filter(AppMenu.project_id == pid).all()]
    pages = db.query(AppPage).filter(AppPage.project_id == pid).order_by(AppPage.depth, AppPage.path).all()
    out = []
    for pg in pages:
        els = db.query(AppElement).filter(AppElement.page_id == pg.id).all()
        out.append({"id": pg.id, "path": pg.path, "title": pg.title, "depth": pg.depth,
                    "source": pg.source or "scan", "roles": pg.roles or "", "entry": pg.entry or "",
                    "scanned_at": pg.scanned_at.isoformat() if pg.scanned_at else "",
                    "buttons": [{"text": e.text, "selector": e.selector, "disabled": e.disabled,
                                 "source": e.source or "scan", "state_note": e.state_note or "",
                                 "roles": e.roles or ""}
                                for e in els if e.kind == "button"],
                    "links": [{"text": e.text, "href": e.href,
                               "source": e.source or "scan", "state_note": e.state_note or "",
                               "roles": e.roles or ""}
                              for e in els if e.kind == "link"]})
    return {"pages": out, "menus": menus}


@router.delete("/{pid}/app-map")
def clear_app_map(pid: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    check_project_access(pid, user, db)
    for old in db.query(AppPage).filter(AppPage.project_id == pid):
        db.query(AppElement).filter(AppElement.page_id == old.id).delete()
    db.query(AppPage).filter(AppPage.project_id == pid).delete()
    db.query(AppMenu).filter(AppMenu.project_id == pid).delete()
    db.commit()
    return {"ok": True}
