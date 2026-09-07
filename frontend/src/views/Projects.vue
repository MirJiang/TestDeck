<script setup>
import { ref, onMounted } from 'vue'
import { api } from '../api'
import { confirmDialog, toast } from '../dialog'

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

// ---- 环境（一个项目只有一个环境地址）与 测试用户（账号密码池） ----
const envOf = ref(null)                 // { pid, name }
const envForm = ref(null)               // { id, base_url, varsText }
const envErr = ref('')
const userOf = ref(null)                // { pid, name }
const users = ref([])
const userForm = ref({ name: '', username: '', password: '', remark: '' })
const userErr = ref('')
const importOpen = ref(false)
const importText = ref('')

async function openEnvs(p) {
  envErr.value = ''
  envOf.value = { pid: p.id, name: p.name }
  const list = await api(`/projects/${p.id}/envs`)
  const e = list[0] || null
  envForm.value = { id: e?.id || '', base_url: e?.base_url || '',
    varsText: Object.entries(e?.variables || {}).map(([k, v]) => `${k}=${v}`).join('\n') }
}
async function saveEnv() {
  envErr.value = ''
  const variables = {}
  envForm.value.varsText.split('\n').filter(l => l.includes('=')).forEach(l => {
    const [k, ...v] = l.split('='); variables[k.trim()] = v.join('=').trim()
  })
  const body = { name: 'default', base_url: envForm.value.base_url, variables }
  try {
    if (envForm.value.id) await api(`/projects/${envOf.value.pid}/envs/${envForm.value.id}`, { method: 'PUT', body })
    else await api(`/projects/${envOf.value.pid}/envs`, { method: 'POST', body })
    toast('环境地址已保存')
  } catch (e) { envErr.value = e.message }
}
async function openUsers(p) {
  userErr.value = ''
  userOf.value = { pid: p.id, name: p.name }
  users.value = await api(`/projects/${p.id}/users`)
  importOpen.value = false; importText.value = ''
}
async function reloadUsers() { users.value = await api(`/projects/${userOf.value.pid}/users`) }
async function addUser() {
  userErr.value = ''
  if (!userForm.value.username.trim()) { userErr.value = '账号不能为空'; return }
  try {
    await api(`/projects/${userOf.value.pid}/users`, { method: 'POST', body: userForm.value })
    userForm.value = { name: '', username: '', password: '', remark: '' }
    reloadUsers()
  } catch (e) { userErr.value = e.message }
}
async function delUser(u) {
  if (!await confirmDialog(`删除用户「${u.name || u.username}」？`, { danger: true, okText: '删除' })) return
  await api(`/projects/${userOf.value.pid}/users/${u.id}`, { method: 'DELETE' })
  reloadUsers()
}
async function doImport() {
  if (!importText.value.trim()) return
  const r = await api(`/projects/${userOf.value.pid}/users/import`, { method: 'POST', body: { text: importText.value } })
  toast(`导入完成：新增 ${r.added}、更新 ${r.updated}${r.skipped ? `、跳过 ${r.skipped} 行（缺账号或密码）` : ''}`)
  importText.value = ''
  reloadUsers()
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
      <thead><tr><th>名称</th><th>说明</th><th>创建于</th><th></th></tr></thead>
      <tbody>
        <tr v-for="p in list" :key="p.id">
          <td><b>{{ p.name }}</b></td><td class="muted">{{ p.desc }}</td>
          <td class="mono muted">{{ p.created_at.slice(0, 10) }}</td>
          <td style="text-align:right"><a @click="openEnvs(p)">环境</a> · <a @click="openUsers(p)">用户</a> · <a @click="openMembers(p)">成员</a> · <a @click="openEdit(p)">编辑</a> · <a style="color:var(--err)" @click="del(p)">删除</a></td>
        </tr>
        <tr v-if="!list.length"><td colspan="4" class="empty">暂无项目</td></tr>
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

  <!-- 环境：一个项目一个环境地址 -->
  <div class="mask" :class="{ on: !!envOf }" @click.self="envOf = null">
    <div class="modal" style="width:560px" v-if="envOf">
      <h3>环境 · {{ envOf.name }}</h3>
      <div class="fld"><label>环境地址（用例里只填路径，域名自动补上）</label>
        <input v-model="envForm.base_url" class="mono" placeholder="https://test-api.example.com"></div>
      <div class="fld"><label>附加变量（每行 key=value；账号密码建议用「用户」维护）</label>
        <textarea v-model="envForm.varsText" rows="4" class="mono" placeholder="shop_id=10086"></textarea></div>
      <div v-if="envErr" style="color:var(--err);font-size:12.5px;margin-bottom:8px">{{ envErr }}</div>
      <div class="ft"><button class="btn" @click="envOf = null">关闭</button>
        <button class="btn pri" @click="saveEnv">保存</button></div>
    </div>
  </div>

  <!-- 测试用户（账号密码池：用例/流程角色选择带出） -->
  <div class="mask" :class="{ on: !!userOf }" @click.self="userOf = null">
    <div class="modal" style="width:680px" v-if="userOf">
      <h3>测试用户 · {{ userOf.name }}</h3>
      <div class="faint" style="font-size:12.5px;margin-bottom:10px">
        创建用例与流程角色时从这里选择带出账号密码，免得每次录入。
      </div>
      <div class="bar" style="margin-bottom:8px">
        <span class="muted">共 {{ users.length }} 个</span>
        <button class="btn sm" @click="importOpen = !importOpen">{{ importOpen ? '收起导入' : '批量导入' }}</button>
      </div>
      <div v-if="importOpen" class="fld" style="border:1px dashed var(--line);border-radius:7px;padding:10px;margin-bottom:10px">
        <label>每行一个用户，逗号/Tab 分隔：两列=账号,密码；三列=名称,账号,密码。账号已存在则更新。</label>
        <textarea v-model="importText" rows="4" class="mono" placeholder="货主王五,wangwu,Wang@123&#10;liusi,Liu@123"></textarea>
        <button class="btn sm pri" style="margin-top:8px" @click="doImport">导入</button>
      </div>
      <table>
        <thead><tr><th>名称</th><th>账号</th><th>密码</th><th>备注</th><th></th></tr></thead>
        <tbody>
          <tr v-for="u in users" :key="u.id">
            <td>{{ u.name }}</td><td class="mono">{{ u.username }}</td>
            <td class="mono">{{ u.password ? '••••' : '' }}</td><td class="muted">{{ u.remark }}</td>
            <td style="text-align:right"><a style="color:var(--err)" @click="delUser(u)">删除</a></td>
          </tr>
          <tr v-if="!users.length"><td colspan="5" class="empty">暂无用户，添加或批量导入后即可在用例/流程里选择</td></tr>
        </tbody>
      </table>
      <div class="row" style="gap:8px;margin-top:10px">
        <input v-model="userForm.name" placeholder="名称（如 货主-王五）" style="width:150px">
        <input v-model="userForm.username" placeholder="账号" style="width:150px">
        <input v-model="userForm.password" placeholder="密码" type="password" style="width:150px">
        <input v-model="userForm.remark" placeholder="备注" style="flex:1">
        <button class="btn sm pri" @click="addUser">添加</button>
      </div>
      <div v-if="userErr" style="color:var(--err);font-size:12.5px;margin-top:6px">{{ userErr }}</div>
      <div class="ft"><button class="btn" @click="userOf = null">关闭</button></div>
    </div>
  </div>
</template>
