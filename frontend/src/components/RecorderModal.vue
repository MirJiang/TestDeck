<script setup>
import { ref, computed, nextTick, onBeforeUnmount } from 'vue'
import { api, getToken } from '../api'
import { confirmDialog } from '../dialog'

// 可视化录制弹窗：
//  - remote 模式：页面画面串流进来，直接在这里点击/输入/跳转，操作即被录成步骤（远程与容器部署可用）
//  - 混合录制：一句话让 AI 代劳当前这步（登录/验证码/长表单），AI 的动作也记成普通步骤，回放零 token
//  - local 模式：在服务器本机弹出真实浏览器操作（仅本地开发可用）
const props = defineProps({
  initialUrl: { type: String, default: '' },
  title: { type: String, default: '录制 UI 用例' },
  roleNote: { type: String, default: '' },   // 流程测试里为某个角色录制时的提示
  envs: { type: Array, default: () => [] },  // 可选：注入 AI 代劳的环境变量（选中的环境）
  presetVars: { type: Object, default: () => ({}) },  // 额外注入 AI 代劳的变量（用例账号/流程角色变量），优先于环境变量
})
const emit = defineEmits(['done', 'close'])

const url = ref(props.initialUrl)
const stage = ref('input')     // input | rec | review | localwait
const errMsg = ref('')
const sess = ref('')
const frameUrl = ref('')
const pageUrl = ref('')
const addr = ref('')
const busy = ref(false)
const stepN = ref(0)
const fillSel = ref('')
const fillVal = ref('')
const liveSteps = ref([])   // 录制中实时编译出的步骤（随轮询刷新）
const stepsBox = ref(null)
const STEP_LABELS = { goto: '打开', click: '点击', fill: '输入', expect_text: '断言文本', click_xy: '点选坐标', drag: '拖拽', ai: 'AI 代劳' }
const stepDetail = (s) => s.action === 'goto' ? s.url
  : s.action === 'fill' ? `${s.selector} = ${s.value}`
  : (s.action === 'click_xy' || s.action === 'drag') ? s.value
  : (s.selector || s.value)
let frameTimer = null
let pollTimer = null
let ws = null
let wsRetried = false
let finished = false

// ---- AI 代劳（混合录制） ----
const aiGoal = ref('')
const aiMax = ref(200)    // AI 代劳步数上限：录制有人盯守且可随时停止，默认放到接近不限
const aiEnvId = ref('')
const aiRunning = ref(false)
const aiGoals = []   // 本次录制中 AI 代劳的目标（录完增强时作为语境传给模型）
const aiVars = computed(() => ({
  ...((props.envs.find(e => e.id === aiEnvId.value) || props.envs[0] || {}).variables || {}),
  ...props.presetVars,
}))

// ---- 录完 review（AI 增强断言） ----
const rawSteps = ref([])
const enh = ref(null)
const enhancing = ref(false)
const assertN = (arr) => (arr || []).filter(s => s.action === 'expect_text').length

async function start(mode) {
  const u = url.value.trim()
  if (!u.startsWith('http')) { errMsg.value = '请先填完整地址（http://…）'; return }
  errMsg.value = ''
  const r = await api('/cases/ui-record/start?url=' + encodeURIComponent(u) + '&mode=' + mode, { method: 'POST' })
  sess.value = r.session
  liveSteps.value = []
  stepN.value = 0
  if (mode === 'local') {
    stage.value = 'localwait'
    pollTimer = setInterval(checkDone, 2000)
  } else {
    stage.value = 'rec'
    addr.value = u
    pageUrl.value = u
    wsRetried = false
    connectStream()
    pollTimer = setInterval(checkDone, 2500)
  }
}

// Screencast 推流：页面重绘后端即推送新帧（WebSocket），无变化不推。
// 连不上时自动重试一次，仍失败退回 400ms 轮询（/frame 接口保留兼容）。
function connectStream() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws'
  ws = new WebSocket(`${proto}://${location.host}/api/v1/cases/ui-record/${sess.value}/stream?token=${getToken()}`)
  ws.binaryType = 'blob'
  ws.onmessage = (e) => {
    if (typeof e.data === 'string') {   // 控制信号：会话结束/消失，交给状态轮询收尾
      try { JSON.parse(e.data).done && checkDone() } catch { /* 忽略 */ }
      return
    }
    if (frameUrl.value) URL.revokeObjectURL(frameUrl.value)
    frameUrl.value = URL.createObjectURL(e.data)
  }
  ws.onclose = () => {
    if (finished || stage.value !== 'rec') return
    if (!wsRetried) { wsRetried = true; setTimeout(connectStream, 800) }
    else if (!frameTimer) frameTimer = setInterval(pullFrame, 400)
  }
}

