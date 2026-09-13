<script setup>
import { ref, computed, watch, onMounted } from 'vue'
import { api } from '../api'
import { confirmDialog, toast } from '../dialog'

// 应用地图：爬取被测系统生成页面清单、跳转关系与按钮状态，注入 AI 执行提示作为先验知识。
// 地图 = 期望基线：来源分 scan（爬取）/ code（源码分析）/ manual（人工），执行观察不回写。
const projects = ref([])
const pid = ref('')
const users = ref([])
const mapList = ref([])
const form = ref({ userIds: [], maxPages: 25 })
const busy = ref(false)
const note = ref('')

const SOURCE_LABEL = { scan: '扫描', code: '源码', manual: '人工' }

watch(pid, async v => {
  users.value = []; mapList.value = []; note.value = ''; selId.value = ''
  form.value = { userIds: [], maxPages: 25 }
  if (v) {
    users.value = await api(`/projects/${v}/users`).catch(() => [])
    await load()
  }
})

async function load() {
  const r = await api(`/projects/${pid.value}/app-map`)
  mapList.value = r.pages; mapMenus.value = r.menus || []
}

async function doScan() {
  busy.value = true
  const n = form.value.userIds.length
  note.value = n > 1
    ? `扫描中：${n} 个角色逐个 AI 登录爬取后按页面合并，可能需要几分钟…`
    : '扫描中：打开页面、（可选）AI 登录、逐页提取按钮与链接，可能需要一两分钟…'
  try {
    const r = await api(`/projects/${pid.value}/app-map/scan`,
      { method: 'POST', body: { user_ids: form.value.userIds, max_pages: form.value.maxPages }, timeout: 600000 })
    note.value = `扫描完成：${r.pages} 个页面、${r.elements} 个元素` +
      (r.roles?.length > 1 ? `（角色：${r.roles.join('、')}）` : '') +
      (r.note ? '；' + r.note : '')
    await load()
  } catch (e) { note.value = '扫描失败：' + e.message } finally { busy.value = false }
}

async function clearMap() {
  if (!await confirmDialog('清除该项目的应用地图？AI 执行将不再注入页面先验知识，重新扫描可恢复。', { danger: true, okText: '清除' })) return
  await api(`/projects/${pid.value}/app-map`, { method: 'DELETE' })
  mapList.value = []; selId.value = ''
}

// ---- 导入 / 人工维护（upsert 合并写入，不删已有数据） ----
const impOpen = ref(false)
const impBusy = ref(false)
const impErr = ref('')
const impForm = ref({ source: 'manual', text: '' })

const IMP_EXAMPLE = `[
  {"path": "/orders", "title": "订单管理", "depth": 1, "elements": [
    {"kind": "button", "text": "新建订单", "selector": "#btn-new"},
    {"kind": "button", "text": "审批", "state_note": "仅审批岗可见"},
    {"kind": "link", "text": "订单详情", "href": "/orders/detail"}
  ]}
]`

function onImpFile(e) {
  const f = e.target.files?.[0]
  if (!f) return
  const rd = new FileReader()
  rd.onload = () => { impForm.value.text = String(rd.result || ''); impErr.value = '' }
  rd.readAsText(f)
  e.target.value = ''
}

async function doImportMap() {
  impErr.value = ''
  let pages
  try {
    pages = JSON.parse(impForm.value.text)
  } catch (e) { impErr.value = 'JSON 解析失败：' + e.message; return }
  if (!Array.isArray(pages) || !pages.length) { impErr.value = '内容需为非空的页面数组'; return }
  const bad = pages.findIndex(p => !p || typeof p.path !== 'string' || !p.path.trim())
  if (bad >= 0) { impErr.value = `第 ${bad + 1} 个页面缺少 path 字段`; return }
  impBusy.value = true
  try {
    const r = await api(`/projects/${pid.value}/app-map/upsert`,
      { method: 'POST', body: { source: impForm.value.source, pages } })
    toast(`已导入：新建 ${r.created} 页、更新 ${r.updated} 页，共 ${r.elements} 个元素（来源：${SOURCE_LABEL[r.source] || r.source}）`)
    impOpen.value = false
    impForm.value.text = ''
    await load()
  } catch (e) { impErr.value = '导入失败：' + e.message } finally { impBusy.value = false }
}

// ---- 双栏浏览：左页面清单（可搜索），右页面详情（按钮/跳转表格） ----
const kw = ref('')
const selId = ref('')
const mapMenus = ref([])

