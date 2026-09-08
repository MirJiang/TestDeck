---
name: app-map-source-scan
description: 分析被测前端系统源码（路由/权限指令/页面按钮），生成带 state_note 的应用地图，经 TestDeck MCP 的 app_map_upsert 工具上传（source=code，ground truth 来源）。当需要为 TestDeck 项目建立或更新"期望基线"地图、而又不方便真实登录扫描（多角色/权限复杂/环境不稳）时使用。
---

# 源码分析生成应用地图（TestDeck 期望基线 · code 来源）

## 背景与原则

TestDeck 的应用地图是**期望模型/基线**：执行测试时地图记录的按钮缺失会在执行明细记警告
（元素级回归信号）。地图有三个来源，本 Skill 产出的是最可靠的一档：

| source | 来源 | 可信度 |
|--------|------|--------|
| `code` | 源码分析（本 Skill） | 最高——代码里写了什么权限/按钮，系统就应该有什么 |
| `scan` | 浏览器爬取（平台「应用地图」页扫描） | 受登录角色、动态渲染影响 |
| `manual` | 人工维护 / upsert | 兜底 |

**只做只读分析 + 上传地图。永远不要把测试执行的观察结果写进地图。**

## 前置条件

1. 被测前端系统的源码目录（本地 checkout）。
2. TestDeck MCP 已连接：地址 `http://<testdeck-host>:8000/mcp`（Streamable HTTP），
   请求头 `Authorization: Bearer <平台登录 token>`。
3. 知道目标 TestDeck 项目 id（用 MCP 工具 `projects_list` 查询确认）。

## 工作流程

### 第一步：识别技术栈与路由入口

先确定框架与路由组织方式，常见形态：

- **Vue 2/3 + vue-router**：`src/router/index.js|ts`、`src/router/modules/*.js`，
  路由对象含 `path` / `name` / `component` / `meta`（`meta.title` 是页面标题）。
- **React + react-router**：`<Route path=...>` JSX、`createBrowserRouter([...])`、
  约定式路由（Next.js/UmiJS 看 `pages/` 或 `src/pages/` 目录结构）。
- **Angular**：`*-routing.module.ts` 里的 `RouterModule.forRoot([...])`。

嵌套路由要拼出**完整路径**（父 path + 子 path）；hash 路由（`createWebHashHistory` /
`HashRouter`）的页面标识写成 `/#/xxx` 形式与平台扫描口径一致——不确定时以平台
「应用地图」页已扫描出的 path 风格为准（先调 MCP `app_map_upsert` 前可让平台方提供
一份现有地图对照）。

### 第二步：提取权限语义（state_note 的核心来源）

逐路由、逐组件找权限控制点，把它们翻译成元素的 `state_note`（出现条件备注）：

| 代码形态 | 含义 | state_note 写法示例 |
|----------|------|---------------------|
| 路由 `meta.roles: ['admin']` / `meta.permissions` | 整个页面仅某角色可见 | 页面级：写进该页所有元素的 state_note 或页面标题备注 |
| `v-hasPermi="['order:audit']"`（Vue 指令） | 按钮仅有该权限码的角色可见 | `仅 order:audit 权限可见` |
| `v-if="user.isApprover"` / `{canAudit && <Button/>}` | 条件渲染 | `仅审批岗可见` |
| `:disabled="row.status !== 'DRAFT'"` / `disabled={...}` | 条件禁用 | `仅草稿态可点` |
| `*ngIf`、`@PreAuthorize`（配套前端调用） | 同上 | 按语义写 |

**规则：凡是"不是任何人在任何状态下都能看到/点击"的元素，必须写 state_note。**
state_note 非空的元素在平台执行比对时会被跳过（条件性出现，缺失不算回归信号）——
写漏 state_note 会造成误报警告，写多了会漏掉真回归，按代码语义如实写。

权限码到角色的映射如果在代码里能找到（权限常量文件、角色-权限矩阵、mock 数据），
把角色名也写进 state_note（如 `仅审批岗（order:audit）可见`），可读性更好。

### 第三步：提取页面按钮与跳转

对每个路由页面组件（含其引用的子组件）：

- **按钮**：`<el-button>`、`<a-button>`、`<Button>`、原生 `<button>`、表格行操作列
  （操作列的每个按钮都要算）、工具栏、下拉菜单项（`<el-dropdown-item>` 等）。
  `text` 取按钮文案（含模板变量时取静态部分，如 `删除` / `批量导出`）；
  有稳定 `id` / `name` 的填 `selector`（`#id` 或 `tag[name="x"]`），没有就留空
  （平台比对按文本匹配，selector 只是辅助）。
- **链接/跳转**：`<router-link to>`、菜单配置（侧边栏菜单数组常单独一个文件，如
  `menu.js` / `layout` 组件）、`this.$router.push` 的固定目标。`kind: "link"`，
  `href` 填目标路径。
- 动态文案按钮（`{{ row.status === 'x' ? '通过' : '驳回' }}`）拆成多个元素，各自带
  state_note。

### 第四步：组装 pages JSON

```json
[
  {
    "path": "/orders",
    "title": "订单管理",
    "depth": 1,
    "elements": [
      {"kind": "button", "text": "新建订单", "selector": "#btn-new"},
      {"kind": "button", "text": "审批", "state_note": "仅审批岗（order:audit）可见"},
      {"kind": "button", "text": "删除", "state_note": "仅草稿态行可点"},
      {"kind": "link", "text": "订单详情", "href": "/orders/detail"}
    ]
  }
]
```

约定：
- `path` 是页面标识（与平台扫描口径一致，含 hash 前缀如果系统是 hash 路由）；
- `depth` 填该页面距首页的菜单层级（估算即可，影响展示排序）；
- `text` 必填（比对按文本），`selector`/`href` 可选；
- 上传是**合并写入**：同 path 页面更新标题、同 `kind+text` 元素更新字段，不会删除
  已有数据；code 来源不会被更弱的来源覆盖。

### 第五步：经 MCP 上传

调用 TestDeck MCP 工具：

```
app_map_upsert(project_id="<项目id>", source="code", pages=<上面的 JSON 数组字符串>)
```

返回 `{pages, elements, created, updated, source}`。上传后建议：

1. 在平台「应用地图」页人工抽查两三个页面（来源应标注「源码」）；
2. 之后平台跑测试时，执行明细里出现的"地图按钮缺失"警告就是元素级回归信号，
   带 state_note 的元素不会误报。

## 边界与禁止事项

- 不要臆造代码里不存在的按钮/页面——地图是期望基线，写错会污染回归信号。
- 不要把登录态、环境差异导致的"看不到"当作源码事实（那是 scan 来源的职责）。
- 大型系统可分批上传（按路由模块），合并语义保证多次上传安全。
- 只读源码 + 只调用 `app_map_upsert`；不调用平台其他写操作工具。
