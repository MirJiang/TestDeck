<script setup>
import { ref, onMounted } from 'vue'
import { api, getUser } from '../api'
import { promptDialog, alertDialog, toast } from '../dialog'

const users = ref([])
const newUser = ref({ username: '', password: '', role: 'member' })
const err = ref('')
const me = getUser()

async function load() { users.value = await api('/auth/users') }
onMounted(load)

async function create() {
  err.value = ''
  try {
    await api('/auth/users', { method: 'POST', body: newUser.value })
    newUser.value = { username: '', password: '', role: 'member' }
    load()
  } catch (e) { err.value = e.message }
}

async function resetPw(u) {
  const np = await promptDialog(`为「${u.username}」设置新密码（至少 6 位）：`,
    { title: '重置密码', inputType: 'password', placeholder: '新密码' })
  if (!np) return
  try { await api(`/auth/users/${u.id}/password`, { method: 'PUT', body: { new_password: np } }); toast('密码已重置 ✓') }
  catch (e) { await alertDialog(e.message, '重置失败') }
}
</script>

<template>
  <div class="hd"><div><h2>用户管理</h2></div></div>
  <div class="panel">
    <table>
      <thead><tr><th>用户名</th><th>角色</th><th>创建于</th><th></th></tr></thead>
      <tbody>
        <tr v-for="u in users" :key="u.id">
          <td><b>{{ u.username }}</b>{{ u.username === me?.username ? '（我）' : '' }}</td>
          <td>{{ u.role === 'admin' ? '管理员' : '成员' }}</td>
          <td class="mono muted">{{ u.created_at.slice(0, 10) }}</td>
          <td style="text-align:right"><a @click="resetPw(u)">重置密码</a></td>
        </tr>
      </tbody>
    </table>
    <div class="row" style="margin-top:14px">
      <input v-model="newUser.username" placeholder="新用户名" style="width:150px">
      <input v-model="newUser.password" type="password" placeholder="初始密码（至少 6 位）" style="width:190px">
      <select v-model="newUser.role"><option value="member">成员</option><option value="admin">管理员</option></select>
      <button class="btn pri" @click="create">创建用户</button>
      <span v-if="err" style="color:var(--err);font-size:12.5px">{{ err }}</span>
    </div>
    <div class="faint" style="font-size:12px;margin-top:8px">
      流程测试里的「角色」（如货主、物流公司）用这里创建的账号登录被测系统，实现多用户同流程测试。
    </div>
  </div>
</template>
