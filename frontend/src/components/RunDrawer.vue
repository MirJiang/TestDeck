<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { api } from '../api'
import { toast, alertDialog } from '../dialog'

const props = defineProps({ title: String, caseId: String, caseType: String, planId: String })
const emit = defineEmits(['close', 'done'])

const running = ref(false)
const result = ref(null)
const err = ref('')
const aiTips = ref(null)
const aiLoading = ref(false)
const solidifying = ref(false)

// 绑定的测试账号（单用例执行时显示/可切换）：项目用户池里选一个，写入用例
const acct = ref({ username: '' })          // 用例当前绑定
const pool = ref([])                        // 项目测试用户池
const acctSel = ref('')                     // 选中的池用户 id
const customAcct = computed(() => acct.value.username && !pool.value.some(u => u.username === acct.value.username))
const acctBusy = ref(false)
onMounted(async () => {
  if (!props.caseId) return
  try {
    acct.value = await api('/cases/' + props.caseId)
    pool.value = await api('/projects/' + acct.value.project_id + '/users').catch(() => [])
    const hit = pool.value.find(u => u.username === acct.value.username)
    acctSel.value = hit ? hit.id : (acct.value.username ? '__custom' : '')
  } catch { /* 展示失败不阻塞执行 */ }
})
async function changeAcct() {
  const u = pool.value.find(x => x.id === acctSel.value)
  if (!u) return
  acctBusy.value = true
  try {
    await api(`/cases/${props.caseId}/account`, { method: 'PUT', body: { username: u.username, password: u.password } })
    acct.value.username = u.username
    toast(`已切换测试账号：${u.username}`)
  } catch (e) { err.value = e.message } finally { acctBusy.value = false }
}

async function analyze() {
  aiLoading.value = true; aiTips.value = null
  try { aiTips.value = await api(`/ai/analyze-run/${result.value.id}`, { method: 'POST' }) }
  catch (e) { err.value = e.message } finally { aiLoading.value = false }
}

// 把 AI 执行明细固化成同类型固定步骤用例：之后回归不再调大模型
async function solidify() {
  solidifying.value = true
  try {
    const src = await api('/cases/' + props.caseId)
    const cfg = (src.steps || [{}])[0] || {}
    const isApi = (cfg.target || 'ui') === 'api'
    const acts = result.value.detail || []
    const body = { project_id: src.project_id, name: src.name + '（固化）', type: 'ai', source: 'manual' }
    if (isApi) {
      const steps = acts.filter(s => s.action === 'request' && s.request)
        .map(s => ({ request: s.request, check: s.check || { type: 'status', expect: '200' }, save: s.save || {} }))
      if (!steps.length) { await alertDialog('本次执行没有可固化的请求', '无法固化'); return }
      body.target = 'api'
      body.goal = ''
      body.fixed_api_steps = steps
    } else {
      const steps = acts.filter(s => ['goto', 'click', 'dblclick', 'fill', 'expect_text', 'screenshot'].includes(s.action))
        .map(s => ({ action: s.action, url: s.url || '', selector: s.selector || '', value: s.value || '' }))
      if (!steps.length) { await alertDialog('本次执行没有可固化的页面操作', '无法固化'); return }
      body.target = 'ui'
      body.goal = ''
      body.fixed_steps = steps
    }
    await api(`/projects/${src.project_id}/cases`, { method: 'POST', body })
    toast(`已固化为固定步骤用例（${body.fixed_steps?.length || body.fixed_api_steps?.length || 0} 步），回归零 token`)
    emit('done')
  } catch (e) { await alertDialog(e.message, '固化失败') }
  finally { solidifying.value = false }
}

let poll = null
onUnmounted(() => { if (poll) clearInterval(poll) })

async function run() {
  running.value = true; result.value = null; err.value = ''
  const path = props.caseId ? `/runs/cases/${props.caseId}/run` : `/runs/plans/${props.planId}/run`
  try {
    // 触发接口立即返回 running 记录（执行在后台继续），轮询直到结束；
    // 环境不传：后端用项目唯一环境兜底（计划用其绑定环境）
    result.value = await api(path, { method: 'POST', body: {} })
    emit('done')
    if (result.value.status === 'running') {
      curRunId = result.value.id
      poll = setInterval(async () => {
        try {
          const r = await api('/runs/' + curRunId)
          result.value = r
          if (r.status !== 'running') {
            clearInterval(poll); poll = null
            curRunId = ''
            running.value = false
            emit('done')
          }
        } catch { /* 瞬时失败等下一轮 */ }
      }, 1500)
    } else {
      running.value = false
    }
  } catch (e) { err.value = e.message; running.value = false }
}

