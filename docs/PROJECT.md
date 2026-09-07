# TestDeck · 自动化测试平台 · 项目文档

面向非专业测试人员的团队内部测试平台：填表式创建测试，不写代码。
测试用例统一由 AI 驱动：目标下分 API 测试（模型自主设计请求）与 UI 测试（模型看页面操作，支持视觉识别验证码），支持录制/固化为固定步骤、多角色流程测试、接口文档导入（Swagger/OpenAPI/Postman）、定时/Git 触发与失败告警。

> 快速上手见根目录 `README.md`；本文档是完整的架构、机制、配置与运维说明。

---

## 1. 功能总览

| 模块 | 能力 |
|---|---|
| 认证与用户 | JWT 登录，admin / member 双角色；独立用户管理（建号、重置密码、自助改密） |
| 项目 / 环境 | 多项目管理，项目级权限隔离（成员只见自己创建或被加入的项目）；每个项目**一个环境地址 + 附加变量**，并提供**项目级测试用户列表**（账号密码池，支持批量导入），创建用例与流程角色时选择带出、可改 |
| AI 用例（统一形态） | 目标下分两类：**API 测试**——模型根据目标自主设计请求（方法/路径/头/体）并发送、检查响应、记住 token；**UI 测试**——模型看页面状态（+截图，可选）实时决策点哪/填什么；支持拖拽（滑块验证码）与坐标点击（点选验证码）。可录制/固化为**固定步骤**回放，回归零 token。历史手动 API/UI 用例仍可执行与删除 |
| 流程测试 | 按业务线串联多角色：每角色独立登录态（API 各自 cookie、UI 各自浏览器会话）；API/UI/AI 步骤混排；**引用已有用例作为一步**（在角色会话内联执行，save 结果直通共享区）；共享变量传递业务单据；截图基线对比（视觉回归）；泳道图展示；执行流式进度、可取消 |
| 测试计划 | **用例 + 流程**的批量执行容器（条目互相独立，失败不中断批次）+ 默认环境；手动 / cron 定时 / Git 默认分支 push 三种触发；执行记录统一进「执行记录」 |
| AI 辅助 | 从 Git 提交生成用例草稿、自然语言生成用例、失败原因分析（用例/计划/流程）、用量统计、回归建议（用例与流程） |
| Git 集成 | 绑定仓库生成 webhook（兼容 GitHub/GitLab push），提交同步并可自动触发回归；内网可用 git-sync CLI |
| 通知 | 执行失败自动推送钉钉/企微群机器人，支持测试发送 |
| 模型配置 | 界面化管理多家厂商模型（国内外 19 家预置 + 自定义），区分按量 API / Token 套餐接入，在线拉取模型列表，连接测试，保存即生效 |
| 报告导出 | 单次执行导出独立 HTML 报告；执行记录导出 CSV |
| 回归建议 | 工作台提示超 14 天未执行的用例与近 7 天新提交 |
| 运维 | 截图 30 天自动清理（`TD_KEEP_DAYS`）；LLM 调用与 token 用量记录 |

## 2. 系统架构

```
浏览器（Vue 3 + Vite SPA）
   │  /api/v1/*（fetch，JWT）
   ▼
FastAPI 后端（uvicorn，单进程）
   ├─ routers/        REST 接口层（auth projects cases runs flows plans git ai settings）
   ├─ engine/         执行引擎
   │    ├─ runner.py       API 用例：变量替换 → httpx → 检查点 → 记住返回值
   │    ├─ ui_runner.py    UI 用例：Playwright 无头浏览器动作编排
   │    ├─ flow_runner.py  流程测试：多角色 API client / 浏览器 context 隔离
   │    ├─ ai_runner.py    AI 用例：页面状态(+截图) → 大模型决策 → 执行动作 循环
   │    ├─ ui_recorder.py  录制：remote 画面串流 / local 弹窗浏览器
   │    ├─ browser.py      引擎选择：Chromium Headless Shell / Lightpanda(CDP)
   │    └─ queue.py        单工作线程串行队列（避免 SQLite 写锁，天然限流）
   ├─ ai.py           LLM 适配（OpenAI 兼容，文本/视觉；未配置降级内置规则）
   ├─ scheduler.py    APScheduler cron 调度（时区 Asia/Shanghai）
   ├─ notify.py       钉钉/企微 webhook 告警
   └─ db.py models.py SQLAlchemy + SQLite（单文件库，挂卷持久化）
```

