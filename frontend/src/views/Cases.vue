<script setup>
import { ref, onMounted } from 'vue'
import { api } from '../api'
import CaseEditor from '../components/CaseEditor.vue'
import RunDrawer from '../components/RunDrawer.vue'
import AiDrawer from '../components/AiDrawer.vue'
import { confirmDialog, alertDialog, toast } from '../dialog'

const projects = ref([])
const pid = ref('')
const list = ref([])
const q = ref('')
const editing = ref(null)
const running = ref(null)
const envs = ref([])
const err = ref('')
const aiOpen = ref(false)
const nlShow = ref(false)
const nlPrompt = ref('')
const nlDraft = ref(null)
const nlLoading = ref(false)
const nlErr = ref('')

async function nlGen() {
  if (!nlPrompt.value.trim()) return
  nlLoading.value = true; nlErr.value = ''; nlDraft.value = null
  try { nlDraft.value = (await api('/ai/gen-from-text',
    { method: 'POST', body: { project_id: pid.value, prompt: nlPrompt.value } })).draft }
  catch (e) { nlErr.value = e.message } finally { nlLoading.value = false }
}

async function nlSave() {
  await api(`/projects/${pid.value}/cases`, { method: 'POST',
    body: { project_id: pid.value, name: nlDraft.value.name, steps: nlDraft.value.steps, source: 'ai' } })
  nlShow.value = false; nlPrompt.value = ''; nlDraft.value = null; load()
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
    goal: '', start_url: '', engine: '', max_steps: 20, fixedSteps: [] }
}
async function openEdit(id) {
  const c = await api('/cases/' + id)
  if (c.type !== 'ai') { await alertDialog('历史 API/UI 用例不再支持编辑，可删除后重建为 AI 用例', '提示'); return }
  const cfg = c.steps?.[0] || {}
  editing.value = { id: c.id, project_id: c.project_id, name: c.name, type: 'ai',
    target: cfg.target || 'ui', goal: cfg.goal || '', start_url: cfg.start_url || '',
    engine: cfg.engine || '', max_steps: cfg.max_steps || 20,
    endpoints: cfg.endpoints || [], fixedSteps: cfg.fixed_steps || []
  }
}

async function save(c) {
  err.value = ''
  const body = { project_id: c.project_id, name: c.name, type: 'ai', target: c.target || 'ui',
    goal: c.goal || '', start_url: c.start_url || '', engine: c.engine || '',
    max_steps: c.max_steps || 20, endpoints: c.endpoints || [],
    fixed_steps: c.fixedSteps || [], source: c.source }
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
      <button class="btn" @click="nlShow = true" :disabled="!pid">AI · 说句话生成</button>
      <button class="btn" @click="aiOpen = true">AI · 从提交生成</button>
      <button class="btn pri" @click="openNew" :disabled="!pid">新建用例</button>
    </div></div>
  <div class="panel">
    <div class="bar">
      <div style="display:flex;gap:8px">
        <select v-model="pid" @change="load"><option v-for="p in projects" :key="p.id" :value="p.id">{{ p.name }}</option></select>
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
  <AiDrawer v-if="aiOpen" :projects="projects" @close="aiOpen = false" @done="load" @saved="load" />

  <div class="mask" :class="{ on: nlShow }" @click.self="nlShow = false">
    <div class="modal">
      <h3>AI · 说句话生成用例</h3>
      <div class="fld"><label>想测什么？用大白话描述</label>
        <textarea v-model="nlPrompt" rows="3" placeholder="例如：测一下登录接口 /api/login，账号密码正确时应该返回 code=0"></textarea></div>
      <button class="btn pri" :disabled="nlLoading" @click="nlGen">{{ nlLoading ? '生成中…' : '生成草稿' }}</button>
      <div v-if="nlErr" style="color:var(--err);margin-top:10px">{{ nlErr }}</div>
      <div v-if="nlDraft" class="panel" style="margin-top:14px;margin-bottom:0;background:#fcfcfd">
        <div style="font-size:13.5px"><b>{{ nlDraft.name }}</b></div>
        <div class="muted" style="font-size:12.5px;margin:6px 0">{{ nlDraft.reason }}</div>
        <div v-for="(s, i) in nlDraft.steps" :key="i" class="mono muted" style="font-size:12px">
          {{ i + 1 }}. {{ s.m }} {{ s.url }} · 检查：{{ s.check?.type }} {{ s.check?.expect || s.check?.field || '' }}</div>
        <div style="margin-top:10px;display:flex;gap:8px">
          <button class="btn sm pri" @click="nlSave">保存为用例</button>
          <button class="btn sm" @click="nlGen">重新生成</button>
        </div>
      </div>
    </div>
  </div>
</template>
