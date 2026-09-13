<script setup>
import { ref, computed, watch, onMounted } from 'vue'
import { api } from '../api'
import FlowDiagram from '../components/FlowDiagram.vue'
import RecorderModal from '../components/RecorderModal.vue'
import { confirmDialog, alertDialog, toast } from '../dialog'

const projects = ref([])
const pid = ref('')
const list = ref([])
const cases = ref([])      // 本项目用例（供「引用用例」步骤选择）
const editing = ref(null)
const running = ref(null)
const envs = ref([])
const err = ref('')
const history = ref(null)
const historyRows = ref([])
const selected = ref(-1)

const UI_ACTIONS = [
  { action: 'goto', label: '打开页面' },
  { action: 'click', label: '点击' },
  { action: 'fill', label: '在输入框输入' },
  { action: 'expect_text', label: '检查页面显示的文字' },
  { action: 'ai', label: 'AI 代劳（现场执行目标）' },
  { action: 'screenshot', label: '截图留档' },
]

onMounted(async () => {
  projects.value = await api('/projects')
  if (projects.value.length) { pid.value = projects.value[0].id; await load() }
})

async function load() {
  list.value = await api(`/flows?project_id=${pid.value}`)
  envs.value = await api(`/projects/${pid.value}/envs`)
  cases.value = await api(`/projects/${pid.value}/cases`)
}

function openNew() {
  editing.value = {
    id: '', project_id: pid.value, name: '', desc: '',
    roles: [{ key: 'r1', name: '', variables: { username: '', password: '' } }],
    steps: [],
  }
  selected.value = -1
}
async function openEdit(id) {
  editing.value = await api('/flows/' + id)
  selected.value = -1
}

function addStep(type) {
  const role = editing.value.roles[0]?.key || ''
  const tpl = {
    ui: { role, type: 'ui', action: 'goto', url: '', selector: '', value: '' },
    ai: { role, type: 'ai', goal: '', url: '', max_steps: 200, retries: 1 },
    case: { role, type: 'case', case_id: '' },
    api: { role, type: 'api', m: 'POST', url: '', headers: '', body: '',
           check: { type: 'status', expect: '200', field: '' }, save: { name: '', from: '' } },
  }
  editing.value.steps.push(tpl[type] || tpl.api)
  selected.value = editing.value.steps.length - 1
}
function onStepType(s) {
  const fresh = { role: s.role, type: s.type }
  if (s.type === 'ui') Object.assign(fresh, { action: 'goto', url: '', selector: '', value: '' })
  else if (s.type === 'ai') Object.assign(fresh, { goal: '', url: '', max_steps: 200 })
  else if (s.type === 'case') Object.assign(fresh, { case_id: '' })
  else Object.assign(fresh, { m: 'POST', url: '', headers: '', body: '',
    check: { type: 'status', expect: '200', field: '' }, save: { name: '', from: '' } })
  Object.keys(s).forEach(k => delete s[k])
  Object.assign(s, fresh)
}
function delStep(i) { editing.value.steps.splice(i, 1) }
function moveStep(i, d) {
  const s = editing.value.steps
  if (i + d < 0 || i + d >= s.length) return
  ;[s[i], s[i + d]] = [s[i + d], s[i]]
  selected.value = i + d
}

function addRole() {
  // 角色只填名称；标识是内部字段（步骤按它引用），自动生成防碰撞
  const keys = new Set(editing.value.roles.map(r => r.key))
  let n = editing.value.roles.length + 1
  while (keys.has('r' + n)) n++
  editing.value.roles.push({ key: 'r' + n, name: '', variables: { username: '', password: '' } })
}
function delRole(i) { editing.value.roles.splice(i, 1) }

