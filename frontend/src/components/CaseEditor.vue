<script setup>
import { ref, computed, watch } from 'vue'
import { api } from '../api'
import { toast } from '../dialog'
import RecorderModal from './RecorderModal.vue'

const props = defineProps({ modelValue: Object }) // {id?, name, target, goal, start_url, engine, max_steps, endpoints, fixedSteps}
const emit = defineEmits(['update:modelValue', 'save', 'close'])

const c = computed(() => props.modelValue)
const isUI = computed(() => (c.value.target || 'ui') === 'ui')
const recOpen = ref(false)
const recMsg = ref('')
const envs = ref([])   // 供录制弹窗「AI 代劳」注入环境变量

async function openRec() {
  if (!envs.value.length && c.value.project_id) {
    try { envs.value = await api(`/projects/${c.value.project_id}/envs`) } catch { /* 无环境也能录 */ }
  }
  recOpen.value = true   // 先取环境再弹窗：地址预填才来得及
}

function onRecDone(steps) {
  c.value.fixedSteps = steps
  recMsg.value = `录制完成，共 ${steps.length} 步，保存后按固定步骤回放（零 token）。`
  recOpen.value = false
}

function delFixed(i) { c.value.fixedSteps.splice(i, 1) }

// ---- 接口文档（API 目标） ----
const docs = ref([])
const selDoc = ref('')
const endpoints = ref([])          // 当前所选文档的端点
const loadingDocs = ref(false)
const showImport = ref(false)
const importName = ref('')
const importText = ref('')
const importErr = ref('')
const importing = ref(false)
const importUrl = ref('')

async function loadDocs() {
  loadingDocs.value = true
  try {
    docs.value = await api(`/projects/${c.value.project_id}/api-docs`)
    selDoc.value = docs.value[0]?.id || ''
    if (selDoc.value) await loadEndpoints()
    else endpoints.value = []
  } finally { loadingDocs.value = false }
}

async function loadEndpoints() {
  if (!selDoc.value) { endpoints.value = []; return }
  endpoints.value = await api(`/projects/${c.value.project_id}/api-docs/${selDoc.value}/endpoints`)
}

watch(selDoc, loadEndpoints)
// 打开已有 API 用例（target 初始即 api）或切换项目时也要拉文档，不能只依赖 target 变化
watch(() => [c.value.target, c.value.project_id], ([t, pid]) => {
  docs.value = []; endpoints.value = []; selDoc.value = ''
  if (t === 'api' && pid) loadDocs()
}, { immediate: true })

function onFile(e) {
  const f = e.target.files[0]
  if (!f) return
  if (f.size > 5 * 1024 * 1024) { importErr.value = '文件超过 5MB'; return }
  const r = new FileReader()
  r.onload = () => { importText.value = r.result; importName.value = f.name.replace(/\.[^.]+$/, '') }
  r.readAsText(f)
}

async function doImport() {
  importErr.value = ''
  if (!importUrl.value.trim() && !importText.value.trim()) { importErr.value = '请填写文档 URL 或粘贴内容'; return }
  importing.value = true
  try {
    const body = importUrl.value.trim()
      ? { url: importUrl.value.trim(), name: importName.value }
      : { content: importText.value, name: importName.value }
    const r = await api(`/projects/${c.value.project_id}/api-docs/import`, { method: 'POST', body })
    await loadDocs()
    selDoc.value = r.id
    showImport.value = false
    importText.value = ''; importName.value = ''; importUrl.value = ''
  } catch (e) { importErr.value = e.message } finally { importing.value = false }
}

const refreshing = ref(false)
async function refreshDoc() {
  if (!selDoc.value) return
  refreshing.value = true
  try {
    const r = await api(`/projects/${c.value.project_id}/api-docs/${selDoc.value}/refresh`, { method: 'POST' })
    toast(`已刷新，共 ${r.count} 个接口`)
    await loadDocs()
  } catch (e) { importErr.value = e.message } finally { refreshing.value = false }
}

function toggleEp(ep) {
  const arr = c.value.endpoints || (c.value.endpoints = [])
  const i = arr.findIndex(x => x.method === ep.method && x.path === ep.path)
  i >= 0 ? arr.splice(i, 1) : arr.push({ method: ep.method, path: ep.path, summary: ep.summary, params: ep.params })
}
const hasEp = ep => (c.value.endpoints || []).some(x => x.method === ep.method && x.path === ep.path)

function save() { emit('save', JSON.parse(JSON.stringify(c.value))) }

