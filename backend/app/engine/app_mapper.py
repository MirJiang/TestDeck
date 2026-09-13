"""应用地图爬取器：扫描被测系统，产出页面清单、页面关系（链接）与按钮（含禁用状态）。

用途：① 给 AI 主导测试注入"系统地图"先验知识（见 app_map_brief）；
     ② 页面/按钮变更检测（重扫对比 + 执行比对信号 map_gaps）；③ 覆盖度视图。

地图 = 期望模型/基线（所有者定调）：只由扫描（scan）、源码分析（code）、人工/upsert（manual）
写入，执行观察永远不回写地图——否则"本来该有按钮现在没有"的回归信号就消失了。

多角色扫描：勾选多个测试用户逐个 AI 登录爬取，按 path 合并；页面与元素记录 roles（谁见过）。
重扫只全量替换 scan 来源的页面/元素，code 与 manual 来源的数据保留。

流程：起始页（项目环境地址 + start_path）→（可选）AI 代劳用测试账号登录 →
同源 BFS 爬取（上限 max_pages / max_depth），每页提取链接与按钮。
"""
from urllib.parse import urljoin, urlparse

from ..db import SessionLocal
from ..models import AppPage, AppElement, AppMenu, now
from .web_interact import HELPERS_JS

_STATIC_EXT = (".js", ".css", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico",
               ".woff", ".woff2", ".ttf", ".map", ".mp4", ".webp", ".webm")

# 每页提取：链接（同源、去静态）与按钮（文本/选择器/是否禁用）。
# 可交互判定走 web_interact.HELPERS_JS 的通用信号集（标签/role/tabindex/onclick/cursor:pointer），
# 无站点特定规则；非原生标签的可点元素（JS 绑定的 div/li 菜单等）打 script 标记 →
# 落库时写 state_note，执行比对跳过、地图摘要里标注"未必常显"。
_EXTRACT_JS = """() => {
""" + HELPERS_JS + """
  const origin = location.origin;
  const norm = h => { try { const u = new URL(h, location.href);
      return u.origin === origin ? (u.pathname + (u.hash || '')) : null; } catch { return null; } };
  const links = [...document.querySelectorAll('a[href]')].slice(0, 60).map(a => ({
    text: (a.innerText || a.title || '').trim().slice(0, 40),
    href: norm(a.href) })).filter(l => l.href);
  const btns = [];
  const seen = new Set();
  for (const el of document.querySelectorAll('body *')) {
    if (btns.length >= 60) break;
    if (!__vis(el) || !__inter(el)) continue;
    const tag = el.tagName.toLowerCase();
    const type = (el.type || '').toLowerCase();
    if (tag === 'input' && !['button', 'submit'].includes(type)) continue;
    const raw = __label(el);
    if (!raw || raw.length > 40 || raw.indexOf(String.fromCharCode(10)) >= 0) continue;
    if (seen.has(raw)) continue;
    seen.add(raw);
    btns.push({ text: raw.slice(0, 40), selector: __sel(el).slice(0, 200),
                disabled: el.disabled === true || el.getAttribute('aria-disabled') === 'true',
                script: !__TAGS.has(el.tagName) });
  }
  return { title: (document.title || '').slice(0, 80), links, btns };
}"""

# 页内点击探索的候选：可见、未被遮挡的通用可交互元素（点击后观察 URL 与渲染变化）
_CLICKABLE_JS = """() => {
""" + HELPERS_JS + """
  const out = [];
  const seen = new Set();
  for (const el of document.querySelectorAll('body *')) {
    if (out.length >= 15) break;
    if (!__vis(el) || !__inter(el) || !__topEl(el)) continue;   // 被弹窗遮罩挡住的不点
    const tag = el.tagName.toLowerCase();
    const type = (el.type || '').toLowerCase();
    if (tag === 'input' && !['button', 'submit', 'search'].includes(type)) continue;
    if (tag === 'label' || tag === 'option' || tag === 'summary') continue;
    const label = __label(el).replace(/[ \\t\\n]+/g, ' ');
    if (!label || label.length > 24) continue;
    if (seen.has(label)) continue;
    seen.add(label);
    out.push(label);
  }
  return out;
}"""

# 探索时绝不点击的词（防误触登出/提交/审批等破坏性动作）
_DENY_WORDS = ("退出", "注销", "登出", "删除", "提交", "保存", "确定", "取消", "搜索",
               "重置", "上传", "发送", "发布", "审核", "审批", "同意", "驳回", "登录", "注册",
               "导出", "打印", "下载", "支付")