// 项目用户池：角色从用户下拉选择，带出名称/账号/密码（密码仍可手改）
const pusers = ref([])
watch(pid, async v => { pusers.value = v ? await api(`/projects/${v}/users`).catch(() => []) : [] }, { immediate: true })
function onPickUser(r) {
  const u = pusers.value.find(x => x.id === r.userId)
  if (!u) return
  r.name = u.name || u.username
  r.variables.username = u.username
  r.variables.password = u.password
}
// 编辑已有流程时按账号回选用户下拉
watch(pusers, list => {
  if (!editing.value) return
  editing.value.roles.forEach(r => {
    if (!r.userId && r.variables?.username)
      r.userId = (list.find(u => u.username === r.variables.username) || {}).id || ''
  })
})

// ---- 角色录制：可视化录制弹窗，录完的步骤挂到该角色名下 ----
const recOpen = ref(false)
const recRole = ref(null)

function recordRole(role) { recRole.value = role; recOpen.value = true }

function onRecDone(steps) {
  const tagged = steps.map(st => ({ ...st, role: recRole.value.key, type: 'ui' }))
  editing.value.steps.push(...tagged)
  recOpen.value = false
  toast(`已为「${recRole.value.name}」追加 ${tagged.length} 步（页面操作）。再换一个角色录，或点保存。`, 3500)
}

async function save() {
  err.value = ''
  const body = JSON.parse(JSON.stringify(editing.value))
  try {
    if (body.id) await api('/flows/' + body.id, { method: 'PUT', body })
    else await api('/flows', { method: 'POST', body })
    editing.value = null; load()
  } catch (e) { err.value = e.message; await alertDialog(e.message, '保存失败') }
}

async function del(f) {
  if (!await confirmDialog(`删除流程「${f.name}」？`, { danger: true, okText: '删除' })) return
  await api('/flows/' + f.id, { method: 'DELETE' }); load()
}

async function manageBaselines(f) {
  const list = await api(`/flows/${f.id}/baselines`)
  if (!list.length) { toast('该流程还没有截图基线（截图步骤首次成功执行后自动留存）'); return }
  if (!await confirmDialog(
    `流程「${f.name}」现有 ${list.length} 张截图基线。\n清除后下次成功执行会按新页面重新留存；改造流程步骤导致对比错位时用。`,
    { danger: true, okText: '清除基线' })) return
  const r = await api(`/flows/${f.id}/baselines`, { method: 'DELETE' })
  toast(`已清除 ${r.removed} 张基线`)
}

const runEnv = ref('')
const runLoading = ref(false)
const result = ref(null)
const aiTips = ref(null)
const aiLoading = ref(false)
let curRunId = ''
let pollTimer = null

async function run(f) {
  running.value = await api('/flows/' + f.id)   // 取完整定义（含 roles）
  result.value = null
  aiTips.value = null
  runEnv.value = envs.value[0]?.id || ''
}
async function doRun() {
  runLoading.value = true; result.value = null; aiTips.value = null
  // 后端边执行边把明细写库；请求返回前轮询最新一条 running 记录，步骤实时显示
  pollTimer = setInterval(async () => {
    try {
      const r = await api(`/runs?flow=${running.value.id}&size=1`)
      const cur = r.items?.[0]
      if (cur && cur.status === 'running') {
        curRunId = cur.id
        result.value = await api('/runs/' + cur.id)
      }
    } catch { /* 轮询失败忽略，等下一轮 */ }
  }, 1200)
  try {
    result.value = await api(`/flows/${running.value.id}/run`,
      { method: 'POST', body: { env_id: runEnv.value } }, { timeout: 600000 })
  } catch (e) { await alertDialog(e.message, '执行失败') } finally {
    runLoading.value = false; curRunId = ''
    clearInterval(pollTimer)
  }
}
async function cancelRun() {
  if (!curRunId) return
  try { await api(`/runs/${curRunId}/cancel`, { method: 'POST' }) } catch { /* 已结束则忽略 */ }
}
async function analyze() {
  aiLoading.value = true; aiTips.value = null
  try { aiTips.value = await api(`/ai/analyze-run/${result.value.id}`, { method: 'POST' }) }
  catch (e) { aiTips.value = { cause: e.message, suggestion: '', engine: 'error' } }
  finally { aiLoading.value = false }
}
async function viewDetail(r) {
  history.value = null
  result.value = await api(`/flows/runs/${r.id}/detail`)
  const f = list.value.find(x => x.id === r.flow_id)
  running.value = { name: r.flow_name, id: r.flow_id, roles: f ? await api('/flows/' + f.id).then(d => d.roles) : [] }
}
async function openHistory(f) {
  history.value = f
  historyRows.value = await api(`/flows/${f.id}/runs`)
}