// ---- 测试账号：项目用户列表选择带出，可改；执行与录制时注入 ${username}/${password} ----
const pusers = ref([])
const selUser = ref('')
watch(() => c.value.project_id, async pid => {
  pusers.value = []; selUser.value = ''
  if (pid) { try { pusers.value = await api(`/projects/${pid}/users`) } catch { /* 无用户也可手填 */ } }
}, { immediate: true })
function onPickUser() {
  const u = pusers.value.find(x => x.id === selUser.value)
  if (u) { c.value.username = u.username; c.value.password = u.password }
}
const presetVars = computed(() => (c.value.username ? { username: c.value.username, password: c.value.password || '' } : {}))

// 录制弹窗预填地址 = 环境地址 + 起始页面（起始页面若已是完整 URL 则直接用）
const recInitialUrl = computed(() => {
  const base = (envs.value[0]?.base_url || '').replace(/\/+$/, '')
  const path = (c.value.start_url || '').trim()
  if (/^https?:\/\//.test(path)) return path
  if (!base) return path
  if (!path) return base
  return base + (path.startsWith('/') ? path : '/' + path)
})
</script>

<template>
  <div class="drawer on">
    <div class="dh"><h3>{{ c.id ? '编辑 AI 用例' : '新建 AI 用例' }}</h3><button class="x" @click="emit('close')">✕</button></div>
    <div class="db">
      <div class="fld"><label>用例名称</label><input v-model="c.name" placeholder="询价单创建到报价全流程"></div>
      <div class="two">
        <div class="fld"><label>测试对象</label>
          <select v-model="c.target">
            <option value="ui">UI 测试（浏览器操作）</option>
            <option value="api">API 测试（接口调用）</option>
          </select></div>
        <div class="fld"><label>最大步数（防止失控）</label>
          <input v-model.number="c.max_steps" type="number" min="3" max="99" placeholder="30"></div>
      </div>

      <div class="fld"><label>测试目标（用大白话描述要做的事和预期结果，可引用 ${'{'}变量{'}'} 如账号密码）</label>
        <textarea v-model="c.goal" rows="4"
          :placeholder="isUI ? '用 ${username} 登录系统，创建一张从上海到北京的询价单，记住单号，页面应提示创建成功' : '调用创建询价单接口，用 ${username} 的身份创建一张上海到北京的单据，应返回 code=0 并记住单号'"></textarea></div>

      <div class="two">
        <div class="fld"><label>测试账号（项目用户列表带出，可改）</label>
          <select v-model="selUser" @change="onPickUser">
            <option value="">— 手动填写 / 不使用 —</option>
            <option v-for="u in pusers" :key="u.id" :value="u.id">{{ u.name ? `${u.name}（${u.username}）` : u.username }}</option>
          </select></div>
        <div class="fld"><label>账号 / 密码（执行时注入 $&#123;username&#125; $&#123;password&#125;）</label>
          <div style="display:flex;gap:8px">
            <input v-model="c.username" class="mono" placeholder="username" style="flex:1">
            <input v-model="c.password" class="mono" type="password" placeholder="password" style="flex:1">
          </div></div>
      </div>

      <template v-if="isUI">
        <div class="two">
          <div class="fld"><label>浏览器引擎</label>
            <select v-model="c.engine">
              <option value="">跟随全局配置</option>
              <option value="chromium">Chromium Headless（默认，截图完整）</option>
              <option value="lightpanda">Lightpanda（轻量 beta，需另行启动其服务）</option>
            </select></div>
          <div class="fld"><label>起始页面（只填路径，域名由环境补上）</label>
            <input v-model="c.start_url" class="mono" placeholder="/page/login"></div>
        </div>

        <div class="panel" style="background:#fcfcfd">
          <div class="bar" style="margin-bottom:6px">
            <h3 style="font-size:13px">固定步骤（可选，替代 AI 自由探索）</h3>
            <button class="btn sm" @click="openRec">录制生成</button>
          </div>
          <div v-if="recMsg" class="muted" style="font-size:12.5px;margin-bottom:6px">{{ recMsg }}</div>
          <div v-if="(c.fixedSteps || []).length">
            <div v-for="(s, i) in c.fixedSteps" :key="i" class="row" style="border-bottom:1px dashed var(--line2);padding:4px 0;font-size:12.5px">
              <span class="mono">{{ i + 1 }}. [{{ s.action }}] {{ s.url || s.selector || s.value }}</span>
              <button class="del" style="margin-left:auto;color:var(--faint)" @click="delFixed(i)">删除</button>
            </div>
            <div class="faint" style="font-size:12px;margin-top:6px">保存后按固定步骤回放（零 token）；填写了「测试目标」则以 AI 探索优先。</div>
          </div>
          <div v-else class="faint" style="font-size:12.5px">
            录一遍页面操作生成固定步骤，之后回归按固定动作执行、不消耗 token；不录制也可以，直接靠「测试目标」让 AI 每次自主执行。
          </div>
        </div>
      </template>

      <div v-else class="panel" style="background:#fcfcfd">
        <div class="bar" style="margin-bottom:8px">
          <h3 style="font-size:13px">接口文档（AI 按真实接口设计请求）</h3>
          <button class="btn sm" @click="showImport = !showImport">{{ showImport ? '收起导入' : '导入文档' }}</button>
        </div>

        <div v-if="showImport" class="fld" style="border:1px dashed var(--line);border-radius:7px;padding:10px;margin-bottom:10px">
          <div class="row" style="margin-bottom:8px">
            <input v-model="importUrl" class="mono" style="flex:1"
              placeholder="从 URL 拉取（优先）：如 http://host/v3/api-docs 或 /openapi.json" @keyup.enter="doImport">
            <button class="btn pri sm" style="white-space:nowrap" :disabled="importing" @click="doImport">
              {{ importing ? '拉取解析中…' : '从 URL 导入' }}</button>
          </div>
          <div class="row" style="margin-bottom:8px">
            <input v-model="importName" placeholder="文档名称（可选）" style="width:200px">
            <label class="btn sm" style="cursor:pointer">
              选择文件<input type="file" accept=".json,.yaml,.yml,.txt" style="display:none" @change="onFile">
            </label>
            <span class="faint" style="font-size:12px">或直接粘贴内容（未填 URL 时使用）：</span>
          </div>
          <textarea v-model="importText" rows="5" class="mono" style="width:100%"
            placeholder="粘贴 Swagger/OpenAPI JSON·YAML 或 Postman Collection（Apifox 请导出 OpenAPI 格式）"></textarea>
          <div v-if="importErr" style="color:var(--err);font-size:12.5px;margin:6px 0">{{ importErr }}</div>
        </div>

        <div v-if="docs.length" class="fld">
          <label>文档</label>
          <div class="row" style="flex-wrap:nowrap">
            <select v-model="selDoc" style="flex:1" @change="loadEndpoints">
              <option v-for="d in docs" :key="d.id" :value="d.id">{{ d.name }}（{{ d.count }} 个接口）</option>
            </select>
            <button class="btn sm" style="white-space:nowrap" :disabled="refreshing" @click="refreshDoc">刷新</button>
          </div>
        </div>

        <div v-if="endpoints.length" style="max-height:240px;overflow:auto;border:1px solid var(--line);border-radius:7px;padding:8px">
          <label v-for="ep in endpoints" :key="ep.id"
            style="display:flex;gap:8px;align-items:center;padding:4px 6px;cursor:pointer;font-size:12.5px">
            <input type="checkbox" style="width:auto" :checked="hasEp(ep)" @change="toggleEp(ep)">
            <span class="chip" style="min-width:52px;text-align:center">{{ ep.method }}</span>
            <span class="mono" style="flex:1;word-break:break-all">{{ ep.path }}</span>
            <span class="muted" style="max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{{ ep.summary }}</span>
          </label>
        </div>
        <div v-else-if="!showImport" class="faint" style="font-size:12.5px">
          {{ loadingDocs ? '加载中…' : (docs.length ? '该文档暂无接口' : '尚未导入接口文档。导入 Swagger / OpenAPI / Postman 文档后，AI 会按真实接口与参数设计请求，而不是猜测路径。') }}
        </div>
        <div v-if="(c.endpoints || []).length" class="muted" style="font-size:12.5px;margin-top:8px">
          已选 <b>{{ c.endpoints.length }}</b> 个接口，将作为 AI 设计请求的依据。
        </div>
        <div class="faint" style="font-size:12.5px;margin-top:8px">
          接口域名来自环境的 Base URL；可用变量在「环境」页维护。
        </div>
      </div>
    </div>
    <div class="df"><button class="btn" @click="emit('close')">取消</button><button class="btn pri" @click="save">保存用例</button></div>
  </div>
  <RecorderModal v-if="recOpen" title="录制固定步骤" :envs="envs" :preset-vars="presetVars"
    :initial-url="recInitialUrl" @done="onRecDone" @close="recOpen = false" />
</template>