def _explorable(text: str) -> bool:
    t = (text or "").replace(" ", "").replace("　", "").strip()
    return bool(t) and not any(w in t for w in _DENY_WORDS)


def _clickables(page) -> list:
    """当前页可见可点击元素的文本清单（探索候选，去重、限 15 个）。"""
    try:
        return page.evaluate(_CLICKABLE_JS)
    except Exception:
        return []


def _dismiss_popups(page):
    """尝试关闭常见的公告/引导弹窗（关闭按钮与"我知道了"类文本），失败忽略。"""
    for sel in ('.el-dialog__headerbtn', '.ant-modal-close', '[class*="close" i]',
                '.el-dialog__footer button:last-child', '.ant-modal-footer button:last-child',
                'button:has-text("我知道了")', 'button:has-text("知道了")',
                'button:has-text("关闭")', 'button:has-text("确 定")', 'button:has-text("确定")',
                'button:has-text("取 消")'):
        try:
            el = page.locator(sel).first
            if el.is_visible(timeout=250):
                el.click(timeout=500)
                page.wait_for_timeout(200)
        except Exception:
            continue


def _logged_in(page) -> bool:
    """登录成功判定（不依赖 AI 的结论）：密码框消失且页面有实际内容。

    SPA 的 hash 路由在登录前后 URL 前缀不变，按 URL 判会误报"仍停留在登录页"。"""
    try:
        return page.evaluate("""() => {
          if (document.querySelectorAll('input[type=password]').length) return false;
          const body = document.body ? document.body.innerText : '';
          return body.trim().length > 50;
        }""")
    except Exception:
        return False

# 执行比对（map_gaps）：在 DOM 里找一组按钮文本，返回缺失清单。
# 用 DOM 全文匹配而不是 ai_drive 的 state.elements——后者截断到前 40 个可见元素，会误报。
_GAPS_JS = """(texts) => {
  const label = el => String(el.innerText || el.value || el.title ||
    el.getAttribute('aria-label') || '').trim();
  const all = [...document.querySelectorAll(
    'button, input[type=submit], input[type=button], [role=button], a')].map(label);
  const body = document.body ? document.body.innerText : '';
  return texts.filter(t => !all.some(l => l && l.includes(t)) && !body.includes(t));
}"""


def _norm_path(url: str, origin: str) -> str | None:
    """同源 URL → 页面标识（path + hash 路由），静态资源与外域返回 None。"""
    try:
        u = urlparse(url)
        if u.scheme not in ("http", "https") or u.netloc != urlparse(origin).netloc:
            return None
        path = u.path or "/"
        if path.lower().endswith(_STATIC_EXT):
            return None
        return path + (u.fragment and "#" + u.fragment or "")
    except Exception:
        return None


def _page_key(url: str) -> str:
    """任意 URL → 地图页面标识（不校验同源，执行比对用）。"""
    try:
        u = urlparse(url)
        return (u.path or "/") + (u.fragment and "#" + u.fragment or "")
    except Exception:
        return ""


def _visit(page, base: str, path: str) -> bool:
    """goto 页面 + 等 SPA 渲染 + 关公告弹窗。失败返回 False。"""
    try:
        page.goto(urljoin(base, path), timeout=20000, wait_until="domcontentloaded")
        page.wait_for_timeout(600)
        _dismiss_popups(page)
        return True
    except Exception:
        return False


def _page_info(page) -> dict:
    """当前页的提取结果（title/links/btns），depth 由调用方回填。"""
    info = page.evaluate(_EXTRACT_JS)
    return {"title": info.get("title", ""), "depth": 0,
            "links": info.get("links", []), "btns": info.get("btns", [])}


