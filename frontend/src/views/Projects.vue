<script setup>
import { ref, onMounted } from 'vue'
import { api } from '../api'
import { confirmDialog } from '../dialog'

const list = ref([])
const show = ref(false)
const form = ref({ id: '', name: '', desc: '' })
const err = ref('')
const memberOf = ref(null)   // {pid, data}
const memberName = ref('')
const memberErr = ref('')

async function openMembers(p) {
  memberOf.value = { pid: p.id, name: p.name, data: await api(`/projects/${p.id}/members`) }
}
async function addMember() {
  memberErr.value = ''
  try {
    await api(`/projects/${memberOf.value.pid}/members`, { method: 'POST', body: { username: memberName.value.trim() } })
    memberName.value = ''
    memberOf.value.data = await api(`/projects/${memberOf.value.pid}/members`)
  } catch (e) { memberErr.value = e.message }
}
async function removeMember(uid) {
  await api(`/projects/${memberOf.value.pid}/members/${uid}`, { method: 'DELETE' })
  memberOf.value.data = await api(`/projects/${memberOf.value.pid}/members`)
}

async function load() { list.value = await api('/projects') }
onMounted(load)

// ---- 环境（入口合并进项目页，一个项目可配多个环境） ----
const envOf = ref(null)                 // { pid, name, list }
const envForm = ref(null)               // { id, name, base_url, varsText }
const envErr = ref('')

async function openEnvs(p) {
  envForm.value = null; envErr.value = ''
  envOf.value = { pid: p.id, name: p.name, list: await api(`/projects/${p.id}/envs`) }
}
async function reloadEnvs() {
  envOf.value.list = await api(`/projects/${envOf.value.pid}/envs`)
  load()   // 环境数列同步刷新
}
function openEnvNew() { envForm.value = { id: '', name: '', base_url: '', varsText: '' } }
function openEnvEdit(e) {
  envForm.value = { id: e.id, name: e.name, base_url: e.base_url,
    varsText: Object.entries(e.variables || {}).map(([k, v]) => `${k}=${v}`).join('\n') }
}
async function saveEnv() {
  envErr.value = ''
  const variables = {}
  envForm.value.varsText.split('\n').filter(l => l.includes('=')).forEach(l => {
    const [k, ...v] = l.split('='); variables[k.trim()] = v.join('=').trim()
  })
  const body = { name: envForm.value.name, base_url: envForm.value.base_url, variables }
  try {
    if (envForm.value.id) await api(`/projects/${envOf.value.pid}/envs/${envForm.value.id}`, { method: 'PUT', body })
    else await api(`/projects/${envOf.value.pid}/envs`, { method: 'POST', body })
    envForm.value = null; reloadEnvs()
  } catch (e) { envErr.value = e.message }
}
async function delEnv(e) {
  if (!await confirmDialog(`删除环境「${e.name}」？`, { danger: true, okText: '删除' })) return
  await api(`/projects/${envOf.value.pid}/envs/${e.id}`, { method: 'DELETE' })
  reloadEnvs()
}

function openNew() { form.value = { id: '', name: '', desc: '' }; show.value = true }
function openEdit(p) { form.value = { id: p.id, name: p.name, desc: p.desc }; show.value = true }

async function save() {
  err.value = ''
  try {
    if (form.value.id) await api('/projects/' + form.value.id, { method: 'PUT', body: { name: form.value.name, desc: form.value.desc } })
    else await api('/projects', { method: 'POST', body: { name: form.value.name, desc: form.value.desc } })
    show.value = false; load()
  } catch (e) { err.value = e.message }
}

async function del(p) {
  if (!await confirmDialog(`删除项目「${p.name}」及其环境配置？`, { danger: true, okText: '删除' })) return
  await api('/projects/' + p.id, { method: 'DELETE' }); load()
}
</script>

