"""通用网页可交互元素识别（无站点特定规则）。

技术思路借鉴开源浏览器 Agent 的成熟做法（browser-use 的 DOM 处理引擎 / enhanced_snapshot）：
可交互 = 信号集并集——
  ① 原生交互标签（a/button/input/select/textarea/summary/option/label）
  ② 交互性 ARIA role（button/link/menuitem/tab/treeitem/checkbox/switch/combobox…）
  ③ tabindex 可聚焦
  ④ onclick 属性 / contenteditable
  ⑤ 计算样式 cursor:pointer——JS 绑定点击的 div/span 几乎都会被设计成手型光标，
     这是捕捉"隐形菜单"的万能信号，不依赖任何 class 命名约定
可见性 = 矩形尺寸/视口位置 + computed style（display/visibility/opacity）+ aria-hidden；
遮挡 = elementFromPoint 判断元素是否真在点击位最上层（弹窗遮罩后的元素判为被遮挡）。

本模块只产出共享的 JS 片段（HELPERS_JS），供 ai_runner._STATE_JS 与 app_mapper 的
点击探索 / 地图提取复用——三个场景对"什么是可点"保持同一套判定。
"""

HELPERS_JS = r"""
const __TAGS = new Set(['A', 'BUTTON', 'INPUT', 'SELECT', 'TEXTAREA', 'SUMMARY', 'OPTION', 'LABEL']);
const __ROLES = new Set(['button', 'link', 'menuitem', 'menuitemradio', 'menuitemcheckbox', 'tab',
  'treeitem', 'option', 'checkbox', 'radio', 'switch', 'combobox', 'searchbox', 'textbox', 'slider']);
const __rectOK = el => {
  const r = el.getBoundingClientRect();
  return r.width > 2 && r.height > 2 && r.bottom > 0 && r.top < innerHeight
      && r.right > 0 && r.left < innerWidth;
};
const __vis = el => {
  if (!__rectOK(el)) return false;
  const cs = getComputedStyle(el);
  if (cs.display === 'none' || cs.visibility === 'hidden' || +cs.opacity < 0.1) return false;
  return el.getAttribute('aria-hidden') !== 'true';
};
const __label = el => (el.getAttribute('aria-label') || el.innerText || el.value
  || el.placeholder || el.title || el.alt || '').trim();
const __inter = el => {
  const type = (el.type || '').toLowerCase();
  if (el.tagName === 'INPUT' && type === 'hidden') return false;
  if (__TAGS.has(el.tagName)) return true;
  if (__ROLES.has((el.getAttribute('role') || '').toLowerCase())) return true;
  const ti = el.getAttribute('tabindex');
  if (ti !== null && ti !== '-1') return true;
  if (el.hasAttribute('onclick') || el.isContentEditable) return true;
  try {
    if (getComputedStyle(el).cursor === 'pointer') return true;
  } catch (e) { /* 未知伪元素样式取不到，按不可交互处理 */ }
  // 菜单/导航类命名约定（通用 Web 约定而非站点规则）：兜住没写手型光标的侧边栏菜单
  const cls = typeof el.className === 'string' ? el.className : '';
  if (/(^|[\s_-])(menu|submenu|nav|dropdown|tab|tree|breadcrumb)([\s_-]|$)/i.test(cls)) return true;
  return false;
};
const __topEl = el => {
  const r = el.getBoundingClientRect();
  const cx = Math.min(Math.max(r.x + r.width / 2, 1), innerWidth - 1);
  const cy = Math.min(Math.max(r.y + r.height / 2, 1), innerHeight - 1);
  const t = document.elementFromPoint(cx, cy);
  return !t || t === el || el.contains(t) || t.contains(el);
};
const __sel = el => {
  const tag = el.tagName.toLowerCase();
  if (el.id) return '#' + CSS.escape(el.id);
  for (const a of ['data-testid', 'data-test', 'name']) {
    const v = el.getAttribute(a);
    if (v) return tag + '[' + a + '="' + String(v).slice(0, 40).replace(/"/g, '') + '"]';
  }
  if (tag === 'input' && el.placeholder)
    return tag + '[placeholder="' + el.placeholder.slice(0, 30).replace(/"/g, '') + '"]';
  if (tag === 'input' && el.type && el.type !== 'text')
    return tag + '[type="' + el.type + '"]';
  const aria = el.getAttribute('aria-label');
  if (aria && aria.length <= 30)
    return tag + '[aria-label="' + aria.replace(/"/g, '') + '"]';
  const text = (el.innerText || '').trim().replace(/\\s+/g, ' ');
  if (text && text.length <= 24)
    return 'text="' + text.replace(/"/g, '') + '"';
  const peers = [...document.querySelectorAll(tag)].filter(e => __vis(e));
  return tag + ' >> nth=' + peers.indexOf(el);
};
"""