关键设计决策：

- **串行执行队列**：所有测试（用例/计划/流程）提交到同一个单工作线程同步执行。避免并发写冲突，也杜绝跨线程事件循环问题；内部团队规模下单并发足够。
- **数据库可插拔**：SQLAlchemy 适配层，默认 SQLite（零配置，本地开发与测试）；生产设 `TD_DATABASE_URL` 一键切 PostgreSQL 或 MySQL（Docker 部署已内置 PostgreSQL 16，健康检查就绪后才启动后端）。`python -m app.cli.db_migrate --to <连接串>` 可把现有 SQLite 数据迁入。
- **AI 决策循环**：`提取页面状态（可见元素/DOM 文本/可选截图）→ LLM 输出 JSON 动作 → 执行 → 回填历史`，直到模型输出 done、连续 3 次相同动作失败熔断、或达到最大步数。
- **渐进式进度**：AI 用例每执行一步、计划每完成一条用例即增量写库；前端轮询渲染，无需长连接。

## 3. 目录结构

```
backend/
  app/
    main.py            入口（建表、轻量迁移、admin 种子、演示数据、静态截图服务）
    auth.py models.py  JWT 认证 / ORM 模型（15 张表）
    db.py perms.py     SQLite 引擎 / 项目级权限
    ai.py              LLM 适配层（配置来源：llm_configs 表 > .env 兜底配置）
    scheduler.py notify.py maintenance.py
    routers/           auth projects cases runs flows plans git ai settings
    engine/            runner ui_runner flow_runner ai_runner ui_recorder browser queue
    cli/git_sync.py    内网无 webhook 时同步提交的命令行
  tests/               单元 + 接口测试（test_*.py）、e2e 脚本、mock 被测系统、假 LLM 服务器
  static/              执行截图（30 天自动清理；禁止对外公开）
frontend/
  src/
    views/             Login Dash Projects Cases Flows Plans Runs Settings Users LLM
    components/        CaseEditor RecorderModal RunDrawer ReportDrawer AiDrawer FlowDiagram DialogHost
    dialog.js          全局 Promise 风格弹窗（alert/confirm/prompt/toast）
  api.js main.js       fetch 封装 / 路由
docs/                  本文档与交互原型（docs/test-platform-ui/index.html）
```

## 4. 核心机制

### 4.1 变量系统
- 环境变量（项目页「环境」管理，如 `username`/`password`）注入所有步骤与 AI 目标，`${name}` 引用。
- API 步骤「记住返回值」（如 `data.token` → `${token}`）在同用例后续步骤可用。
- 流程测试中 AI 步骤的 `save` 与 API 步骤的 `save` 共享同一命名空间，实现"AI 创建单据 → 后续角色引用单号"。
- 流程「引用用例」步骤在所选角色的会话里内联执行：用例内部的 `${变量}` 能读到角色变量与全流程共享变量，其 save 的结果自动进入共享区——接线无需配置，天然生效。

