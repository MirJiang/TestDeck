# TestDeck · 自动化测试平台

[![CI](https://github.com/MirJiang/TestDeck/actions/workflows/ci.yml/badge.svg)](https://github.com/MirJiang/TestDeck/actions/workflows/ci.yml)

**让不会写代码的同事也能做测试，让 AI 替你点完整个流程。**

TestDeck 是一个面向团队内部的开源测试平台：填表式创建 API / UI 测试，浏览器操作录一遍自动出用例；
AI 智能测试只需一句大白话目标（"用这个账号登录，创建一张询价单"），大模型看着页面自己点按钮、填表单、
过滑块验证码；跑通后一键固化为常规用例，回归零 AI 消耗。还带多角色流程测试、定时/Git 触发、钉钉企微告警。

```text
API 测试 ── 填地址 + 选检查点
UI 测试 ── 录制一遍自动出步骤 / 手填动作
AI 测试 ── 写目标就行，模型当大脑（支持视觉识别验证码）
流程测试 ── 多角色串成业务线，泳道图看结果
```

## 📸 界面预览

| 登录 | 工作台 · 执行概览 |
|:---:|:---:|
| ![登录页](docs/screenshots/login.png) | ![工作台](docs/screenshots/dash.png) |

| AI 用例 · 列表 | AI 用例 · 编辑（UI 目标） |
|:---:|:---:|
| ![用例列表](docs/screenshots/cases.png) | ![AI 用例编辑](docs/screenshots/case-editor.png) |

| 接口文档一键导入 | 可视化录制（远程串流） |
|:---:|:---:|
| ![接口文档导入](docs/screenshots/case-api-docs.png) | ![可视化录制](docs/screenshots/recorder.png) |

| 流程测试 · 泳道图 | 系统设置 · 模型配置 |
|:---:|:---:|
| ![流程泳道图](docs/screenshots/flow.png) | ![系统设置](docs/screenshots/settings.png) |

---

## ✨ 特性

- 🧪 **用例统一 AI 驱动**：目标下分 API 测试（模型自主设计请求）/ UI 测试（模型看页面操作）；录制可固化为固定步骤，回归零 token
- 🎥 **可视化录制**：页面画面直接串流到平台里，点一遍自动出步骤；登录验证码这类麻烦事一句话让 **AI 代劳**（它的动作也记成步骤，回放零 token）；录完还能让 AI 补断言、参数化测试数据
- 🤖 **AI 当大脑**：页面状态（DOM + 截图）实时交给大模型决策，支持滑块/点选等图形验证码（需视觉模型）
- 📥 **接口文档一键导入**：Swagger 2.0 / OpenAPI 3.x / Postman（Apifox 导出 OpenAPI 亦可），AI 按真实接口与参数设计请求，不猜测路径
- 👥 **流程测试**：货主、多家物流公司各自登录，按业务线串成一条流程，API/UI/AI 步骤混排，
  **可引用已有用例作为一步**（角色会话执行、变量自动接线），泳道图展示，截图基线对比发现页面改版
- 🧩 **高级断言**：四类填表式检查点之外，支持 JSONPath 表达式断言（`$.data.list[*].sku`）
- 🏭 **多厂商模型配置**：智谱 / DeepSeek / 通义千问 / Kimi / OpenAI / Claude 等 19 项预置 + 自定义，
  区分按量 API 与 Token 套餐入口，在线拉取模型列表，界面配置即生效
- 🪶 **低配部署**：可选 Lightpanda 轻量引擎（内存约为 Chromium 的 1/9），专门照顾配置拉跨的服务器
- ⏰ **三种触发**：手动 / cron 定时 / Git 默认分支 push（webhook 或内网 CLI 同步），计划可同时包含用例与流程
- 🔔 **失败告警**：自动推送钉钉/企微群机器人
- 📄 **报告导出**：单次执行独立 HTML 报告、执行记录 CSV
- 🔐 **项目级权限**：成员只见自己的项目；admin 全局可见

## 🚀 快速开始

```bash
# 后端（http://127.0.0.1:8000）
cd backend
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt   # Linux/macOS 用 .venv/bin/pip
.venv/Scripts/python -m playwright install chromium --only-shell        # 精简无头内核
.venv/Scripts/python -m uvicorn app.main:app --port 8000

# 前端（http://localhost:5173，已代理 /api）
cd frontend
npm install && npm run dev
```

默认账号 `admin / admin123`（仅本地默认，生产务必设置 `TD_ADMIN_PASSWORD` 修改）。

想立刻体验？再起一个内置演示被测系统，平台里已预置"询价单全流程"演示数据：

```bash
cd backend && .venv/Scripts/python -m uvicorn tests.mock_target:app --port 9001
```

## 🐳 Docker 部署

```bash
docker compose up -d --build                 # 标准部署，http://localhost:8080
```

服务器配置有限？用低配专版：后端镜像不装 Chromium，UI/AI 测试走 Lightpanda 容器
（内存约为 Chromium 的 1/9）：

```bash
docker compose -f docker-compose.lowmem.yml up -d --build
```

## 🤖 AI 智能测试

1. **「模型配置」页**添加模型：19 家厂商预置（含按量 API / Token 套餐双入口），填 Key、
   点「拉取模型列表」直接选，模型支持视觉就勾上「支持视觉」——保存即生效，无需重启
2. **新建用例 → 类型选「AI 智能测试」**，目标写大白话：

   > 用 ${username} / ${password} 登录系统，创建一张从上海到北京的询价单，记住单号，页面应提示创建成功

3. 执行时平台把页面上的按钮、输入框、文字（勾选视觉再附截图）实时交给大模型，
   由它一步步决定点哪、填什么、拖多远，每步的"思考"在执行抽屉里实时滚动
4. 跑通后点「固化为普通 UI 用例」——AI 负责"写用例"，回归用固化版跑，零 token

能力与边界：滑块/点选/图片文字验证码可识别（需视觉模型）；连续失败自动熔断、最大步数防失控；
模型未配置时平台其余 AI 辅助功能（用例生成、失败分析）自动降级为内置规则。

## 🎥 UI 可视化录制

用例编辑器点「开始录制」，被测页面画面串流进平台：直接在画面上点击、输入、跳转，
步骤自动生成；点到输入框会弹出输入条，滚轮翻页，误点与连点自动去重。
整个过程不需要在服务器旁——远程与容器部署一样录。

## 🧭 流程测试

按业务线把多个角色串起来测：每个角色独立登录态（API 各自 cookie、UI 各自浏览器会话），
API / UI / AI 步骤任意混排，前一步「记住」的单据号全流程共享，某步失败即中断。
执行结果以泳道图展示，每个角色一格，一眼看到断在哪。

## ⚙️ 配置

配置只来自一个地方：**项目根的 `.env` 文件**。复制 `.env.example` 为 `.env` 修改即可（已被 gitignore，不会泄露）；
不建 `.env` 也能跑（全部有内置默认值）。格式：`KEY=value`，支持 `#` 注释。

| 配置项 | 默认 | 说明 |
|---|---|---|
| `TD_DB` | `backend/testdeck.db` | SQLite 路径（本地开发/测试默认） |
| `TD_DATABASE_URL` | 未设 | 生产数据库连接串，设置后优先于 SQLite：`postgresql+psycopg2://user:pass@host:5432/testdeck` 或 `mysql+pymysql://user:pass@host:3306/testdeck?charset=utf8mb4` |
| `TD_SECRET` | 开发默认值 | JWT 签名密钥，**生产必须设置** |
| `TD_ADMIN_PASSWORD` | `admin123` | 初始 admin 密码，**生产必须修改** |
| `TD_BROWSER_ENGINE` | `chromium` | `lightpanda` 启用轻量引擎 |
| `TD_LIGHTPANDA_URL` / `TD_LIGHTPANDA_BIN` | — | Lightpanda 服务地址 / 可执行文件（自动拉起） |
| `TD_LLM_BASE_URL` / `TD_LLM_KEY` / `TD_LLM_MODEL` | — | LLM 兜底配置（界面「模型配置」优先） |
| `TD_KEEP_DAYS` | `30` | 截图保留天数 |

已有 SQLite 数据要搬进 MySQL/PG？一条命令（目标表非空自动跳过，不覆盖）：

```bash
python -m app.cli.db_migrate --to "postgresql+psycopg2://user:pass@host:5432/testdeck"
```

## 🏗️ 架构

```text
Vue 3 SPA ──▶ FastAPI ──▶ 执行引擎（单线程串行队列）
                            ├─ runner       API：httpx + 检查点
                            ├─ ui_runner    UI：Playwright 无头浏览器
                            ├─ flow_runner  流程：多角色隔离执行
                            ├─ ai_runner    AI：状态→LLM→动作 循环（可选视觉）
                            └─ browser      Chromium Headless / Lightpanda(CDP)
                          APScheduler 定时 · Git webhook · 钉钉/企微告警
                          SQLAlchemy 数据库层（默认 SQLite 零配置；生产一键切 MySQL/PostgreSQL）
```

### 🔌 MCP 接入（让你的 AI agent 操作测试平台）

平台内置 MCP 服务（`http://<host>:8000/mcp`，Streamable HTTP）。在 Cursor / Claude 等支持 MCP 的客户端加一条配置，你的 agent 就能直接建用例、跑测试、查结果：

```json
{ "mcpServers": { "testdeck": { "url": "http://localhost:8000/mcp",
  "headers": { "Authorization": "Bearer <你的平台登录token>" } } } }
```

可用工具：项目/测试用户/用例查询、执行用例与流程、查看执行明细、创建/删除用例、AI 用量统计（权限跟随登录用户）。

深度说明（核心机制、数据模型、API 全表、引擎回退链）见 [docs/PROJECT.md](docs/PROJECT.md)。

## 🧪 测试

```bash
cd backend && .venv/Scripts/python -m pytest tests -q   # 76 个单元/接口测试（含安全用例）
# E2E：起后端与 mock 被测系统后依次跑 tests/e2e*.py
```

## 🗺️ Roadmap

**已完成**

- [x] 截图视觉回归（流程截图基线像素 diff，超阈值判失败并出对比图）
- [x] 接口文档导入（Swagger / OpenAPI / Postman Collection，AI 按真实接口设计请求）
- [x] UI 用例执行视频回放（Chromium Screencast 落盘 webm，用例与流程执行明细内可播放，随截图定期清理）
- [x] Postman / Har 用例批量导入（HAR → 多步骤用例；Postman Collection → 每请求一用例，静态资源过滤、同源相对化）
- [x] AI 主导测试增强（AI 步骤失败自动重试；失败摘要带模型分档建议）
- [x] 执行并发队列（`TD_WORKERS` 可配 1-16 并发，默认 1 保持串行；SQLite busy_timeout 锁保护）

**准备做**

- [ ] Lightpanda 引擎正式支持（执行链路已完整可用：AI 用例/固定步骤/流程均支持，视觉相关能力——录像、视觉截图、截图留档与基线对比——自动降级跳过不影响执行；正式支持等上游补齐 Windows 构建与截图能力）

## 📄 许可证

[MIT](LICENSE)
