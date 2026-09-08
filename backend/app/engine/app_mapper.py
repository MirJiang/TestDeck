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
from ..models import AppPage, AppElement, now

_STATIC_EXT = (".js", ".css", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico",
               ".woff", ".woff2", ".ttf", ".map", ".mp4", ".webp", ".webm")

# 每页提取：链接（同源、去静态）与按钮（文本/选择器/是否禁用）
_EXTRACT_JS = """() => {
  const origin = location.origin;
  const norm = h => { try { const u = new URL(h, location.href);
      return u.origin === origin ? (u.pathname + (u.hash || '')) : null; } catch { return null; } };
  const links = [...document.querySelectorAll('a[href]')].slice(0, 60).map(a => ({
    text: (a.innerText || a.title || '').trim().slice(0, 40),
    href: norm(a.href) })).filter(l => l.href);
  const btns = [...document.querySelectorAll('button, input[type=submit], input[type=button], [role=button]')].slice(0, 60).map(b => {
    const sel = b.id ? '#' + b.id : (b.name ? b.tagName.toLowerCase() + '[name="' + b.name + '"]' : '');
    return { text: (b.innerText || b.value || b.title || '').trim().slice(0, 40),
             selector: sel, disabled: b.disabled === true || b.getAttribute('aria-disabled') === 'true' };
  });
  return { title: (document.title || '').slice(0, 80), links, btns };
}"""

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


def _crawl(base: str, start: str, origin: str, role: str, username: str, password: str,
           max_pages: int, max_depth: int) -> tuple[dict, str]:
    """单角色爬取：登录（可选）→ BFS。返回 (pages, note)，pages: path -> {title, depth, links, btns}。"""
    from playwright.sync_api import sync_playwright

    note = ""
    pages: dict[str, dict] = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1280, "height": 800})
        page = ctx.new_page()
        try:
            page.goto(start, timeout=30000)

            # 可选登录：交给 AI 代劳一轮（平台哲学：动态页面不写死脚本）
            if username:
                from .ai_runner import ai_drive
                r = ai_drive(page, f"用账号 ${{username}} 和密码 ${{password}} 登录当前系统，"
                                   "直到离开登录页或出现系统首页为止",
                             {"username": username, "password": password},
                             max_steps=15, run_id=f"map{int(__import__('time').time()) % 10**9:09d}",
                             shot_tag="login")
                if r.get("status") != "passed":
                    note = f"{role}登录未完成({(r.get('summary') or '')[:60]})"

            # BFS：queue 里的都是同源 path
            queue = [(_norm_path(page.url, origin) or "/", 0)]
            while queue and len(pages) < max_pages:
                path, depth = queue.pop(0)
                if path in pages or depth > max_depth:
                    continue
                try:
                    page.goto(urljoin(base, path), timeout=20000, wait_until="domcontentloaded")
                    page.wait_for_timeout(600)   # 给 SPA 渲染留时间
                    info = page.evaluate(_EXTRACT_JS)
                except Exception:
                    continue
                pages[path] = {"title": info.get("title", ""), "depth": depth,
                               "links": info.get("links", []), "btns": info.get("btns", [])}
                if depth < max_depth:
                    for l in info.get("links", []):
                        target = l.get("href") or ""
                        if target and target not in pages and not target.startswith("#"):
                            queue.append((target, depth + 1))
        finally:
            browser.close()
    return pages, note


def _merge_roles(merged: dict, pages: dict, role: str):
    """把单角色爬取结果并进 merged：path -> {title, depth, roles, btns{(text,sel):{...}}, links{(text,href):{...}}}。"""
    for path, info in pages.items():
        m = merged.setdefault(path, {"title": "", "depth": info["depth"], "roles": [],
                                     "btns": {}, "links": {}})
        m["depth"] = min(m["depth"], info["depth"])
        if info.get("title") and not m["title"]:
            m["title"] = info["title"]
        if role not in m["roles"]:
            m["roles"].append(role)
        for b in info.get("btns", []):
            key = (b.get("text", ""), b.get("selector", ""))
            e = m["btns"].setdefault(key, {"disabled": True, "roles": []})
            e["disabled"] = e["disabled"] and bool(b.get("disabled"))   # 任一角色可点即算可点
            if role not in e["roles"]:
                e["roles"].append(role)
        for l in info.get("links", []):
            key = (l.get("text", ""), l.get("href", ""))
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
    merged_roles: list[str] = []
    notes = []
    for u in crawls:
        role = (u.get("name") or u.get("username") or "匿名").strip() or "匿名"
        merged_roles.append(role)
        pages, note = _crawl(base, start, base, role,
                             u.get("username", ""), u.get("password", ""), max_pages, max_depth)
        _merge_roles(merged, pages, role)
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
                pg.scanned_at = now()
                db.query(AppElement).filter(
                    AppElement.page_id == pg.id, AppElement.source == "scan").delete()
            else:
                pg = AppPage(project_id=project_id, path=path[:512], title=info["title"],
                             depth=info["depth"], source="scan", roles=roles)
                db.add(pg)
                db.flush()
            for (text, sel), e in info["btns"].items():
                db.add(AppElement(page_id=pg.id, kind="button", text=text, selector=sel[:512],
                                  disabled=e["disabled"], source="scan",
                                  roles=",".join(e["roles"])[:255]))
                elements_n += 1
            for (text, href), e in info["links"].items():
                db.add(AppElement(page_id=pg.id, kind="link", text=text, href=href[:512],
                                  source="scan", roles=",".join(e["roles"])[:255]))
                elements_n += 1
        db.commit()
    finally:
        db.close()
    note = "；".join(notes)
    if note:
        note += "，地图可能只含部分角色可见页面"
    return {"pages": len(merged), "elements": elements_n,
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
                for e in els if e.kind == "button")[:240]
            roles = f"［{pg.roles}］" if (pg.roles or "").strip() else ""
            out.append(f"- {pg.path}（{pg.title}）{roles}{('；按钮：' + btns) if btns else ''}")
        return "\n".join(out)[:3000]
    finally:
        db.close()
