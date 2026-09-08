"""应用地图：多角色扫描合并、期望基线 upsert、执行比对信号（不启动浏览器）。"""
import pytest
from fastapi.testclient import TestClient

from app import config
config.set("TD_DB", ":memory:")

from app.main import app  # noqa
from app.db import SessionLocal  # noqa
from app.models import AppPage, AppElement  # noqa
from app.engine import app_mapper  # noqa
from app.engine.app_mapper import upsert_map, map_gaps, scan_app  # noqa


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def token(client):
    r = client.post("/api/v1/auth/login", json={"username": "admin", "password": "admin123"})
    assert r.status_code == 200
    return r.json()["token"]


def H(token):
    return {"Authorization": f"Bearer {token}"}


def _mk_project(client, token, name="地图项目"):
    return client.post("/api/v1/projects", json={"name": name, "desc": ""},
                       headers=H(token)).json()["id"]


# ---------- A3：upsert 通道（REST + 合并语义） ----------

def test_upsert_rest_and_get(client, token):
    pid = _mk_project(client, token, "upsert-rest")
    r = client.post(f"/api/v1/projects/{pid}/app-map/upsert", headers=H(token), json={
        "source": "code",
        "pages": [{"path": "/orders", "title": "订单", "depth": 1, "elements": [
            {"kind": "button", "text": "新建订单", "selector": "#btn-new"},
            {"kind": "button", "text": "审批", "selector": "", "state_note": "仅审批岗可见"},
            {"kind": "link", "text": "详情", "href": "/orders/detail"}]}]})
    assert r.status_code == 200
    assert r.json() == {"pages": 1, "elements": 3, "created": 1, "updated": 0, "source": "code"}

    m = client.get(f"/api/v1/projects/{pid}/app-map", headers=H(token)).json()
    assert len(m) == 1 and m[0]["source"] == "code" and m[0]["path"] == "/orders"
    btns = {b["text"]: b for b in m[0]["buttons"]}
    assert btns["新建订单"]["source"] == "code"
    assert btns["审批"]["state_note"] == "仅审批岗可见"
    assert m[0]["links"][0]["href"] == "/orders/detail"


def test_upsert_rejects_empty_and_bad_source(client, token):
    pid = _mk_project(client, token, "upsert-bad")
    r = client.post(f"/api/v1/projects/{pid}/app-map/upsert", headers=H(token),
                    json={"source": "code", "pages": []})
    assert r.status_code == 400
    r = client.post(f"/api/v1/projects/{pid}/app-map/upsert", headers=H(token), json={
        "source": "hacker",   # 非法来源按 manual 处理
        "pages": [{"path": "/x", "elements": [{"kind": "button", "text": "b"}]}]})
    assert r.json()["source"] == "manual"


def test_upsert_merge_semantics(client, token):
    """同键元素更新不新增；弱来源不覆盖强来源；state_note 只在传入非空时更新。"""
    pid = _mk_project(client, token, "upsert-merge")
    upsert_map(pid, [{"path": "/p", "title": "旧标题", "elements": [
        {"kind": "button", "text": "提交", "selector": "#old", "state_note": "草稿态可见"}]}], "code")
    out = upsert_map(pid, [{"path": "/p", "title": "新标题", "elements": [
        {"kind": "button", "text": "提交", "selector": "#new"},          # 同键：更新 selector
        {"kind": "button", "text": "取消"}]}], "manual")                  # 新元素：manual
    assert out == {"pages": 1, "elements": 2, "created": 0, "updated": 1, "source": "manual"}
    m = client.get(f"/api/v1/projects/{pid}/app-map", headers=H(token)).json()[0]
    assert m["title"] == "新标题" and m["source"] == "code"    # code > manual，页面来源不被弱化
    btns = {b["text"]: b for b in m["buttons"]}
    assert btns["提交"]["selector"] == "#new"
    assert btns["提交"]["source"] == "code"
    assert btns["提交"]["state_note"] == "草稿态可见"          # 空 state_note 不覆盖已有备注
    assert btns["取消"]["source"] == "manual"


# ---------- A1：多角色扫描合并 ----------

def _fake_crawl_factory(per_role):
    def fake_crawl(base, start, origin, role, username, password, max_pages, max_depth):
        return per_role[role], ""
    return fake_crawl


