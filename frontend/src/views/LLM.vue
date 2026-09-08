<script setup>
import { ref, computed, onMounted } from 'vue'
import { api, getUser } from '../api'
import { toast, confirmDialog, alertDialog } from '../dialog'

const isAdmin = getUser()?.role === 'admin'
const items = ref([])
const info = ref(null)
const usage = ref(null)

// ---- 通知渠道 ----
const notifies = ref([])
const nShow = ref(false)
const nform = ref(null)   // {id, name, url, on_fail}
const nErr = ref('')
const nSaving = ref(false)

// 厂商目录：urls 里可同时提供 开放平台 API 与 Token/Coding 套餐 两种接入地址
const VENDORS = [
  { name: 'OpenAI', cat: '国际厂商', color: '#10a37f', urls: { api: { label: '开放平台 API', base_url: 'https://api.openai.com/v1', model: 'gpt-4o-mini' } } },
  { name: 'Anthropic', cat: '国际厂商', color: '#d97757', urls: { api: { label: '开放平台 API', base_url: 'https://api.anthropic.com/v1', model: 'claude-sonnet-4-5' } } },
  { name: 'Gemini', cat: '国际厂商', color: '#4285f4', urls: { api: { label: 'OpenAI 兼容', base_url: 'https://generativelanguage.googleapis.com/v1beta/openai', model: 'gemini-2.0-flash' } } },
  { name: 'OpenRouter', cat: '国际厂商', color: '#6467f2', urls: { api: { label: '聚合 API', base_url: 'https://openrouter.ai/api/v1', model: 'openai/gpt-4o-mini' } }, tip: '一个 Key 聚合数百模型' },
  { name: 'xAI Grok', cat: '国际厂商', color: '#1d1d1f', urls: { api: { label: '开放平台 API', base_url: 'https://api.x.ai/v1', model: 'grok-3' } } },
  { name: 'Mistral', cat: '国际厂商', color: '#fa520f', urls: { api: { label: '开放平台 API', base_url: 'https://api.mistral.ai/v1', model: 'mistral-small-latest' } } },
  { name: '智谱 AI', cat: '国内厂商', color: '#3178f6', urls: {
      api: { label: '按量 API（开放平台）', base_url: 'https://open.bigmodel.cn/api/paas/v4', model: 'glm-4-flash' },
      plan: { label: 'Coding / Token 套餐', base_url: 'https://open.bigmodel.cn/api/coding/paas/v4', model: 'glm-4.6' } } },
  { name: 'DeepSeek', cat: '国内厂商', color: '#4d6bfe', urls: { api: { label: '按量 API（开放平台）', base_url: 'https://api.deepseek.com/v1', model: 'deepseek-chat' } } },
  { name: '通义千问', cat: '国内厂商', color: '#615ced', urls: {
      api: { label: '按量 API（阿里云百炼）', base_url: 'https://dashscope.aliyuncs.com/compatible-mode/v1', model: 'qwen-plus' },
      plan: { label: '百炼 Token 套餐（Key 独立）', base_url: 'https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1', model: 'qwen3-coder-plus' },
      plan2: { label: 'qwen.ai 套餐（海外线，Key 独立）', base_url: 'https://portal.qwen.ai/v1', model: 'qwen3-coder-plus' } } },
  { name: 'Kimi', cat: '国内厂商', color: '#0d0d0d', urls: {
      api: { label: '按量 API（开放平台）', base_url: 'https://api.moonshot.cn/v1', model: 'kimi-k2-0905-preview' },
      plan: { label: 'Kimi For Coding 套餐', base_url: 'https://api.kimichain.com/v1', model: 'kimi-for-coding' } } },
  { name: '豆包', cat: '国内厂商', color: '#00c8be', urls: { api: { label: '按量 API（火山方舟）', base_url: 'https://ark.cn-beijing.volces.com/api/v3', model: 'doubao-1.5-pro-32k' } } },
  { name: '腾讯混元', cat: '国内厂商', color: '#0052d9', urls: { api: { label: '按量 API（开放平台）', base_url: 'https://api.hunyuan.cloud.tencent.com/v1', model: 'hunyuan-turbos-latest' } } },
  { name: '百度千帆', cat: '国内厂商', color: '#2932e1', urls: { api: { label: '按量 API（千帆 V2）', base_url: 'https://qianfan.baidubce.com/v2', model: 'ernie-4.0-turbo-8k' } } },
  { name: 'MiniMax', cat: '国内厂商', color: '#f23f5d', urls: {
      api: { label: '按量 API（开放平台）', base_url: 'https://api.minimax.chat/v1', model: 'abab6.5s-chat' },
      plan: { label: 'Coding 套餐（Key 不通用）', base_url: 'https://api.minimax.chat/v1', model: 'MiniMax-M2' } } },
  { name: '硅基流动', cat: '国内厂商', color: '#059669', urls: { api: { label: '按量 API', base_url: 'https://api.siliconflow.cn/v1', model: 'deepseek-ai/DeepSeek-V3' } }, tip: '聚合开源模型' },
  { name: 'Ollama', cat: '本地与中转', color: '#6b7280', urls: { api: { label: '本地服务', base_url: 'http://127.0.0.1:11434/v1', model: 'qwen2.5:7b' } } },
  { name: 'LM Studio', cat: '本地与中转', color: '#8b5cf6', urls: { api: { label: '本地服务', base_url: 'http://127.0.0.1:1234/v1', model: 'local-model' } } },
  { name: 'vLLM', cat: '本地与中转', color: '#0891b2', urls: { api: { label: '本地服务', base_url: 'http://127.0.0.1:8001/v1', model: 'served-model' } } },
  { name: 'One-API 中转', cat: '本地与中转', color: '#f59e0b', urls: { api: { label: '自建中转', base_url: 'http://127.0.0.1:3000/v1', model: 'gpt-4o-mini' } }, tip: '聚合自选模型' },
  { name: '自定义', cat: '', color: '#6b7280', urls: { api: { label: '自定义接口', base_url: '', model: '' } } },
]

