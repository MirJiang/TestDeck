"""MCP 服务：把平台能力暴露成模型可调用的工具（Model Context Protocol）。

外部 AI agent（Cursor / Claude / 内部 agent）连接 http://<host>:8000/mcp（Streamable HTTP），
请求头带平台登录 token（Authorization: Bearer <token>）即可像用户一样操作平台：
查项目/用例、跑测试、看结果。只暴露测试资产与执行类操作，用户管理/模型配置等
管理面不开放。

依赖官方 MCP SDK（1.x）：FastMCP + streamable_http_app，挂载为 FastAPI 子应用。
"""
import json

from mcp.server.fastmcp import Context

from .db import SessionLocal
from .models import User, Project, Env, TestCase, TestRun, Flow, ProjectUser, LLMLog
from .auth import resolve_token
from .perms import accessible_project_ids


def _user_of(ctx):
    """从 MCP 请求头解析平台用户；失败返回 (None, 错误信息)。"""
    req = None
    try:
        req = ctx.request_context.request
    except Exception:
        req = None
    auth = (req.headers.get("authorization") or "") if req is not None else ""
    token = auth[7:] if auth.lower().startswith("bearer ") else ""
    db = SessionLocal()
    try:
        user = resolve_token(token, db)
    finally:
        db.close()
    if not user:
        return None, "未认证或 token 失效：请配置请求头 Authorization: Bearer <平台登录 token>"
    return user, None


def _can_see(user: User, project_id: str, db) -> bool:
    if user.role == "admin":
        return True
    return project_id in set(accessible_project_ids(user, db))