def _crawl(base: str, start: str, origin: str, role: str, username: str, password: str,
           max_pages: int, max_depth: int) -> tuple[dict, str]:
    """单角色爬取：登录（可选）→ 点击探索 DFS。返回 (pages, note, menus)。

    SPA 的菜单大多不是 <a href>（JS 绑定的 li/span），只跟链接会"看不见"整个侧边栏——
    所以除链接外逐个点击可见可点元素：URL 变了就把新页面入栈深入，没变就重提取
    （子菜单展开会出现新条目）。菜单项以 li/role=menuitem 识别并打 menu 标记。
    """
    import time as _time

    from playwright.sync_api import sync_playwright

    note = ""
    pages: dict[str, dict] = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1280, "height": 800})
        page = ctx.new_page()
        try:
            page.goto(start, timeout=30000)

            # 可选登录：交给 AI 代劳一轮（平台哲学：动态页面不写死脚本）。
            # 验证码识别有失败率 → 失败重载页面换一张验证码再试一次；
            # 成败不采信 AI 的结论，由爬取器按页面内容自行判定。
            if username:
                from .ai_runner import ai_drive
                goal = ("用账号 ${username} 和密码 ${password} 登录当前系统。"
                        "提交登录并通过验证码后，只要页面出现登录后的内容（欢迎语/菜单/工作台）即算成功"
                        "——hash 路由下地址前缀不变是正常的，不要据此判失败。")
                variables = {"username": username, "password": password}
                for _attempt in range(2):
                    ai_drive(page, goal, variables, max_steps=15,
                             run_id=f"map{int(_time.time() * 1000) % 10**9:09d}",
                             shot_tag="login")
                    page.wait_for_timeout(800)
                    if _logged_in(page):
                        break
                    try:   # 换一张验证码重来
                        page.goto(start, timeout=30000)
                        page.wait_for_timeout(600)
                    except Exception:
                        break
                if not _logged_in(page):
                    note = f"{role}登录未完成"

            deadline = _time.time() + 480          # 硬性墙钟预算，防前端 10 分钟超时
            state = {"count": 0, "clicked": set(), "menus": {}}

            def dfs(path: str, depth: int, entry: str = ""):
                if (path in pages or state["count"] >= max_pages
                        or depth > max_depth or _time.time() > deadline):
                    return
                if not _visit(page, base, path):
                    return
                pages[path] = _page_info(page)
                pages[path]["entry"] = entry
                state["count"] += 1
                if depth >= max_depth or _time.time() > deadline:
                    return
                info = pages[path]
                pending = ([("click", t) for t in _clickables(page) if _explorable(t)]
                           + [("goto", lk["href"]) for lk in info["links"]
                              if lk.get("href") and not lk["href"].startswith("#")])[:20]
                import os
                done_here = set()
                while pending and state["count"] < max_pages and _time.time() <= deadline:
                    kind, target = pending.pop(0)
                    key = (kind, path, target)
                    if key in state["clicked"] or key in done_here:
                        continue
                    done_here.add(key); state["clicked"].add(key)
                    if os.environ.get("TD_SCAN_DEBUG"):
                        print(f"  [scan] {path} click={target!r}", flush=True)
                    if kind == "goto":
                        try:
                            page.goto(urljoin(base, target), timeout=20000,
                                      wait_until="domcontentloaded")
                        except Exception:
                            continue
                    else:
                        try:
                            page.locator(f'text="{target}"').first.click(timeout=2000)
                        except Exception:
                            continue
                    page.wait_for_timeout(1200)
                    _dismiss_popups(page)
                    new_path = _norm_path(page.url, origin)
                    if os.environ.get("TD_SCAN_DEBUG"):
                        hit = page.evaluate(
                            "() => [...document.querySelectorAll('*')].some(e => e.children.length === 0"
                            " && (e.textContent || '').includes('汽运询价单'))")
                        print(f"  [scan]   → new_path={new_path!r} DOM含汽运询价单={hit}", flush=True)
                    if new_path and new_path != path and new_path not in pages:
                        dfs(new_path, depth + 1, entry if kind == "goto" else target)
                        _visit(page, base, path)     # 回到本页继续探索剩余可点项
                    elif kind == "click":
                        # 未跳转：点开的可能是子菜单/面板。新元素合并进地图（AI 先验），
                        # 新条目顺带点一层把功能页按钮也并入；随后 Esc 收起弹层。
                        known = {(b.get("text"), b.get("selector")) for b in info["btns"]}
                        fresh = [b for b in _page_info(page)["btns"]
                                 if (b.get("text"), b.get("selector")) not in known]
                        for b in fresh:
                            known.add((b.get("text"), b.get("selector")))
                            info["btns"].append(b)
                            if b.get("script") and b.get("text") and not b["text"].isdigit():
                                ts = state["menus"].setdefault(target, [])
                                if b["text"] not in ts:
                                    ts.append(b["text"])
                            if os.environ.get("TD_SCAN_DEBUG") and b.get("text"):
                                print(f"  [scan]   ✓ 新元素 {b.get('text')!r}", flush=True)
                        for b in [x for x in fresh if x.get("text") and _explorable(x["text"])][:6]:
                            try:
                                page.locator(f'text="{b["text"]}"').first.click(timeout=2000)
                            except Exception:
                                continue
                            page.wait_for_timeout(1000)
                            _dismiss_popups(page)
                            np2 = _norm_path(page.url, origin)
                            if np2 and np2 != path and np2 not in pages:
                                dfs(np2, depth + 1, entry=b["text"])
                                _visit(page, base, path)
                                break    # 回来后子菜单已折叠，剩余新条目点不到，跳出
                            for b2 in _page_info(page)["btns"]:
                                k2 = (b2.get("text"), b2.get("selector"))
                                if k2 not in known:
                                    known.add(k2)
                                    info["btns"].append(b2)
                                    if b2.get("script") and b2.get("text") and not b2["text"].isdigit():
                                        ts = state["menus"].setdefault(b["text"], [])
                                        if b2["text"] not in ts:
                                            ts.append(b2["text"])
                                    if os.environ.get("TD_SCAN_DEBUG") and b2.get("text"):
                                        print(f"  [scan]   ✓ 深层新元素 {b2.get('text')!r}", flush=True)
                        try:
                            page.keyboard.press("Escape")   # 收起弹层/下拉
                            page.wait_for_timeout(200)
                        except Exception:
                            pass

            dfs(_norm_path(page.url, origin) or "/", 0)
        finally:
            browser.close()
    menus = [{"parent": p, "text": t} for p, ts in state["menus"].items() for t in ts]
    return pages, note, menus


