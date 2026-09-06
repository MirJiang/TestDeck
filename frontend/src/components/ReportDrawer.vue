<script setup>
import { ref, onMounted } from 'vue'
import { api, getToken } from '../api'

const props = defineProps({ runId: String })
const emit = defineEmits(['close'])
const run = ref(null)

onMounted(async () => { run.value = await api('/runs/' + props.runId) })

const stepsOf = (d) => Array.isArray(d) && d.length && 'idx' in d[0]

const srcOf = (r) => r.plan_name
  ? (r.flow_name ? `${r.plan_name} · 流程 ${r.flow_name}` : `计划 · ${r.plan_name}`)
  : (r.flow_name ? `流程 · ${r.flow_name}` : `用例 · ${r.case_name}`)

async function exportHtml() {
  const resp = await fetch('/api/v1/runs/' + props.runId + '/export',
    { headers: { Authorization: 'Bearer ' + getToken() } })
  const blob = await resp.blob()
  const a = document.createElement('a')
  a.href = URL.createObjectURL(blob)
  a.download = `report-${props.runId}.html`
  a.click(); URL.revokeObjectURL(a.href)
}
</script>

<template>
  <div class="drawer on">
    <div class="dh"><h3>执行报告 · {{ runId }}</h3>
      <div style="display:flex;gap:8px;align-items:center">
        <button class="btn sm" v-if="run" @click="exportHtml">导出报告</button>
        <button class="x" @click="emit('close')">✕</button>
      </div></div>
    <div class="db">
      <div v-if="!run" class="muted">加载中…</div>
      <template v-if="run">
        <div style="display:flex;gap:26px;margin-bottom:14px;font-size:13px">
          <div><div class="muted" style="font-size:11.5px">来源</div><b>{{ srcOf(run) }}</b></div>
          <div><div class="muted" style="font-size:11.5px">环境</div><b>{{ run.env_name }}</b></div>
          <div><div class="muted" style="font-size:11.5px">触发</div><b>{{ run.trigger_by }}</b></div>
          <div><div class="muted" style="font-size:11.5px">总耗时</div><b class="mono">{{ run.duration }}s</b></div>
        </div>
        <div style="margin-bottom:14px">
          <span :class="run.status === 'passed' ? 'st ok' : 'st err'">{{ run.status === 'passed' ? '全部通过' : '存在失败' }}</span>
          <span class="mono muted" style="margin-left:10px">通过 {{ run.pass_n }} · 失败 {{ run.fail_n }}</span>
        </div>

        <template v-if="stepsOf(run.detail)">
          <div v-for="s in run.detail" :key="s.idx" style="border:1px solid var(--line);border-radius:7px;padding:10px 12px;margin-bottom:8px">
            <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
              <span :class="s.pass ? 'st ok' : 'st err'">{{ s.pass ? '通过' : '未通过' }}</span>
              <span v-if="s.role_name" class="chip">{{ s.role_name }}</span>
              <span class="mono" style="font-size:12.5px">{{ s.m || s.action || s.type }} {{ s.url || s.target }}</span>
              <span v-if="s.saved" class="chip">已记住 {{ s.saved }}</span>
            </div>
            <div class="muted" style="font-size:12.5px;margin-top:6px">{{ s.pass ? '✓' : '✗' }} {{ s.reason }}</div>
            <div v-if="!s.pass && s.response" class="mono" style="font-size:11.5px;margin-top:6px;color:var(--err);word-break:break-all">{{ s.response?.slice(0, 400) }}</div>
            <div v-if="s.actions?.length" style="margin-top:8px;border-left:2px solid var(--line);padding-left:10px">
              <div v-for="(a, j) in s.actions" :key="j" style="display:flex;gap:7px;align-items:baseline;font-size:12px;padding:2px 0">
                <span :style="{ color: a.pass ? 'var(--ok)' : 'var(--err)' }">{{ a.pass ? '✓' : '✗' }}</span>
                <span class="mono" style="word-break:break-all">{{ a.m || a.action || a.type }} {{ a.url || a.target || '' }}</span>
                <span class="faint" style="font-size:11.5px">{{ a.reason }}</span>
              </div>
            </div>
            <img v-if="s.screenshot" :src="s.screenshot" loading="lazy" style="max-width:100%;border:1px solid var(--line);border-radius:6px;margin-top:8px">
            <div v-if="s.diff" style="margin-top:8px">
              <div class="muted" style="font-size:12px;margin-bottom:4px">基线（左）与本次（右）对比：</div>
              <img :src="s.diff" loading="lazy" style="max-width:100%;border:1px solid var(--err);border-radius:6px">
            </div>
          </div>
        </template>

        <table v-else>
          <thead><tr><th>条目</th><th>结果</th><th>检查点</th><th v-if="false"></th></tr></thead>
          <tbody>
            <template v-for="(c, i) in run.detail" :key="c.case_id || c.flow_id || i">
              <tr>
                <td><span class="chip">{{ c.flow_id ? '流程' : '用例' }}</span> {{ c.name }}</td>
                <td><span :class="c.pass ? 'st ok' : 'st err'">{{ c.pass ? '通过' : '失败' }}</span></td>
                <td class="mono muted">{{ c.pass_n }}/{{ c.pass_n + c.fail_n }}</td>
              </tr>
            </template>
          </tbody>
        </table>
      </template>
    </div>
  </div>
</template>