### 4.2 UI 可视化录制
- **remote 模式**（默认）：后端起无头浏览器，用 Chromium Screencast（Playwright `page.screencast`）推流——页面重绘即产生 JPEG 帧经 WebSocket 推给前端，无变化不推送（空闲零开销）；用户在画面上点击/输入/跳转，指令经队列回传执行并记录为步骤。支持远程与容器部署，画面区误点不记录、连点去重、连续输入合并。
- **AI 代劳（混合录制）**：录制中输入一句话目标（如"用 ${username} 登录，看到欢迎页为止"），AI 在当前录制页面上执行（`ai_drive`，每步刷新画面），**达成目标后整轮记为一个「AI 代劳」步骤（只存目标文本）**——回放时 AI 在该步骤现场重新执行，验证码等动态内容每次重新识别；人工点选的部分仍为零 token 固定步骤。流程角色录制时尤为省事。可选注入某个环境的变量。
- **录完 AI 增强**：完成录制后可一键让大模型补充关键断言（expect_text）、把账号/密码类输入参数化为 `${变量}`（附建议默认值）、起用例名；未配置模型时原样返回并引导，原始步骤随时可用。
- **local 模式**：后端本机弹出有头浏览器，注入脚本记录，关窗后编译步骤（仅本地开发可用）。

### 4.3 AI 智能测试
- 用例配置：`测试对象(target: ui/api)` + `目标(goal)` + `起始页面`(UI) + `最大步数` + 可选`引擎`。
- **API 目标**：模型每轮输出 request（方法/相对路径/头/体），经 httpx 发往环境 Base URL；check 四类检查点复用；save 记住 token/单据号供后续请求引用。
- **接口文档导入**：Swagger 2.0 / OpenAPI 3.x / Postman Collection（Apifox 导出 OpenAPI 亦可）解析为项目接口库（含参数与字段定义，$ref 自动展开），用例勾选接口后 AI 按真实接口设计请求。
- **固定步骤**：录制或固化的产物（UI: fixed_steps / API: fixed_api_steps），存在且无 goal 时按步骤原样回放，零 token。
- 每轮发给模型：目标、可用变量（含角色/共享变量）、最近 12 步历史、可见交互元素（选择器含 id/name/placeholder/type/同标签序号兜底）、页面文字摘要；若模型配置勾选「支持视觉」再附当前视口截图。
- 模型动作集：`goto / click / click_xy / drag / fill / expect_text / save / done`。
- 防失控：连续 3 次相同动作失败熔断；最大步数上限；每次执行记录 token 用量。
- 固化：AI 跑通后可把操作明细转存为普通 UI 用例，回归零 token。

### 4.4 浏览器引擎
- 默认 Chromium Headless Shell（Playwright 精简无头内核）。
- 可选 Lightpanda（AI 原生轻量引擎，beta，内存约为 Chromium 的 1/9）：经 CDP 连接，支持自动拉起进程（`TD_LIGHTPANDA_BIN`）；不可用时自动回退 Chromium，回退信息写入执行明细。
- 低配部署：`docker-compose.lowmem.yml` 后端镜像不装 Chromium，统一走 Lightpanda 容器。

### 4.5 高级断言与视觉回归
- **JSONPath 断言**：检查点类型选「JSONPath 断言（高级）」，字段写表达式（如 `$.data.list[*].id`）；期望值留空 = 匹配到任意值即通过，填值 = 任一匹配值等于它（弱类型）即通过。四类基础检查点（status/contains/field_eq/not_empty）之外的兜底能力。
- **截图基线对比**：流程的「截图留档」步骤，首次通过自动留存基线（`static/base-{flow}-{步骤}.png`），之后每次执行与基线做像素比对，差异超过阈值（`TD_SHOT_DIFF_PCT`，默认 2%）判失败并生成「基线｜本次」并排对比图；`TD_SHOT_DIFF=0` 关闭。调整流程步骤顺序后基线对应关系会变化：流程列表「基线」入口可查看/清除，或重新跑一次成功执行刷新。

### 4.6 调度与触发
- 计划保存即同步 Schedule 表并重建 APScheduler 任务（`分 时 日 月 周`）。
- Git webhook（随机 secret 鉴权）收到默认分支 push → 落库提交 + 触发 trigger=git 的计划；内网用 `python -m app.cli.git_sync --repo <路径> --webhook <地址>`。

