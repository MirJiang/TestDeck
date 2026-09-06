<script setup>
import { ref, computed, onBeforeUnmount } from 'vue'
import { api, getToken } from '../api'

// 可视化录制弹窗：
//  - remote 模式：页面画面串流进来，直接在这里点击/输入/跳转，操作即被录成步骤（远程与容器部署可用）
//  - 混合录制：一句话让 AI 代劳当前这步（登录/验证码/长表单），AI 的动作也记成普通步骤，回放零 token
//  - local 模式：在服务器本机弹出真实浏览器操作（仅本地开发可用）
const props = defineProps({
  initialUrl: { type: String, default: '' },
  title: { type: String, default: '录制 UI 用例' },
  roleNote: { type: String, default: '' },   // 流程测试里为某个角色录制时的提示
  envs: { type: Array, default: () => [] },  // 可选：注入 AI 代劳的环境变量（选中的环境）
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
let frameTimer = null
let pollTimer = null
let finished = false

// ---- AI 代劳（混合录制） ----
const aiGoal = ref('')
const aiEnvId = ref('')
const aiRunning = ref(false)
const aiVars = computed(() =>
  (props.envs.find(e => e.id === aiEnvId.value) || props.envs[0] || {}).variables || {})

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
  if (mode === 'local') {
    stage.value = 'localwait'
    pollTimer = setInterval(checkDone, 2000)
  } else {
    stage.value = 'rec'
    addr.value = u
    pageUrl.value = u
    frameTimer = setInterval(pullFrame, 400)
    pollTimer = setInterval(checkDone, 2500)
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
  if (!aiGoal.value.trim() || busy.value) return
  errMsg.value = ''
  aiRunning.value = true
  const r = await cmd({ op: 'ai', goal: aiGoal.value.trim(), vars: aiVars.value, max_steps: 12 }, 300000)
  aiRunning.value = false
  if (r.ok) {
    aiGoal.value = ''
    stepN.value += r.ai_steps || 0
  } else {
    errMsg.value = r.error || 'AI 未完成目标，画面停留在它操作到的地方，可继续手动录制'
  }
}

// ---- 完成 → review ----
async function stopToReview() {
  if (finished) return
  finished = true
  clearInterval(frameTimer); clearInterval(pollTimer)
  const s = await api('/cases/ui-record/' + sess.value)
  rawSteps.value = s.steps
  stepN.value = s.steps.length
  stage.value = 'review'
}

async function doEnhance() {
  enhancing.value = true; errMsg.value = ''
  try {
    enh.value = await api('/ai/enhance-steps',
      { method: 'POST', body: { steps: rawSteps.value, url: url.value } }, { timeout: 180000 })
  } catch (e) { errMsg.value = e.message } finally { enhancing.value = false }
}

function use(steps) { emit('done', steps) }

onBeforeUnmount(() => {
  finished = true
  clearInterval(frameTimer); clearInterval(pollTimer)
  if (frameUrl.value) URL.revokeObjectURL(frameUrl.value)
})
</script>

<template>
  <div class="mask rec on" @click.self="emit('close')">
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
          <button class="btn" style="margin-left:auto" @click="emit('close')">放弃</button>
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
        </div>
        <div class="rec-tools" style="margin-top:8px">
          <span class="muted" style="font-size:12.5px;white-space:nowrap">AI 代劳</span>
          <input v-model="aiGoal" style="flex:1;min-width:240px"
            placeholder="这一步让 AI 做，如：用 ${'{'}username{'}'} 登录，看到欢迎页为止" @keyup.enter="doAi">
          <select v-if="envs.length" v-model="aiEnvId" :disabled="busy" style="width:auto">
            <option v-for="e in envs" :key="e.id" :value="e.id">{{ e.name }}</option>
          </select>
          <button class="btn sm pri" :disabled="busy || !aiGoal.trim()" @click="doAi">
            {{ aiRunning ? 'AI 操作中…' : '让 AI 做' }}</button>
        </div>
        <div class="rec-shot" :class="{ busy }">
          <img v-if="frameUrl" :src="frameUrl" @click="onImgClick" @wheel="onWheel" draggable="false" alt="录制画面">
          <div v-else class="empty" style="padding:80px 0">正在连接页面…</div>
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