async function load() {
  info.value = await api('/settings/llm')
  items.value = info.value.items
  usage.value = await api('/ai/usage')
  notifies.value = await api('/settings/notify')
}
// ---- MCP 接入（每个用户自己的长时效令牌） ----
const mcp = ref(null)
async function loadMcp() { mcp.value = await api('/settings/mcp') }
async function copyMcp() {
  try {
    await navigator.clipboard.writeText(mcp.value.config_json)
    toast('已复制：粘贴到 Cursor / Claude 等客户端的 MCP 配置即可接入', 4000)
  } catch {
    toast('复制失败，请手动选择文本复制')
  }
}
// 配置 + 工具清单一起复制：贴进 agent 规则/系统提示词，AI 未连接也知道平台有哪些工具
function toolsDoc() {
  const tools = mcp.value?.tools || []
  const lines = tools.map(t => `### ${t.name}\n${t.description || ''}`)
  return `# TestDeck MCP 接入\n\n## 客户端配置\n\n\`\`\`json\n${mcp.value.config_json}\n\`\`\`\n\n` +
    `## 工具清单（共 ${tools.length} 个；连接后客户端也会自动发现）\n\n` + lines.join('\n\n')
}
async function copyMcpFull() {
  try {
    await navigator.clipboard.writeText(toolsDoc())
    toast('已复制配置+工具说明（Markdown）：可贴进 agent 规则或团队文档', 4000)
  } catch {
    toast('复制失败，请手动选择文本复制')
  }
}
loadMcp()

onMounted(load)

function openNAdd() {
  nform.value = { id: 0, name: '', url: '', on_fail: true }
  nErr.value = ''; nShow.value = true
}
function openNEdit(n) {
  nform.value = { id: n.id, name: n.name, url: n.url, on_fail: n.on_fail }
  nErr.value = ''; nShow.value = true
}
async function saveN() {
  nErr.value = ''
  if (!nform.value.url.startsWith('http')) { nErr.value = '地址需以 http(s):// 开头'; return }
  nSaving.value = true
  try {
    if (nform.value.id) await api('/settings/notify/' + nform.value.id, { method: 'PUT', body: nform.value })
    else await api('/settings/notify', { method: 'POST', body: nform.value })
    nShow.value = false
    toast(nform.value.id ? '已保存 ✓' : '已添加 ✓')
    await load()
  } catch (e) { nErr.value = e.message } finally { nSaving.value = false }
}
async function testN(n) {
  n._testing = true
  try {
    const r = await api(`/settings/notify/${n.id}/test`, { method: 'POST' })
    n._test = r.ok ? { ok: true, text: `已发送 ✓ ${r.status}` } : { ok: false, text: `发送失败：${r.status}` }
  } catch (e) { n._test = { ok: false, text: e.message } } finally { n._testing = false }
}
async function delN(n) {
  if (!await confirmDialog(`删除通知渠道「${n.name}」？`, { danger: true, okText: '删除' })) return
  await api('/settings/notify/' + n.id, { method: 'DELETE' })
  await load()
}

