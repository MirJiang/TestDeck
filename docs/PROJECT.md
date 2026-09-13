# TestDeck · 自动化测试平台 · 项目文档

面向非专业测试人员的团队内部测试平台：填表式创建测试，不写代码。
测试用例统一由 AI 驱动：目标下分 API 测试（模型自主设计请求）与 UI 测试（模型看页面操作，支持视觉识别验证码），支持录制/固化为固定步骤、多角色流程测试、接口文档导入（Swagger/OpenAPI/Postman）、定时/Git 触发与失败告警。

> 快速上手见根目录 `README.md`；本文档是完整的架构、机制、配置与运维说明。

---

## 1. 功能总览

| 模块 | 能力 |
|---|---|
| 认证与用户 | JWT 登录（滑动续期：令牌剩余不足半程自动换发 X-Renewed-Token），admin / member 双角色；独立用户管理（建号、重置密码、自助改密） |
| 项目 / 环境 | 多项目管理，项目级权限隔离（成员只见自己创建或被加入的项目）；每个项目**一个环境地址 + 附加变量**，并提供**项目级测试用户列表**（账号密码池，支持批量导入），创建用例与流程角色时选择带出、可改 |
| AI 用例（统一形态） | 目标下分两类：**API 测试**——模型根据目标自主设计请求（方法/路径/头/体）并发送、检查响应、记住 token；**UI 测试**——模型看页面状态（+截图，可选）实时决策点哪/填什么；支持拖拽（滑块验证码）与坐标点击（点选验证码）。可录制/固化为**固定步骤**回放，回归零 token。历史手动 API/UI 用例仍可执行与删除 |
| 流程测试 | 按业务线串联多角色：每角色独立登录态（API 各自 cookie、UI 各自浏览器会话）；API/UI/AI 步骤混排；**引用已有用例作为一步**（在角色会话内联执行，save 结果直通共享区）；共享变量传递业务单据；截图基线对比（视觉回归）；泳道图展示；执行流式进度、可取消 |
| 测试计划 | **用例 + 流程**的批量执行容器（条目互相独立，失败不中断批次）+ 默认环境；手动 / cron 定时 / Git 默认分支 push 三种触发；执行记录统一进「执行记录」 |
| AI 辅助 | 从 Git 提交生成用例草稿、自然语言生成用例、失败原因分析（用例/计划/流程）、用量统计、回归建议（用例与流程） |
| Git 集成 | 绑定仓库生成 webhook（兼容 GitHub/GitLab push），提交同步并可自动触发回归；内网可用 git-sync CLI |
| 通知 | 执行失败自动推送钉钉/企微群机器人，支持测试发送 |
| 模型配置 | 界面化管理多家厂商模型（国内外 19 家预置 + 自定义），区分按量 API / Token 套餐接入，在线拉取模型列表，连接测试，保存即生效 |
| 报告导出 | 单次执行导出独立 HTML 报告；执行记录导出 CSV |
| MCP 服务 | 平台能力暴露为 MCP 工具（`/mcp`，Streamable HTTP）：外部 AI agent 带平台 token 即可查项目/用例、跑测试、看结果、上传应用地图、**借受控浏览器会话干活**（白名单动作，无 shell/文件能力） |
| 应用地图 | **期望基线**（只由扫描/源码分析/人工写入，执行观察不回写）：多角色扫描合并按角色可见性、元素带来源（扫描/源码/人工）与状态备注；AI 执行时每步比对，地图按钮缺失记**警告**（元素级回归信号，不判失败） |
| 回归建议 | 工作台提示超 14 天未执行的用例与近 7 天新提交 |
| 运维 | 截图 30 天自动清理（`TD_KEEP_DAYS`）；LLM 调用与 token 用量记录（按次归属执行记录，执行列表/详情可见；模型配置页按天汇总近 14 天） |

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
   │    ├─ ai_runner.py    AI 用例门面：ai_drive 委托 brain_agentscope；含 AI-API 请求循环与动作执行层
   │    ├─ brain_agentscope.py  AgentScope Brain：ReAct agent + 白名单工具集 + 多模型回退链
   │    ├─ app_mapper.py   应用地图：多角色爬取合并 / 执行比对信号 / upsert 合并写入
   │    ├─ browser_sessions.py  MCP 浏览器会话：外部 agent 借受控页面干活（独占线程+命令泵）
   │    ├─ ui_recorder.py  录制：remote 画面串流 / local 弹窗浏览器
   │    ├─ browser.py      引擎选择：Chromium Headless Shell / Lightpanda(CDP)
   │    └─ queue.py        执行队列：TD_WORKERS 可配并发（默认 1 保持串行；SQLite 已配 busy_timeout）
   ├─ ai.py           LLM 适配（OpenAI 兼容，文本/视觉；未配置降级内置规则）
   ├─ scheduler.py    APScheduler cron 调度（时区 Asia/Shanghai）
   ├─ notify.py       钉钉/企微 webhook 告警
   └─ db.py models.py SQLAlchemy + SQLite（单文件库，挂卷持久化）
