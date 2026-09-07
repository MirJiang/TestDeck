<script setup>
import { ref, onMounted } from 'vue'
import { api } from '../api'

const props = defineProps({ projects: Array })
const emit = defineEmits(['close', 'saved'])

const pid = ref(props.projects[0]?.id || '')
const repos = ref([])
const commits = ref([])
const result = ref(null)
const loading = ref(false)
const err = ref('')
const showRepo = ref(false)
const repoForm = ref({ repo_url: '', provider: 'github', default_branch: 'main' })
const webhookInfo = ref('')

onMounted(refresh)

async function refresh() {
  err.value = ''; result.value = null
  repos.value = await api(`/integrations/git/repos?project_id=${pid.value}`)
  commits.value = repos.value[0] ? await api(`/integrations/git/commits?repo_id=${repos.value[0].id}`) : []
}

async function addRepo() {
  try {
    const r = await api('/integrations/git/repos', { method: 'POST',
      body: { project_id: pid.value, ...repoForm.value } })
    webhookInfo.value = r.webhook_url
    showRepo.value = false; await refresh()
  } catch (e) { err.value = e.message }
}

async function simulatePush() {
  // 演示用：向自己的 webhook 推一条示例 push（生产环境由 Git 平台推送）
  const repo = repos.value[0]
  if (!repo) return
  await api(`/integrations/git/webhook/${repo.webhook_secret}`, { method: 'POST',
    body: { ref: 'refs/heads/main', commits: [
      { id: Math.random().toString(16).slice(2, 9), message: 'feat: 新增退款接口 /api/refund/create', author: { name: '李娜' } },
      { id: Math.random().toString(16).slice(2, 9), message: 'fix: 优惠券接口 /api/coupon/amount 计算修复 (#90)', author: { name: '王强' } },
    ] } })
  await refresh()
}

async function analyze() {
  const repo = repos.value[0]
  if (!repo) return err.value = '请先配置仓库'
  loading.value = true; err.value = ''
  try {
    result.value = await api('/ai/gen-from-commits', { method: 'POST', body: { repo_id: repo.id, commit_ids: [] } })
  } catch (e) { err.value = e.message } finally { loading.value = false }
}

async function saveDraft(d) {
  await api(`/projects/${pid.value}/cases`, { method: 'POST',
    body: { project_id: pid.value, name: d.name, steps: d.steps, source: 'ai' } })
  emit('saved')
}
</script>

<template>
  <div class="drawer on">
    <div class="dh"><h3>AI · 从 Git 提交生成用例</h3><button class="x" @click="emit('close')">✕</button></div>
    <div class="db">
      <div class="fld"><label>项目</label>
        <select v-model="pid" @change="refresh">
          <option v-if="!projects.length" value="" disabled>暂无项目，请先创建</option>
          <option v-for="p in projects" :key="p.id" :value="p.id">{{ p.name }}</option>
        </select></div>

      <div v-if="!repos.length" class="panel" style="background:#fcfcfd">
        <h3 style="font-size:13px">这个项目还没绑定 Git 仓库</h3>
        <div class="muted" style="font-size:12.5px;margin-bottom:10px">绑定后，push 事件会自动同步提交记录；AI 据此生成用例草稿。</div>
        <div class="two">
          <div class="fld"><label>仓库地址</label><input v-model="repoForm.repo_url" class="mono" placeholder="https://github.com/org/repo"></div>
          <div class="fld"><label>默认分支</label><input v-model="repoForm.default_branch" placeholder="main"></div>
        </div>
        <button class="btn pri" @click="addRepo">绑定仓库</button>
      </div>

      <template v-else>
        <div class="bar">
          <h3 style="font-size:13px">最近的提交</h3>
          <button class="btn sm" @click="simulatePush">模拟一次 push（演示）</button>
        </div>
        <div class="muted" style="font-size:12px;margin-bottom:8px">
          webhook 地址：<span class="mono">{{ '/api/v1/integrations/git/webhook/' + repos[0].webhook_secret }}</span>
        </div>
        <div v-for="c in commits" :key="c.id"
          style="border:1px solid var(--line);border-radius:7px;padding:8px 10px;margin-bottom:6px">
          <div style="font-size:13px">{{ c.message }}</div>
          <div class="faint" style="font-size:12px;margin-top:2px">
            <span class="mono">{{ c.sha }}</span> · {{ c.author }} · {{ c.branch }} {{ c.analyzed ? '· 已分析' : '' }}</div>
        </div>
        <div v-if="!commits.length" class="empty" style="padding:16px 0">还没有提交记录</div>
        <button class="btn pri" :disabled="loading" @click="analyze" style="margin-top:8px">
          {{ loading ? 'AI 正在分析…' : '分析提交并生成用例' }}</button>
      </template>

      <div v-if="err" style="color:var(--err);margin-top:10px">{{ err }}</div>

      <div v-if="result" class="panel" style="margin-top:14px;margin-bottom:0">
        <div class="bar"><h3 style="font-size:13px">AI 分析结果</h3>
          <span class="chip">{{ result.engine === 'builtin' ? '内置分析' : result.engine }}</span></div>

        <div v-for="(d, i) in result.drafts" :key="i" style="border:1px solid var(--line);border-radius:7px;padding:12px;margin-bottom:10px">
          <div style="font-size:13.5px"><b>草稿 · {{ d.name }}</b></div>
          <div class="muted" style="font-size:12.5px;margin:6px 0">{{ d.reason }}</div>
          <div v-for="(s, j) in d.steps" :key="j" class="mono muted" style="font-size:12px">
            {{ j + 1 }}. {{ s.m }} {{ s.url }} · 检查：{{ s.check?.type }} {{ s.check?.expect || s.check?.field || '' }}</div>
          <div style="margin-top:10px;display:flex;gap:8px">
            <button class="btn sm pri" @click="saveDraft(d)">保存为用例</button>
            <button class="btn sm" @click="emit('close')">去修改</button>
          </div>
        </div>

        <div v-for="(c, i) in result.covered" :key="'c' + i" style="border:1px solid var(--line);border-radius:7px;padding:12px;margin-bottom:10px;background:#fcfcfd">
          <div style="font-size:13.5px"><b>{{ c.api }}</b> <span class="faint">已有用例「{{ c.case_name }}」覆盖</span></div>
          <div class="muted" style="font-size:12.5px;margin:6px 0">该接口被提交「{{ c.commit }}」修改，建议立即执行验证。</div>
          <router-link to="/cases" @click="emit('close')"><button class="btn sm">去用例页执行</button></router-link>
        </div>

        <div v-if="!result.drafts.length && !result.covered.length" class="muted">这些提交未发现受影响的接口。</div>
        <div class="faint" style="font-size:12px;margin-top:8px">AI 只生成草稿，保存后仍可随意修改。</div>
      </div>
    </div>
  </div>
</template>
