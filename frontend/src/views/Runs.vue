<script setup>
import { ref, onMounted } from 'vue'
import { api, getToken } from '../api'
import ReportDrawer from '../components/ReportDrawer.vue'

const items = ref([])
const total = ref(0)
const page = ref(1)
const report = ref(null)

async function load() {
  const r = await api(`/runs?page=${page.value}&size=20`)
  items.value = r.items; total.value = r.total
}
onMounted(load)

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
  <div class="panel">
    <table>
      <thead><tr><th>执行 ID</th><th>来源</th><th>环境</th><th>结果</th><th>耗时</th><th>触发</th><th>时间</th><th></th></tr></thead>
      <tbody>
        <tr v-for="r in items" :key="r.id">
          <td class="mono">{{ r.id }}</td>
          <td>{{ srcOf(r) }}</td>
          <td>{{ r.env_name }}</td>
          <td><span class="pass"><span class="track"><i :class="{ bad: r.fail_n }" :style="{ width: pct(r) + '%' }"></i></span>
            <span class="pct">{{ r.pass_n }}/{{ r.pass_n + r.fail_n }} · {{ pct(r) }}%</span></span></td>
          <td class="mono">{{ r.duration }}s</td>
          <td class="muted">{{ r.trigger_by }}</td>
          <td class="mono muted">{{ r.created_at.replace('T', ' ').slice(0, 16) }}</td>
          <td><a @click="report = r">报告</a></td>
        </tr>
        <tr v-if="!items.length"><td colspan="8" class="empty">暂无执行记录</td></tr>
      </tbody>
    </table>
    <div v-if="pages() > 1" class="bar" style="margin-top:12px">
      <span class="muted">第 {{ page }} / {{ pages() }} 页</span>
      <div style="display:flex;gap:8px">
        <button class="btn sm" :disabled="page <= 1" @click="page--; load()">上一页</button>
        <button class="btn sm" :disabled="page >= pages()" @click="page++; load()">下一页</button>
      </div>
    </div>
  </div>
  <ReportDrawer v-if="report" :runId="report.id" @close="report = null" />
</template>