```

关键设计决策：

- **执行队列（默认串行，可开并发）**：所有测试提交到同一个线程池，默认 1 个 worker 天然串行——避免并发写冲突，也杜绝跨线程事件循环问题。团队规模上来后 `.env` 设 `TD_WORKERS=N`（≤16）开启并发执行；SQLite 已配 `busy_timeout`，多 worker 写锁等待自动重试，生产建议切 PostgreSQL/MySQL。
- **数据库可插拔**：SQLAlchemy 适配层，默认 SQLite（零配置，本地开发与测试）；生产设 `TD_DATABASE_URL` 一键切 PostgreSQL 或 MySQL（Docker 部署已内置 PostgreSQL 16，健康检查就绪后才启动后端）。`python -m app.cli.db_migrate --to <连接串>` 可把现有 SQLite 数据迁入。
- **AI 决策循环（AgentScope，B4 基准达标后唯一路径）**：`ai_drive` 门面委托 AgentScope ReAct agent——动作注册为白名单工具、done 走结构化输出、模型走 llm_configs 多模型回退链（失败自动换下一档）。真实环境基准（登录用例各 3 轮）：成功率 1/3 vs legacy 0/3（通过轮当场解开点选验证码）、平均 22.5s vs 94.7s；token 偏高（≈3 倍，思考模式 + ReAct 重发历史）但绝对值可忽略。防失控机制：连续 3 次相同动作失败熔断、最大步数上限、取消即停、token 用量入 `llm_logs`。模型兼容层（`brain_agentscope`）：思考模式模型（DeepSeek 等）拒绝 tool_choice="none"/强制函数，回退链自动降级；工具函数返回 ToolChunk 保证截图以 image_url 进消息（返回 ToolResponse 会被序列化成文本，一张截图 ≈13 万 token 撑爆上下文）。
- **渐进式进度**：AI 用例每执行一步、计划每完成一条用例即增量写库；前端轮询渲染，无需长连接。触发接口立即返回（执行在队列线程继续），避免长执行把 HTTP 请求拖到超时。

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
    engine/            runner ui_runner flow_runner ai_runner brain_agentscope app_mapper browser_sessions ui_recorder browser queue
    cli/git_sync.py    内网无 webhook 时同步提交的命令行
  tests/               单元 + 接口测试（test_*.py）、e2e 脚本、mock 被测系统、假 LLM 服务器
  static/              执行截图（30 天自动清理；禁止对外公开）
frontend/
  src/
    views/             Login Dash Projects Cases Flows Plans Runs Settings Users LLM AppMap
    components/        CaseEditor RecorderModal RunDrawer ReportDrawer FlowDiagram DialogHost
    dialog.js          全局 Promise 风格弹窗（alert/confirm/prompt/toast）
  api.js main.js       fetch 封装 / 路由
skills/                交付给外部 agent 的 Skill（app-map-source-scan：源码分析生成地图，经 MCP 上传）
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
- **AgentScope Brain（唯一决策循环）**：`ai_drive` 委托 AgentScope ReAct agent（`engine/brain_agentscope.py`）——动作集注册为白名单工具（权限引擎 DONT_ASK + ALLOW 规则强制，绝不注册 shell/文件/代码执行类工具）、done 走结构化输出、模型走 llm_configs 多模型回退链（失败自动换下一档）、视觉模式经 `browser_look` 工具按需取截图（实测可解开点选验证码）。执行明细/on_step 流式进度/取消/断路器/token 记账与页面状态注入与旧循环同构，调用方零改动。
- 防失控：连续 3 次相同动作失败熔断；最大步数上限（默认 200，用例/流程步骤可调）；每次执行的 token 消耗按 run_id 归属 `llm_logs`，执行列表/详情与模型配置页（按天）可见。**token 优化**：工具每步返回的页面状态做增量回报——同页面且元素集合一致时只回紧凑摘要（输入值变化单独列出）；**元素短句柄寻址**（借鉴编码 agent 的「文件:行号」稳定寻址）：状态回包里元素标注为 `[b3] input 请输入账号`，动作填句柄即可，句柄整个执行期与 selector 一一对应不复用，比每步重发完整 CSS 选择器省大量 token；`browser_fill_many` 一次批量填 ≤12 个输入框（工具层拆成原子 fill，明细与固化回放不变）；上下文压缩阈值由 `TD_MODEL_CONTEXT_SIZE` 控制（默认 65536，0=用模型默认 128k），压缩触发比例 0.3（阈值 ≈20k，原 0.8≈52k）、压缩后保留比例 0.06、截图仅保留最近一张（每张估算 2000 token，每步推理前裁剪）、单条工具结果上限 12k，压缩摘要为结构化「测试状态卡片」（目标/当前页/表单已填/变量/下一步/坑），比通用续作摘要更省且不失真（压缩调用同样经模型回退链记账，计入 llm_logs）；地图已收录的页面省略页面文字段，但含校验/报错关键词（必填/失败/错误等）时保留——校验提示是临时文字、地图里没有；click 常规超时（5s）后强制重试一次，减少模型"看截图改坐标"的两步回退；日期/时间提示词引导优先 fill 完整值。历史在两次压缩之间纯追加（前缀稳定），利于 qwen 系隐式上下文缓存计费折扣；压缩是唯一的缓存重置点。**弹窗优先提取**：可见 dialog/drawer 的内容单独提取（配额 40，主页面 80）——弹窗 DOM 追加在 body 末尾，顺序遍历会被主页面挤出上限导致 AI 对弹窗盲操；弹窗内零交互信号的表格行降级收录为可点条目（row，弹窗里的行几乎必然响应点击/双击）。选择器重复时追加 `>> nth=k`，同文案元素（如两个「添加」按钮）各自有唯一句柄。白名单含 browser_dblclick（双击选行类弹窗，单击高亮双击选中的控件）、browser_click_xy_many（验证码点选一次点完 N 个坐标，省逐点往返）、browser_scroll（滚轮滚动，不依赖滚动条位置，滚的是指针所在的可滚动区域）；状态提取放开视口边界——屏外已渲染元素也收录（点击/填写句柄时浏览器自动滚过去），解决长表单折叠线以下区块 AI 看不见也够不着的死结。
- 固化：AI 跑通后可把操作明细转存为普通 UI 用例，回归零 token。

### 4.4 浏览器引擎
- 默认 Chromium Headless Shell（Playwright 精简无头内核）。
- 可选 Lightpanda（AI 原生轻量引擎，beta，内存约为 Chromium 的 1/9）：经 CDP 连接，支持自动拉起进程（`TD_LIGHTPANDA_BIN`）；不可用时自动回退 Chromium，回退信息写入执行明细。**视觉能力自检降级**（不影响执行链路）：执行录像（Screencast）、AI 视觉截图（退回纯文本决策）、每步截图留档、截图留档步骤与基线对比（标记跳过而非失败）在不支持的引擎上自动降级；正式支持等上游补齐 Windows 构建与截图能力。
- 低配部署：`docker-compose.lowmem.yml` 后端镜像不装 Chromium，统一走 Lightpanda 容器。

### 4.4b MCP 服务（外部 AI agent 接入）
- 官方 MCP SDK（1.x）的 FastMCP 挂载于 `/mcp`（Streamable HTTP），`TD_MCP=0` 可关。
- 鉴权复用平台 JWT：客户端在 MCP 配置里加请求头 `Authorization: Bearer <登录 token>`，工具按该用户的项目权限执行。
- 18 个工具：projects_list / project_users_list / cases_list / case_get / runs_list / run_get / case_run / flow_run / case_create / case_delete / ai_usage / **app_map_upsert**（合并写入应用地图，源码分析 Skill 的上传通道）/ **browser_open · browser_state · browser_act · browser_screenshot · browser_sessions · browser_close**（受控浏览器会话：外部 agent 出脑子、平台出手，动作仅限白名单 goto/click/fill/expect_text/click_xy/drag/save，会话独占线程、空闲 30 分钟自动回收、每用户限 2 个/全局限 8 个）；管理面（用户/模型配置）不暴露。
- 系统设置页「MCP 接入」一键复制完整配置：内含**每个用户自己的长时效专用令牌**（10 年有效、不受登录过期与服务重启影响，权限跟随账号；修改密码后全部令牌自动吊销、重新生成即可）。
- 接入地址优先取 `TD_PUBLIC_URL`（反向代理场景），未配置时取请求 Host；开发环境经 Vite 代理（/mcp 已配转发）。
- 注意：客户端所在机器若开有系统代理（Clash 等），需让 localhost 直连（Cursor 等对 localhost 默认直连；Python 客户端设 `NO_PROXY=localhost,127.0.0.1`）。

### 4.4c 应用地图（期望基线 · AI 的系统先验知识）
- **定位（所有者定调）**：地图 = 期望模型/基线，只由三个来源写入——`scan` 爬取 / `code` 源码分析（skills/app-map-source-scan 经 MCP 上传，ground truth）/ `manual` 人工 upsert；**执行观察永远不回写地图**，否则"本来该有按钮现在没有"的回归信号就消失了。
- **多角色扫描**：扫描时可勾选多个测试用户，逐个 AI 代劳登录后单独爬取，按 path 合并——页面与元素记录 `roles`（哪些角色见过），按钮"任一角色可点即算可点"。重扫只全量替换 scan 来源的页面/元素，code/manual 来源保留。爬取采用**点击探索**：除跟 `<a href>` 外，逐个点击可见可点元素（含 li/div 菜单项）发现新页面——SPA 的 JS 菜单（如 div.sub-menu-item 子菜单）不再是盲区；登录成败由爬取器按页面内容判定（不采信 AI 结论，验证码失败自动换一张重试一次），每页探索前先尝试关闭公告弹窗；探索点击带破坏性文案黑名单（退出/删除/提交等绝不点）。可交互元素的识别是**通用的**（借鉴开源浏览器 Agent browser-use 的信号集思路，无任何站点特定规则）：原生交互标签 ∪ 交互性 ARIA role ∪ tabindex ∪ onclick/contenteditable ∪ 计算样式 cursor:pointer ∪ menu/nav 类名约定，可见性三重判定（矩形+computed style+遮挡 elementFromPoint）；共享判定逻辑在 `app/engine/web_interact.py`（状态提取/点击探索/地图提取三处复用）。
- 产出：页面清单（路径/标题/层级/来源/角色）、每页按钮（含禁用状态、来源、`state_note` 出现条件备注）与链接（跳转关系），存 `app_pages`/`app_elements`。
- 注入：AI 用例执行、流程 AI 步骤、固定步骤回放的 ai 动作都会把地图摘要注入提示词（页面关系 + 按钮清单 + 条件备注），AI 不再只看当前页盲猜。
- **执行比对信号（元素级回归检测）**：ai_drive 每步把当前页 DOM 与地图比对，地图记录的按钮缺失时在执行明细记 `warning`（不判失败、不计入通过/失败数；`state_note` 非空的条件性元素跳过比对）；前端执行明细/报告与导出 HTML 展示警告，MCP run_get 原样透出。
- **upsert 通道**：REST `POST /projects/{pid}/app-map/upsert` 与 MCP `app_map_upsert`（合并写入：页面按 path、元素按 kind+text 同键更新，来源强度 code > manual > scan，不删已有数据）。

### 4.5 高级断言与视觉回归
- **JSONPath 断言**：检查点类型选「JSONPath 断言（高级）」，字段写表达式（如 `$.data.list[*].id`）；期望值留空 = 匹配到任意值即通过，填值 = 任一匹配值等于它（弱类型）即通过。四类基础检查点（status/contains/field_eq/not_empty）之外的兜底能力。
- **截图基线对比**：流程的「截图留档」步骤，首次通过自动留存基线（`static/base-{flow}-{步骤}.png`），之后每次执行与基线做像素比对，差异超过阈值（`TD_SHOT_DIFF_PCT`，默认 2%）判失败并生成「基线｜本次」并排对比图；`TD_SHOT_DIFF=0` 关闭。调整流程步骤顺序后基线对应关系会变化：流程列表「基线」入口可查看/清除，或重新跑一次成功执行刷新。

### 4.6 调度与触发
- 计划保存即同步 Schedule 表并重建 APScheduler 任务（`分 时 日 月 周`）。
- Git webhook（随机 secret 鉴权）收到默认分支 push → 落库提交 + 触发 trigger=git 的计划；内网用 `python -m app.cli.git_sync --repo <路径> --webhook <地址>`。

### 4.7 权限模型
- admin 全可见；member 仅见自己创建或被加入的项目（`perms.check_project_access`）。
- 模型配置、用户管理、通知渠道写操作仅 admin。

## 5. 数据模型（17 表）

`users` 账号 · `projects` 项目 · `project_members` 成员 · `envs` 环境（一项目一条）· `project_users` **项目测试用户**（账号密码池，名称/账号/密码/备注）·
`test_cases` 用例（api/ui/ai，含绑定的测试账号 `username/password`）·
`test_plans` 计划（`case_ids` + `flow_ids`）· `schedules` cron 调度 ·
`test_runs` **统一执行记录**（单用例 `case_id` / 单流程 `flow_id` / 整计划 `plan_id` 三种来源，流程历史也查此表）·
`flows` 流程定义 ·
`git_repos` 仓库绑定（含 webhook secret）· `commit_syncs` 同步的提交 · `notify_channels` 告警渠道（含 webhook 地址）·
`llm_configs` 模型配置（API Key 静态加密）· `llm_logs` LLM 调用与 token 用量 ·
`app_pages`/`app_elements` **应用地图**（期望基线：页面与按钮/链接，含 `source` 来源 scan/code/manual、`roles` 可见角色、元素 `state_note` 出现条件备注）。

> 旧版独立 `flow_runs` 表已在启动迁移中并入 `test_runs`（数据自动搬运后删表）。

## 6. API 一览（前缀 `/api/v1`）

| 分组 | 端点 |
|---|---|
| 认证 | `POST /auth/login` · `GET /auth/me` · `POST/GET /auth/users` · `PUT /auth/password` · `PUT /auth/users/{uid}/password` |
| 项目 | `GET/POST /projects` · `PUT/DELETE /projects/{pid}` · `GET/POST /projects/{pid}/envs`（一项目仅一条）· `PUT/DELETE .../envs/{eid}` · `GET/POST /projects/{pid}/users` · `PUT/DELETE .../users/{uid}` · `POST .../users/import` · `GET/POST /projects/{pid}/members` · `DELETE .../members/{uid}` |
| 用例 | `GET/POST /projects/{pid}/cases` · `POST /projects/{pid}/cases/import-assets`（HAR/Postman）· `GET/PUT/DELETE /cases/{cid}` · `POST /cases/{cid}/copy` |
| 执行 | `POST /runs/cases/{cid}/run` · `POST /runs/plans/{pid}/run`（均立即返回 running 记录，后台执行、前端轮询进度；MCP 的 case_run 保持同步等待）· `GET /runs` · `GET /runs/{rid}`（含 tokens 消耗） · `GET /runs/{rid}/export` · `GET /runs/export.csv` |
| 流程 | `GET/POST /flows` · `GET/PUT/DELETE /flows/{fid}` · `POST /flows/{fid}/run` · `GET /flows/{fid}/runs` · `GET /flows/runs/{rid}/detail` |
| 计划 | `GET/POST /plans` · `PUT/DELETE /plans/{pid}` |
| Git | `GET/POST /integrations/git/repos` · `DELETE .../repos/{rid}` · `GET /integrations/git/commits` · `POST /integrations/git/webhook/{secret}` |
| AI | `POST /ai/gen-from-commits` · `POST /ai/gen-from-text` · `POST /ai/analyze-run/{rid}` · `GET /ai/usage` · `GET /ai/regression-advice` |
| 设置 | `GET/POST /settings/notify` · `PUT/DELETE /settings/notify/{cid}` · `POST /settings/notify/{cid}/test` · `GET/POST /settings/llm` · `PUT/DELETE /settings/llm/{id}` · `POST /settings/llm/{id}/activate` · `POST /settings/llm/test` · `POST /settings/llm/models` |
| 录制 | `POST /cases/ui-record/start` · `GET /cases/ui-record/{sid}/frame`（轮询兜底）· **`WS /cases/ui-record/{sid}/stream`**（Screencast 推流，`?token=` 鉴权）· `POST /cases/ui-record/{sid}/cmd` · `POST /cases/ui-record/{sid}/cancel-ai` · `GET /cases/ui-record/{sid}` |
| 应用地图 | `POST /projects/{pid}/app-map/scan`（可多角色 `user_ids`）· `GET /projects/{pid}/app-map` · `POST /projects/{pid}/app-map/upsert`（合并写入）· `DELETE /projects/{pid}/app-map` |
| MCP | `STREAMABLE-HTTP /mcp`（工具：项目/用户/用例查询、用例与流程执行、执行明细、创建删除用例、用量统计、地图 upsert、浏览器会话六件套） |
| 健康 | `GET /health` |

## 7. 配置参考

### 配置项（.env 文件）

| 变量 | 默认 | 说明 |
|---|---|---|
| `TD_DB` | `backend/testdeck.db` | SQLite 路径，本地开发/测试默认 |
| `TD_DATABASE_URL` | 未设 | 生产数据库连接串（PostgreSQL/MySQL），设置后优先于 SQLite |
| `TD_SECRET` | 自动生成 | JWT 签名密钥，同时派生凭据加密根（`app/crypto.py`，LLM Key 与告警 webhook 静态加密）：未配置时自动生成随机密钥并持久化到 `backend/.secret_key`（已 gitignore），绝不使用默认常量；生产多实例/重建容器请在 `.env` 显式固定。**轮换 TD_SECRET 会使已存密文不可解**（读回为空，需重新录入 Key） |
| `TD_ADMIN_PASSWORD` | `admin123` | 初始 admin 密码，**生产必须修改** |
| `TD_SEED_DEMO` | `1` | 是否预置询价单演示数据 |
| `TD_NO_SCHEDULER` | 未设 | 设为 1 禁用调度（测试用） |
| `TD_WORKERS` | `1` | 执行队列并发数（1-16）：默认串行，团队规模上来后调大 |
| `TD_MCP` | `1` | 设 0 关闭 MCP 服务（/mcp 端点与工具） |
| `TD_PUBLIC_URL` | 未设 | 平台对外访问地址（如 `https://td.corp.com`）：MCP 配置里的接入 URL 优先用它，避免反向代理改写 Host 导致地址错误 |
| `TD_KEEP_DAYS` | `30` | 截图与执行录像保留天数 |
| `TD_MODEL_CONTEXT_SIZE` | `65536` | 模型上下文窗口声明（token）：压缩阈值=0.8×此值，长执行更早压缩历史省 token；设 0 用模型默认（通常 128k） |
| `TD_VIDEO_SPEED` | `4` | 执行录像倍速压缩倍数：落盘后立即抽帧转码（限 8fps），时长与体积同比例下降；设 0/1 关闭保留原片。ffmpeg 取系统 PATH 或 imageio-ffmpeg 自带构建，都不可用时保留原片 |
| `TD_SHOT_DIFF` | `1` | 设为 0 关闭流程截图基线对比 |
| `TD_SHOT_DIFF_PCT` | `2` | 截图与基线的差异阈值（百分比，超过判失败） |
| `TD_BROWSER_ENGINE` | `chromium` | `lightpanda` 启用轻量引擎 |
| `TD_BROWSER_SESSION_TTL` | `1800` | MCP 浏览器会话空闲回收秒数 |
| `TD_LIGHTPANDA_URL` | `http://127.0.0.1:9222` | Lightpanda CDP 地址 |
| `TD_LIGHTPANDA_BIN` | 未设 | lightpanda 可执行文件路径，设置后平台自动拉起 |
| `TD_LLM_BASE_URL/KEY/MODEL` | 未设 | LLM 兜底配置（「模型配置」页的库内配置优先） |