async function pullFrame() {
  if (!sess.value || finished) return
  try {
    const resp = await fetch(`/api/v1/cases/ui-record/${sess.value}/frame?t=${Date.now()}`,
      { headers: { Authorization: 'Bearer ' + getToken() } })
    if (!resp.ok) return
    const blob = await resp.blob()
    if (frameUrl.value) URL.revokeObjectURL(frameUrl.value)
    frameUrl.value = URL.createObjectURL(blob)
  } catch { /* 网络抖动，下一轮再拉 */ }
}

async function checkDone() {
  if (!sess.value || finished) return
  try {
    const s = await api('/cases/ui-record/' + sess.value)
    stepN.value = s.steps.length
    liveSteps.value = s.steps
    nextTick(() => { const el = stepsBox.value; if (el) el.scrollTop = el.scrollHeight })
    // AI 代劳收尾：启动即返回的指令靠这里看结果（passed / failed / cancelled）
    const ai = s.ai
    if (ai && ai.state === 'running') {
      aiRunning.value = true
    } else if (aiRunning.value) {
      aiRunning.value = false
      if (ai.state === 'passed') { aiGoals.push(ai.goal); aiGoal.value = '' }
      else errMsg.value = ai.state === 'cancelled'
        ? '已停止 AI 代劳，画面停留在它操作到的地方，可继续手动录制'
        : (ai.summary || 'AI 未完成目标，画面停留在它操作到的地方，可继续手动录制')
    }
    if (s.done) await stopToReview()   // local：用户关窗；remote：会话异常终止
  } catch { /* 忽略轮询错误 */ }
}

async function cmd(body, timeout) {
  busy.value = true
  try { return await api(`/cases/ui-record/${sess.value}/cmd`, { method: 'POST', body }, { timeout }) }
  catch (e) { return { ok: false, error: e.message } }
  finally { busy.value = false }
}

function onImgClick(e) {
  if (busy.value || !frameUrl.value) return
  const img = e.currentTarget
  const r = img.getBoundingClientRect()
  const x = Math.round((e.clientX - r.left) * (img.naturalWidth / r.width))
  const y = Math.round((e.clientY - r.top) * (img.naturalHeight / r.height))
  errMsg.value = ''
  cmd({ op: 'click', x, y }).then(info => {
    if (info.ok && info.input) { fillSel.value = info.sel; fillVal.value = '' }
    else if (!info.ok) errMsg.value = info.error
  })
}

function onWheel(e) {
  e.preventDefault()
  if (!busy.value) cmd({ op: 'scroll', dy: Math.round(e.deltaY) })
}

async function doFill() {
  if (!fillSel.value || fillVal.value === '') return
  const r = await cmd({ op: 'fill', selector: fillSel.value, value: fillVal.value })
  if (r.ok) { fillSel.value = ''; fillVal.value = '' } else errMsg.value = r.error
}

async function doGoto() {
  if (!addr.value.trim().startsWith('http')) { errMsg.value = '地址需以 http(s):// 开头'; return }
  const r = await cmd({ op: 'goto', url: addr.value.trim() })
  if (!r.ok) errMsg.value = r.error
}

async function doAi() {
  if (!aiGoal.value.trim() || busy.value || aiRunning.value) return
  errMsg.value = ''
  // 入队即返回：AI 执行可能很久，进度/结果由 checkDone 轮询会话状态收尾
  const r = await cmd({ op: 'ai', goal: aiGoal.value.trim(), vars: aiVars.value,
                        max_steps: Math.min(999, Math.max(1, parseInt(aiMax.value) || 200)) })
  if (r.ok) aiRunning.value = true
  else errMsg.value = r.error
}

async function cancelAi() {
  try { await api(`/cases/ui-record/${sess.value}/cancel-ai`, { method: 'POST' }) } catch { /* 会话可能已结束 */ }
}

// ---- 完成 → review ----
async function stopToReview() {
  if (finished) return
  finished = true
  clearInterval(frameTimer); clearInterval(pollTimer)
  if (ws) { try { ws.close() } catch { /* 已关闭 */ } ws = null }
  const s = await api('/cases/ui-record/' + sess.value)
  rawSteps.value = s.steps
  stepN.value = s.steps.length
  stage.value = 'review'
}

async function doEnhance() {
  enhancing.value = true; errMsg.value = ''
  try {
    enh.value = await api('/ai/enhance-steps',
      { method: 'POST', body: { steps: rawSteps.value, url: url.value, context: aiGoals.join('；') } },
      { timeout: 180000 })
  } catch (e) { errMsg.value = e.message } finally { enhancing.value = false }
}

function use(steps) { emit('done', steps) }

// 放弃录制须显式确认：误触（如点到遮罩空白处）绝不丢步骤——遮罩不响应点击
async function abandon() {
  const n = liveSteps.value.length || rawSteps.value.length
  if (n && !await confirmDialog(`放弃本次录制？已录的 ${n} 步将不会保留。`, { danger: true, okText: '放弃' })) return
  emit('close')
}

