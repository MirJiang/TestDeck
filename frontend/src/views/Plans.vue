<script setup>
import { ref, computed, onMounted } from 'vue'
import { api } from '../api'
import RunDrawer from '../components/RunDrawer.vue'
import { confirmDialog } from '../dialog'

const projects = ref([])
const list = ref([])
const envsOf = ref({})
const casesOf = ref({})   // project_id -> 该项目用例
const flowsOf = ref({})   // project_id -> 该项目流程
const show = ref(false)
const form = ref(null)
const running = ref(null)
const err = ref('')
const filter = ref('all')

onMounted(async () => {
  projects.value = await api('/projects')
  for (const p of projects.value) {
    envsOf.value[p.id] = await api(`/projects/${p.id}/envs`)
    casesOf.value[p.id] = await api(`/projects/${p.id}/cases`)
    flowsOf.value[p.id] = await api(`/flows?project_id=${p.id}`)
  }
  await load()
})

async function load() { list.value = await api('/plans') }

const shown = computed(() =>
  filter.value === 'all' ? list.value : list.value.filter(p => p.trigger === filter.value))

function openNew() {
  const p = projects.value[0]
  form.value = { id: '', project_id: p?.id || '', name: '', case_ids: [], flow_ids: [],
    env_id: envsOf.value[p?.id]?.[0]?.id || '', trigger: 'manual', cron: '', enabled: true }
  show.value = true
}
function openEdit(pl) { form.value = { flow_ids: [], ...pl }; show.value = true }

function toggleIn(arr, id) {
  const i = arr.indexOf(id)
  i >= 0 ? arr.splice(i, 1) : arr.push(id)
}

function onProjectChange() {
  form.value.env_id = (envsOf.value[form.value.project_id] || [])[0]?.id || ''
}

async function save() {
  err.value = ''
  const body = { ...form.value }
  try {
    if (body.id) await api('/plans/' + body.id, { method: 'PUT', body })
    else await api('/plans', { method: 'POST', body })
    show.value = false; load()
  } catch (e) { err.value = e.message }
}

async function del(pl) {
  if (!await confirmDialog(`删除计划「${pl.name}」？`, { danger: true, okText: '删除' })) return
  await api('/plans/' + pl.id, { method: 'DELETE' }); load()
}

function run(pl) {
  const env = envsOf.value[pl.project_id]?.find(e => e.id === pl.env_id) || envsOf.value[pl.project_id]?.[0]
  running.value = { title: pl.name, planId: pl.id, envs: envsOf.value[pl.project_id] }
}
</script>