// 编辑视图给泳道图的步骤（引用用例的步骤补上用例名用于展示）
const editSteps = computed(() => (editing.value?.steps || []).map(s =>
  s.type === 'case'
    ? { ...s, case_name: cases.value.find(c => c.id === s.case_id)?.name || '（未选择用例）' }
    : s))

// 执行结果里只有 detail；转成泳道图需要的步骤形状（detail 与步骤同序）
function stepsFor(result) {
  return (result.detail || []).map(d => ({
    role: d.role, type: d.type, action: 'goto', url: d.type === 'case' ? '' : (d.target || ''),
    case_name: d.type === 'case' ? (d.target || '').replace('[用例] ', '') : '',
    goal: d.type === 'ai' ? (d.target || '').replace('[AI] ', '') : '',
    selector: '', value: '', saved: d.saved ? { name: d.saved } : null,
  }))
}
</script>

<template>
  <div class="hd"><div><h2>流程测试</h2></div>
    <button class="btn pri" @click="openNew" :disabled="!pid">新建流程</button></div>

  <div class="panel">
    <div class="bar">
      <select v-model="pid" @change="load"><option v-if="!projects.length" value="" disabled>暂无项目，请先创建</option><option v-for="p in projects" :key="p.id" :value="p.id">{{ p.name }}</option></select>
      <span class="muted">{{ list.length }} 条流程</span>
    </div>
    <table>
      <thead><tr><th>流程</th><th>角色</th><th>步骤</th><th>更新</th><th></th></tr></thead>
      <tbody>
        <tr v-for="f in list" :key="f.id">
          <td><b>{{ f.name }}</b></td>
          <td class="mono">{{ f.roles }}</td>
          <td class="mono">{{ f.steps }}</td>
          <td class="mono muted">{{ f.updated_at.slice(0, 10) }}</td>
          <td style="text-align:right"><a @click="run(f)">执行</a> · <a @click="openEdit(f.id)">编辑</a> ·
            <a @click="openHistory(f)">记录</a> · <a @click="manageBaselines(f)">基线</a> ·
            <a style="color:var(--err)" @click="del(f)">删除</a></td>
        </tr>
        <tr v-if="!list.length"><td colspan="5" class="empty">
          {{ pid ? '暂无流程，点右上角新建；先加角色（各自登录），再用录制或手动步骤串出业务线' : '请先创建项目' }}</td></tr>
      </tbody>
    </table>
  </div>

  <!-- 流程编辑抽屉 -->
  <div class="drawer" :class="{ on: !!editing }" v-if="editing">
    <div class="dh"><h3>{{ editing.id ? '编辑流程' : '新建流程' }}</h3><button class="x" @click="editing = null">✕</button></div>
    <div class="db">
      <div class="fld"><label>流程名称</label><input v-model="editing.name" placeholder="询价单全流程"></div>
      <div class="fld"><label>说明</label><input v-model="editing.desc" placeholder="货主发单，多家物流报价"></div>

      <div class="bar">
        <h3 style="font-size:13px">角色（每个角色独立登录）</h3>
        <button class="btn sm" @click="addRole">+ 加角色</button>
      </div>
      <div v-for="(r, i) in editing.roles" :key="i"
           style="display:flex;gap:8px;align-items:center;border:1px solid var(--line);border-radius:7px;padding:10px;margin-bottom:8px">
        <input v-model="r.name" placeholder="角色名称，如 货主" style="width:118px;flex:none">
        <select v-model="r.userId" style="flex:1;min-width:150px" @change="onPickUser(r)"
                title="从项目测试用户里选（项目页「用户」维护），选中带出名称与账号密码">
          <option value="">— 选择测试用户 —</option>
          <option v-for="u in pusers" :key="u.id" :value="u.id">{{ u.name ? `${u.name}（${u.username}）` : u.username }}</option>
        </select>
        <input v-model="r.variables.password" placeholder="登录密码" type="password" style="width:108px;flex:none"
               title="选择用户后自动带出，可改">
        <button class="btn sm" style="flex:none" :disabled="recOpen" @click="recordRole(r)">● 录制</button>
        <button style="background:none;color:var(--err);flex:none" @click="delRole(i)">删</button>
      </div>
      <div v-if="!pusers.length" class="faint" style="font-size:12px;margin:-2px 0 8px">
        项目还没有测试用户：先到项目页「用户」里添加或批量导入，这里就能选择带出账号密码。
      </div>

      <div class="bar" style="margin-top:10px">
        <h3 style="font-size:13px">流程图（点击卡片编辑对应步骤）</h3>
        <div style="display:flex;gap:6px">
          <button class="btn sm" @click="addStep('ui')">+ 页面操作</button>
          <button class="btn sm" @click="addStep('ai')">+ AI 步骤</button>
          <button class="btn sm" @click="addStep('case')">+ 引用用例</button>
          <button class="btn sm" @click="addStep('api')">+ 接口调用</button>
        </div>
      </div>
      <FlowDiagram :roles="editing.roles" :steps="editSteps" editable @select="selected = $event" />

      <div v-for="(s, i) in editing.steps" :key="i" v-show="selected === i" class="step open" style="margin-top:12px">
        <div class="hd">
          <span class="idx">{{ i + 1 }}</span>
          <span class="chip">{{ editing.roles.find(r => r.key === s.role)?.name || s.role }}</span>
          <button class="del" @click="selected = -1">收起</button>
        </div>
        <div class="bd">
          <div class="two">
            <div class="fld"><label>以哪个角色操作</label>
              <select v-model="s.role"><option v-for="r in editing.roles" :key="r.key" :value="r.key">{{ r.name }}</option></select></div>
            <div class="fld"><label>类型</label>
              <select v-model="s.type" @change="onStepType(s)">
                <option value="ui">页面操作（无头浏览器）</option>
                <option value="ai">AI 智能操作（大模型决策）</option>
                <option value="case">引用用例（复用已有测试）</option>
                <option value="api">接口调用</option>
              </select></div>
          </div>

          <template v-if="s.type === 'case'">
            <div class="fld"><label>要引用的用例</label>
              <select v-model="s.case_id">
                <option value="" disabled>选择用例…</option>
                <option v-for="c in cases" :key="c.id" :value="c.id">
                  {{ c.name }}（{{ c.type === 'ai' ? 'AI' : c.type.toUpperCase() }}）</option>
              </select></div>
            <div class="faint" style="font-size:12px;margin:-4px 0 10px;line-height:1.7">
              用例会在上方所选角色的会话里执行：它内部写的 $&#123;变量&#125; 能读到角色变量与全流程共享变量，
              save 记住的结果自动进入共享区供后续步骤使用。
            </div>
          </template>

          <template v-if="s.type === 'ai'">
            <div class="fld"><label>这一步要 AI 做什么（大白话，可引用 ${'{'}变量{'}'} 和共享单号）</label>
              <textarea v-model="s.goal" rows="3" placeholder="以该角色身份登录，创建一张询价单，记住单号到 ${bill_no}"></textarea></div>
            <div class="two">
              <div class="fld"><label>起始页面（可选，只填路径）</label>
                <input v-model="s.url" class="mono" placeholder="/page/login"></div>
              <div class="fld"><label>最大步数</label>
                <input v-model.number="s.max_steps" type="number" min="3" max="9999" placeholder="200"></div>
            </div>
          </template>

          <template v-if="s.type === 'ui'">
            <div class="fld"><label>动作</label>
              <select v-model="s.action"><option v-for="a in UI_ACTIONS" :key="a.action" :value="a.action">{{ a.label }}</option></select></div>
            <div class="fld" v-if="['goto'].includes(s.action)"><label>页面地址（只填路径）</label>
              <input v-model="s.url" class="mono" placeholder="/page/inquiry/quote?id=${inquiry_id}&carrier=A"></div>
            <div class="fld" v-if="['click','fill'].includes(s.action)"><label>元素（CSS 选择器）</label>
              <input v-model="s.selector" class="mono" placeholder="#quote"></div>
            <div class="fld" v-if="s.action === 'fill'"><label>输入内容</label>
              <input v-model="s.value" placeholder="1280"></div>
            <div class="fld" v-if="s.action === 'expect_text'"><label>页面应显示的文字</label>
              <input v-model="s.value" placeholder="报价成功"></div>
            <div class="fld" v-if="s.action === 'ai'"><label>AI 要完成的目标（大白话，可引用 $&#123;变量&#125;）</label>
              <input v-model="s.value" placeholder="用 $&#123;username&#125; 登录并完成滑块验证码，进入首页为止"></div>
            <div class="fld" v-if="s.action === 'ai'"><label>失败自动重试（次，0-3）</label>
              <input v-model.number="s.retries" type="number" min="0" max="3" placeholder="1"></div>
          </template>

          <template v-if="s.type === 'api'">
            <div class="fld"><label>请求</label>
              <div class="reqline" style="margin-bottom:0">
                <select v-model="s.m"><option>GET</option><option>POST</option><option>PUT</option><option>DELETE</option></select>
                <input v-model="s.url" class="mono" placeholder="/api/inquiry/create">
              </div></div>
            <div class="two">
              <div class="fld"><label>检查 · 满足什么算通过</label>
                <select v-model="s.check.type">
                  <option value="status">状态码是 200</option>
                  <option value="contains">返回内容包含…</option>
                  <option value="field_eq">某个字段的值等于…</option>
                  <option value="not_empty">返回了数据（不为空）</option>
                  <option value="jsonpath">JSONPath 断言（高级）</option>
                </select></div>
              <div class="fld"><label>期望值 / 字段名</label>
                <div class="two" style="margin-bottom:0">
                  <input v-if="['field_eq','not_empty','jsonpath'].includes(s.check.type)"
                    v-model="s.check.field" class="mono" placeholder="data.id 或 $.data.list[*].sku">
                  <input v-model="s.check.expect" class="mono" placeholder="0 或 200">
                </div></div>
            </div>
            <div class="two">
              <div class="fld"><label>提交的数据 JSON（可选）</label>
                <input v-model="s.body" class="mono" placeholder='{"from":"上海","to":"北京"}'></div>
              <div class="fld"><label>记住返回值（全流程共享）</label>
                <div class="two" style="margin-bottom:0">
                  <input v-model="s.save.name" placeholder="inquiry_id">
                  <input v-model="s.save.from" class="mono" placeholder="data.id">
                </div></div>
            </div>
          </template>
          <div class="row">
            <button class="btn sm" @click="moveStep(i, -1)">↑ 上移</button>
            <button class="btn sm" @click="moveStep(i, 1)">↓ 下移</button>
            <button class="btn sm" style="color:var(--err)" @click="delStep(i); selected = -1">删除此步</button>
          </div>
        </div>
      </div>

      <div v-if="err" style="color:var(--err)">{{ err }}</div>
    </div>
    <div class="df"><button class="btn" @click="editing = null">取消</button><button class="btn pri" @click="save">保存流程</button></div>
  </div>

  <RecorderModal v-if="recOpen && editing" title="为角色录制操作" :envs="envs"
    :initial-url="envs[0]?.base_url || ''"
    :role-note="`以「${recRole?.name}」的身份录制`" :preset-vars="recRole?.variables || {}" @done="onRecDone" @close="recOpen = false" />

  <!-- 执行抽屉（泳道图 + 截图） -->
  <div class="drawer on" v-if="running">
    <div class="dh"><h3>执行流程 · {{ running.name }}</h3><button class="x" @click="running = null">✕</button></div>
    <div class="db">
      <div style="display:flex;gap:8px;align-items:center;margin-bottom:14px">
        <span class="muted">环境</span>
        <select v-model="runEnv"><option v-if="!envs.length" value="" disabled>暂无环境</option><option v-for="e in envs" :key="e.id" :value="e.id">{{ e.name }}</option></select>
        <button v-if="runLoading" class="btn" style="margin-left:auto" @click="cancelRun">取消执行</button>
        <button v-else class="btn pri" style="margin-left:auto" :disabled="!runEnv" @click="doRun">
          {{ result ? '再次执行' : '立即执行' }}</button>
      </div>

      <div v-if="runLoading" class="muted" style="margin-bottom:12px">
        <span class="spin" style="border-color:#c8cdd6;border-top-color:var(--acc)"></span>正在执行，完成的步骤会实时出现在下方泳道图…
      </div>

      <template v-if="result">
        <div class="row" style="margin-bottom:12px;align-items:center">
          <span v-if="result.status === 'running'" class="st run"><span class="spin"></span>执行中</span>
          <span v-else :class="result.status === 'passed' ? 'st ok' : 'st err'">{{ result.status === 'passed' ? '流程跑通' : '流程中断' }}</span>
          <span class="mono muted">{{ result.pass_n }}/{{ result.pass_n + result.fail_n }} 步 · {{ result.duration }}s</span>
          <button v-if="result.status === 'failed' && !runLoading" class="btn sm" :disabled="aiLoading"
            style="margin-left:auto" @click="analyze">{{ aiLoading ? '分析中…' : 'AI 分析失败原因' }}</button>
        </div>
        <div v-if="aiTips" style="border:1px solid var(--acc-weak);background:var(--acc-weak);border-radius:7px;padding:10px 12px;margin-bottom:12px">
          <div style="font-size:13px"><b>AI 分析</b> <span class="chip">{{ aiTips.engine === 'builtin' ? '内置规则' : aiTips.engine }}</span></div>
          <div style="font-size:12.5px;margin-top:5px">可能原因：{{ aiTips.cause }}</div>
          <div v-if="aiTips.suggestion" style="font-size:12.5px;margin-top:3px">{{ aiTips.suggestion }}</div>
        </div>
        <FlowDiagram v-if="running.roles?.length" :roles="running.roles" :steps="stepsFor(result)" :detail="result.detail" />
        <div v-else class="muted">该流程的角色定义缺失，请重新编辑保存后执行。</div>
        <div v-for="(d, i) in (result.detail || []).filter(x => x.video)" :key="'vid' + i" style="margin-top:10px">
          <div class="muted" style="font-size:12.5px;margin-bottom:4px">🎬 {{ d.target }}</div>
          <video :src="d.video" controls style="max-width:100%;border:1px solid var(--line);border-radius:7px"></video>
        </div>
      </template>
    </div>
  </div>

  <!-- 执行记录抽屉 -->
  <div class="drawer on" v-if="history">
    <div class="dh"><h3>执行记录 · {{ history.name }}</h3><button class="x" @click="history = null">✕</button></div>
    <div class="db">
      <table>
        <thead><tr><th>执行 ID</th><th>结果</th><th>耗时</th><th>时间</th><th></th></tr></thead>
        <tbody>
          <tr v-for="r in historyRows" :key="r.id">
            <td class="mono">{{ r.id }}</td>
            <td><span :class="r.status === 'passed' ? 'st ok' : 'st err'">{{ r.status === 'passed' ? '跑通' : '中断' }}</span></td>
            <td class="mono">{{ r.duration }}s</td>
            <td class="mono muted">{{ r.created_at.replace('T',' ').slice(0,16) }}</td>
            <td><a @click="viewDetail(r)">详情</a></td>
          </tr>
          <tr v-if="!historyRows.length"><td colspan="5" class="empty">暂无记录</td></tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