### 模型配置页
多厂商配置列表（智谱/DeepSeek/通义/Kimi/MiniMax/OpenAI/Claude/Gemini/OpenRouter/Ollama 等 19 项预置），每条含：
接入方式（按量 API / Token 套餐，Key 各自独立）、Base URL、API Key（存库，回显脱敏）、模型名（可在线拉取厂商 `/models` 列表）、
「支持视觉」开关（勾选后 AI 用例每步附截图，token 约增 1~2k/步）、连接测试。其中一条设为「使用中」，AI 功能全部走它；无使用中条目时回退 .env 兜底配置。

### 7.1 结构迁移与凭据加密

- **结构迁移（Alembic）**：表结构变更一律走 `backend/migrations/versions`，启动自动应用（有版本行 → upgrade head；已有业务表但无版本行 → stamp 打基线；全新库 → 建到最新；内存库跳过；失败只记日志不阻断启动）。改 `app/models.py` 后生成增量迁移：`cd backend && .venv/Scripts/python -m alembic revision --autogenerate -m "..."`，人工过目后随代码提交。
- **凭据加密**：`llm_configs.api_key` 与 `notify_channels.url` 落库自动 Fernet 加密（`enc:v1:` 前缀），读取透明解密；存量明文由启动迁移补加密（幂等）。密钥从 TD_SECRET 派生，轮换 TD_SECRET 后旧密文读回为空，需在界面重新录入。

