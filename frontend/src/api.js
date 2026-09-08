const TOKEN_KEY = 'td_token'
const USER_KEY = 'td_user'

export function getToken() { return localStorage.getItem(TOKEN_KEY) }
export function getUser() { try { return JSON.parse(localStorage.getItem(USER_KEY)) } catch { return null } }
export function setAuth(token, user) {
  localStorage.setItem(TOKEN_KEY, token)
  localStorage.setItem(USER_KEY, JSON.stringify(user))
}
export function clearAuth() {
  localStorage.removeItem(TOKEN_KEY); localStorage.removeItem(USER_KEY)
  // 登录时后端同步下发的 td_token Cookie（/static 截图鉴权用）一并清掉
  document.cookie = 'td_token=; Max-Age=0; path=/'
}

export async function api(path, opts = {}) {
  const headers = { 'Content-Type': 'application/json', ...(opts.headers || {}) }
  const t = getToken()
  if (t) headers.Authorization = `Bearer ${t}`
  const ctrl = new AbortController()
  const timer = setTimeout(() => ctrl.abort(), opts.timeout || 120000)
  let resp
  try {
    resp = await fetch('/api/v1' + path, { ...opts, headers, signal: ctrl.signal,
      body: opts.body ? JSON.stringify(opts.body) : undefined })
  } catch (e) {
    if (e.name === 'AbortError') throw new Error('请求超时，请检查服务是否在线')
    throw e
  } finally { clearTimeout(timer) }
  // 401 视为会话过期跳登录页；但登录接口本身的 401（密码错误）要走正常报错展示
  if (resp.status === 401 && !path.startsWith('/auth/login')) { clearAuth(); location.href = '/'; throw new Error('未登录') }
  const data = await resp.json().catch(() => ({}))
  if (!resp.ok) throw new Error(data.detail || `请求失败 (${resp.status})`)
  return data
}
