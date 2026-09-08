import json
import time
import secrets
from datetime import datetime
from sqlalchemy import (Column, Integer, String, Text, Boolean, DateTime, Float, JSON)
from .db import Base

# 说明：列统一带长度（MySQL 要求主键/唯一列必须有长度）；
# 不使用数据库级外键约束，实体关系由应用层维护（各 DB 方言行为一致）。


def uid() -> str:
    return secrets.token_hex(8)


def now() -> datetime:
    return datetime.utcnow()


class User(Base):
    __tablename__ = "users"
    id = Column(String(64), primary_key=True, default=uid)
    username = Column(String(64), unique=True, nullable=False)
    password_hash = Column(String(128), nullable=False)
    role = Column(String(16), default="member")  # admin | member
    created_at = Column(DateTime, default=now)


class Project(Base):
    __tablename__ = "projects"
    id = Column(String(64), primary_key=True, default=uid)
    name = Column(String(255), nullable=False)
    desc = Column(Text, default="")
    owner_id = Column(String(64))
    created_at = Column(DateTime, default=now)


class Env(Base):
    __tablename__ = "envs"
    id = Column(String(64), primary_key=True, default=uid)
    project_id = Column(String(64), nullable=False)
    name = Column(String(128), nullable=False)
    base_url = Column(String(512), default="")
    variables = Column(JSON, default=dict)


class TestCase(Base):
    __tablename__ = "test_cases"
    id = Column(String(64), primary_key=True, default=uid)
    project_id = Column(String(64), nullable=False)
    name = Column(String(255), nullable=False)
    type = Column(String(16), default="api")  # api | ui | ai
    steps = Column(JSON, default=list)        # [{m,url,headers,body,check{...},save{...}}]
    source = Column(String(16), default="manual")  # manual | ai | git
    creator_id = Column(String(64))
    updated_at = Column(DateTime, default=now, onupdate=now)
    # 绑定的测试账号（从项目用户列表带出，可改）：执行时注入 ${username}/${password}
    username = Column(String(255), default="")
    password = Column(String(255), default="")


class ProjectUser(Base):
    """项目级测试用户（账号密码池）：创建用例/流程角色时选择带出，免重复录入。"""
    __tablename__ = "project_users"
    id = Column(String(64), primary_key=True, default=uid)
    project_id = Column(String(64), nullable=False, index=True)
    name = Column(String(128), default="")     # 显示名（如：货主-王五）
    username = Column(String(255), nullable=False)
    password = Column(String(255), default="")
    remark = Column(String(255), default="")
    created_at = Column(DateTime, default=now)


class AppPage(Base):
    """应用地图·页面（爬取器产出）：一个项目一份地图，重扫只全量替换 scan 来源的页面。"""
    __tablename__ = "app_pages"
    id = Column(String(64), primary_key=True, default=uid)
    project_id = Column(String(64), nullable=False, index=True)
    path = Column(String(512), default="")     # 路径（含 hash 路由），作为页面标识
    title = Column(String(255), default="")
    depth = Column(Integer, default=0)         # 距起始页的跳数
    source = Column(String(16), default="scan")  # scan=爬取 | code=源码分析 | manual=手动/upsert
    roles = Column(String(255), default="")      # 见到该页的角色（逗号分隔），多角色扫描合并产出
    scanned_at = Column(DateTime, default=now)


class AppElement(Base):
    """应用地图·页面元素：按钮（含禁用状态）与链接（跳转关系）。

    地图 = 期望基线：只由扫描 / 源码分析 / 人工维护写入，执行观察永不回写。
    state_note 非空表示元素仅在特定状态下出现（如"审批中才显示"），执行比对时跳过。
    """
    __tablename__ = "app_elements"
    id = Column(String(64), primary_key=True, default=uid)
    page_id = Column(String(64), nullable=False, index=True)
    kind = Column(String(16), default="button")   # button | link
    text = Column(String(255), default="")
    selector = Column(String(512), default="")
    href = Column(String(512), default="")        # link：目标路径
    disabled = Column(Boolean, default=False)     # button：扫描时是否禁用
    source = Column(String(16), default="scan")   # scan | code | manual
    state_note = Column(String(255), default="")  # 出现条件备注（非空 = 条件性元素，比对跳过）
    roles = Column(String(255), default="")       # 见到该元素的角色（逗号分隔）


class TestPlan(Base):
    __tablename__ = "test_plans"
    id = Column(String(64), primary_key=True, default=uid)
    project_id = Column(String(64), nullable=False)
    name = Column(String(255), nullable=False)
    case_ids = Column(JSON, default=list)
    flow_ids = Column(JSON, default=list)   # 计划可同时包含用例与流程
    env_id = Column(String(64))
    trigger = Column(String(16), default="manual")  # manual | cron | git
    cron = Column(String(64), default="")
    enabled = Column(Boolean, default=True)
    created_at = Column(DateTime, default=now)


class TestRun(Base):
    """统一执行记录：单用例（case_id）/ 单流程（flow_id）/ 整个计划（plan_id，明细含用例+流程）。"""
    __tablename__ = "test_runs"
    id = Column(String(64), primary_key=True)
    plan_id = Column(String(64))
    plan_name = Column(String(255), default="")
    case_id = Column(String(64))
    case_name = Column(String(255), default="")
    flow_id = Column(String(64))
    flow_name = Column(String(255), default="")
    env_id = Column(String(64))
    env_name = Column(String(128), default="")
    status = Column(String(16), default="running")  # running | passed | failed
    pass_n = Column(Integer, default=0)
    fail_n = Column(Integer, default=0)
    duration = Column(Float, default=0.0)
    detail = Column(JSON, default=list)
    trigger_by = Column(String(128), default="user")
    created_at = Column(DateTime, default=now)