const filtered = computed(() => {
  const k = kw.value.trim().toLowerCase()
  if (!k) return mapList.value
  return mapList.value.filter(pg =>
    pg.path.toLowerCase().includes(k) || (pg.title || '').toLowerCase().includes(k) ||
    (pg.entry || '').toLowerCase().includes(k) ||
    pg.buttons.some(b => (b.text || '').toLowerCase().includes(k)))
})
const selPage = computed(() =>
  mapList.value.find(p => p.id === selId.value) || filtered.value[0] || null)

// 浏览模式（无搜索词）：按扫描到的导航菜单层级展示（菜单父子边 + 页面按入口挂载）；
// 搜索模式：平铺过滤结果
const menuRows = computed(() => {
  if (kw.value.trim()) return null
  const nodes = new Map()
  for (const m of mapMenus.value) {
    for (const t of [m.parent, m.text])
      if (!nodes.has(t)) nodes.set(t, { text: t, children: [], pages: [] })
  }
  for (const m of mapMenus.value) {
    const p = nodes.get(m.parent)
    if (!p.children.some(c => c.text === m.text)) p.children.push(nodes.get(m.text))
  }
  const isChild = new Set(mapMenus.value.map(m => m.text))
  const tops = [...nodes.values()].filter(n => !isChild.has(n.text))
  const out = []
  // 页面按入口挂到菜单项下（entry = 扫描时点击进入该页的菜单文本）
  for (const pg of filtered.value) {
    const n = nodes.get((pg.entry || "").trim())
    if (n) n.pages.push(pg)
  }
  for (const pg of filtered.value) if (!(pg.entry || "").trim()) out.push({ type: "page", pg, depth: 0 })
  const seen = new Set()
  const walk = (node, depth) => {
    if (seen.has(node.text)) return
    seen.add(node.text)
    const pages = node.pages
    out.push({ type: "menu", text: node.text, depth, nPages: pages.length })
    for (const c of node.children) walk(c, depth + 1)
    for (const pg of pages) out.push({ type: "page", pg, depth: depth + 1 })
  }
  tops.forEach(n => walk(n, 0))
  const inTree = new Set(out.filter(r => r.type === "page").map(r => r.pg.id))
  for (const pg of filtered.value) if (!inTree.has(pg.id)) out.push({ type: "page", pg, depth: 0 })
  return out
})

function pageLabel(pg) {
  try {
    const t = new URLSearchParams(pg.path.split("?")[1] || "").get("tourl")
    if (t) return t
  } catch { /* 非法查询串按原路径展示 */ }
  return pg.path.split("?")[0]
}

const stats = computed(() => ({
  pages: mapList.value.length,
  elements: mapList.value.reduce((n, p) => n + p.buttons.length + p.links.length, 0),
  roles: [...new Set(mapList.value.flatMap(p => (p.roles || '').split(',').filter(Boolean)))],
  lastScan: mapList.value.map(p => p.scanned_at).filter(Boolean).sort().reverse()[0] || '',
}))

onMounted(async () => {
  projects.value = await api('/projects')
  if (projects.value.length) pid.value = projects.value[0].id
})
</script>