onBeforeUnmount(() => {
  // 关闭弹窗时若 AI 还在跑，尽力通知后端中止（否则它会继续消耗 token 到步数上限）
  if (aiRunning.value && sess.value) {
    try {
      fetch('/api/v1/cases/ui-record/' + sess.value + '/cancel-ai',
        { method: 'POST', headers: { Authorization: 'Bearer ' + getToken() }, keepalive: true })
    } catch { /* 已离开 */ }
  }
  finished = true
  clearInterval(frameTimer); clearInterval(pollTimer)
  if (ws) { try { ws.close() } catch { /* 已关闭 */ } ws = null }
  if (frameUrl.value) URL.revokeObjectURL(frameUrl.value)
})
</script>

<template>
  <div class="mask rec on">
    <div class="modal rec-box">
      <h3>{{ title }}<span v-if="roleNote" class="muted" style="font-weight:400"> · {{ roleNote }}</span></h3>

      <template v-if="stage === 'input'">
        <div class="fld"><label>被测页面完整地址</label>
          <input v-model="url" class="mono" placeholder="http://192.168.1.10:8080/login" @keyup.enter="start('remote')"></div>
        <div v-if="errMsg" style="color:var(--err);font-size:12.5px;margin-bottom:8px">{{ errMsg }}</div>
        <div style="display:flex;gap:8px;align-items:center">
          <button class="btn pri" @click="start('remote')">开始录制</button>
          <button class="btn" @click="start('local')">在服务器本机弹出浏览器录制</button>
        </div>
        <div class="faint" style="font-size:12.5px;margin-top:10px">
          「开始录制」会在平台里看到页面画面，直接点击、输入即可，支持远程与容器部署；
          弹出浏览器的方式仅在后端与你在同一台机器时可用。
        </div>
      </template>

      <template v-else-if="stage === 'localwait'">
        <div class="dlg-msg">已在服务器上打开真实浏览器。请在那个窗口里操作（点击、输入都会被记录），完成后<b>关闭浏览器窗口</b>，步骤会自动出现在这里。</div>
        <div style="margin-top:14px;display:flex;gap:8px;align-items:center">
          <span class="st run"><span class="spin"></span>录制中</span>
          <span class="mono muted">已录 {{ stepN }} 步</span>
          <button class="btn" style="margin-left:auto" @click="abandon">放弃</button>
        </div>
        <div class="rec-steps" style="margin-top:12px">
          <div class="rec-steps-tt">已录步骤（实时）</div>
          <div ref="stepsBox" class="rec-steps-bd">
            <div v-for="(s, i) in liveSteps" :key="i" class="rec-step">
              <span class="n">{{ i + 1 }}</span>
              <span class="a">{{ STEP_LABELS[s.action] || s.action }}</span>
              <span class="d" :title="stepDetail(s)">{{ stepDetail(s) }}</span>
            </div>
            <div v-if="!liveSteps.length" class="faint" style="padding:8px 11px;font-size:12px">
              在弹出的浏览器窗口里的操作会实时出现在这里
            </div>
          </div>
        </div>
      </template>

      <template v-else-if="stage === 'review'">
        <div class="dlg-msg">已录制 <b>{{ rawSteps.length }}</b> 步（其中断言 {{ assertN(rawSteps) }} 个）。</div>
        <template v-if="enh">
          <div v-if="enh.enhanced" style="border:1px solid var(--acc-weak);background:var(--acc-weak);border-radius:7px;padding:10px 12px;margin:10px 0">
            <div style="font-size:13px"><b>AI 增强</b> <span class="chip">{{ enh.engine }}</span>
              <span class="mono muted" style="margin-left:6px">{{ enh.steps.length }} 步 · 断言 {{ assertN(enh.steps) }} 个</span></div>
            <div style="font-size:12.5px;margin-top:5px">{{ enh.note || '已为关键动作补充断言并参数化测试数据。' }}</div>
            <div v-if="Object.keys(enh.vars || {}).length" style="font-size:12.5px;margin-top:4px" class="muted">
              已参数化（记得在「环境」里配置默认值）：
              <span v-for="(v, k) in enh.vars" :key="k" class="chip">${{ '{' + k + '}' }} = {{ v }}</span>
            </div>
          </div>
          <div v-else class="muted" style="font-size:12.5px;margin:10px 0">{{ enh.note }}</div>
        </template>
        <div v-if="errMsg" style="color:var(--err);font-size:12.5px;margin-bottom:8px">{{ errMsg }}</div>
        <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
          <button v-if="!enh || !enh.enhanced" class="btn" :disabled="enhancing || !rawSteps.length" @click="doEnhance">
            {{ enhancing ? 'AI 分析中…' : 'AI 补充断言（可反复用）' }}</button>
          <span style="flex:1"></span>
          <button class="btn" @click="abandon">放弃</button>
          <button class="btn" @click="use(rawSteps)">使用原始步骤</button>
          <button v-if="enh && enh.enhanced" class="btn pri" @click="use(enh.steps)">使用增强结果</button>
        </div>
      </template>

      <template v-else>
        <div class="rec-tools">
          <input v-model="addr" class="mono" style="flex:1;min-width:260px" placeholder="页面地址，改完点「前往」" @keyup.enter="doGoto">
          <button class="btn sm" :disabled="busy" @click="doGoto">前往</button>
          <button class="btn sm" :disabled="busy" @click="cmd({ op: 'back' })">← 后退</button>
          <button class="btn sm" :disabled="busy" @click="cmd({ op: 'scroll', dy: -360 })">↑</button>
          <button class="btn sm" :disabled="busy" @click="cmd({ op: 'scroll', dy: 360 })">↓</button>
          <span class="mono muted">已录 {{ stepN }} 步</span>
          <button class="btn pri sm" :disabled="busy" @click="cmd({ op: 'finish' }).then(stopToReview)">完成录制</button>
          <button class="btn sm" :disabled="busy" @click="abandon">放弃</button>
        </div>
        <div class="rec-tools" style="margin-top:8px">
          <span class="muted" style="font-size:12.5px;white-space:nowrap">AI 代劳</span>
          <input v-model="aiGoal" style="flex:1;min-width:240px"
            placeholder="这一步让 AI 做，如：用 ${'{'}username{'}'} 登录，看到欢迎页为止" @keyup.enter="doAi">
          <span class="faint" style="font-size:12px;white-space:nowrap" title="AI 代劳单轮最多执行的步数（防失控）">步数上限</span>
          <input v-model.number="aiMax" type="number" min="1" max="999" class="mono" style="width:64px" :disabled="busy">
          <select v-if="envs.length" v-model="aiEnvId" :disabled="busy" style="width:auto">
            <option v-for="e in envs" :key="e.id" :value="e.id">{{ e.name }}</option>
          </select>
          <button class="btn sm pri" :disabled="busy || aiRunning || !aiGoal.trim()" @click="doAi">
            {{ aiRunning ? 'AI 操作中…' : '让 AI 做' }}</button>
          <button class="btn sm" v-if="aiRunning" @click="cancelAi">停止 AI</button>
        </div>
        <div class="rec-live">
          <div class="rec-shot" :class="{ busy }">
            <img v-if="frameUrl" :src="frameUrl" @click="onImgClick" @wheel="onWheel" draggable="false" alt="录制画面">
            <div v-else class="empty" style="padding:80px 0">正在连接页面…</div>
          </div>
          <div class="rec-steps-side">
            <div class="rec-steps-tt">已录步骤 <b class="mono">{{ liveSteps.length }}</b></div>
            <div ref="stepsBox" class="rec-steps-bd">
              <div v-for="(s, i) in liveSteps" :key="i" class="rec-step">
                <span class="n">{{ i + 1 }}</span>
                <span class="a">{{ STEP_LABELS[s.action] || s.action }}</span>
                <span class="d" :title="stepDetail(s)">{{ stepDetail(s) }}</span>
              </div>
              <div v-if="!liveSteps.length" class="faint" style="padding:8px 11px;font-size:12px">
                在画面上的每次点击、输入都会实时出现在这里
              </div>
            </div>
          </div>
        </div>
        <div v-if="fillSel" class="rec-fill">
          <span class="sel">{{ fillSel }}</span>
          <input v-model="fillVal" placeholder="要输入的内容" style="flex:1" @keyup.enter="doFill">
          <button class="btn pri sm" :disabled="busy || fillVal === ''" @click="doFill">输入</button>
          <button class="btn sm" @click="fillSel = ''">取消</button>
        </div>
        <div v-else class="faint" style="font-size:12.5px;margin-top:8px">
          在画面上点击即记录一步；点到输入框后可在这里输入内容。滚轮可翻页（不记录为步骤）；
          登录、验证码这类麻烦事交给 AI 代劳，它做的每一步也会被记下来。
        </div>
        <div v-if="aiRunning" class="muted" style="font-size:12.5px;margin-top:6px">
          <span class="spin" style="border-color:#c8cdd6;border-top-color:var(--acc)"></span>
          AI 正在页面上操作（可多轮决策，请稍候），画面会逐步刷新…
        </div>
        <div v-if="errMsg" style="color:var(--err);font-size:12.5px;margin-top:8px">{{ errMsg }}</div>
      </template>

      <div v-if="stage === 'input'" class="ft"><button class="btn" @click="emit('close')">取消</button></div>
    </div>
  </div>
</template>
