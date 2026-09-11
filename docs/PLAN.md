# TestDeck 迭代排期（2026-09-08 与所有者讨论定稿）

> 本文档是内部迭代排期；产品级 Roadmap 见 README。批次内的条目按序执行，跨批次可并行启动不冲突的部分。

> **执行进度（2026-09-12）**：批次 A/B/C 全部完成。B4 已收尾：真实环境基准对比达标
> （智链测试项目·货主登录用例，新旧各 3 轮——agentscope 成功率 1/3、平均 22.5s，通过轮当场解开点选验证码；
> legacy 0/3、平均 94.7s，全部被验证码耗尽步数；另单轮验证 agentscope 也解开验证码通过，17.3s）。
> AgentScope 转为唯一决策循环，legacy 手搓循环与 `benchmark_brain.py` 已删除，`TD_BRAIN` 开关移除。
> 迁移中发现并修复两个模型兼容问题：
> ① DeepSeek 等思考模式模型拒绝 tool_choice="none"/强制函数（400）——回退链层降级兼容（none→去工具、强制→auto）；
> ② 工具函数必须返回 ToolChunk——返回 ToolResponse 会被适配层 str() 成含 base64 的对象 repr 文本，
> 一张截图 ≈ 13 万 token，几轮撑爆 128k 上下文。

## 批次 A：应用地图可靠性（平台内闭环）

所有者定调：**地图 = 期望模型/基线**，执行观察永远不写入地图（否则"本来该有按钮现在没有"的回归信号消失）。

| # | 内容 | 要点 |
|---|---|---|
| A1 | 多角色扫描 + 来源字段 | 扫描勾选多个测试用户逐个 AI 登录、按角色合并；元素加 `source`（scan/code/manual）与 `state_note`；前端来源标注展示 |
| A2 | 执行比对信号 | ai_drive 每步的元素清单与地图比对，地图记录的按钮缺失时在执行明细记**警告**（不判失败）——元素级回归检测。落地约定：明细条目加 `warning` 字段（字符串，空为无警告），不计入 pass_n/fail_n；前端执行明细与报告抽屉展示警告角标，MCP run_get 原样透出 |
| A3 | 上传通道 | REST `POST /projects/{pid}/app-map/upsert` + MCP 工具 `app_map_upsert`（合并写入、来源标注） |

## 批次 B：Agent Brain 完全迁移 AgentScope（2.x）

所有者决策：**完全迁移，不做双引擎**（迁移期内允许旧路径带 flag 短暂共存，仅供 B4 基准对比，对比完成即删，见 B2/B4）。安全红线：**agent 的工具集 = 浏览器测试动作（goto/click/fill/expect_text/click_xy/drag）+ 测试专用动作（save 提取响应字段存变量、done 给出结论），绝不提供 shell / 文件读写 / 代码执行类工具**（Docker 内也一样）；用 AgentScope 2.0 工具权限做白名单强制。

| # | 内容 | 要点 |
|---|---|---|
| B1 | 前置验证（迁移第一步，非可选项） | 装 agentscope 锁版本；验证三件事：同步队列线程 ↔ async agent 的桥接；动作集注册为 toolkit + 工具权限白名单；OpenAI 兼容模型客户端对接平台 llm_configs（注意：现状只有"一条使用中配置 + .env 兜底"，**多模型回退链是本次新增能力**而非对接现有；迁移须保留 .env 兜底语义） |
| B2 | ai_drive 改造为 AgentScope 门面 | 对外签名不变（所有调用方零改动：AI 用例/录制 AI 代劳/流程 AI 步骤/应用地图登录全走它）；手搓 prompt/parse 循环暂留 flag 后面作为 B4 对照基线，B4 完成后删除 |
| B3 | 可观测性回接 | 每个工具执行点回填 detail（idx/action/target/pass/reason/think/截图）+ on_step 流式进度；token 用量从 AgentScope usage 回写 llm_logs；取消机制（queue registry）在工具内检查生效；断路器（连续相同失败）保留 |
| B4 | 视觉与成本 | 视觉模式页面截图进 AgentScope 消息；动作工具外零开放式工具，控制 token；用真实用例跑新旧两条路径的基准对比（成功率/平均 token/耗时），达标后切默认为 AgentScope 并删除旧循环——双路径共存仅限此对比窗口，不构成双引擎 |

## 批次 C：生态扩展（C1 依赖 A3；C2 依赖 B2/B3 改造后的执行环境）

| # | 内容 | 要点 |
|---|---|---|
| C1 | 源码分析 Skill | `skills/` 目录交付 SKILL.md：读前端路由/权限指令 → 生成带 state_note 的地图 → 经 MCP 上传（ground truth 来源） |
| C2 | 浏览器会话 MCP 化 | 把"驱动 TestDeck 浏览器页面"做成 MCP 工具组，外部 agent 借 harness 干活（他们出脑子，我们出受控执行环境） |

## 已排队的其他事项

- 已完成未提交的代码批次（MCP 服务、应用地图 v1、安全加固、若干 UI 调整）待所有者确认后 commit
- 技术债清单：**四项已于 2026-09-12 完成**——凭据静态加密（api_key/webhook，TD_SECRET 派生密钥）、CI 补 ruff lint（`.github/workflows/ci.yml` + `backend/ruff.toml`，顺带修掉 format_endpoints 未导入的存量 bug）、登录滑动续期（X-Renewed-Token）、Alembic 迁移（基线 + 启动自动 stamp/upgrade）