def _merge_roles(merged: dict, pages: dict, role: str):
    """把单角色爬取结果并进 merged：path -> {title, depth, roles, btns{(text,sel):{...}}, links{(text,href):{...}}}。"""
    for path, info in pages.items():
        m = merged.setdefault(path, {"title": "", "depth": info["depth"], "roles": [],
                                     "btns": {}, "links": {}, "entry": info.get("entry", "")})
        m["depth"] = min(m["depth"], info["depth"])
        if not m.get("entry") and info.get("entry"):
            m["entry"] = info["entry"]
        if info.get("title") and not m["title"]:
            m["title"] = info["title"]
        if role not in m["roles"]:
            m["roles"].append(role)
        for b in info.get("btns", []):
            key = (b.get("text", ""), b.get("selector", ""))
            e = m["btns"].setdefault(key, {"disabled": True, "script": False, "roles": []})
            e["disabled"] = e["disabled"] and bool(b.get("disabled"))
            e["script"] = e["script"] or bool(b.get("script"))   # 任一角色可点即算可点
            if role not in e["roles"]:
                e["roles"].append(role)
        for lk in info.get("links", []):
            key = (lk.get("text", ""), lk.get("href", ""))
            e = m["links"].setdefault(key, {"roles": []})
            if role not in e["roles"]:
                e["roles"].append(role)