<template>
  <div class="hd"><div><h2>应用地图</h2></div>
    <div style="display:flex;gap:8px;align-items:center" v-if="pid">
      <input v-model.number="form.maxPages" type="number" min="1" max="50" class="mono" style="width:64px" title="页数上限" :disabled="busy">
      <button class="btn pri" :disabled="busy" @click="doScan">{{ busy ? '扫描中…' : '扫描' }}</button>
      <button class="btn" :disabled="busy" @click="impOpen = true" title="粘贴或选择 JSON 合并写入地图（源码分析产物 / 人工维护）">导入</button>
      <button class="btn" v-if="mapList.length && !busy" @click="clearMap">清除</button>
    </div>
  </div>
  <div class="faint" style="font-size:12.5px;margin-bottom:12px">
    扫描被测系统生成页面清单、跳转关系与按钮（含禁用状态），作为 AI 测试的先验知识注入执行提示——AI 不再只看当前页盲猜。
    地图是<b>期望基线</b>：执行时发现地图按钮缺失会记警告（元素级回归信号）。
    <template v-if="projects.length">
      <select v-model="pid" style="width:180px;margin-left:8px">
        <option v-for="p in projects" :key="p.id" :value="p.id">{{ p.name }}</option>
      </select>
    </template>
  </div>
  <div v-if="users.length" class="panel" style="padding:10px 12px;margin-bottom:10px">
    <div class="muted" style="font-size:12px;margin-bottom:6px">
      登录角色（可多选）：每个勾选的测试账号由 AI 代劳登录后单独爬取，结果按页面合并——不同角色可见的菜单/按钮都会进地图
    </div>
    <div style="display:flex;flex-wrap:wrap;gap:6px">
      <label v-for="u in users" :key="u.id" class="chip"
             style="cursor:pointer;display:inline-flex;align-items:center;gap:4px"
             :style="form.userIds.includes(u.id) ? 'border-color:var(--pri);color:var(--pri)' : ''">
        <input type="checkbox" :value="u.id" v-model="form.userIds" :disabled="busy" style="margin:0">
        {{ u.name ? `${u.name}（${u.username}）` : u.username }}
      </label>
    </div>
  </div>
  <div v-if="note" class="muted" style="font-size:12.5px;margin-bottom:10px">{{ note }}</div>

  <div class="panel" v-if="projects.length && mapList.length">
    <!-- 统计条 -->
    <div style="display:flex;gap:16px;flex-wrap:wrap;align-items:center;padding:2px 4px 10px;border-bottom:1px solid var(--line);margin-bottom:10px">
      <span class="muted" style="font-size:12.5px">页面 <b style="color:var(--ink)">{{ stats.pages }}</b></span>
      <span class="muted" style="font-size:12.5px">元素 <b style="color:var(--ink)">{{ stats.elements }}</b></span>
      <span class="muted" style="font-size:12.5px" v-if="stats.roles.length">角色 <b style="color:var(--ink)">{{ stats.roles.join('、') }}</b></span>
      <span class="faint" style="font-size:11.5px;margin-left:auto">最近扫描 {{ stats.lastScan.slice(0, 16).replace('T', ' ') }}</span>
    </div>
    <!-- 双栏：左清单 / 右详情 -->
    <div style="display:flex;gap:0;align-items:stretch">
      <div style="width:330px;flex:none;border-right:1px solid var(--line);padding-right:10px">
        <input v-model="kw" placeholder="搜索页面 / 标题 / 按钮…" style="width:100%;margin-bottom:8px">
        <div style="max-height:58vh;overflow:auto">
          <template v-if="!kw.trim()">
            <template v-for="row in menuRows" :key="row.type + (row.text || row.pg.id)">
              <div v-if="row.type === 'menu'"
                   :style="{ padding: '6px 10px 4px', paddingLeft: (8 + row.depth * 12) + 'px' }">
                <b style="font-size:12.5px">{{ row.text }}</b>
                <span class="faint" style="font-size:11px;margin-left:6px">{{ row.nPages }} 页</span>
              </div>
              <div v-else @click="selId = row.pg.id"
                   :style="{ cursor: 'pointer', padding: '6px 10px', paddingLeft: (20 + row.depth * 12) + 'px', borderRadius: '6px', marginBottom: '2px',
                             background: selPage && selPage.id === row.pg.id ? 'var(--acc-weak)' : 'transparent' }">
                <div style="display:flex;gap:6px;align-items:center">
                  <b class="mono" style="font-size:12px;word-break:break-all;flex:1">{{ pageLabel(row.pg) }}</b>
                  <span class="faint" style="font-size:11px;flex:none">{{ row.pg.buttons.length }} 钮</span>
                </div>
              </div>
            </template>
            <div v-if="!menuRows.length" class="empty" style="font-size:12px">没有匹配的页面</div>
          </template>
          <template v-else>
            <div v-for="pg in filtered" :key="pg.id" @click="selId = pg.id"
                 :style="{ cursor: 'pointer', padding: '7px 10px', borderRadius: '6px', marginBottom: '2px',
                           background: selPage && selPage.id === pg.id ? 'var(--acc-weak)' : 'transparent' }">
              <div style="display:flex;gap:6px;align-items:center">
                <span class="chip">L{{ pg.depth }}</span>
                <b class="mono" style="font-size:12px;word-break:break-all;flex:1">{{ pg.path }}</b>
                <span v-if="pg.source !== 'scan'" class="chip">{{ SOURCE_LABEL[pg.source] || pg.source }}</span>
              </div>
              <div style="display:flex;gap:6px;align-items:center;margin-top:2px">
                <span class="muted" style="font-size:11.5px;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{{ pg.title || '（无标题）' }}</span>
                <span class="faint" style="font-size:11px;flex:none">{{ pg.buttons.length }} 钮{{ pg.links.length ? ' · ' + pg.links.length + ' 链' : '' }}</span>
              </div>
            </div>
            <div v-if="!filtered.length" class="empty" style="font-size:12px">没有匹配的页面</div>
          </template>
        </div>
      </div>
      <div style="flex:1;min-width:0;padding-left:14px;max-height:64vh;overflow:auto">
        <template v-if="selPage">
          <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:4px">
            <span class="chip">L{{ selPage.depth }}</span>
            <span v-if="selPage.source !== 'scan'" class="chip">{{ SOURCE_LABEL[selPage.source] || selPage.source }}</span>
            <b class="mono" style="font-size:13px;word-break:break-all">{{ selPage.path }}</b>
          </div>
          <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:12px">
            <span style="font-size:15px;font-weight:600">{{ selPage.title || '（无标题）' }}</span>
            <span v-if="selPage.roles" class="chip">👤 {{ selPage.roles }}</span>
            <span class="faint" style="font-size:11.5px;margin-left:auto">扫描于 {{ selPage.scanned_at?.slice(0, 16).replace('T', ' ') }}</span>
          </div>
          <div class="muted" style="font-size:12px;margin-bottom:6px">按钮 / 可点元素（{{ selPage.buttons.length }}）</div>
          <table v-if="selPage.buttons.length">
            <thead><tr><th>文本</th><th>选择器</th><th>状态</th><th>来源</th><th>说明</th><th>角色</th></tr></thead>
            <tbody>
              <tr v-for="(b, i) in selPage.buttons" :key="'b' + i">
                <td style="max-width:180px;word-break:break-all">{{ b.text || '—' }}</td>
                <td class="mono" style="font-size:11.5px;max-width:180px;word-break:break-all">{{ b.selector || '—' }}</td>
                <td><span :class="b.disabled ? 'st err' : 'st ok'">{{ b.disabled ? '禁用' : '可用' }}</span></td>
                <td><span class="chip">{{ SOURCE_LABEL[b.source] || b.source }}</span></td>
                <td class="muted" style="font-size:11.5px;max-width:150px">{{ b.state_note || '—' }}</td>
                <td class="muted" style="font-size:11.5px">{{ b.roles || '—' }}</td>
              </tr>
            </tbody>
          </table>
          <div v-if="!selPage.buttons.length" class="empty" style="font-size:12px">该页面没有记录到按钮</div>
          <div v-if="selPage.links.length" style="margin-top:14px">
            <div class="muted" style="font-size:12px;margin-bottom:6px">跳转关系（{{ selPage.links.length }}）</div>
            <div style="display:flex;flex-wrap:wrap;gap:4px">
              <span v-for="(l, i) in selPage.links" :key="'l' + i" class="chip">{{ l.text || '链接' }} → {{ l.href }}</span>
            </div>
          </div>
        </template>
        <div v-else class="empty" style="font-size:12.5px">左侧选择一个页面查看按钮与跳转详情</div>
      </div>
    </div>
  </div>
  <div class="panel" v-else-if="projects.length"><div class="empty">还没有地图数据，选好项目与登录角色后点「扫描」生成</div></div>
  <div class="panel" v-else><div class="empty">请先创建项目</div></div>

  <!-- 导入 / 人工维护：upsert 合并写入（页面按 path、元素按 kind+text 同键更新，不删已有数据） -->
  <div class="mask" :class="{ on: impOpen }" @click.self="impOpen = false">
    <div class="modal" style="width:640px" v-if="impOpen">
      <h3>导入应用地图</h3>
      <div class="fld"><label>来源</label>
        <select v-model="impForm.source">
          <option value="manual">人工维护（manual）</option>
          <option value="code">源码分析产物（code，可信度最高，不被弱来源覆盖）</option>
        </select></div>
      <div class="fld"><label>文件（或直接粘贴 JSON 内容）</label>
        <input type="file" accept=".json,application/json" @change="onImpFile"></div>
      <div class="fld"><label>页面数组</label>
        <textarea v-model="impForm.text" rows="10" class="mono" :placeholder="IMP_EXAMPLE"></textarea></div>
      <div class="faint" style="font-size:12px;margin:-4px 0 8px">
        合并写入：同 path 页面更新标题/层级，同 kind+text 元素更新字段，不删除已有数据。
        state_note 写元素的出现条件（如「仅审批岗可见」）——非空的元素属条件性出现，执行比对时跳过、不产生缺失警告。
        与 MCP 工具 app_map_upsert、源码分析 Skill（skills/app-map-source-scan）同一通道。
      </div>
      <div v-if="impErr" style="color:var(--err);font-size:12.5px;margin-bottom:8px">{{ impErr }}</div>
      <div class="ft"><button class="btn" @click="impOpen = false">取消</button>
        <button class="btn pri" :disabled="impBusy" @click="doImportMap">{{ impBusy ? '导入中…' : '导入' }}</button></div>
    </div>
  </div>
</template>