### 4.7 权限模型
- admin 全可见；member 仅见自己创建或被加入的项目（`perms.check_project_access`）。
- 模型配置、用户管理、通知渠道写操作仅 admin。

## 5. 数据模型（14 表）

`users` 账号 · `projects` 项目 · `project_members` 成员 · `envs` 环境 · `test_cases` 用例（api/ui/ai）·
`test_plans` 计划（`case_ids` + `flow_ids`）· `schedules` cron 调度 ·
`test_runs` **统一执行记录**（单用例 `case_id` / 单流程 `flow_id` / 整计划 `plan_id` 三种来源，流程历史也查此表）·
`flows` 流程定义 ·
`git_repos` 仓库绑定（含 webhook secret）· `commit_syncs` 同步的提交 · `notify_channels` 告警渠道（含 webhook 地址）·
`llm_configs` 模型配置（**含 API Key 明文**）· `llm_logs` LLM 调用与 token 用量。

> 旧版独立 `flow_runs` 表已在启动迁移中并入 `test_runs`（数据自动搬运后删表）。

## 6. API 一览（前缀 `/api/v1`）

| 分组 | 端点 |
|---|---|
| 认证 | `POST /auth/login` · `GET /auth/me` · `POST/GET /auth/users` · `PUT /auth/password` · `PUT /auth/users/{uid}/password` |
| 项目 | `GET/POST /projects` · `PUT/DELETE /projects/{pid}` · `GET/POST /projects/{pid}/envs` · `PUT/DELETE .../envs/{eid}` · `GET/POST /projects/{pid}/members` · `DELETE .../members/{uid}` |
| 用例 | `GET/POST /projects/{pid}/cases` · `GET/PUT/DELETE /cases/{cid}` |
| 执行 | `POST /runs/cases/{cid}/run` · `POST /runs/plans/{pid}/run` · `GET /runs` · `GET /runs/{rid}` · `GET /runs/{rid}/export` · `GET /runs/export.csv` |
| 流程 | `GET/POST /flows` · `GET/PUT/DELETE /flows/{fid}` · `POST /flows/{fid}/run` · `GET /flows/{fid}/runs` · `GET /flows/runs/{rid}/detail` |
| 计划 | `GET/POST /plans` · `PUT/DELETE /plans/{pid}` |
| Git | `GET/POST /integrations/git/repos` · `DELETE .../repos/{rid}` · `GET /integrations/git/commits` · `POST /integrations/git/webhook/{secret}` |
| AI | `POST /ai/gen-from-commits` · `POST /ai/gen-from-text` · `POST /ai/analyze-run/{rid}` · `GET /ai/usage` · `GET /ai/regression-advice` |
| 设置 | `GET/POST /settings/notify` · `PUT/DELETE /settings/notify/{cid}` · `POST /settings/notify/{cid}/test` · `GET/POST /settings/llm` · `PUT/DELETE /settings/llm/{id}` · `POST /settings/llm/{id}/activate` · `POST /settings/llm/test` · `POST /settings/llm/models` |
| 录制 | `POST /cases/ui-record/start` · `GET /cases/ui-record/{sid}/frame` · `POST /cases/ui-record/{sid}/cmd` · `GET /cases/ui-record/{sid}` |
| 健康 | `GET /health` |

## 7. 配置参考

### 配置项（.env 文件）