def test_scan_multi_role_merge(monkeypatch, client, token):
    pid = _mk_project(client, token, "scan-merge")
    # 预埋：code 来源页面（重扫须保留）+ 旧 scan 页面（本次没扫到，须删除）
    upsert_map(pid, [{"path": "/from-code", "elements": [{"kind": "button", "text": "码"}]}], "code")
    db = SessionLocal()
    gone = AppPage(project_id=pid, path="/gone", title="旧扫描页", source="scan")
    db.add(gone); db.commit(); db.close()

    per_role = {
        "货主": {
            "/home": {"title": "首页", "depth": 0,
                      "links": [{"text": "订单", "href": "/orders"}],
                      "btns": [{"text": "新建", "selector": "", "disabled": True},
                               {"text": "导出", "selector": "", "disabled": True}]},
            "/orders": {"title": "订单", "depth": 1, "links": [], "btns": []},
        },
        "审批员": {
            "/home": {"title": "首页", "depth": 0, "links": [],
                      "btns": [{"text": "新建", "selector": "", "disabled": False},
                               {"text": "审批", "selector": "", "disabled": False}]},
            "/audit": {"title": "待审", "depth": 1, "links": [], "btns": []},
        },
    }
    monkeypatch.setattr(app_mapper, "_crawl", _fake_crawl_factory(per_role))
    r = scan_app("http://x", pid, "", [{"name": "货主", "username": "a", "password": "p"},
                                       {"name": "审批员", "username": "b", "password": "p"}],
                 max_pages=10)
    assert r["pages"] == 3 and r["roles"] == ["货主", "审批员"]

    m = {pg["path"]: pg for pg in client.get(f"/api/v1/projects/{pid}/app-map", headers=H(token)).json()}
    assert set(m) == {"/home", "/orders", "/audit", "/from-code"}   # code 保留、/gone 删除
    home = m["/home"]
    assert set(home["roles"].split(",")) == {"货主", "审批员"}
    btns = {b["text"]: b for b in home["buttons"]}
    assert btns["新建"]["disabled"] is False                  # 任一角色可点即算可点
    assert set(btns["新建"]["roles"].split(",")) == {"货主", "审批员"}
    assert btns["审批"]["roles"] == "审批员"                   # 角色可见性记录
    assert btns["导出"]["disabled"] is True
    assert m["/from-code"]["source"] == "code"


def test_scan_login_failure_note(monkeypatch, client, token):
    pid = _mk_project(client, token, "scan-note")

    def fake_crawl(base, start, origin, role, username, password, max_pages, max_depth):
        return {"/home": {"title": "首页", "depth": 0, "links": [], "btns": []}}, \
            f"{role}登录未完成(验证码失败)"
    monkeypatch.setattr(app_mapper, "_crawl", fake_crawl)
    r = scan_app("http://x", pid, "", [{"name": "货主", "username": "a", "password": "p"}])
    assert "货主登录未完成" in r["note"] and "部分角色可见" in r["note"]


# ---------- A2：执行比对信号 ----------

class _GapPage:
    """map_gaps 用到的最小页面：evaluate(js, texts) 返回缺失清单。"""

    def __init__(self, missing):
        self.missing = missing
        self.asked = []

    def evaluate(self, js, arg=None):
        self.asked.append(arg)
        return [t for t in (arg or []) if t in self.missing]


def test_map_gaps_warns_missing_and_skips_state_note(client, token):
    pid = _mk_project(client, token, "gaps")
    upsert_map(pid, [{"path": "/orders", "elements": [
        {"kind": "button", "text": "新建"},
        {"kind": "button", "text": "导出"},
        {"kind": "button", "text": "审批", "state_note": "仅审批岗可见"}]}], "scan")
    page = _GapPage(missing={"导出"})
    w = map_gaps(pid, "http://x/orders?kw=1", page)
    assert "导出" in w and "地图按钮" in w
    # state_note 元素条件性出现：不参与比对
    assert set(page.asked[0]) == {"新建", "导出"}


def test_map_gaps_silent_cases(client, token):
    pid = _mk_project(client, token, "gaps-silent")
    upsert_map(pid, [{"path": "/orders", "elements": [{"kind": "button", "text": "新建"}]}], "scan")
    assert map_gaps("", "http://x/orders", _GapPage({"新建"})) == ""      # 无项目不比
    assert map_gaps(pid, "http://x/unknown", _GapPage({"新建"})) == ""    # 地图没有该页
    assert map_gaps(pid, "http://x/orders", _GapPage(set())) == ""        # 按钮都在


def test_ai_drive_records_warning(client, token, monkeypatch):
    """ai_drive 每步比对：地图按钮缺失记 warning，不判失败不计数；同一缺失只报一次。"""
    from app.engine.ai_runner import ai_drive

    pid = _mk_project(client, token, "gaps-drive")
    upsert_map(pid, [{"path": "/login", "elements": [{"kind": "button", "text": "忘记密码"}]}], "scan")

    state = {"url": "http://t/login", "title": "登录",
             "elements": [{"selector": "#btn", "tag": "button", "text": "登 录", "value": ""}],
             "text": "登录"}

    class Page:
        mouse = None

        def evaluate(self, js, arg=None):
            if arg is None:
                return state
            return [t for t in arg if t == "忘记密码"]

        def goto(self, url, timeout=0):
            pass

        def click(self, sel, timeout=0):
            pass

        def inner_text(self, sel, timeout=0):
            return "登录成功"

        def screenshot(self, path="", full_page=False, type=None, quality=None):
            return b"shot"

    script = iter([
        {"action": "click", "selector": "#btn", "think": "点登录"},
        {"action": "expect_text", "value": "登录成功", "think": "验证"},
        {"action": "done", "pass": True, "reason": "ok"},
    ])
    monkeypatch.setattr("app.ai.llm_available", lambda: True)
    monkeypatch.setattr("app.ai.chat_json_sync", lambda *a, **k: next(script))

    r = ai_drive(Page(), "登录", {}, max_steps=6, project_id=pid)
    assert r["status"] == "passed" and r["fail_n"] == 0        # 警告不影响判定
    warns = [d.get("warning", "") for d in r["detail"]]
    assert "忘记密码" in warns[0]                               # 第一步报缺失
    assert warns[1] == "" and warns[2] == ""                    # 同一缺失不重复刷屏
