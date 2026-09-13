<script setup>
import { ref, onUnmounted } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { api, getUser, clearAuth } from './api'
import { toast } from './dialog'
import DialogHost from './components/DialogHost.vue'

const router = useRouter()
const route = useRoute()
const user = getUser()

// 执行中数量角标：任何页面都能一眼看到有没有测试在跑
const execN = ref(0)
let execPoll = null
if (user) {
  const pollExec = async () => {
    try { execN.value = (await api('/runs?status=running&size=1')).total || 0 } catch { /* 离线忽略 */ }
  }
  pollExec()
  execPoll = setInterval(pollExec, 5000)
}
onUnmounted(() => { if (execPoll) clearInterval(execPoll) })

const nav = [
  { cap: '概览' },
  { path: '/', label: '工作台' },
  { path: '/runs', label: '执行记录' },
  { cap: '测试资产' },
  { path: '/projects', label: '项目' },
  { path: '/appmap', label: '应用地图' },
  { path: '/cases', label: '用例' },
  { path: '/flows', label: '流程测试' },
  { path: '/plans', label: '测试计划' },
  { cap: '系统' },
  { path: '/settings', label: '系统设置' },
  { path: '/users', label: '用户管理' },
]

function logout() { clearAuth(); router.push('/login') }

// ---- 右上角用户菜单 ----
const showUser = ref(false)
const showPw = ref(false)
const pw = ref({ old_password: '', new_password: '' })
const pwErr = ref('')

async function changePw() {
  pwErr.value = ''
  if ((pw.value.new_password || '').length < 6) { pwErr.value = '新密码至少 6 位'; return }
  try {
    await api('/auth/password', { method: 'PUT', body: pw.value })
    showPw.value = false
    pw.value = { old_password: '', new_password: '' }
    toast('密码已修改 ✓')
  } catch (e) { pwErr.value = e.message }
}
</script>

<template>
  <div v-if="route.path === '/login'"><router-view /></div>
  <div v-else class="layout">
    <aside class="side">
      <div class="brand">Test<em>Deck</em></div>
      <nav class="nav">
        <template v-for="item in nav" :key="item.label || item.cap">
          <div v-if="item.cap" class="cap">{{ item.cap }}</div>
          <router-link v-else class="it" :to="item.path" :class="{ on: route.path === item.path }">{{ item.label }}<span
            v-if="item.path === '/runs' && execN" class="n runn">{{ execN }}</span></router-link>
        </template>
      </nav>
    </aside>
    <div class="main">
      <div class="top">
        <div class="path">{{ nav.find(n => n.path === route.path)?.label || '' }}</div>
        <div style="display:flex;gap:10px;align-items:center">
          <div style="position:relative">
            <button class="avatar-btn" :title="user?.username" @click="showUser = !showUser">
              {{ (user?.username || '?').slice(0, 2).toUpperCase() }}
            </button>
            <div v-if="showUser" style="position:fixed;inset:0;z-index:85" @click="showUser = false"></div>
            <div v-if="showUser" class="user-menu">
              <div class="um-head">
                <b>{{ user?.username }}</b>
                <div class="faint" style="font-size:12px">{{ user?.role === 'admin' ? '管理员' : '成员' }}</div>
              </div>
              <button class="um-it" @click="showPw = true; showUser = false">修改密码</button>
              <button class="um-it" style="color:var(--err)" @click="logout">退出登录</button>
            </div>
          </div>
        </div>
      </div>
      <div class="body"><router-view /></div>
    </div>
  </div>

  <!-- 修改密码 -->
  <div class="mask on" v-if="showPw" @click.self="showPw = false">
    <div class="modal" style="width:400px">
      <h3>修改密码</h3>
      <div class="fld"><label>原密码</label>
        <input v-model="pw.old_password" type="password" autocomplete="current-password"></div>
      <div class="fld"><label>新密码（至少 6 位）</label>
        <input v-model="pw.new_password" type="password" autocomplete="new-password"
          @keyup.enter="changePw"></div>
      <div v-if="pwErr" style="color:var(--err);font-size:12.5px;margin-bottom:8px">{{ pwErr }}</div>
      <div class="ft">
        <button class="btn" @click="showPw = false">取消</button>
        <button class="btn pri" @click="changePw">修改</button>
      </div>
    </div>
  </div>
  <DialogHost />
</template>