const vendorColor = name => VENDORS.find(v => v.name === name)?.color || '#6b7280'
const SOURCE_TEXT = { db: '界面配置', env: '环境变量', none: '未配置' }

// ---- 添加 / 编辑弹窗 ----
const showEdit = ref(false)
const form = ref(null)      // {id, name, vendor, url_type, base_url, api_key, model}
const formErr = ref('')
const saving = ref(false)
const testing = ref(false)
const testMsg = ref(null)
const fetched = ref([])
const fetching = ref(false)
const fetchErr = ref('')
const mFilter = ref('')

function resetFetch() { fetched.value = []; fetchErr.value = ''; mFilter.value = ''; testMsg.value = null }

function openAdd() {
  form.value = { id: 0, name: '', vendor: '', url_type: 'api', base_url: '', api_key: '', model: '', vision: false }
  formErr.value = ''; resetFetch(); showEdit.value = true
}
function openEdit(r) {
  form.value = { id: r.id, name: r.name, vendor: r.vendor, url_type: r.url_type,
    base_url: r.base_url, api_key: '', model: r.model, vision: !!r.vision }
  formErr.value = ''; resetFetch(); showEdit.value = true
}

const curVendor = computed(() => VENDORS.find(v => v.name === form.value?.vendor) || null)
const urlTypes = computed(() =>
  curVendor.value ? Object.entries(curVendor.value.urls).map(([key, u]) => ({ key, ...u })) : [])

function onVendor() {
  const v = curVendor.value
  form.value.url_type = v ? Object.keys(v.urls)[0] : 'api'
  applyUrl()
}
function onUrlType() { applyUrl() }
function applyUrl() {
  const u = curVendor.value?.urls[form.value.url_type]
  if (u && u.base_url) { form.value.base_url = u.base_url; if (u.model) form.value.model = u.model }
}

async function save() {
  formErr.value = ''
  const f = form.value
  if (!f.base_url.startsWith('http')) { formErr.value = '地址需以 http(s):// 开头'; return }
  if (!f.model.trim()) { formErr.value = '请填写模型名'; return }
  if (!f.id && !f.api_key.trim()) { formErr.value = '请填写 API Key'; return }
  saving.value = true
  try {
    const body = { name: f.name.trim(), vendor: f.vendor, url_type: f.url_type,
      base_url: f.base_url.trim(), api_key: f.api_key.trim(), model: f.model.trim(),
      vision: !!f.vision }
    if (f.id) await api('/settings/llm/' + f.id, { method: 'PUT', body })
    else await api('/settings/llm', { method: 'POST', body })
    showEdit.value = false
    toast(f.id ? '已保存 ✓' : '已添加 ✓')
    await load()
  } catch (e) { formErr.value = e.message } finally { saving.value = false }
}

async function testForm() {
  testing.value = true; testMsg.value = null
  try {
    const r = await api('/settings/llm/test', { method: 'POST',
      body: { base_url: form.value.base_url, api_key: form.value.api_key, model: form.value.model } })
    testMsg.value = r.ok ? { ok: true, text: `连接成功 · ${r.model} · ${r.ms}ms` }
                         : { ok: false, text: `连接失败：${r.error}` }
  } catch (e) { testMsg.value = { ok: false, text: e.message } } finally { testing.value = false }
}

async function fetchModels() {
  fetchErr.value = ''
  if (!form.value.base_url.startsWith('http')) { fetchErr.value = '请先填接口地址'; return }
  fetching.value = true; fetched.value = []; mFilter.value = ''
  try {
    const r = await api('/settings/llm/models', { method: 'POST',
      body: { base_url: form.value.base_url, api_key: form.value.api_key, id: form.value.id || 0 } })
    if (r.ok) {
      fetched.value = r.models
      if (!r.models.length) fetchErr.value = '厂商返回了空列表，请检查 Key 或手工填写模型名'
      else if (!form.value.model) form.value.model = r.models[0]
    } else fetchErr.value = r.error
  } catch (e) { fetchErr.value = e.message } finally { fetching.value = false }
}