| 变量 | 默认 | 说明 |
|---|---|---|
| `TD_DB` | `backend/testdeck.db` | SQLite 路径，本地开发/测试默认 |
| `TD_DATABASE_URL` | 未设 | 生产数据库连接串（PostgreSQL/MySQL），设置后优先于 SQLite |
| `TD_SECRET` | 开发默认值 | JWT 签名密钥，**生产必须设置** |
| `TD_ADMIN_PASSWORD` | `admin123` | 初始 admin 密码，**生产必须修改** |
| `TD_SEED_DEMO` | `1` | 是否预置询价单演示数据 |
| `TD_NO_SCHEDULER` | 未设 | 设为 1 禁用调度（测试用） |
| `TD_KEEP_DAYS` | `30` | 截图保留天数 |
| `TD_SHOT_DIFF` | `1` | 设为 0 关闭流程截图基线对比 |
| `TD_SHOT_DIFF_PCT` | `2` | 截图与基线的差异阈值（百分比，超过判失败） |
| `TD_BROWSER_ENGINE` | `chromium` | `lightpanda` 启用轻量引擎 |
| `TD_LIGHTPANDA_URL` | `http://127.0.0.1:9222` | Lightpanda CDP 地址 |
| `TD_LIGHTPANDA_BIN` | 未设 | lightpanda 可执行文件路径，设置后平台自动拉起 |
| `TD_LLM_BASE_URL/KEY/MODEL` | 未设 | LLM 兜底配置（「模型配置」页的库内配置优先） |

### 模型配置页
多厂商配置列表（智谱/DeepSeek/通义/Kimi/MiniMax/OpenAI/Claude/Gemini/OpenRouter/Ollama 等 19 项预置），每条含：
接入方式（按量 API / Token 套餐，Key 各自独立）、Base URL、API Key（存库，回显脱敏）、模型名（可在线拉取厂商 `/models` 列表）、
「支持视觉」开关（勾选后 AI 用例每步附截图，token 约增 1~2k/步）、连接测试。其中一条设为「使用中」，AI 功能全部走它；无使用中条目时回退 .env 兜底配置。

## 8. 部署

| 方式 | 命令 | 说明 |
|---|---|---|
| 本地开发 | `uvicorn app.main:app --port 8000` + `npm run dev` | 前端 5173 已代理 /api |
| 标准 Docker | `docker compose up -d --build` | 8080 端口，含完整 Chromium，内置 PostgreSQL 16 |
| 低配服务器 | `docker compose -f docker-compose.lowmem.yml up -d --build` | 后端镜像无 Chromium，UI/AI 走 Lightpanda 容器 |

## 9. 测试

```bash
cd backend && .venv/Scripts/python -m pytest tests -q     # 76 个单元/接口测试
# E2E（需先起后端与 mock 被测系统 9001）：
tests/e2e.py e2e_m2.py e2e_m3.py e2e_m5.py e2e_flow.py
# 假 LLM 服务器（AI 引擎联调用）：uvicorn tests.fake_llm:app --port 9111
```

## 10. 安全与隐私（公开/外发前必读）

**绝不可公开或提交到仓库的文件**（含真实密钥与业务数据）：

| 路径 | 内容 |
|---|---|
| `backend/testdeck.db` | **全部运行数据**：模型 API Key 明文、账号密码哈希、告警 webhook 地址（含 access_token）、执行记录与被测系统响应 |
| `backend/static/` | 执行截图（可能含被测系统页面与业务数据） |
| `backend/.venv/`、`frontend/node_modules/`、`frontend/dist/` | 本地产物/构建产物 |

**代码中允许公开的敏感字样**（均为示例或带安全说明）：
`admin123`（默认种子密码，README 明确要求生产修改）、测试脚本中的 `sk-test1234567890` 等假 Key、placeholder 中的 `192.168.1.10` 示例地址。

**发布前检查清单**：
1. 确认 `.gitignore` 生效（仓库根已提供），`git status` 不含上表三类路径；
2. 若仓库曾误提交 `testdeck.db`，需轮换该库中所有 LLM Key 与 webhook 地址（git 历史会保留）；
3. 生产部署修改 `TD_ADMIN_PASSWORD` 与 `TD_SECRET`；
4. 告警 webhook 与模型 Key 只存运行库，泄露即轮换。