<template>
  <div class="hd"><div><h2>测试计划</h2></div>
    <button class="btn pri" @click="openNew" :disabled="!projects.length">新建计划</button></div>
  <div class="panel">
    <div class="bar">
      <select v-model="filter" style="width:auto">
        <option value="all">全部触发方式</option>
        <option value="manual">手动</option>
        <option value="cron">定时</option>
        <option value="git">Git 触发</option>
      </select>
      <span class="muted">{{ shown.length }} 个计划 · 定时任务由 trigger=cron 的计划生成</span>
    </div>
    <table>
      <thead><tr><th>计划</th><th>内容</th><th>环境</th><th>调度</th><th>状态</th><th></th></tr></thead>
      <tbody>
        <tr v-for="p in shown" :key="p.id">
          <td><b>{{ p.name }}</b></td>
          <td class="mono">{{ (p.case_ids || []).length }} 用例 · {{ (p.flow_ids || []).length }} 流程</td>
          <td>{{ envsOf[p.project_id]?.find(e => e.id === p.env_id)?.name || '—' }}</td>
          <td>{{ p.trigger === 'cron' && p.cron ? '定时 · ' + p.cron : p.trigger === 'git' ? 'Git 触发' : '手动' }}</td>
          <td><span :class="p.enabled ? 'st ok' : 'st off'">{{ p.enabled ? '启用' : '停用' }}</span></td>
          <td style="text-align:right"><a @click="run(p)">执行</a> · <a @click="openEdit(p)">编辑</a> ·
            <a style="color:var(--err)" @click="del(p)">删除</a></td>
        </tr>
        <tr v-if="!shown.length"><td colspan="6" class="empty">暂无计划</td></tr>
      </tbody>
    </table>
  </div>

  <div class="mask" :class="{ on: show }" @click.self="show = false">
    <div class="modal" style="width:600px">
      <h3>{{ form?.id ? '编辑计划' : '新建计划' }}</h3>
      <template v-if="form">
        <div class="fld"><label>计划名称</label><input v-model="form.name" placeholder="每日冒烟"></div>
        <div class="two">
          <div class="fld"><label>项目</label>
            <select v-model="form.project_id" @change="onProjectChange">
              <option v-if="!projects.length" value="" disabled>暂无项目，请先创建</option>
              <option v-for="p in projects" :key="p.id" :value="p.id">{{ p.name }}</option></select></div>
          <div class="fld"><label>环境</label>
            <select v-model="form.env_id"><option v-if="!(envsOf[form.project_id] || []).length" value="" disabled>暂无环境，可在项目页添加</option><option v-for="e in envsOf[form.project_id] || []" :key="e.id" :value="e.id">{{ e.name }}</option></select></div>
        </div>
        <div class="two">
          <div class="fld"><label>包含的用例（点选，{{ form.case_ids.length }} 条）</label>
            <div style="max-height:150px;overflow:auto;border:1px solid var(--line);border-radius:6px;padding:8px">
              <label v-for="c in casesOf[form.project_id] || []" :key="c.id"
                style="display:flex;gap:8px;align-items:center;padding:4px 6px;cursor:pointer">
                <input type="checkbox" style="width:auto" :checked="form.case_ids.includes(c.id)" @change="toggleIn(form.case_ids, c.id)">
                <span>{{ c.name }}</span>
              </label>
              <div v-if="!(casesOf[form.project_id] || []).length" class="faint" style="padding:6px">该项目暂无用例</div>
            </div>
          </div>
          <div class="fld"><label>包含的流程（点选，{{ form.flow_ids.length }} 条）</label>
            <div style="max-height:150px;overflow:auto;border:1px solid var(--line);border-radius:6px;padding:8px">
              <label v-for="f in flowsOf[form.project_id] || []" :key="f.id"
                style="display:flex;gap:8px;align-items:center;padding:4px 6px;cursor:pointer">
                <input type="checkbox" style="width:auto" :checked="form.flow_ids.includes(f.id)" @change="toggleIn(form.flow_ids, f.id)">
                <span>{{ f.name }}<span class="faint">（{{ f.roles }} 角色 · {{ f.steps }} 步）</span></span>
              </label>
              <div v-if="!(flowsOf[form.project_id] || []).length" class="faint" style="padding:6px">该项目暂无流程</div>
            </div>
          </div>
        </div>
        <div class="two">
          <div class="fld"><label>执行方式</label>
            <select v-model="form.trigger">
              <option value="manual">手动执行</option>
              <option value="cron">定时执行</option>
              <option value="git">Git 默认分支有提交时自动执行</option>
            </select></div>
          <div class="fld" v-if="form.trigger === 'cron'"><label>cron 表达式（分 时 日 月 周）</label>
            <input v-model="form.cron" class="mono" placeholder="0 8 * * *（每天 08:00）"></div>
        </div>
        <div v-if="err" style="color:var(--err);font-size:12.5px">{{ err }}</div>
      </template>
      <div class="ft"><button class="btn" @click="show = false">取消</button><button class="btn pri" @click="save">保存</button></div>
    </div>
  </div>

  <RunDrawer v-if="running" :title="running.title" :planId="running.planId" :envs="running.envs"
    @close="running = null" @done="load" />
</template>