## 8. 部署

| 方式 | 命令 | 说明 |
|---|---|---|
| 本地开发 | `uvicorn app.main:app --port 8000` + `npm run dev` | 前端 5173 已代理 /api |
| 标准 Docker | `docker compose up -d --build` | 8080 端口，含完整 Chromium，内置 PostgreSQL 16 |
| 低配服务器 | `docker compose -f docker-compose.lowmem.yml up -d --build` | 后端镜像无 Chromium，UI/AI 走 Lightpanda 容器 |

## 9. 测试

```bash
cd backend && .venv/Scripts/python -m pytest tests -q     # 107 个单元/接口测试
# E2E（需先起后端与 mock 被测系统 9001）：
tests/e2e.py e2e_m2.py e2e_m3.py e2e_m5.py e2e_flow.py
# 假 LLM 服务器（AI 引擎联调用）：uvicorn tests.fake_llm:app --port 9111
```

## 10. 安全与隐私（公开/外发前必读）

**绝不可公开或提交到仓库的文件**（含真实密钥与业务数据）：

| 路径 | 内容 |
|---|---|
| `backend/testdeck.db` | **全部运行数据**：模型 API Key 与告警 webhook（**静态加密存储**，密钥源自 TD_SECRET）、账号密码哈希、执行记录与被测系统响应 |
| `backend/.secret_key`、`backend/.token_epoch` | JWT 签名密钥（未配 TD_SECRET 时自动生成）与令牌版本（已 gitignore） |
| `backend/static/` | 执行截图与录像（可能含被测系统页面与业务数据）——**已加登录鉴权**：浏览器走登录时下发的 `td_token` Cookie，程序访问带 `Authorization: Bearer` |
| `backend/.venv/`、`frontend/node_modules/`、`frontend/dist/` | 本地产物/构建产物 |

**代码中允许公开的敏感字样**（均为示例或带安全说明）：
`admin123`（默认种子密码，README 明确要求生产修改）、测试脚本中的 `sk-test1234567890` 等假 Key、placeholder 中的 `192.168.1.10` 示例地址。

**发布前检查清单**：
1. 确认 `.gitignore` 生效（仓库根已提供），`git status` 不含上表三类路径；
2. 若仓库曾误提交 `testdeck.db`，需轮换该库中所有 LLM Key 与 webhook 地址（git 历史会保留）；
3. 生产部署修改 `TD_ADMIN_PASSWORD` 与 `TD_SECRET`；
4. 告警 webhook 与模型 Key 只存运行库，泄露即轮换。
