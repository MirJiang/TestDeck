<script setup>
import { ref, watch, onMounted } from 'vue'
import { api } from '../api'
import CaseEditor from '../components/CaseEditor.vue'
import RunDrawer from '../components/RunDrawer.vue'
import RecorderModal from '../components/RecorderModal.vue'
import { confirmDialog, alertDialog, toast } from '../dialog'

const projects = ref([])
const pid = ref('')
const list = ref([])
const q = ref('')
const editing = ref(null)
const running = ref(null)
const envs = ref([])
const err = ref('')
const nlShow = ref(false)
// ---- HAR / Postman 存量资产导入 ----
const impOpen = ref(false)
const impForm = ref({ format: 'har', name: '', text: '' })
const impErr = ref('')
const impBusy = ref(false)

function onImpFile(e) {
  const f = e.target.files[0]
  if (!f) return
  const r = new FileReader()
  r.onload = () => { impForm.value.text = r.result; if (!impForm.value.name) impForm.value.name = f.name.replace(/\.[^.]+$/, '') }
  r.readAsText(f)
}

async function doImport() {
  impErr.value = ''
  if (!impForm.value.text.trim()) { impErr.value = '先粘贴或选择文件'; return }
  impBusy.value = true
  try {
    const r = await api(`/projects/${pid.value}/cases/import-assets`, { method: 'POST', body: impForm.value })
    toast(`已导入 ${r.count} 个用例${r.names?.length ? '：' + r.names.slice(0, 3).join('、') + (r.count > 3 ? ' 等' : '') : ''}`)
    impOpen.value = false
    impForm.value = { format: 'har', name: '', text: '' }
    load()
  } catch (e) { impErr.value = e.message } finally { impBusy.value = false }
}
// ---- AI 现场生成用例：选账号 → 填目标 → 打开录制弹窗让 AI 现场执行，全程可视、步骤实时记录 ----
const nlForm = ref({ userId: '', url: '', goal: '' })
const nlRec = ref(null)          // 活动的现场会话 {url, goal, username, password}
const pusers = ref([])
watch(pid, async v => { pusers.value = v ? await api(`/projects/${v}/users`).catch(() => []) : [] }, { immediate: true })

function openNl() {
  nlForm.value = { userId: '', url: envs.value[0]?.base_url || '', goal: '' }
  nlShow.value = true
}

function startNlLive() {
  const goal = nlForm.value.goal.trim()
  if (!goal) { toast('先告诉 AI 要测什么'); return }
  if (!nlForm.value.url.trim().startsWith('http')) { toast('先填被测页面地址'); return }
  const u = pusers.value.find(x => x.id === nlForm.value.userId)
  nlRec.value = { url: nlForm.value.url.trim(), goal,
                  username: u?.username || '', password: u?.password || '' }
  nlShow.value = false
}

function onNlDone(steps) {
  const r = nlRec.value
  const base = (envs.value[0]?.base_url || '').replace(/\/+$/, '')
  let start = r.url
  if (base && start.startsWith(base)) start = start.slice(base.length) || '/'   // 起始页面只存路径
  editing.value = { id: '', project_id: pid.value, name: r.goal.slice(0, 24), type: 'ai', target: 'ui',
    goal: r.goal, start_url: start, engine: '', max_steps: 30,
    fixedSteps: steps, username: r.username, password: r.password, source: 'ai' }
  nlRec.value = null
  toast('AI 现场执行完成，已带出用例——确认无误后点「保存用例」', 4000)
}

onMounted(async () => {
  projects.value = await api('/projects')
  if (projects.value.length) { pid.value = projects.value[0].id; await load() }
})

async function load() {
  if (!pid.value) return
  list.value = await api(`/projects/${pid.value}/cases`)
  envs.value = await api(`/projects/${pid.value}/envs`)
}

