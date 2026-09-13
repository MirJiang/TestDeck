<script setup>
import { ref, computed, onMounted } from 'vue'
import { api, getToken } from '../api'
import ReportDrawer from '../components/ReportDrawer.vue'

const items = ref([])
const total = ref(0)
const page = ref(1)
const report = ref(null)

const projects = ref([])
const fProject = ref('')
const cases = ref([])
const fCase = ref(''); const fStatus = ref(''); const fStart = ref(''); const fEnd = ref('')
const caseKw = ref(''); const caseSel = ref(null); const caseOpen = ref(false)

const caseCandidates = computed(() => {
  const kws = caseKw.value.trim().toLowerCase().split(/\s+/).filter(Boolean)
  return cases.value.filter(c => {
    if (fProject.value && c.project_id !== fProject.value) return false
    const hay = (c.name + ' ' + c.project_name).toLowerCase()
    return kws.every(k => hay.includes(k))
  }).slice(0, 50)
})
function pickCase(c) { caseSel.value = c; caseKw.value = c.name; fCase.value = c.id; caseOpen.value = false }
function clearCase() { caseSel.value = null; caseKw.value = ''; fCase.value = ''; caseOpen.value = false }
function pickProject() {   // 切项目后清掉不属于它的已选用例
  if (caseSel.value && caseSel.value.project_id !== fProject.value) clearCase()
}

async function load() {
  const p = new URLSearchParams({ page: page.value, size: 20 })
  if (fProject.value) p.set('project', fProject.value)
  if (fCase.value) p.set('case', fCase.value)
  else if (caseKw.value.trim()) p.set('case_kw', caseKw.value.trim())   // 未选具体用例时按名称模糊搜
  if (fStatus.value) p.set('status', fStatus.value)
  if (fStart.value) p.set('start', fStart.value)
  if (fEnd.value) p.set('end', fEnd.value)
  const r = await api('/runs?' + p)
  items.value = r.items; total.value = r.total
}
function query() { page.value = 1; load() }
function reset() {
  fProject.value = ''; clearCase(); fStatus.value = ''; fStart.value = ''; fEnd.value = ''
  page.value = 1; load()
}
onMounted(async () => {
  projects.value = await api('/projects').catch(() => [])
  cases.value = await api('/cases/brief').catch(() => [])
  await load()
})

function pct(r) { const t = r.pass_n + r.fail_n; return t ? Math.round(r.pass_n / t * 100) : 0 }
const pages = () => Math.max(1, Math.ceil(total.value / 20))

const srcOf = (r) => r.plan_id
  ? (r.flow_id ? `${r.plan_name} · 流程 ${r.flow_name}` : `计划 · ${r.plan_name}`)
  : (r.flow_id ? `流程 · ${r.flow_name}` : `用例 · ${r.case_name}`)

async function exportCsv() {
  const resp = await fetch('/api/v1/runs/export.csv', { headers: { Authorization: 'Bearer ' + getToken() } })
  const blob = await resp.blob()
  const a = document.createElement('a')
  a.href = URL.createObjectURL(blob); a.download = 'runs.csv'; a.click(); URL.revokeObjectURL(a.href)
}
</script>

<template>
  <div class="hd"><div><h2>执行记录</h2><div class="sub">共 {{ total }} 次</div></div>
    <button class="btn" @click="exportCsv">导出 CSV</button></div>
  <div class="panel" style="padding:10px 14px">
    <div style="display:flex;gap:12px;flex-wrap:wrap;align-items:center">
      <label style="display:flex;gap:6px;align-items:center">项目
        <select v-model="fProject" @change="pickProject" style="max-width:180px">
          <option value="">全部项目</option>
          <option v-for="p in projects" :key="p.id" :value="p.id">{{ p.name }}</option>
        </select></label>
      <label style="display:flex;gap:6px;align-items:center">用例
        <div style="position:relative">
          <input v-model="caseKw" @focus="caseOpen = true" @input="caseOpen = true" @blur="caseOpen = false"
                 placeholder="全部用例（可输入模糊过滤）" style="width:210px">
          <div v-if="caseOpen && caseCandidates.length"
               style="position:absolute;top:100%;left:0;z-index:30;min-width:100%;max-height:240px;overflow:auto;background:var(--panel);border:1px solid var(--line);border-radius:6px;box-shadow:0 4px 14px rgba(0,0,0,.14)">
            <div style="padding:6px 10px;color:var(--sub);cursor:pointer" @mousedown.prevent="clearCase">全部用例</div>
            <div v-for="c in caseCandidates" :key="c.id"
                 style="padding:6px 10px;cursor:pointer;white-space:nowrap;overflow:hidden;text-overflow:ellipsis"
                 @mousedown.prevent="pickCase(c)">{{ c.name }}（{{ c.project_name }}）</div>
          </div>
        </div></label>
      <label style="display:flex;gap:6px;align-items:center">结果
        <select v-model="fStatus">
          <option value="">全部</option>
          <option value="passed">成功</option>
          <option value="failed">失败</option>
          <option value="running">执行中</option>
        </select></label>
      <label style="display:flex;gap:6px;align-items:center">时间
        <input type="date" v-model="fStart"> ~ <input type="date" v-model="fEnd"></label>
      <button class="btn pri" @click="query">查询</button>
      <button class="btn" @click="reset">重置</button>
    </div>
  </div>
  <div class="panel">
    <table>
      <thead><tr><th>执行 ID</th><th>来源</th><th>结果</th><th>耗时</th><th>Token</th><th>触发</th><th>时间</th><th></th></tr></thead>
      <tbody>
        <tr v-for="r in items" :key="r.id">
          <td class="mono">{{ r.id }}</td>
          <td>{{ srcOf(r) }}</td>
          <td><span class="st" :class="r.status === 'passed' ? 'ok' : (r.status === 'running' ? 'run' : 'err')">{{ r.status === 'passed' ? '成功' : (r.status === 'running' ? '执行中' : '失败') }}</span>
            <span class="pass"><span class="track"><i :class="{ bad: r.fail_n }" :style="{ width: pct(r) + '%' }"></i></span>
            <span class="pct">{{ r.pass_n }}/{{ r.pass_n + r.fail_n }} · {{ pct(r) }}%</span></span></td>
          <td class="mono">{{ r.duration }}s</td>
          <td class="mono muted">{{ (r.tokens || 0).toLocaleString() }}</td>
          <td class="muted">{{ r.trigger_by }}</td>
          <td class="mono muted">{{ r.created_at.replace('T', ' ').slice(0, 16) }}</td>
          <td><a @click="report = r">报告</a></td>
        </tr>
        <tr v-if="!items.length"><td colspan="8" class="empty">暂无执行记录</td></tr>
      </tbody>
    </table>
    <div class="bar" style="margin-top:12px;justify-content:flex-end">
      <span class="muted">共 {{ total }} 次 · 第 {{ page }} / {{ pages() }} 页</span>
      <button class="btn sm" :disabled="page <= 1" @click="page--; load()">上一页</button>
      <button class="btn sm" :disabled="page >= pages()" @click="page++; load()">下一页</button>
    </div>
  </div>
  <ReportDrawer v-if="report" :runId="report.id" @close="report = null" />
</template>