def scan_app(base_url: str, project_id: str, start_path: str = "",
             users: list[dict] | None = None,
             max_pages: int = 25, max_depth: int = 3) -> dict:
    """同步多角色爬取（由 API 在线程池调用）。

    users: [{name, username, password}, ...]，空列表 = 不登录匿名扫一轮。
    落库只替换 scan 来源的页面/元素，code/manual 来源保留。
    返回 {pages, elements, roles, skipped, note}。
    """
    base = (base_url or "").rstrip("/")
    if not base.startswith(("http://", "https://")):
        return {"pages": 0, "elements": 0, "error": "项目未配置有效的环境地址"}
    start = base + ((start_path.startswith("/") or start_path.startswith("#")) and start_path or
                    (start_path and "/" + start_path or ""))

    crawls = users or [{"name": "匿名", "username": "", "password": ""}]
    merged: dict[str, dict] = {}
    merged_menus: dict = {}
    merged_roles: list[str] = []
    notes = []
    for u in crawls:
        role = (u.get("name") or u.get("username") or "匿名").strip() or "匿名"
        merged_roles.append(role)
        pages, note, role_menus = _crawl(base, start, base, role,
                             u.get("username", ""), u.get("password", ""), max_pages, max_depth)
        _merge_roles(merged, pages, role)
        for m in role_menus:
            key = (m["parent"], m["text"])
            if key not in merged_menus:
                merged_menus[key] = m
        if note:
            notes.append(note)

    # 落库：scan 来源全量替换，其余来源保留
    elements_n = 0
    db = SessionLocal()
    try:
        existing = {pg.path: pg for pg in db.query(AppPage).filter(AppPage.project_id == project_id)}
        # 本次没扫到的 scan 页删掉（系统里已不存在）；code/manual 页不动
        for path, pg in existing.items():
            if path not in merged and (pg.source or "scan") == "scan":
                db.query(AppElement).filter(AppElement.page_id == pg.id).delete()
                db.delete(pg)
        for path, info in merged.items():
            roles = ",".join(info["roles"])[:255]
            pg = existing.get(path)
            if pg:
                pg.title = info["title"] or pg.title
                pg.depth = info["depth"]
                pg.roles = _merge_csv(pg.roles, roles)
                pg.entry = info.get("entry") or pg.entry or ""
                pg.scanned_at = now()
                db.query(AppElement).filter(
                    AppElement.page_id == pg.id, AppElement.source == "scan").delete()
            else:
                pg = AppPage(project_id=project_id, path=path[:512], title=info["title"],
                             depth=info["depth"], source="scan", roles=roles,
                             entry=info.get("entry", ""))
                db.add(pg)
                db.flush()
            for (text, sel), e in info["btns"].items():
                if not text and not sel:
                    continue
                db.add(AppElement(page_id=pg.id, kind="button", text=text, selector=sel[:512],
                                  disabled=e["disabled"], source="scan",
                                  state_note="JS 绑定元素（未必常显）" if e.get("script") else "",
                                  roles=",".join(e["roles"])[:255]))
                elements_n += 1
            for (text, href), e in info["links"].items():
                db.add(AppElement(page_id=pg.id, kind="link", text=text, href=href[:512],
                                  source="scan", roles=",".join(e["roles"])[:255]))
                elements_n += 1
        db.commit()
    finally:
        db.close()
    db.query(AppMenu).filter(AppMenu.project_id == project_id).delete()
    for (parent, text) in merged_menus:
        db.add(AppMenu(project_id=project_id, parent=parent[:255], text=text[:255]))
    db.commit()
    note = "；".join(notes)
    if note:
        note += "，地图可能只含部分角色可见页面"
    return {"pages": len(merged), "elements": elements_n, "menus": len(merged_menus),
            "roles": [r for r in merged_roles], "note": note}


def _merge_csv(old: str, new: str) -> str:
    """逗号分隔列表合并去重（角色名累积）。"""
    seen = [s for s in (old or "").split(",") if s]
    for s in (new or "").split(","):
        if s and s not in seen:
            seen.append(s)
    return ",".join(seen)[:255]


def map_gaps(project_id: str, url: str, page) -> str:
    """执行比对信号：当前页在地图中有记录时，检查地图按钮是否还在 DOM 里。

    返回警告文本（空 = 无异常）。state_note 非空的元素是条件性出现的，跳过不比。
    只报缺失（期望有而实际没有），执行中新出现的元素不回写地图。
    """
    if not project_id:
        return ""
    key = _page_key(url or "")
    if not key:
        return ""
    db = SessionLocal()
    try:
        pg = db.query(AppPage).filter(AppPage.project_id == project_id, AppPage.path == key).first()
        if not pg:
            return ""
        rows = db.query(AppElement).filter(
            AppElement.page_id == pg.id, AppElement.kind == "button").all()
        expect = {}   # text -> source（同文本取来源优先级最高的展示）
        for e in rows:
            t = (e.text or "").strip()
            if t and not (e.state_note or "").strip():
                expect[t] = e.source or "scan"
        if not expect:
            return ""
        texts = list(expect.keys())
    finally:
        db.close()
    try:
        missing = page.evaluate(_GAPS_JS, texts)
    except Exception:
        return ""
    if not missing:
        return ""
    shown = "、".join(f"{t}（{expect[t]}）" for t in missing[:5])
    more = f" 等 {len(missing)} 项" if len(missing) > 5 else ""
    return f"地图按钮在当前页面缺失：{shown}{more}"