function openNew() {
  editing.value = { id: '', project_id: pid.value, name: '', type: 'ai', target: 'ui',
    goal: '', start_url: '', engine: '', max_steps: 30, fixedSteps: [], username: '', password: '' }
}
async function openEdit(id) {
  const c = await api('/cases/' + id)
  if (c.type !== 'ai') { await alertDialog('历史 API/UI 用例不再支持编辑，可删除后重建为 AI 用例', '提示'); return }
  const cfg = c.steps?.[0] || {}
  editing.value = { id: c.id, project_id: c.project_id, name: c.name, type: 'ai',
    target: cfg.target || 'ui', goal: cfg.goal || '', start_url: cfg.start_url || '',
    engine: cfg.engine || '', max_steps: cfg.max_steps || 30,
    endpoints: cfg.endpoints || [], fixedSteps: cfg.fixed_steps || [],
    username: c.username || '', password: c.password || '' }
}

async function save(c) {
  err.value = ''
  const body = { project_id: c.project_id, name: c.name, type: 'ai', target: c.target || 'ui',
    goal: c.goal || '', start_url: c.start_url || '', engine: c.engine || '',
    max_steps: c.max_steps || 20, endpoints: c.endpoints || [],
    fixed_steps: c.fixedSteps || [], source: c.source,
    username: c.username || '', password: c.password || '' }
  try {
    if (c.id) await api('/cases/' + c.id, { method: 'PUT', body })
    else await api(`/projects/${c.project_id}/cases`, { method: 'POST', body })
    editing.value = null; load()
  } catch (e) { err.value = e.message; await alertDialog(e.message, '保存失败') }
}

async function copyCase(c) {
  const r = await api('/cases/' + c.id + '/copy', { method: 'POST' })
  toast(`已复制为「${c.name}（副本）」`)
  await load()
}

async function del(c) {
  if (!await confirmDialog(`删除用例「${c.name}」？`, { danger: true, okText: '删除' })) return
  await api('/cases/' + c.id, { method: 'DELETE' }); load()
}

function runCase(c) { running.value = { title: c.name, caseId: c.id, caseType: c.type } }

const filtered = () => list.value.filter(c => c.name.includes(q.value.trim()))
</script>

