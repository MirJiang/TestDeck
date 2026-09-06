<script setup>
import { ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { api, setAuth } from '../api'

const router = useRouter()
const username = ref('admin')
const password = ref('')
const err = ref('')
const loading = ref(false)
const showPw = ref(false)
const pwInput = ref(null)

onMounted(() => pwInput.value?.focus())   // 用户名已预填，直接聚焦密码框

async function login() {
  if (loading.value) return
  loading.value = true; err.value = ''
  try {
    const r = await api('/auth/login', { method: 'POST', body: { username: username.value, password: password.value } })
    setAuth(r.token, r.user)
    router.push('/')
  } catch (e) { err.value = e.message } finally { loading.value = false }
}

const features = [
  { t: 'AI 智能用例', d: '一句大白话目标，模型自主设计请求、操作页面' },
  { t: '多角色流程测试', d: '按业务线串起各角色，变量与业务单据自动传递' },
  { t: '计划化回归', d: '手动 / 定时 / Git 提交触发，用例与流程一起跑' },
  { t: '失败即时告警', d: '钉钉 / 企微群机器人推送失败详情与报告' },
]
</script>

<template>
  <div class="lg">
    <!-- 左：品牌区 -->
    <div class="lg-brand">
      <div class="lg-top">
        <span class="lg-mark">TD</span>
        <span class="lg-logo">Test<em>Deck</em></span>
      </div>

      <div class="lg-hero">
        <h1>让每一次发布<br>都有测试兜底</h1>
        <p>填表式创建自动化测试 · AI 驱动 · 不用写代码</p>
        <div class="lg-feats">
          <div class="lg-feat" v-for="f in features" :key="f.t">
            <div class="t"><span class="ck">✓</span>{{ f.t }}</div>
            <div class="d">{{ f.d }}</div>
          </div>
        </div>
      </div>
    </div>

    <!-- 右：登录表单 -->
    <div class="lg-wrap">
      <div class="lg-mini">
        <span class="lg-mark">TD</span>
        <span class="lg-logo">Test<em>Deck</em></span>
      </div>
      <form class="lg-form" @submit.prevent="login">
        <h2>欢迎回来</h2>
        <p class="sub">登录 TestDeck，继续你的测试</p>

        <div class="lg-f">
          <label for="lg-user">用户名</label>
          <input id="lg-user" v-model="username" autocomplete="username" placeholder="请输入用户名">
        </div>
        <div class="lg-f">
          <label for="lg-pw">密码</label>
          <div class="lg-pw">
            <input id="lg-pw" ref="pwInput" v-model="password" :type="showPw ? 'text' : 'password'"
              autocomplete="current-password" placeholder="请输入密码">
            <button type="button" class="lg-eye" :title="showPw ? '隐藏密码' : '显示密码'" @click="showPw = !showPw">
              <svg v-if="!showPw" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <path d="M1 12s4-7 11-7 11 7 11 7-4 7-11 7S1 12 1 12z"/><circle cx="12" cy="12" r="3"/>
              </svg>
              <svg v-else viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19M14.12 14.12a3 3 0 1 1-4.24-4.24"/>
                <line x1="1" y1="1" x2="23" y2="23"/>
              </svg>
            </button>
          </div>
        </div>

        <div v-if="err" class="lg-err">
          <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" style="flex-shrink:0;margin-top:1px">
            <circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/>
          </svg>
          {{ err }}
        </div>

        <button type="submit" class="lg-btn" :disabled="loading">
          <span v-if="loading" class="spin"></span>
          {{ loading ? '登录中…' : '登 录' }}
        </button>

        <p class="lg-hint">初始账号 admin · 初始密码 admin123</p>
      </form>
    </div>
  </div>
</template>

<style scoped>
.lg{display:flex;min-height:100vh}

/* ---- 左侧品牌区 ---- */
.lg-brand{position:relative;flex:0 0 52%;min-width:460px;background:var(--ink);color:#e8eaee;
  display:flex;flex-direction:column;padding:40px 52px;overflow:hidden}
.lg-brand::before{content:"";position:absolute;inset:0;
  background-image:radial-gradient(rgba(255,255,255,.05) 1px,transparent 1px);background-size:24px 24px;pointer-events:none}
.lg-brand::after{content:"";position:absolute;top:-180px;right:-140px;width:520px;height:520px;
  background:radial-gradient(closest-side,rgba(47,111,237,.26),transparent 70%);pointer-events:none}
.lg-top{position:relative;display:flex;align-items:center;gap:11px}
.lg-mark{width:32px;height:32px;border-radius:8px;background:linear-gradient(135deg,#2f6fed,#5b8def);
  color:#fff;font-family:var(--mono);font-weight:700;font-size:13px;
  display:flex;align-items:center;justify-content:center;letter-spacing:.02em}
.lg-logo{font-size:17px;font-weight:600;color:#fff;letter-spacing:.02em}
.lg-logo em{font-style:normal;color:#5b8def;font-family:var(--mono)}

.lg-hero{position:relative;margin:auto 0;max-width:470px;padding:56px 0}
.lg-hero h1{font-size:30px;line-height:1.4;font-weight:650;color:#f2f4f7;letter-spacing:.01em}
.lg-hero>p{margin-top:12px;font-size:14.5px;color:#a2abba}
.lg-feats{margin-top:32px;display:grid;grid-template-columns:1fr 1fr;gap:18px 24px}
.lg-feat .t{display:flex;align-items:center;gap:8px;font-size:14px;color:#e8eaee;font-weight:600}
.lg-feat .ck{width:18px;height:18px;border-radius:50%;background:rgba(91,141,239,.18);color:#7ea6f5;
  font-size:11px;display:inline-flex;align-items:center;justify-content:center;flex-shrink:0}
.lg-feat .d{margin-top:5px;font-size:12.5px;color:#8d96a4;line-height:1.6;padding-left:26px}

/* ---- 右侧表单区 ---- */
.lg-wrap{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;background:var(--bg);padding:40px 24px}
.lg-mini{display:none;align-items:center;gap:10px;margin-bottom:18px}
.lg-form{width:400px;max-width:100%;background:#fff;border:1px solid var(--line);border-radius:12px;
  padding:34px 34px 26px;box-shadow:0 8px 30px rgba(22,24,29,.06)}
.lg-form h2{font-size:19px;color:var(--ink);font-weight:650}
.lg-form .sub{margin:6px 0 24px;font-size:13.5px;color:var(--sub)}
.lg-f{margin-bottom:14px}
.lg-f label{display:block;font-size:13px;color:var(--sub);margin-bottom:6px;font-weight:500}
.lg-f input{width:100%;font-size:14px;padding:9px 12px;border-radius:7px}
.lg-pw{position:relative}
.lg-pw input{padding-right:42px}
.lg-eye{position:absolute;right:6px;top:50%;transform:translateY(-50%);width:28px;height:28px;
  display:flex;align-items:center;justify-content:center;color:var(--faint);border-radius:5px}
.lg-eye:hover{color:var(--txt);background:var(--line2)}
.lg-err{display:flex;gap:8px;background:var(--err-bg);color:var(--err);border:1px solid #f3d7d7;
  border-radius:7px;padding:8px 11px;font-size:13px;margin-bottom:14px;line-height:1.55}
.lg-btn{width:100%;height:40px;margin-top:6px;border-radius:7px;background:var(--ink);color:#fff;
  font-size:14.5px;font-weight:600;display:flex;align-items:center;justify-content:center;transition:.12s}
.lg-btn:hover:not(:disabled){background:#2a2d34}
.lg-btn:disabled{opacity:.55;cursor:default}
.lg-hint{margin-top:18px;text-align:center;font-size:12.5px;color:var(--faint)}

/* Chrome 自动填充黄底修正 */
:deep(input:-webkit-autofill){-webkit-box-shadow:0 0 0 1000px #fff inset;-webkit-text-fill-color:var(--txt)}

@media (max-width:960px){.lg-brand{display:none}.lg-mini{display:flex}}
</style>