<template>
  <div class="hd"><div><h2>项目</h2></div>
    <button class="btn pri" @click="openNew">新建项目</button></div>
  <div class="panel">
    <table>
      <thead><tr><th>名称</th><th>说明</th><th>环境数</th><th>创建于</th><th></th></tr></thead>
      <tbody>
        <tr v-for="p in list" :key="p.id">
          <td><b>{{ p.name }}</b></td><td class="muted">{{ p.desc }}</td><td class="mono">{{ p.envs }}</td>
          <td class="mono muted">{{ p.created_at.slice(0, 10) }}</td>
          <td style="text-align:right"><a @click="openEnvs(p)">环境</a> · <a @click="openMembers(p)">成员</a> · <a @click="openEdit(p)">编辑</a> · <a style="color:var(--err)" @click="del(p)">删除</a></td>
        </tr>
        <tr v-if="!list.length"><td colspan="5" class="empty">暂无项目</td></tr>
      </tbody>
    </table>
  </div>

  <div class="mask" :class="{ on: show }" @click.self="show = false">
    <div class="modal">
      <h3>{{ form.id ? '编辑项目' : '新建项目' }}</h3>
      <div class="fld"><label>项目名称</label><input v-model="form.name" placeholder="商城中台"></div>
      <div class="fld"><label>说明</label><textarea v-model="form.desc" rows="3" placeholder="一句话描述项目范围"></textarea></div>
      <div v-if="err" style="color:var(--err);font-size:12.5px;margin-bottom:8px">{{ err }}</div>
      <div class="ft"><button class="btn" @click="show = false">取消</button><button class="btn pri" @click="save">保存</button></div>
    </div>
  </div>

  <div class="mask" :class="{ on: !!memberOf }" @click.self="memberOf = null">
    <div class="modal" v-if="memberOf">
      <h3>成员管理 · {{ memberOf.name }}</h3>
      <div class="fld"><label>创建人：{{ memberOf.data.owner }}</label></div>
      <table>
        <thead><tr><th>成员</th><th></th></tr></thead>
        <tbody>
          <tr v-for="m in memberOf.data.members" :key="m.user_id">
            <td>{{ m.username }}</td>
            <td style="text-align:right"><a style="color:var(--err)" @click="removeMember(m.user_id)">移出</a></td>
          </tr>
          <tr v-if="!memberOf.data.members.length"><td colspan="2" class="muted">暂无其他成员（创建人和管理员始终可见）</td></tr>
        </tbody>
      </table>
      <div class="fld" style="margin-top:12px"><label>按用户名添加成员</label>
        <div style="display:flex;gap:8px;align-items:center">
          <input v-model="memberName" placeholder="用户名" style="flex:1" @keyup.enter="addMember">
          <button class="btn pri" style="flex-shrink:0" @click="addMember">添加</button>
        </div></div>
      <div v-if="memberErr" style="color:var(--err);font-size:12.5px">{{ memberErr }}</div>
      <div class="ft"><button class="btn" @click="memberOf = null">关闭</button></div>
    </div>
  </div>

  <!-- 环境管理（原独立「环境」页合并至此） -->
  <div class="mask" :class="{ on: !!envOf }" @click.self="envOf = null">
    <div class="modal" style="width:640px" v-if="envOf">
      <h3>环境 · {{ envOf.name }}</h3>
      <template v-if="!envForm">
        <div class="bar" style="margin-bottom:8px">
          <span class="muted">用例里只填路径，域名与变量按环境自动带入</span>
          <button class="btn sm pri" @click="openEnvNew">新建环境</button>
        </div>
        <table>
          <thead><tr><th>名称</th><th>Base URL</th><th>变量</th><th></th></tr></thead>
          <tbody>
            <tr v-for="e in envOf.list" :key="e.id">
              <td><b>{{ e.name }}</b></td><td class="mono">{{ e.base_url }}</td>
              <td><span v-for="(v, k) in e.variables" :key="k" class="chip">{{ k }}</span></td>
              <td style="text-align:right"><a @click="openEnvEdit(e)">编辑</a> ·
                <a style="color:var(--err)" @click="delEnv(e)">删除</a></td>
            </tr>
            <tr v-if="!envOf.list.length"><td colspan="4" class="empty">暂无环境</td></tr>
          </tbody>
        </table>
        <div class="ft"><button class="btn" @click="envOf = null">关闭</button></div>
      </template>
      <template v-else>
        <div class="fld"><label>名称</label><input v-model="envForm.name" placeholder="测试环境"></div>
        <div class="fld"><label>Base URL（用例里只填路径，域名会自动补上）</label>
          <input v-model="envForm.base_url" class="mono" placeholder="https://test-api.example.com"></div>
        <div class="fld"><label>变量（每行 key=value，如 username=admin）</label>
          <textarea v-model="envForm.varsText" rows="5" class="mono" placeholder="username=admin&#10;password=******"></textarea></div>
        <div class="faint" style="font-size:12px;margin:-4px 0 10px">
          提示：变量会注入用例与 AI 提示词——包含密码等敏感值时，它们会发送给你配置的大模型厂商。
        </div>
        <div v-if="envErr" style="color:var(--err);font-size:12.5px;margin-bottom:8px">{{ envErr }}</div>
        <div class="ft">
          <button class="btn" @click="envForm = null">返回列表</button>
          <button class="btn pri" @click="saveEnv">保存</button>
        </div>
      </template>
    </div>
  </div>
</template>