def upsert_map(project_id: str, pages: list[dict], source: str = "manual") -> dict:
    """合并写入地图（REST 与 MCP 共用）。

    pages: [{path, title?, depth?, elements: [{kind, text, selector?, href?, disabled?, state_note?}]}]
    页面按 path 匹配：存在则更新 title/depth 并对元素做同键 upsert；不存在则新建。
    元素同键 = kind+text（无文本元素按 kind+selector/href）——与执行比对（按文本找按钮）口径一致。
    已存在元素的 source/state_note 不被覆盖为更弱的来源（code > manual > scan），
    state_note 只在传入非空时更新。返回 {pages, elements, created, updated}。
    """
    source = source if source in ("scan", "code", "manual") else "manual"
    rank = {"scan": 0, "manual": 1, "code": 2}

    def _el_key(kind: str, text: str, selector: str, href: str):
        return (kind, text) if text else (kind, selector or href)

    created = updated = elements_n = 0
    db = SessionLocal()
    try:
        existing = {pg.path: pg for pg in db.query(AppPage).filter(AppPage.project_id == project_id)}
        for item in pages or []:
            path = str(item.get("path") or "").strip()[:512]
            if not path:
                continue
            pg = existing.get(path)
            if pg:
                if item.get("title"):
                    pg.title = str(item["title"])[:255]
                if item.get("depth") is not None:
                    try:
                        pg.depth = int(item["depth"])
                    except (TypeError, ValueError):
                        pass
                if rank.get(source, 1) >= rank.get(pg.source or "scan", 0):
                    pg.source = source
                updated += 1
            else:
                pg = AppPage(project_id=project_id, path=path,
                             title=str(item.get("title") or "")[:255],
                             depth=int(item.get("depth") or 0), source=source)
                db.add(pg)
                db.flush()
                existing[path] = pg
                created += 1
            els = db.query(AppElement).filter(AppElement.page_id == pg.id).all()
            by_key = {_el_key(e.kind, (e.text or "").strip(), (e.selector or "").strip(),
                              (e.href or "").strip()): e for e in els}
            for el in item.get("elements") or []:
                kind = "link" if str(el.get("kind", "button")).lower() == "link" else "button"
                text = str(el.get("text") or "").strip()[:255]
                selector = str(el.get("selector") or "").strip()[:512]
                href = str(el.get("href") or "").strip()[:512]
                row = by_key.get(_el_key(kind, text, selector, href))
                if row:
                    if selector:
                        row.selector = selector
                    if href:
                        row.href = href
                    if el.get("disabled") is not None:
                        row.disabled = bool(el["disabled"])
                    if str(el.get("state_note") or "").strip():
                        row.state_note = str(el["state_note"]).strip()[:255]
                    if rank.get(source, 1) >= rank.get(row.source or "scan", 0):
                        row.source = source
                else:
                    row = AppElement(page_id=pg.id, kind=kind, text=text, selector=selector,
                                     href=href, disabled=bool(el.get("disabled")),
                                     source=source,
                                     state_note=str(el.get("state_note") or "").strip()[:255])
                    db.add(row)
                    by_key[_el_key(kind, text, selector, href)] = row
                elements_n += 1
        db.commit()
    finally:
        db.close()
    return {"pages": len(pages or []), "elements": elements_n,
            "created": created, "updated": updated, "source": source}


def app_map_brief(project_id: str, max_pages: int = 20) -> str:
    """给 AI 提示词注入的系统地图摘要：页面清单 + 每页按钮（供 ai_drive 做先验知识）。"""
    db = SessionLocal()
    try:
        rows = db.query(AppPage).filter(AppPage.project_id == project_id).order_by(AppPage.depth).limit(max_pages)
        if not rows.count():
            return ""
        out = ["系统页面地图（路径 → 标题；〔按钮〕可点，〔禁用〕当前不可点；→ 指跳转；※ 为条件性出现）："]
        for pg in rows:
            els = db.query(AppElement).filter(AppElement.page_id == pg.id).all()
            btns = "、".join(
                f"{e.text or e.selector}{'（禁用）' if e.disabled else ''}{'※' + e.state_note if (e.state_note or '').strip() else ''}"
                for e in els if e.kind == "button")[:500]
            roles = f"［{pg.roles}］" if (pg.roles or "").strip() else ""
            out.append(f"- {pg.path}（{pg.title}）{roles}{('；按钮：' + btns) if btns else ''}")
        return "\n".join(out)[:3000]
    finally:
        db.close()


def map_page_keys(project_id: str, max_pages: int = 60) -> set:
    """项目地图已收录的页面标识集合（key = _page_key 的产物）。

    给 AI 执行时判定"当前页是否地图已知页"用：已知页的页面文字与地图/元素清单
    高度重复，状态回包可省略以省 token。
    """
    if not project_id:
        return set()
    db = SessionLocal()
    try:
        return {r[0] for r in db.query(AppPage.path).filter(
            AppPage.project_id == project_id).limit(max_pages) if r[0]}
    finally:
        db.close()
