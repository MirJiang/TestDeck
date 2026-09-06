<script setup>
import { ref, onMounted } from 'vue'
import { api } from '../api'
import ReportDrawer from '../components/ReportDrawer.vue'

const stats = ref({ runs: 0, today: 0, passRate: '-', fail: 0 })
const recent = ref([])
const report = ref(null)
const advice = ref(null)

onMounted(async () => {
  try { advice.value = await api('/ai/regression-advice') } catch { }
  const r = await api('/runs?size=8')
  recent.value = r.items
  const total = r.total, passed = r.items.filter(x => x.status === 'passed').length
  stats.value = {
    runs: total,
    today: r.items.filter(x => x.created_at.startsWith(new Date().toISOString().slice(0, 10))).length,
    passRate: r.items.length ? Math.round(passed / r.items.length * 100) + '%' : '—',
    fail: r.items.filter(x => x.status === 'failed').length,
  }
})

function pct(r) { const t = r.pass_n + r.fail_n; return t ? Math.round(r.pass_n / t * 100) : 0 }
</script>

<template>
  <div class="hd"><div><h2>工作台</h2></div></div>

  <div v-if="advice && (advice.stale_cases.length || advice.stale_flows?.length || advice.recent_commits.length)" class="panel" style="border-color:var(--acc-weak);background:#fbfcfe">
    <div class="bar"><h3>回归建议</h3><span class="muted" style="font-size:12.5px">{{ advice.advice }}</span></div>
    <div v-if="advice.stale_cases.length" style="margin-bottom:8px">
      <div class="muted" style="font-size:12px;margin-bottom:4px">超过 14 天未执行的用例</div>
      <a v-for="s in advice.stale_cases.slice(0, 5)" :key="s.case_id" class="chip" @click="$router.push('/cases')">{{ s.case_name }}（{{ s.last_run.slice(0, 10) }}）</a>
    </div>
    <div v-if="advice.stale_flows?.length" style="margin-bottom:8px">
      <div class="muted" style="font-size:12px;margin-bottom:4px">超过 14 天未执行的流程</div>
      <a v-for="s in advice.stale_flows.slice(0, 5)" :key="s.flow_id" class="chip" @click="$router.push('/flows')">{{ s.flow_name }}（{{ s.last_run.slice(0, 10) }}）</a>
    </div>
    <div v-if="advice.recent_commits.length">
      <div class="muted" style="font-size:12px;margin-bottom:4px">近 7 天的新提交（可到用例页让 AI 生成测试）</div>
      <div v-for="cm in advice.recent_commits.slice(0, 3)" :key="cm.sha" class="mono muted" style="font-size:12px">
        {{ cm.sha }} {{ cm.message }} · {{ cm.author }}</div>
    </div>
  </div>
  <div class="grid4">
    <div><div class="k">累计执行</div><div class="v">{{ stats.runs }}</div></div>
    <div><div class="k">今日执行</div><div class="v">{{ stats.today }}</div></div>
    <div><div class="k">近期通过率</div><div class="v">{{ stats.passRate }}</div></div>
    <div><div class="k">近期失败</div><div class="v" :style="stats.fail ? 'color:var(--err)' : ''">{{ stats.fail }}</div></div>
  </div>
  <div class="panel">
    <div class="bar"><h3>最近执行</h3><router-link to="/runs">全部记录 →</router-link></div>
    <table>
      <thead><tr><th>执行 ID</th><th>来源</th><th>环境</th><th>结果</th><th>耗时</th><th>触发</th><th>时间</th><th></th></tr></thead>
      <tbody>
        <tr v-for="r in recent" :key="r.id">
          <td class="mono">{{ r.id }}</td>
          <td>{{ r.plan_name ? '计划 · ' + r.plan_name : '用例 · ' + r.case_name }}</td>
          <td>{{ r.env_name }}</td>
          <td><span class="pass"><span class="track"><i :class="{ bad: r.fail_n }" :style="{ width: pct(r) + '%' }"></i></span>
              <span class="pct">{{ r.pass_n }}/{{ r.pass_n + r.fail_n }}</span></span></td>
          <td class="mono">{{ r.duration }}s</td>
          <td class="muted">{{ r.trigger_by }}</td>
          <td class="mono muted">{{ r.created_at.replace('T', ' ').slice(0, 16) }}</td>
          <td><a @click="report = r">报告</a></td>
        </tr>
        <tr v-if="!recent.length"><td colspan="8" class="empty">还没有执行记录，去「用例」页创建第一条测试吧</td></tr>
      </tbody>
    </table>
  </div>
  <ReportDrawer v-if="report" :runId="report.id" @close="report = null" />
</template>
