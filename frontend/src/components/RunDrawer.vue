<script setup>
import { ref, computed } from 'vue'
import { api } from '../api'
import { toast, alertDialog } from '../dialog'

const props = defineProps({ title: String, caseId: String, caseType: String, planId: String, envs: Array })
const emit = defineEmits(['close', 'done'])

const envId = ref(props.envs[0]?.id || '')
const running = ref(false)
const result = ref(null)
const err = ref('')
const aiTips = ref(null)
const aiLoading = ref(false)
const solidifying = ref(false)

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
      const steps = acts.filter(s => ['goto', 'click', 'fill', 'expect_text', 'screenshot'].includes(s.action))
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

async function run() {
  running.value = true; result.value = null; err.value = ''
  const path = props.caseId ? `/runs/cases/${props.caseId}/run` : `/runs/plans/${props.planId}/run`
  // 后端边执行边把明细写库；请求返回前先轮询最新一条 running 记录，步骤实时显示
  const filter = props.caseId ? `case=${props.caseId}` : `plan=${props.planId}`
  const poll = setInterval(async () => {
    try {
      const r = await api(`/runs?${filter}&size=1`)
      const cur = r.items?.[0]
      if (cur && cur.status === 'running') {
        curRunId = cur.id
        result.value = await api('/runs/' + cur.id)
      }
    } catch { /* 轮询失败忽略，等下一轮 */ }
  }, 1200)
  try {
    result.value = await api(path, { method: 'POST', body: { env_id: envId.value } }, { timeout: 600000 })
    emit('done')
  } catch (e) { err.value = e.message } finally {
    running.value = false
    curRunId = ''
    clearInterval(poll)
  }
}

let curRunId = ''
async function cancel() {
  if (!curRunId) return
  try { await api(`/runs/${curRunId}/cancel`, { method: 'POST' }); } catch { /* 已结束则忽略 */ }
}

// 结果展示：单用例 detail 是步骤数组，计划 detail 是用例数组
const steps = computed(() => result.value ? (result.value.case_id ? result.value.detail : null) : null)
const casesOfPlan = computed(() => result.value ? (result.value.plan_id ? result.value.detail : null) : null)

// 计划条目展开：点一行看该用例/流程内部的步骤明细
const expandedItem = ref(-1)
function toggleItem(i) { expandedItem.value = expandedItem.value === i ? -1 : i }
</script>

<template>
  <div class="drawer on">
    <div class="dh"><h3>执行 · {{ title }}</h3><button class="x" @click="emit('close')">✕</button></div>
    <div class="db">
      <div style="display:flex;gap:8px;align-items:center;margin-bottom:14px">
        <span class="muted">环境</span>
        <select v-model="envId"><option v-if="!envs.length" value="" disabled>暂无环境，可在项目页添加</option><option v-for="e in envs" :key="e.id" :value="e.id">{{ e.name }}</option></select>
        <button v-if="running" class="btn" style="margin-left:auto" @click="cancel">取消执行</button>
        <button v-else class="btn pri" style="margin-left:auto" :disabled="!envId" @click="run">
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
            <span class="mono muted">{{ result.duration }}s · 通过 {{ result.pass_n }} / 失败 {{ result.fail_n }}</span>
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
          <div v-for="s in steps" :key="s.idx" style="border:1px solid var(--line);border-radius:7px;padding:10px 12px;margin-bottom:8px">
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