def _j(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def _build():
    from mcp.server.fastmcp import FastMCP
    mcp = FastMCP("TestDeck")
    mcp.settings.streamable_http_path = "/"   # 挂载后完整地址即 /mcp

    # ---------- 查询类 ----------

    @mcp.tool()
    def projects_list(ctx: Context = None) -> str:
        """列出我有权限访问的项目（含环境地址）"""
        user, err = _user_of(ctx)
        if err:
            return err
        db = SessionLocal()
        try:
            qs = db.query(Project).all() if user.role == "admin" else \
                db.query(Project).filter(Project.id.in_(accessible_project_ids(user, db)))
            out = []
            for p in qs:
                env = db.query(Env).filter(Env.project_id == p.id).first()
                out.append({"id": p.id, "name": p.name, "desc": p.desc,
                            "base_url": env.base_url if env else ""})
            return _j(out)
        finally:
            db.close()

    @mcp.tool()
    def project_users_list(project_id: str, ctx: Context = None) -> str:
        """列出项目的测试用户（账号密码池，用于测试时带出凭据）"""
        user, err = _user_of(ctx)
        if err:
            return err
        db = SessionLocal()
        try:
            if not _can_see(user, project_id, db):
                return "无权访问该项目"
            us = db.query(ProjectUser).filter(ProjectUser.project_id == project_id).all()
            return _j([{"id": u.id, "name": u.name, "username": u.username,
                        "remark": u.remark} for u in us])
        finally:
            db.close()

    @mcp.tool()
    def cases_list(project_id: str, ctx: Context = None) -> str:
        """列出项目下的测试用例（AI 用例为主：目标/起始页/绑定账号）"""
        user, err = _user_of(ctx)
        if err:
            return err
        db = SessionLocal()
        try:
            if not _can_see(user, project_id, db):
                return "无权访问该项目"
            cs = db.query(TestCase).filter(TestCase.project_id == project_id).all()
            out = []
            for c in cs:
                cfg = (c.steps or [{}])[0] if c.steps else {}
                out.append({"id": c.id, "name": c.name, "type": c.type,
                            "target": cfg.get("target", ""), "goal": (cfg.get("goal") or "")[:120],
                            "start_url": cfg.get("start_url", ""),
                            "username": getattr(c, "username", "")})
            return _j(out)
        finally:
            db.close()

    @mcp.tool()
    def case_get(case_id: str, ctx: Context = None) -> str:
        """查看单个用例的完整配置（含固定步骤）"""
        user, err = _user_of(ctx)
        if err:
            return err
        db = SessionLocal()
        try:
            c = db.get(TestCase, case_id)
            if not c or not _can_see(user, c.project_id, db):
                return "用例不存在或无权访问"
            cfg = (c.steps or [{}])[0] if c.steps else {}
            return _j({"id": c.id, "project_id": c.project_id, "name": c.name, "type": c.type,
                       "target": cfg.get("target", ""), "goal": cfg.get("goal", ""),
                       "start_url": cfg.get("start_url", ""), "max_steps": cfg.get("max_steps", 30),
                       "fixed_steps": cfg.get("fixed_steps", []),
                       "username": getattr(c, "username", "")})
        finally:
            db.close()

    @mcp.tool()
    def runs_list(limit: int = 10, ctx: Context = None) -> str:
        """最近的执行记录（状态/通过数/耗时）"""
        user, err = _user_of(ctx)
        if err:
            return err
        db = SessionLocal()
        try:
            qs = db.query(TestRun).order_by(TestRun.created_at.desc()).limit(min(limit, 50)).all()
            out = []
            for r in qs:
                if not _visible_run(user, r, db):
                    continue
                out.append({"id": r.id, "case": r.case_name, "flow": r.flow_name, "plan": r.plan_name,
                            "status": r.status, "pass": f"{r.pass_n}/{r.pass_n + r.fail_n}",
                            "duration": r.duration, "time": str(r.created_at)[:19]})
            return _j(out)
        finally:
            db.close()

    def _visible_run(user, r, db) -> bool:
        if user.role == "admin":
            return True
        pid = r.plan_id
        if pid:
            return pid in set(accessible_project_ids(user, db))
        if r.case_id:
            c = db.get(TestCase, r.case_id)
            return bool(c and _can_see(user, c.project_id, db))
        if r.flow_id:
            f = db.get(Flow, r.flow_id)
            return bool(f and _can_see(user, f.project_id, db))
        return False

    @mcp.tool()
    def run_get(run_id: str, ctx: Context = None) -> str:
        """执行记录明细：每个步骤的动作/结果/失败原因（含 AI 决策摘要）"""
        user, err = _user_of(ctx)
        if err:
            return err
        db = SessionLocal()
        try:
            r = db.get(TestRun, run_id)
            if not r or not _visible_run(user, r, db):
                return "记录不存在或无权访问"
            steps = []
            for d in (r.detail or []):
                steps.append({"action": d.get("action") or d.get("m", ""),
                              "target": d.get("target") or d.get("url", ""),
                              "pass": d.get("pass"), "reason": d.get("reason", "")})
            return _j({"id": r.id, "status": r.status, "pass_n": r.pass_n, "fail_n": r.fail_n,
                       "duration": r.duration, "steps": steps})
        finally:
            db.close()

    # ---------- 执行类 ----------

    @mcp.tool()
    async def case_run(case_id: str, wait_seconds: int = 90, ctx: Context = None) -> str:
        """执行一个测试用例并等待结果（AI 用例由模型现场执行，可能较慢）。返回执行摘要。"""
        user, err = _user_of(ctx)
        if err:
            return err
        from .routers.runs import _exec_case_sync   # 复用路由的完整执行编排
        db = SessionLocal()
        try:
            c = db.get(TestCase, case_id)
            if not c or not _can_see(user, c.project_id, db):
                return "用例不存在或无权访问"
            env = db.query(Env).filter(Env.project_id == c.project_id).first()
            if not env:
                return "项目未配置环境地址"
            run = await _exec_case_sync(c, env, f"user:{user.username}")
            steps = [{"action": d.get("action") or d.get("m", ""), "pass": d.get("pass"),
                      "reason": (d.get("reason") or "")[:100]} for d in (run.detail or [])]
            return _j({"id": run.id, "status": run.status, "pass_n": run.pass_n,
                       "fail_n": run.fail_n, "duration": run.duration, "steps": steps})
        finally:
            db.close()

    @mcp.tool()
    async def flow_run(flow_id: str, wait_seconds: int = 120, ctx: Context = None) -> str:
        """执行一个多角色测试流程并等待结果。返回执行摘要。"""
        user, err = _user_of(ctx)
        if err:
            return err
        from .routers.runs import _exec_flow_sync
        db = SessionLocal()
        try:
            f = db.get(Flow, flow_id)
            if not f or not _can_see(user, f.project_id, db):
                return "流程不存在或无权访问"
            env = db.query(Env).filter(Env.project_id == f.project_id).first()
            run = await _exec_flow_sync(f, env, f"user:{user.username}")
            return _j({"id": run.id, "status": run.status, "pass_n": run.pass_n,
                       "fail_n": run.fail_n, "duration": run.duration})
        finally:
            db.close()

    # ---------- 创建/删除 ----------

    @mcp.tool()
    def case_create(project_id: str, name: str, goal: str, start_url: str = "",
                    username: str = "", password: str = "", max_steps: int = 30, ctx: Context = None) -> str:
        """创建 AI 用例：goal 用大白话描述要做的事与预期（可引用 ${username} ${password}），
        start_url 填路径（域名用项目环境地址），username/password 可选绑定测试账号。"""
        user, err = _user_of(ctx)
        if err:
            return err
        db = SessionLocal()
        try:
            if not _can_see(user, project_id, db):
                return "无权访问该项目"
            c = TestCase(project_id=project_id, name=name, type="ai",
                         steps=[{"target": "ui", "goal": goal, "start_url": start_url,
                                 "max_steps": max_steps, "engine": "", "endpoints": [],
                                 "fixed_steps": [], "fixed_api_steps": []}],
                         source="mcp", creator_id=user.id,
                         username=username, password=password)
            db.add(c)
            db.commit()
            return _j({"id": c.id, "name": c.name})
        finally:
            db.close()

    @mcp.tool()
    def case_delete(case_id: str, ctx: Context = None) -> str:
        """删除用例（不可恢复）"""
        user, err = _user_of(ctx)
        if err:
            return err
        db = SessionLocal()
        try:
            c = db.get(TestCase, case_id)
            if not c or not _can_see(user, c.project_id, db):
                return "用例不存在或无权访问"
            db.delete(c)
            db.commit()
            return "已删除"
        finally:
            db.close()

    @mcp.tool()
    def ai_usage(ctx: Context = None) -> str:
        """平台 LLM 用量统计（模型/调用数/token）"""
        user, err = _user_of(ctx)
        if err:
            return err
        db = SessionLocal()
        try:
            rows = db.query(LLMLog).order_by(LLMLog.created_at.desc()).limit(200).all()
            agg = {}
            for r in rows:
                a = agg.setdefault(r.model, {"calls": 0, "prompt": 0, "completion": 0, "fail": 0})
                a["calls"] += 1
                a["prompt"] += r.prompt_tokens or 0
                a["completion"] += r.completion_tokens or 0
                if not r.ok:
                    a["fail"] += 1
            return _j(agg)
        finally:
            db.close()

    @mcp.tool()
    def app_map_upsert(project_id: str, pages: str, source: str = "manual", ctx: Context = None) -> str:
        """合并写入应用地图（期望基线）：源码分析或人工维护的页面/元素经此上传。
        pages 为 JSON 数组字符串：[{"path":"/orders","title":"订单","depth":1,
        "elements":[{"kind":"button","text":"新建","selector":"#btn-new",
        "state_note":"仅审批岗可见"}]}]。
        页面按 path 匹配，元素按 kind+text+selector/href 同键更新，不删已有数据；
        source 取 code（源码分析）或 manual（人工）。"""
        user, err = _user_of(ctx)
        if err:
            return err
        db = SessionLocal()
        try:
            if not _can_see(user, project_id, db):
                return "无权访问该项目"
        finally:
            db.close()
        try:
            data = json.loads(pages)
        except Exception as e:
            return f"pages 不是合法 JSON：{e}"
        if not isinstance(data, list) or not data:
            return "pages 需为非空 JSON 数组"
        from .engine.app_mapper import upsert_map
        return _j(upsert_map(project_id, data, source))

    # ---------- 浏览器会话（批次 C2：外部 agent 借平台受控执行环境干活） ----------

    @mcp.tool()
    async def browser_open(url: str, project_id: str = "", engine: str = "",
                           ctx: Context = None) -> str:
        """打开一个受控浏览器会话并导航到 url（他们出脑子，平台出手）。
        url 可以是完整地址；若以 / 开头且给了 project_id，则拼该项目环境地址。
        engine 可选 chromium|lightpanda（默认平台配置）。返回 session_id 供后续工具使用。
        会话空闲 30 分钟自动回收；每用户最多 2 个并发会话。"""
        user, err = _user_of(ctx)
        if err:
            return err
        target = (url or "").strip()
        if target.startswith("/"):
            if not project_id:
                return "相对路径需要同时给 project_id（用于拼环境地址）"
            db = SessionLocal()
            try:
                if not _can_see(user, project_id, db):
                    return "无权访问该项目"
                env = db.query(Env).filter(Env.project_id == project_id).first()
            finally:
                db.close()
            if not env or not (env.base_url or "").startswith(("http://", "https://")):
                return "项目未配置有效的环境地址"
            target = env.base_url.rstrip("/") + target
        elif not target.startswith(("http://", "https://")):
            return "url 需以 http(s):// 或 / 开头"
        import asyncio
        from .engine.browser_sessions import open_session
        try:
            info = await asyncio.to_thread(open_session, user.id, target, engine)
        except RuntimeError as e:
            return str(e)
        return _j(info)

    def _sess(session_id: str, user):
        from .engine.browser_sessions import get_session
        return get_session(session_id, user.id, is_admin=(user.role == "admin"))

    @mcp.tool()
    async def browser_state(session_id: str, ctx: Context = None) -> str:
        """读取会话当前页面状态：url/标题/可交互元素（selector 可直接用于 browser_act）/页面文字/会话变量"""
        user, err = _user_of(ctx)
        if err:
            return err
        try:
            sess = _sess(session_id, user)
        except KeyError as e:
            return str(e)
        import asyncio
        return _j(await asyncio.wrap_future(sess.post(sess.do_state)))

    @mcp.tool()
    async def browser_act(session_id: str, action: str, params: str = "",
                          ctx: Context = None) -> str:
        """在会话页面上执行一个白名单动作并返回结果+最新页面状态。
        action 仅限：goto(url)|click(selector)|fill(selector,value)|expect_text(value)|
        click_xy(x,y)|drag(x,y,x2,y2)|save(name,value)。
        params 为 JSON 对象字符串，如 {"selector":"#btn"}；值可用 ${变量} 引用会话变量。
        没有也不会有 shell/文件/代码执行类动作。"""
        user, err = _user_of(ctx)
        if err:
            return err
        try:
            sess = _sess(session_id, user)
        except KeyError as e:
            return str(e)
        try:
            p = json.loads(params) if params else {}
        except Exception as e:
            return f"params 不是合法 JSON：{e}"
        if not isinstance(p, dict):
            return "params 需为 JSON 对象"
        import asyncio
        return _j(await asyncio.wrap_future(sess.post(sess.do_action, action, p)))

    @mcp.tool()
    async def browser_screenshot(session_id: str, ctx: Context = None) -> str:
        """会话页面视口截图留档，返回 /static/ 图片地址（平台静态资源路径）"""
        user, err = _user_of(ctx)
        if err:
            return err
        try:
            sess = _sess(session_id, user)
        except KeyError as e:
            return str(e)
        import asyncio
        try:
            return _j(await asyncio.wrap_future(sess.post(sess.do_screenshot)))
        except Exception as e:
            return f"截图失败：{type(e).__name__}: {e}"[:200]

    @mcp.tool()
    def browser_sessions(ctx: Context = None) -> str:
        """列出我的活动浏览器会话（含空闲秒数；空闲超时会被自动回收）"""
        user, err = _user_of(ctx)
        if err:
            return err
        from .engine.browser_sessions import list_sessions
        return _j(list_sessions(user.id, is_admin=(user.role == "admin")))

    @mcp.tool()
    def browser_close(session_id: str, ctx: Context = None) -> str:
        """关闭浏览器会话（用完及时关，释放并发额度）"""
        user, err = _user_of(ctx)
        if err:
            return err
        from .engine.browser_sessions import close_session
        try:
            close_session(session_id, user.id, is_admin=(user.role == "admin"))
        except KeyError as e:
            return str(e)
        return "已关闭"

    return mcp


mcp = _build()
mcp_app = mcp.streamable_http_app()   # 挂载到 FastAPI：app.mount("/mcp", mcp_app)
