<script setup>
import { ref, watch, onMounted } from 'vue'
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
  users.value = []; mapList.value = []; note.value = ''
  form.value = { userIds: [], maxPages: 25 }
  if (v) {
    users.value = await api(`/projects/${v}/users`).catch(() => [])
    await load()
  }
})

async function load() { mapList.value = await api(`/projects/${pid.value}/app-map`) }

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
  mapList.value = []
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

onMounted(async () => {
  projects.value = await api('/projects')
  if (projects.value.length) pid.value = projects[0].id
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

  <div class="panel" v-if="projects.length">
    <div v-for="pg in mapList" :key="pg.id"
         style="border:1px solid var(--line);border-radius:7px;padding:8px 12px;margin:8px 0">
      <div style="display:flex;gap:8px;align-items:baseline;flex-wrap:wrap">
        <span class="chip">L{{ pg.depth }}</span>
        <span v-if="pg.source !== 'scan'" class="chip" :title="'来源：' + (SOURCE_LABEL[pg.source] || pg.source)">{{ SOURCE_LABEL[pg.source] || pg.source }}</span>
        <b class="mono" style="font-size:12.5px;word-break:break-all">{{ pg.path }}</b>
        <span class="muted" style="font-size:12px">{{ pg.title }}</span>
        <span v-if="pg.roles" class="faint" style="font-size:11px" :title="`见到该页的角色：${pg.roles}`">👤 {{ pg.roles }}</span>
        <span class="faint" style="font-size:11px;margin-left:auto">{{ pg.scanned_at?.slice(0, 16).replace('T', ' ') }}</span>
      </div>
      <div v-if="pg.buttons.length" style="margin-top:5px;display:flex;flex-wrap:wrap;gap:4px">
        <span v-for="(b, i) in pg.buttons" :key="'b' + i" class="chip"
              :style="b.disabled ? 'opacity:.5;text-decoration:line-through' : ''"
              :title="[b.selector,
                       b.disabled ? '扫描时禁用' : '',
                       b.source !== 'scan' ? '来源：' + (SOURCE_LABEL[b.source] || b.source) : '',
                       b.state_note ? '条件：' + b.state_note : '',
                       b.roles ? '角色：' + b.roles : ''].filter(Boolean).join('；')">
          {{ b.text || b.selector || '按钮' }}<template v-if="b.source !== 'scan'"> ·{{ SOURCE_LABEL[b.source] || b.source }}</template><template v-if="b.state_note"> ※</template>
        </span>
      </div>
      <div v-if="pg.links.length" style="margin-top:4px;font-size:12px" class="muted">
        跳转：{{ pg.links.slice(0, 8).map(l => (l.text || '链接') + ' → ' + l.href).join('；') }}
        {{ pg.links.length > 8 ? '…' : '' }}
      </div>
    </div>
    <div v-if="!mapList.length && !busy" class="empty">还没有地图数据，选好项目与登录角色后点「扫描」生成</div>
  </div>
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