<template>
  <div class="hd"><div><h2>用例</h2></div>
    <div style="display:flex;gap:8px">
      <button class="btn" @click="openNl" :disabled="!pid">AI · 现场生成用例</button>
      <button class="btn" @click="impOpen = true" :disabled="!pid">导入 HAR/Postman</button>
      <button class="btn pri" @click="openNew" :disabled="!pid">新建用例</button>
    </div></div>
  <div class="panel">
    <div class="bar">
      <div style="display:flex;gap:8px">
        <select v-model="pid" @change="load"><option v-if="!projects.length" value="" disabled>暂无项目，请先创建</option><option v-for="p in projects" :key="p.id" :value="p.id">{{ p.name }}</option></select>
        <input v-model="q" placeholder="筛选用例名…" style="width:180px">
      </div>
      <span class="muted">{{ filtered().length }} 条</span>
    </div>
    <table>
      <thead><tr><th>用例</th><th>类型</th><th>步骤</th><th>来源</th><th>更新</th><th></th></tr></thead>
      <tbody>
        <tr v-for="c in filtered()" :key="c.id">
          <td><b>{{ c.name }}</b></td>
          <td><template v-if="c.type === 'ai'">
                <span class="tag-ai">AI</span>
                <span class="chip" style="margin-left:4px">{{ c.target === 'api' ? 'API' : 'UI' }}</span>
                <span v-if="c.fixed" class="chip" style="margin-left:2px">固定</span>
              </template>
              <template v-else>
                <span :class="c.type === 'ui' ? 'tag-ui' : 'tag-api'">{{ c.type === 'ui' ? 'UI' : 'API' }}</span>
                <span class="chip" style="margin-left:4px">历史</span>
              </template></td>
          <td class="mono">{{ c.type === 'ai' ? (c.fixed ? '固定' : 'AI') : c.steps }}</td>
          <td class="muted">{{ c.source === 'manual' ? '手动创建' : c.source === 'ai' ? 'AI 生成' : 'Git 分析' }}</td>
          <td class="mono muted">{{ c.updated_at.slice(0, 10) }}</td>
          <td style="text-align:right">
            <a v-if="c.type === 'ai'" @click="openEdit(c.id)">编辑</a><span v-else class="faint">历史</span> ·
            <a @click="runCase(c)">执行</a> ·
            <a @click="copyCase(c)">复制</a> · <a style="color:var(--err)" @click="del(c)">删除</a></td>
        </tr>
        <tr v-if="!list.length"><td colspan="6" class="empty">{{ pid ? '暂无用例，点右上角「新建用例」开始' : '请先创建项目' }}</td></tr>
      </tbody>
    </table>
  </div>

  <CaseEditor v-if="editing" v-model="editing" @save="save" @close="editing = null" />
  <RunDrawer v-if="running" :title="running.title" :caseId="running.caseId" :caseType="running.caseType" :envs="envs"
    @close="running = null" @done="load" />
  <!-- 存量资产导入：HAR / Postman Collection -->
  <div class="mask" :class="{ on: impOpen }" @click.self="impOpen = false">
    <div class="modal" style="width:620px" v-if="impOpen">
      <h3>导入存量测试资产</h3>
      <div class="fld"><label>格式</label>
        <select v-model="impForm.format">
          <option value="har">HAR（浏览器开发者工具导出的请求日志 → 一个多步骤用例）</option>
          <option value="postman">Postman Collection v2.x（每个请求 → 一个用例）</option>
        </select></div>
      <div class="fld"><label>用例名前缀（可选）</label>
        <input v-model="impForm.name" placeholder="如：回归-2026Q4"></div>
      <div class="fld"><label>文件（或直接粘贴 JSON 内容）</label>
        <input type="file" accept=".har,.json,application/json" @change="onImpFile"></div>
      <div class="fld"><label>内容</label>
        <textarea v-model="impForm.text" rows="6" class="mono" placeholder='{"log": {"entries": ...}}'></textarea></div>
      <div class="faint" style="font-size:12px;margin:-4px 0 8px">
        静态资源请求自动过滤；与项目环境地址同源的请求转为相对路径。Postman 的测试脚本不会迁移，导入后可在用例里补检查点。
      </div>
      <div v-if="impErr" style="color:var(--err);font-size:12.5px;margin-bottom:8px">{{ impErr }}</div>
      <div class="ft"><button class="btn" @click="impOpen = false">取消</button>
        <button class="btn pri" :disabled="impBusy" @click="doImport">{{ impBusy ? '导入中…' : '导入' }}</button></div>
    </div>
  </div>

  <!-- AI 现场生成用例：预填表单（选账号/地址/目标）→ 录制弹窗里 AI 现场执行 -->
  <div class="mask" :class="{ on: nlShow }" @click.self="nlShow = false">
    <div class="modal">
      <h3>AI · 现场生成用例</h3>
      <div class="fld"><label>测试账号（项目用户列表，可改）</label>
        <select v-model="nlForm.userId">
          <option value="">— 不使用账号 —</option>
          <option v-for="u in pusers" :key="u.id" :value="u.id">{{ u.name ? `${u.name}（${u.username}）` : u.username }}</option>
        </select></div>
      <div class="fld"><label>被测页面地址</label>
        <input v-model="nlForm.url" class="mono" placeholder="https://test.example.com/login"></div>
      <div class="fld"><label>要让 AI 做什么？（大白话，可引用 $&#123;username&#125; $&#123;password&#125;）</label>
        <textarea v-model="nlForm.goal" rows="3" placeholder="用 ${username} 登录，创建一张从上海到北京的询价单，页面应提示创建成功"></textarea></div>
      <div class="faint" style="font-size:12px;margin:-4px 0 10px">
        点击开始后会打开录制画面：AI 现场执行目标、每步实时记录，你全程可视、可随时停止或接着手动录。
      </div>
      <div class="ft"><button class="btn" @click="nlShow = false">取消</button>
        <button class="btn pri" @click="startNlLive">开始（AI 现场执行）</button></div>
    </div>
  </div>

  <RecorderModal v-if="nlRec" title="AI 现场生成用例" :envs="envs"
    :initial-url="nlRec.url" :auto-start="true" :auto-goal="nlRec.goal"
    :preset-vars="{ username: nlRec.username, password: nlRec.password }"
    @done="onNlDone" @close="nlRec = null" />
</template>