let curRunId = ''
async function cancel() {
  if (!curRunId) return
  try { await api(`/runs/${curRunId}/cancel`, { method: 'POST' }); } catch { /* 已结束则忽略 */ }
}

// 结果展示：单用例 detail 是步骤数组，计划 detail 是用例数组
const steps = computed(() => result.value ? (result.value.case_id ? result.value.detail : null) : null)
const casesOfPlan = computed(() => result.value ? (result.value.plan_id ? result.value.detail : null) : null)

// 过程折叠：默认只展开最后的 done 结论步骤（含截图）与录像，前面的过程步骤折一行
const folded = ref(true)
const tail = computed(() => {
  const list = steps.value || []
  const doneIdx = list.map((s, i) => (s.action === 'done' ? i : -1)).filter(i => i >= 0)
  const start = doneIdx.length ? doneIdx[doneIdx.length - 1] : Math.max(0, list.length - 1)
  return { start, list: list.slice(start) }
})
const foldCount = computed(() => tail.value.start)

// 计划条目展开：点一行看该用例/流程内部的步骤明细
const expandedItem = ref(-1)
function toggleItem(i) { expandedItem.value = expandedItem.value === i ? -1 : i }
</script>

<template>
  <div class="drawer on">
    <div class="dh"><h3>执行 · {{ title }}</h3><button class="x" @click="emit('close')">✕</button></div>
    <div class="db">
      <div style="display:flex;gap:8px;align-items:center;margin-bottom:14px">
        <template v-if="caseId">
          <span class="muted">账号</span>
          <select v-model="acctSel" :disabled="acctBusy" @change="changeAcct" title="执行时注入 ${username}/${password}">
            <option v-if="!pool.length && !acct.username" value="" disabled>未绑定账号（可在项目页用户池添加）</option>
            <option v-if="customAcct" value="__custom" disabled>{{ acct.username }}（当前绑定）</option>
            <option v-for="u in pool" :key="u.id" :value="u.id">{{ (u.name ? u.name + ' · ' : '') + u.username }}</option>
          </select>
        </template>
        <span v-else class="muted">按计划配置执行</span>
        <button v-if="running" class="btn" style="margin-left:auto" @click="cancel">取消执行</button>
        <button v-else class="btn pri" style="margin-left:auto" @click="run">
          {{ result ? '再次执行' : '立即执行' }}</button>
      </div>
      <div v-if="err" style="color:var(--err)">{{ err }}</div>

      <div v-if="running" class="muted">正在发送请求并逐条检查…</div>

      <div v-if="result" class="panel" style="margin-top:6px">
        <div class="bar">
          <h3 style="font-size:13px">结果 ·
            <span v-if="result.status === 'running'" class="st run"><span class="spin"></span>执行中，步骤实时更新…</span>
            <span v-else :class="result.status === 'passed' ? 'st ok' : 'st err'">{{ result.status === 'passed' ? '全部通过' : '有一步没通过' }}</span>
          </h3>
          <div style="display:flex;gap:8px;align-items:center">
            <span class="mono muted">{{ result.duration }}s · 通过 {{ result.pass_n }} / 失败 {{ result.fail_n }}<template v-if="result.tokens != null"> · Token {{ result.tokens.toLocaleString() }}</template></span>
            <button v-if="caseType === 'ai' && result.case_id && result.status === 'passed'" class="btn sm pri"
              :disabled="solidifying" @click="solidify">{{ solidifying ? '固化中…' : '固化为普通 UI 用例' }}</button>
            <button v-if="result.status === 'failed'" class="btn sm" :disabled="aiLoading" @click="analyze">
              {{ aiLoading ? '分析中…' : 'AI 分析失败原因' }}</button>
          </div>
        </div>

        <div v-if="aiTips" style="border:1px solid var(--acc-weak);background:var(--acc-weak);border-radius:7px;padding:10px 12px;margin-bottom:10px">
          <div style="font-size:13px"><b>AI 分析</b> <span class="chip">{{ aiTips.engine === 'builtin' ? '内置规则' : aiTips.engine }}</span></div>
          <div style="font-size:12.5px;margin-top:5px">可能原因：{{ aiTips.cause }}</div>
          <div style="font-size:12.5px;margin-top:3px">{{ aiTips.suggestion }}</div>
        </div>

        <template v-if="steps">
          <div v-if="foldCount > 0" style="border:1px dashed var(--line);border-radius:7px;padding:7px 12px;margin-bottom:8px;display:flex;gap:8px;align-items:center">
            <span class="muted" style="font-size:12.5px">{{ folded ? '已折叠 ' + foldCount + ' 步过程明细' : '过程明细已全部展开（' + steps.length + ' 步）' }}</span>
            <button class="btn sm" @click="folded = !folded">{{ folded ? '展开全部' : '收起，只看结论' }}</button>
          </div>
          <div v-for="s in (folded ? tail.list : steps)" :key="s.idx" style="border:1px solid var(--line);border-radius:7px;padding:10px 12px;margin-bottom:8px">
            <div style="display:flex;gap:8px;align-items:center">
              <span :class="s.pass ? 'st ok' : 'st err'">{{ s.pass ? '通过' : '未通过' }}</span>
              <span class="mono" style="font-size:12.5px">{{ s.action ? '[' + s.action + '] ' + s.target : s.m + ' ' + s.url }}</span>
              <span v-if="s.engine" class="chip">{{ s.engine }}</span>
              <span v-if="s.saved" class="chip">已记住 {{ s.saved }}</span>
            </div>
            <div v-if="s.think" class="muted" style="font-size:12px;margin-top:5px;color:var(--acc)">🤖 {{ s.think }}</div>
            <div class="muted" style="font-size:12.5px;margin-top:6px">{{ s.pass ? '✓ ' : '✗ ' }}{{ s.reason }}（{{ s.ms }}ms）</div>
            <img v-if="s.screenshot" :src="s.screenshot" style="max-width:100%;border:1px solid var(--line);border-radius:6px;margin-top:8px">
            <video v-if="s.video" :src="s.video" controls style="max-width:100%;border:1px solid var(--line);border-radius:6px;margin-top:8px"></video>
            <div v-if="!s.pass && s.response" class="muted mono" style="font-size:11.5px;margin-top:6px;color:var(--err);word-break:break-all">{{ s.response?.slice(0, 300) }}</div>
          </div>
        </template>

        <table v-if="casesOfPlan">
          <thead><tr><th>条目</th><th>结果</th><th>明细</th></tr></thead>
          <tbody>
            <template v-for="(c, i) in casesOfPlan" :key="c.case_id || c.flow_id || i">
              <tr style="cursor:pointer" @click="toggleItem(i)">
                <td><span class="chip">{{ c.flow_id ? '流程' : '用例' }}</span> {{ c.name }}
                  <span class="faint" style="font-size:11px">{{ expandedItem === i ? '▴' : '▾' }}</span></td>
                <td><span :class="c.pass ? 'st ok' : 'st err'">{{ c.pass ? '通过' : '失败' }}</span></td>
                <td class="mono muted">{{ c.pass_n }}/{{ c.pass_n + c.fail_n }} 检查点</td>
              </tr>
              <tr v-if="expandedItem === i">
                <td colspan="3" style="background:#fafbfc;padding:6px 16px 10px">
                  <div v-for="(s, j) in (c.detail || [])" :key="j"
                    style="display:flex;gap:8px;align-items:baseline;font-size:12.5px;padding:2px 0">
                    <span :style="{ color: s.pass ? 'var(--ok)' : 'var(--err)' }">{{ s.pass ? '✓' : '✗' }}</span>
                    <span class="mono" style="word-break:break-all">{{ s.m || s.action || s.type }} {{ s.url || s.target || '' }}</span>
                    <span class="faint" style="font-size:12px">{{ s.reason }}</span>
                  </div>
                  <div v-if="!(c.detail || []).length" class="faint" style="font-size:12px">该条目没有步骤明细</div>
                </td>
              </tr>
            </template>
          </tbody>
        </table>
      </div>
    </div>
  </div>
</template>