const shownModels = computed(() => {
  const q = mFilter.value.trim().toLowerCase()
  const list = q ? fetched.value.filter(m => m.toLowerCase().includes(q)) : fetched.value
  return list.slice(0, 48)
})

// ---- 列表操作 ----
async function activate(r) {
  await api(`/settings/llm/${r.id}/activate`, { method: 'POST' })
  toast(`已切换为使用「${r.name}」`)
  await load()
}
async function testRow(r) {
  r._testing = true
  try {
    const res = await api(`/settings/llm/${r.id}/test`, { method: 'POST' })
    r._test = res.ok ? { ok: true, text: `${res.ms}ms ✓` } : { ok: false, text: res.error }
  } catch (e) { r._test = { ok: false, text: e.message } } finally { r._testing = false }
}
async function delRow(r) {
  if (!await confirmDialog(`删除模型配置「${r.name}」？`, { danger: true, okText: '删除' })) return
  await api('/settings/llm/' + r.id, { method: 'DELETE' })
  await load()
}
</script>

<template>
  <div class="hd"><div><h2>系统设置</h2></div>
    <div style="display:flex;gap:8px;align-items:center">
      <span v-if="info" class="st" :class="info.available ? 'ok' : 'off'">
        {{ info.available ? '可用' : '未配置' }}<template v-if="info.available"> · {{ SOURCE_TEXT[info.source] }}</template>
      </span>
      <button v-if="isAdmin" class="btn pri" @click="openAdd">+ 添加模型</button>
    </div>
  </div>

  <div class="grid4" v-if="usage">
    <div><div class="k">当前模型</div><div class="v" style="font-size:15px;padding-top:4px">
      {{ items.find(x => x.is_active)?.name || '—' }}</div></div>
    <div><div class="k">配置数</div><div class="v">{{ items.length }}</div></div>
    <div><div class="k">Tokens</div><div class="v">{{ usage.prompt_tokens + usage.completion_tokens }}</div>
      <div class="d">调用 {{ usage.calls }} 次 · 输入 {{ usage.prompt_tokens }} / 输出 {{ usage.completion_tokens }}</div></div>
    <div><div class="k">失败</div><div class="v" :style="usage.failed ? 'color:var(--err)' : ''">{{ usage.failed }}</div></div>
  </div>

  <div class="panel">
    <table v-if="items.length">
      <thead><tr><th>状态</th><th>名称</th><th>厂商</th><th>模型</th><th>接入</th><th>Base URL</th><th></th></tr></thead>
      <tbody>
        <tr v-for="r in items" :key="r.id">
          <td><span :class="r.is_active ? 'st ok' : 'st off'">{{ r.is_active ? '使用中' : '备用' }}</span></td>
          <td><b>{{ r.name }}</b><span v-if="r.vision" class="chip" style="margin-left:5px">视觉</span>
            <div v-if="r._test" style="margin-top:3px"><span :class="r._test.ok ? 'st ok' : 'st err'" style="font-size:12px">{{ r._test.text }}</span></div>
          </td>
          <td><span class="v-dot" :style="{ background: vendorColor(r.vendor) }"></span>{{ r.vendor || '—' }}</td>
          <td class="mono">{{ r.model }}</td>
          <td><span class="chip">{{ r.url_type.startsWith('plan') ? '套餐' : 'API' }}</span></td>
          <td class="mono muted" style="max-width:260px;word-break:break-all;font-size:12px">{{ r.base_url }}</td>
          <td style="text-align:right;white-space:nowrap" v-if="isAdmin">
            <a v-if="!r.is_active" @click="activate(r)">设为使用</a> ·
            <a @click="testRow(r)">{{ r._testing ? '测试中…' : '测试' }}</a> ·
            <a @click="openEdit(r)">编辑</a> ·
            <a style="color:var(--err)" @click="delRow(r)">删除</a>
          </td>
          <td v-else></td>
        </tr>
      </tbody>
    </table>
    <div v-else class="empty">
      还没有模型配置。点右上角「添加模型」，选厂商、填 Key 即可；{{ info?.env_fallback ? '当前由环境变量兜底。' : '配置后 AI 功能立即可用。' }}
    </div>
    <div v-if="info && !isAdmin && !items.length" class="muted" style="margin-top:10px">仅管理员可修改模型配置。</div>
  </div>

  <!-- 失败告警通知渠道 -->
  <div class="panel" v-if="mcp">
    <div class="bar">
      <h3>MCP 接入（让外部 AI agent 操作测试平台）</h3>
      <div style="display:flex;gap:8px">
        <button class="btn sm" @click="loadMcp">重新生成</button>
        <button class="btn sm" @click="copyMcpFull" title="复制 Markdown：客户端配置 + 全部工具的名称与用途说明，可贴进 agent 规则/系统提示词或团队文档">复制配置+工具说明</button>
        <button class="btn sm pri" @click="copyMcp">一键复制配置</button>
      </div>
    </div>
    <div class="muted" style="font-size:12.5px;margin-bottom:8px">
      在 Cursor / Claude 等支持 MCP 的客户端粘贴下方配置即可，连接后客户端会自动发现全部工具。令牌为<b>长时效专用令牌</b>
      （10 年有效、不受登录过期与服务重启影响，权限跟随你的账号）；修改密码后全部令牌自动吊销、需重新生成。
    </div>
    <textarea readonly :value="mcp.config_json" rows="7" class="mono"
              style="width:100%;font-size:12px;background:#fcfcfd"></textarea>
    <details v-if="mcp.tools?.length" style="margin-top:10px">
      <summary style="cursor:pointer;font-size:13px" class="muted">
        平台暴露的 {{ mcp.tools.length }} 个 MCP 工具（名称与用途）
      </summary>
      <table style="margin-top:8px">
        <thead><tr><th style="width:200px">工具</th><th>用途</th></tr></thead>
        <tbody>
          <tr v-for="t in mcp.tools" :key="t.name">
            <td class="mono" style="font-size:12px">{{ t.name }}</td>
            <td class="muted" style="font-size:12px;white-space:pre-line">{{ (t.description || '').split('\n').slice(0, 2).join('\n') }}</td>
          </tr>
        </tbody>
      </table>
    </details>
  </div>

  <div class="panel">
    <div class="bar">
      <h3>失败告警通知（钉钉 / 企微群机器人）</h3>
      <button v-if="isAdmin" class="btn sm pri" @click="openNAdd">+ 添加渠道</button>
    </div>
    <table v-if="notifies.length">
      <thead><tr><th>名称</th><th>Webhook</th><th>触发</th><th>最近推送</th><th></th></tr></thead>
      <tbody>
        <tr v-for="n in notifies" :key="n.id">
          <td><b>{{ n.name }}</b>
            <div v-if="n._test" style="margin-top:3px"><span :class="n._test.ok ? 'st ok' : 'st err'" style="font-size:12px">{{ n._test.text }}</span></div>
          </td>
          <td class="mono muted" style="max-width:300px;word-break:break-all;font-size:12px">{{ n.url }}</td>
          <td>{{ n.on_fail ? '失败时' : '—' }}</td>
          <td class="muted">{{ n.last_status || '—' }}</td>
          <td style="text-align:right;white-space:nowrap" v-if="isAdmin">
            <a @click="testN(n)">{{ n._testing ? '测试中…' : '测试' }}</a> ·
            <a @click="openNEdit(n)">编辑</a> ·
            <a style="color:var(--err)" @click="delN(n)">删除</a>
          </td>
          <td v-else></td>
        </tr>
      </tbody>
    </table>
    <div v-else class="empty">
      {{ isAdmin ? '还没有通知渠道。添加群机器人 webhook 后，执行失败会自动推送到群里。' : '管理员尚未配置通知渠道。' }}
    </div>
  </div>

  <!-- 通知渠道 添加 / 编辑 -->
  <div class="mask on" v-if="nShow" @click.self="nShow = false">
    <div class="modal" style="width:480px">
      <h3>{{ nform.id ? '编辑通知渠道' : '添加通知渠道' }}</h3>
      <div class="fld"><label>名称</label><input v-model="nform.name" placeholder="测试群告警"></div>
      <div class="fld"><label>群机器人 webhook 地址</label>
        <input v-model="nform.url" class="mono" placeholder="https://oapi.dingtalk.com/robot/send?access_token=…"></div>
      <div class="fld"><label style="display:flex;align-items:center;gap:7px;cursor:pointer">
        <input type="checkbox" v-model="nform.on_fail" style="width:auto"> 执行失败时通知</label></div>
      <div v-if="nErr" style="color:var(--err);font-size:12.5px;margin-bottom:8px">{{ nErr }}</div>
      <div class="ft">
        <button class="btn" @click="nShow = false">取消</button>
        <button class="btn pri" :disabled="nSaving" @click="saveN">{{ nSaving ? '保存中…' : '保存' }}</button>
      </div>
    </div>
  </div>

  <!-- 添加 / 编辑弹窗 -->
  <div class="mask on" v-if="showEdit" @click.self="showEdit = false">
    <div class="modal" style="width:600px">
      <h3>{{ form.id ? '编辑模型' : '添加模型' }}</h3>
      <div class="two">
        <div class="fld"><label>厂商</label>
          <select v-model="form.vendor" @change="onVendor">
            <option value="" disabled>选择厂商…</option>
            <optgroup v-for="cat in ['国际厂商', '国内厂商', '本地与中转']" :key="cat" :label="cat">
              <option v-for="v in VENDORS.filter(x => x.cat === cat)" :key="v.name" :value="v.name">
                {{ v.name }}<template v-if="v.tip">（{{ v.tip }}）</template></option>
            </optgroup>
          </select></div>
        <div class="fld"><label>接入方式<template v-if="urlTypes.length > 1">（地址不同，按需选择）</template></label>
          <select v-model="form.url_type" @change="onUrlType" :disabled="urlTypes.length < 2">
            <option v-for="u in urlTypes" :key="u.key" :value="u.key">{{ u.label }}</option>
          </select></div>
      </div>
      <div v-if="urlTypes.length > 1" class="faint" style="font-size:12px;margin:-4px 0 10px">
        套餐与按量计费的 Key 不通用，请填对应渠道申请的 Key；预置地址如有更新以厂商文档为准，可直接修改。
      </div>
      <div class="fld"><label>名称（列表里显示，留空默认用模型名）</label>
        <input v-model="form.name" placeholder="如：智谱 GLM 主力"></div>
      <div class="fld"><label>接口地址 Base URL</label>
        <input v-model="form.base_url" class="mono" placeholder="https://open.bigmodel.cn/api/paas/v4"></div>
      <div class="two">
        <div class="fld"><label>API Key<template v-if="form.id">（留空保持不变）</template></label>
          <input v-model="form.api_key" type="password" placeholder="sk-…" autocomplete="new-password"></div>
        <div class="fld"><label>模型<template v-if="fetched.length"> · 已拉取 {{ fetched.length }} 个</template></label>
          <div class="row" style="flex-wrap:nowrap">
            <input v-model="form.model" class="mono" style="flex:1" placeholder="glm-4-flash">
            <button class="btn" style="white-space:nowrap" :disabled="fetching" @click="fetchModels">
              {{ fetching ? '获取中…' : '拉取列表' }}</button>
          </div>
          <div v-if="fetched.length" class="model-list">
            <input v-model="mFilter" placeholder="输入关键字筛选，如 glm / deepseek" style="margin-bottom:8px">
            <div class="model-chips">
              <span v-for="m in shownModels" :key="m" class="model-chip" :class="{ on: form.model === m }"
                @click="form.model = m">{{ m }}</span>
            </div>
          </div>
          <div v-if="fetchErr" style="color:var(--err);font-size:12.5px;margin-top:6px">{{ fetchErr }}</div>
        </div>
      </div>
      <div v-if="formErr" style="color:var(--err);font-size:12.5px;margin-bottom:8px">{{ formErr }}</div>
      <div class="fld" style="margin-bottom:14px">
        <label style="display:flex;align-items:center;gap:7px;cursor:pointer">
          <input type="checkbox" v-model="form.vision" style="width:auto">
          支持视觉（可接收页面截图，AI 用例可识别滑块/图片验证码）
        </label>
        <div v-if="form.vision" class="faint" style="font-size:12px;margin-top:5px">
          勾选后 AI 用例每步会附带截图发给模型，token 消耗会增加（每步约 +1~2k）。
        </div>
      </div>
      <div style="display:flex;gap:8px;align-items:center">
        <button class="btn pri" :disabled="saving" @click="save">{{ saving ? '保存中…' : (form.id ? '保存' : '添加') }}</button>
        <button class="btn" :disabled="testing" @click="testForm">{{ testing ? '测试中…' : '测试连接' }}</button>
        <span v-if="testMsg" :class="testMsg.ok ? 'st ok' : 'st err'">{{ testMsg.text }}</span>
      </div>
    </div>
  </div>
</template>
