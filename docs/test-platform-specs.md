# TestDeck · 自动化测试平台 实施规格 v0.1

> 2026-09-04 · 配套原型：`test-platform-ui/index.html` · 本文件是一期开发的单一事实源，契约变更先改本文。

## 1. 产品定位

面向**非专业测试人员**的团队内部综合测试平台：像填表一样完成 API/UI 测试，不写代码；同时借助 **AI** 和 **Git 集成**让用例"自动长出来"，而不是全靠手写。

角色（v1 从简）：
- **管理员**：管理项目/成员/环境/定时任务/集成配置
- **成员**：创建与执行用例、查看报告

## 2. 技术栈

| 层 | 选型 | 说明 |
|---|---|---|
| 后端 | FastAPI + SQLAlchemy + SQLite(默认，可换 PG) | |
| 任务执行 | asyncio 内置队列 + APScheduler(cron) | v1 单机；量大再引 Celery |
| 前端 | Vue3 + Vite + Pinia，风格按现有原型 | |
| HTTP 执行 | httpx | 支持变量替换、超时、忽略 TLS（可选） |
| UI 执行 | Playwright（二期，独立 worker） | |
| LLM | OpenAI 兼容接口适配层（可接 GLM/DeepSeek/内部网关） | model 名走配置，不绑厂商 |
| Git | webhook(GitHub/GitLab/Gitea) + 本地 git clone 分析 | |

## 3. 数据模型（核心）

```
Project    id, name, desc, owner_id, created_at
Env        id, project_id, name, base_url, vars(JSON), created_at
TestCase   id, project_id, name, type(api|ui), steps(JSON), source(manual|ai|git), creator_id, updated_at
TestPlan   id, project_id, name, case_ids(JSON), env_id, trigger(manual|cron|git), cron, git_branch, enabled
TestRun    id, plan_id?, case_id?, env_id, status, pass_n, fail_n, duration, detail(JSON), trigger_by(user|cron|git), created_at
GitRepo    id, project_id, provider, repo_url, webhook_secret, default_branch, token(加密)
CommitSync id, repo_id, commit_sha, author, message, analyzed(0|1|2), created_at
AiTask     id, kind(gen-case|analyze-fail|nl-case), input(JSON), output(JSON), status, cost_tokens, created_at
```

### 3.1 步骤 JSON（与原型「填表式」一一对应）

```json
{
  "m": "POST",
  "url": "/api/login",
  "headers": "Authorization: Bearer ${token}",
  "body": "{\"username\": \"${username}\"}",
  "check": { "type": "status|contains|field_eq|not_empty",
             "field": "code",        // field_eq 时
             "expect": "0" },
  "save":  { "name": "token", "from": "data.token" }   // 可选
}
```

**检查点 → 断言映射**（引擎内实现，用户无感）：
`status` → resp.status_code==200；`contains` → expect 子串在 body 中；`field_eq` → jsonpath(field) 值 == expect（弱类型比较）；`not_empty` → jsonpath 字段存在且非空。高级用户可在「高级选项」写原生 JSONPath 断言，二者并存。

变量替换：`${name}` 从 环境变量 + 前置步骤 save 结果 求值；步骤失败默认中止用例（可勾选"失败继续"）。

## 4. API 契约（REST，JWT Bearer，`/api/v1`）

| 模块 | 端点 |
|---|---|
| 认证 | POST /auth/login · POST /auth/users(admin) |
| 项目/环境 | CRUD: /projects, /projects/{id}/envs |
| 用例 | CRUD: /projects/{id}/cases · POST /cases/{id}/run |
| 计划 | CRUD: /plans · POST /plans/{id}/run |
| 执行 | GET /runs?plan=&case=&page= · GET /runs/{id}（含步骤明细） |
| 定时 | GET/PUT /schedules（启停、改 cron） |
| Git | POST /integrations/git/repos · GET /integrations/git/commits?repo= · POST /webhook/git/{secret}（无需 JWT） |
| AI | POST /ai/gen-from-commits {repo, shas[]} · POST /ai/gen-from-text {prompt} · POST /ai/analyze-run/{run_id} |

## 5. AI 能力设计（统一原则：**AI 只产草稿，保存必须人工确认**）

1. **从 Git 提交生成用例**（核心差异化）
   流水线：取 commit message + diff → LLM 提取「本次改动影响的接口/页面」→ 匹配平台已有用例：
   - 已覆盖 → 提示"该接口已有用例 X，是否执行"
   - 未覆盖 → 生成用例草稿（含建议检查点），用户在抽屉里确认/修改后保存
   diff 不发给外部 LLM 可配置（脱敏：仅发 message + 变更文件路径 + 接口签名，默认不发代码体）
2. **自然语言生成用例**：「测一下登录接口，密码错误时应该返回 401」→ 草稿
3. **失败分析**：失败 Run 的请求/响应 → LLM 给出可能原因与建议，附在报告页
4. **定时回归建议**：长期不执行的用例、近期改动多的模块 → 工作台提醒

LLM 输出强制 JSON Schema（步骤结构同 3.1），解析失败自动重试一次。

## 6. Git 集成设计

- **配置**：项目绑定 Git 仓库（webhook secret + 只读 token）
- **webhook 事件**：
  - `push` / `mr-merged`：按分支匹配计划 → `trigger=git` 的计划自动执行（即"执行已有测试流程"）
  - 同时入 `CommitSync` 队列供 AI 分析
- **手动分析**：用例页「从提交生成」→ 选最近提交（可多选）→ 走 §5.1 流水线
- 兼容：无 webhook 环境（内网）提供 `testdeck git-sync` 命令行/CI 步骤拉取分析

## 7. 权限

v1 两角色：管理员全权；成员可建/改自己项目内资产、执行一切、看全部报告；项目级隔离（成员只看被加入的项目）。Git token 与 webhook secret 服务端加密存储。

## 8. 部署

docker-compose 单机：`backend(uvicorn) + frontend(nginx) + sqlite volume`；LLM 走外部 API（key 环境变量注入）。2C4G 可跑（Playwright worker 二期再加容器）。

## 9. 分期

- **M1 · API 测试闭环**（3.1/3.3/4 除 Git/AI 外全部）：登录、项目/环境/用例/计划、执行引擎、报告、cron
- **M2 · AI + Git**：§5、§6 全部，重点验收"push 到 merge 后 30 分钟内，平台给出用例建议或已自动执行回归"
- **M3 · UI 测试**：Playwright worker、录制回放、截图对比