class Schedule(Base):
    __tablename__ = "schedules"
    id = Column(String(64), primary_key=True, default=uid)
    plan_id = Column(String(64), nullable=False)
    cron = Column(String(64), nullable=False)
    enabled = Column(Boolean, default=True)
    last_run_at = Column(DateTime)


class GitRepo(Base):
    __tablename__ = "git_repos"
    id = Column(String(64), primary_key=True, default=uid)
    project_id = Column(String(64), nullable=False)
    provider = Column(String(32), default="github")  # github | gitlab | gitea | other
    repo_url = Column(String(512), default="")
    webhook_secret = Column(String(64), default=lambda: secrets.token_hex(12))
    default_branch = Column(String(64), default="main")


class CommitSync(Base):
    __tablename__ = "commit_syncs"
    id = Column(String(64), primary_key=True, default=uid)
    repo_id = Column(String(64), nullable=False)
    sha = Column(String(40), nullable=False)
    author = Column(String(128), default="")
    message = Column(String(512), default="")
    branch = Column(String(128), default="")
    files = Column(JSON, default=list)
    analyzed = Column(Integer, default=0)  # 0 未分析 1 已分析
    created_at = Column(DateTime, default=now)


class ProjectMember(Base):
    __tablename__ = "project_members"
    id = Column(String(64), primary_key=True, default=uid)
    project_id = Column(String(64), nullable=False)
    user_id = Column(String(64), nullable=False)


class NotifyChannel(Base):
    __tablename__ = "notify_channels"
    id = Column(String(64), primary_key=True, default=uid)
    name = Column(String(128), nullable=False)
    url = Column(String(512), nullable=False)          # 钉钉/企微 群机器人 webhook
    on_fail = Column(Boolean, default=True)
    enabled = Column(Boolean, default=True)
    last_status = Column(String(64), default="")
    created_at = Column(DateTime, default=now)


class LLMConfig(Base):
    """模型配置列表（「模型配置」页维护，可添加多个厂商/模型，其中一条设为「使用中」）。

    url_type: api=开放平台按量 API；plan=Token/Coding 套餐（部分厂商走独立地址）。
    无使用中的条目时，退回 .env 兜底配置（TD_LLM_*）。
    """
    __tablename__ = "llm_configs"
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(128), default="")                 # 显示名，空则用模型名
    vendor = Column(String(64), default="")                # 厂商名
    url_type = Column(String(16), default="api")           # api | plan | plan2
    base_url = Column(String(512), default="")
    api_key = Column(String(512), default="")
    model = Column(String(128), default="glm-4-flash")
    vision = Column(Boolean, default=False)                # 模型支持图片输入（视觉识别验证码等）
    is_active = Column(Boolean, default=False)             # 使用中（全局唯一）
    created_at = Column(DateTime, default=now)


class LLMLog(Base):
    __tablename__ = "llm_logs"
    id = Column(String(64), primary_key=True, default=uid)
    kind = Column(String(32), default="")                  # gen-commits | gen-text | analyze | agent-step
    model = Column(String(128), default="")
    prompt_tokens = Column(Integer, default=0)
    completion_tokens = Column(Integer, default=0)
    ok = Column(Boolean, default=True)
    created_at = Column(DateTime, default=now)


class ApiDoc(Base):
    """导入的 API 文档（Swagger2 / OpenAPI3 / Postman；Apifox 导出 OpenAPI 亦可）。"""
    __tablename__ = "api_docs"
    id = Column(String(64), primary_key=True, default=uid)
    project_id = Column(String(64), nullable=False)
    name = Column(String(255), default="")
    format = Column(String(32), default="")            # swagger2 | openapi3 | postman
    base_url = Column(String(512), default="")
    spec = Column(JSON, default=dict)                  # 解析出的端点（回溯用）
    endpoints_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=now)


class ApiEndpoint(Base):
    """文档解析出的接口端点（喂给 AI-API 用例的确切接口清单）。"""
    __tablename__ = "api_endpoints"
    id = Column(String(64), primary_key=True, default=uid)
    project_id = Column(String(64), nullable=False)
    doc_id = Column(String(64), nullable=False)
    method = Column(String(16), nullable=False)
    path = Column(String(512), nullable=False)
    summary = Column(String(255), default="")
    params = Column(JSON, default=dict)                # {query:[{name,type}], header, path, body:{字段:类型}}
    created_at = Column(DateTime, default=now)


class Flow(Base):
    """流程测试：按业务线串起多角色的 API/UI 步骤。

    roles: [{"key":"shipper","name":"货主","variables":{"username":"sw","password":"***"}}, ...]
    steps: [{"role":"shipper","type":"ui","action":"goto","url":"/page/x", ...}
            {"role":"shipper","type":"api","m":"POST","url":"/api/x","check":{...},"save":{"name":"inquiry_id","from":"data.id"}}]
    save 提取的变量进入共享命名空间，后续任意角色的步骤可用 ${name} 引用。
    执行记录统一存 TestRun（flow_id 指向本表）。
    """
    __tablename__ = "flows"
    id = Column(String(64), primary_key=True, default=uid)
    project_id = Column(String(64), nullable=False)
    name = Column(String(255), nullable=False)
    desc = Column(Text, default="")
    roles = Column(JSON, default=list)
    steps = Column(JSON, default=list)
    updated_at = Column(DateTime, default=now, onupdate=now)
