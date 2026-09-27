"""AI 接口：从提交生成用例草稿、自然语言生成、失败分析。AI 只产草稿，保存走普通用例接口。"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from ..db import get_db
from ..models import User, TestCase, TestRun, GitRepo, CommitSync
from ..auth import current_user
from ..perms import check_project_access
from .. import ai as A
import json

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
    check_project_access(repo.project_id, user, db)   # 仓库所属项目的权限校验
    q = db.query(CommitSync).filter(CommitSync.repo_id == repo.id, CommitSync.analyzed == 0)
    if body.commit_ids:
        q = q.filter(CommitSync.id.in_(body.commit_ids))
    commits = q.order_by(CommitSync.created_at.desc()).limit(20).all()
    if not commits:
        raise HTTPException(400, "没有待分析的提交")
    existing = db.query(TestCase).filter(TestCase.project_id == repo.project_id).all()
    result = await _gen_from_commits(commits, existing)
    # 模型可用（含"确认无受影响接口"的空结果）或启发式有产出才标记已分析，
    # 避免 LLM 故障时把这批提交的分析机会静默消费掉
    if A.llm_available() or result["drafts"] or result["covered"]:
        for c in commits:
            c.analyzed = 1
        db.commit()
    return result


@router.post("/gen-from-text")
async def gen_from_text(body: TextIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    check_project_access(body.project_id, user, db)   # 含项目存在性校验（404）
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
    from .runs import _authorize_run   # 与执行记录查看共用的项目归属校验
    run = db.get(TestRun, run_id)
    if not run:
        raise HTTPException(404, "执行记录不存在")
    _authorize_run(run, user, db)
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
    """全平台 LLM 用量汇总：仅 admin（member 的用量在执行记录里按 run 可见）。"""
    from ..auth import require_admin
    require_admin(user)
    from ..ai import usage_summary
    return usage_summary()


class EnhanceIn(BaseModel):
    steps: list[dict] = []     # 可视化录制产出的 UI 步骤
    url: str = ""              # 录制的起始地址（给模型上下文）
    name: str = ""             # 可选：已有用例名
    context: str = ""          # 录制时的操作意图（如 AI 代劳的目标），帮助模型更有把握补断言


class DesignDraftIn(BaseModel):
    project_id: str = ""
    goal: str                  # 测试意图（大白话，如"测货主创建询价单"）
    url: str = ""              # 可选：起始页面，帮助模型对准路径与选择器风格


@router.post("/draft-design")
async def draft_design(body: DesignDraftIn, db: Session = Depends(get_db),
                       user: User = Depends(current_user)):
    """AI 起草测试设计：把测试意图拆成测试点（操作意图 + 可执行验收断言）。
    人确认/编辑后存进用例——预期在设计阶段固化，判定权与执行者解耦。"""
    if body.project_id:
        check_project_access(body.project_id, user, db)
    from ..engine.assertions import ASSERT_TYPES
    sys_prompt = (
        "你是测试设计师。把测试意图拆解为若干测试点，每个测试点给出操作意图与验收断言。"
        "规则：\n"
        "1) 断言是「可验证的结果」不是「要做的动作」——「页面显示登录成功」是断言，「点击登录按钮」不是；\n"
        "2) 断言只能用这些类型：" +
        "、".join(ASSERT_TYPES) +
        "。expect_text/expect_not_text 填 value（页面应/不应出现的文字）；expect_url 填 value（URL 应包含的片段）；"
        "expect_element 填 selector（应可见的元素）；expect_value 填 selector+value（输入框/元素应有的值）；"
        "expect_api 填 m/url/field/expect（对被测系统接口的断言，url 用相对路径，可用 ${变量} 引用执行期保存的值如 ${bill_no}）；\n"
        "3) 每个测试点 1-3 条断言，覆盖主流程结果即可，不追求穷举；涉及单据/编号流转的，"
        "优先补一条 expect_api 做端到端数据一致性验证；\n"
        "4) 负向场景（如错误密码应被拒绝）也拆成测试点，断言写预期的错误提示；\n"
        "5) selector 不确定时用语义化的猜测（如 #msg、.error），人会在确认时修正。\n"
        '只输出 JSON：{"points":[{"name":"测试点名","intent":"操作意图一句话",'
        '"asserts":[{"type":"...","value":"..."}]}],"note":"给确认者的一句提醒（如哪些 selector 需人工核对）"}'
    )
    payload = json.dumps({"测试意图": body.goal, "起始页面": body.url}, ensure_ascii=False)
    out = await A.chat_json(sys_prompt, payload, kind="gen-text")
    points = (out or {}).get("points") if isinstance(out, dict) else None
    if points and isinstance(points, list):
        from .cases import _validated_points
        try:
            points = _validated_points(points)   # 原语合法性：引擎执行不了的断言不放行
        except HTTPException:
            points = []
    if not points:
        return {"points": [], "note": "大模型未配置或未返回有效设计，可手动添加测试点"
                "（或到「系统设置」配置模型后再试）", "engine": "builtin"}
    return {"points": points, "note": str((out or {}).get("note") or "")[:300],
            "engine": A.current_model()}


@router.post("/enhance-steps")
async def enhance_steps(body: EnhanceIn, user: User = Depends(current_user)):
    """录制步骤 AI 增强：补关键断言、参数化测试数据、建议用例名。

    大模型未配置或返回无效时原样返回（enhanced=False），前端引导直接使用原始步骤。
    """
    if not body.steps:
        raise HTTPException(400, "没有可增强的步骤")
    sys_prompt = (
        "你是 UI 自动化用例评审助手。输入是可视化录制得到的步骤"
        '（action ∈ goto/click/fill/expect_text，含 url/selector/value 字段）。返回 JSON：'
        '{"name":"建议的用例名","steps":[增强后的步骤数组],"note":"改动说明","vars":{"变量名":"原值"}}。'
        "要求：1) 保持原有动作的顺序与内容不变；"
        "2) 只在关键动作（提交/登录/保存等）之后插入 expect_text 断言，没把握就不要编造；"
        "输入里的 context 描述了录制时的操作意图（如要登录到什么系统、期望出现什么提示），"
        "是补断言的重要依据，有把握时就用它；"
        "3) fill 的 value 中像账号/密码/手机号/邮箱的值替换为 ${username} 这类变量，"
        "并在 vars 里给出原值；普通业务数据保持原样；"
        "4) 除插入断言与参数化外不要增删改任何步骤。只输出 JSON。"
    )
    payload = json.dumps({"url": body.url, "context": body.context, "steps": body.steps[:80]},
                         ensure_ascii=False)
    out = await A.chat_json(sys_prompt, payload, kind="gen-text")
    if out and isinstance(out.get("steps"), list) and out["steps"]:
        return {"enhanced": True, "name": str(out.get("name") or body.name),
                "steps": out["steps"], "note": str(out.get("note") or ""),
                "vars": out.get("vars") or {}, "engine": A.current_model()}
    return {"enhanced": False, "name": body.name, "steps": body.steps,
            "note": "大模型未配置或未返回有效结果，已保留原始录制步骤（可到「系统设置」配置模型后再试）",
            "vars": {}, "engine": "builtin"}


@router.get("/regression-advice")
def regression_advice(db: Session = Depends(get_db), user: User = Depends(current_user)):
    """回归建议：超过 14 天未执行的用例与流程 + 近 7 天有提交但未跑过的项目。"""
    from datetime import datetime, timedelta
    from ..models import TestRun, Flow
    from ..perms import accessible_project_ids
    ids = set(accessible_project_ids(user, db))
    case_q = db.query(TestCase).filter(TestCase.project_id.in_(ids)) if ids else db.query(TestCase).filter(False)
    flow_q = db.query(Flow).filter(Flow.project_id.in_(ids)) if ids else db.query(Flow).filter(False)
    threshold = datetime.utcnow() - timedelta(days=14)
    stale = []
    for c in case_q.all():
        last = (db.query(TestRun).filter(TestRun.case_id == c.id)
                .order_by(TestRun.created_at.desc()).first())
        if last is None or last.created_at < threshold:
            stale.append({"case_id": c.id, "case_name": c.name,
                          "last_run": last.created_at.isoformat() if last else "从未执行"})
    stale_flows = []
    for f in flow_q.all():
        last = (db.query(TestRun).filter(TestRun.flow_id == f.id)
                .order_by(TestRun.created_at.desc()).first())
        if last is None or last.created_at < threshold:
            stale_flows.append({"flow_id": f.id, "flow_name": f.name,
                                "last_run": last.created_at.isoformat() if last else "从未执行"})
    week_ago = datetime.utcnow() - timedelta(days=7)
    hot = []
    from ..models import CommitSync
    commit_q = db.query(CommitSync).filter(CommitSync.created_at > week_ago)
    if user.role != "admin":   # member 只看自己项目下仓库的提交
        repo_ids = {r.id for r in db.query(GitRepo).filter(GitRepo.project_id.in_(ids))}
        commit_q = commit_q.filter(CommitSync.repo_id.in_(repo_ids) if repo_ids else False)
    for cm in commit_q.order_by(CommitSync.created_at.desc()).limit(50).all():
        # repo→project 映射在列表页拼
        hot.append({"message": cm.message, "author": cm.author, "sha": cm.sha[:8],
                    "created_at": cm.created_at.isoformat()})
    return {"stale_cases": stale[:10], "stale_flows": stale_flows[:10], "recent_commits": hot[:10],
            "advice": (f"有 {len(stale)} 条用例、{len(stale_flows)} 条流程超过 14 天未执行，建议安排一次回归。"
                       if stale or stale_flows else "用例执行情况良好。") +
                      (f" 近 7 天有 {len(hot)} 条新提交。" if hot else "")}
