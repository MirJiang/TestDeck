"""安全回归：XSS 转义、IDOR 拦截、登录限速、改密吊销、执行取消。"""
import os  # noqa: F401

from app import config
config.set("TD_DB", ":memory:")

from fastapi.testclient import TestClient
from app.main import app  # noqa


def _client():
    cm = TestClient(app)
    cm.__enter__()
    return cm


def _login(client, u="admin", p="admin123"):
    r = client.post("/api/v1/auth/login", json={"username": u, "password": p})
    return {"Authorization": "Bearer " + r.json()["token"]}


def _mk_project(client, H, name):
    return client.post("/api/v1/projects", json={"name": name}, headers=H).json()["id"]


def test_export_html_escapes_xss():
    """报告导出对被测系统返回的内容做 HTML 转义。"""
    client = _client()
    H = _login(client)
    pid = _mk_project(client, H, "XSS")
    eid = client.post(f"/api/v1/projects/{pid}/envs", headers=H,
                      json={"name": "E", "base_url": "http://127.0.0.1:59997"}).json()["id"]
    evil = '<img src=x onerror=alert(1)>'
    cid = client.post(f"/api/v1/projects/{pid}/cases", headers=H, json={
        "project_id": pid, "name": "x", "type": "api",
        "steps": [{"m": "GET", "url": "http://127.0.0.1:59997/x",
                   "check": {"type": "status", "expect": "200"}}]}).json()["id"]
    # 直接构造一条含恶意 reason 的执行记录（模拟被测系统返回注入内容）
    from app.db import SessionLocal
    from app.models import TestRun
    db = SessionLocal()
    run = TestRun(id="R-EVIL", case_id=cid, case_name=evil, env_name="E",
                  detail=[{"idx": 1, "m": "GET", "url": evil, "pass": False, "reason": evil, "ms": 1}])
    db.add(run); db.commit(); db.close()

    r = client.get("/api/v1/runs/R-EVIL/export", headers=H)
    assert r.status_code == 200
    assert "<img src=x" not in r.text          # 原样标签被转义
    assert "&lt;img src=x" in r.text           # 转义后的实体存在
    client.__exit__(None, None, None)


def test_idor_blocked():
    """member 访问他人项目的用例/执行记录/git 仓库均被拒。"""
    client = _client()
    H = _login(client)
    client.post("/api/v1/auth/users", headers=H, json={"username": "zw", "password": "zw12345"})
    H2 = _login(client, "zw", "zw12345")

    pid = _mk_project(client, H, "管理员私有")
    cid = client.post(f"/api/v1/projects/{pid}/cases", headers=H, json={
        "project_id": pid, "name": "c", "type": "ai", "target": "api", "goal": "g"}).json()["id"]
    rid = client.post(f"/api/v1/projects/{pid}/envs", headers=H,
                      json={"name": "E", "base_url": "http://t"}).json()["id"]
    gid = client.post("/api/v1/integrations/git/repos", headers=H, json={
        "project_id": pid, "repo_url": "http://git/x"}).json()["id"]

    assert client.get(f"/api/v1/cases/{cid}", headers=H2).status_code == 403
    assert client.put(f"/api/v1/cases/{cid}", headers=H2, json={
        "project_id": pid, "name": "hack", "type": "ai"}).status_code == 403
    assert client.post(f"/api/v1/projects/{pid}/envs", headers=H2,
                       json={"name": "x"}).status_code == 403
    assert client.put(f"/api/v1/projects/{pid}/envs/{rid}", headers=H2,
                      json={"name": "x"}).status_code == 403
    assert client.post(f"/api/v1/runs/cases/{cid}/run", headers=H2,
                       json={"env_id": rid}).status_code == 403
    assert client.get("/api/v1/integrations/git/repos", headers=H2).json() == []   # 列表只可见自己项目
    assert client.delete(f"/api/v1/integrations/git/repos/{gid}", headers=H2).status_code == 403

    # admin 不受影响
    assert client.get(f"/api/v1/cases/{cid}", headers=H).status_code == 200
    client.__exit__(None, None, None)


def test_login_throttle():
    client = _client()
    for _ in range(5):
        client.post("/api/v1/auth/login", json={"username": "nobody", "password": "wrong"})
    r = client.post("/api/v1/auth/login", json={"username": "nobody", "password": "anypass"})
    assert r.status_code == 429                    # 锁定期间即使密码正确也拒绝
    client.__exit__(None, None, None)


def test_password_change_revokes_token():
    client = _client()
    H_admin = _login(client)
    client.post("/api/v1/auth/users", headers=H_admin,
                json={"username": "tmp1", "password": "tmp12345"})
    H_tmp = _login(client, "tmp1", "tmp12345")
    assert client.get("/api/v1/auth/me", headers=H_tmp).status_code == 200

    # 改密后旧 token 失效
    client.put("/api/v1/auth/password", headers=H_tmp,
               json={"old_password": "tmp12345", "new_password": "new12345"})
    assert client.get("/api/v1/auth/me", headers=H_tmp).status_code == 401
    # 新密码可登录
    H_new = _login(client, "tmp1", "new12345")
    assert client.get("/api/v1/auth/me", headers=H_new).status_code == 200
    client.__exit__(None, None, None)


def test_cancel_flag():
    from app.engine import queue
    queue.register("R-X")
    assert queue.is_cancelled("R-X") is False
    queue.cancel("R-X")
    assert queue.is_cancelled("R-X") is True
    queue.unregister("R-X")
    assert queue.is_cancelled("R-X") is False


def test_credentials_encrypted_at_rest():
    """API Key 与告警 webhook 落库为密文（enc:v1:），接口读回是可用明文。"""
    from sqlalchemy import text
    from app.db import engine, SessionLocal
    from app.models import LLMConfig, NotifyChannel
    client = _client()
    H = _login(client)
    r = client.post("/api/v1/settings/llm", json={
        "name": "加密测试", "vendor": "X", "url_type": "api",
        "base_url": "https://api.x.com/v1", "api_key": "sk-secret-123456",
        "model": "m1", "vision": False}, headers=H)
    assert r.status_code == 200
    cid = r.json()["id"]
    raw = engine.connect().execute(
        text("SELECT api_key FROM llm_configs WHERE id = :i"), {"i": cid}).scalar()
    assert raw.startswith("enc:v1:") and "sk-secret-123456" not in raw   # 库里是密文

    n = client.post("/api/v1/settings/notify", json={
        "name": "群", "url": "https://oapi.dingtalk.com/robot/send?access_token=tok123"}, headers=H)
    assert n.status_code == 200
    raw_url = engine.connect().execute(
        text("SELECT url FROM notify_channels WHERE id = :i"), {"i": n.json()["id"]}).scalar()
    assert raw_url.startswith("enc:v1:") and "tok123" not in raw_url

    db = SessionLocal()
    assert db.get(LLMConfig, cid).api_key == "sk-secret-123456"          # ORM 读取透明解密可用
    assert "tok123" in db.get(NotifyChannel, n.json()["id"]).url
    db.close()
    client.__exit__(None, None, None)


def test_crypto_roundtrip_and_legacy(monkeypatch):
    """加密往返一致；存量明文（无前缀）原样透传；密钥轮换后密文解不开返回空。"""
    from app import crypto
    enc = crypto.encrypt("sk-abc")
    assert enc.startswith("enc:v1:") and enc != "sk-abc"
    assert crypto.decrypt(enc) == "sk-abc"
    assert crypto.decrypt("sk-plain-legacy") == "sk-plain-legacy"    # 存量明文透传
    assert crypto.encrypt("") == "" and crypto.decrypt("") == ""
    # 模拟密钥轮换：换掉 JWT 密钥来源
    monkeypatch.setattr("app.auth._secret", lambda: "another-secret")
    assert crypto.decrypt(enc) == ""


def test_sliding_token_renewal():
    """临期令牌自动换发（X-Renewed-Token），新令牌不再携带续期头。"""
    import time
    from jose import jwt as _jwt
    from app.auth import _secret, _token_version
    from app.models import User
    from app.db import SessionLocal
    client = _client()
    H = _login(client)
    db = SessionLocal()
    uid = db.query(User).filter(User.username == "admin").first().id
    db.close()
    old = _jwt.encode({"sub": uid, "epoch": _token_version(),
                       "exp": int(time.time()) + 60},   # 剩余 1 分钟 < 半个有效期
                      _secret(), algorithm="HS256")
    r = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {old}"})
    assert r.status_code == 200
    renewed = r.headers.get("x-renewed-token")
    assert renewed
    r2 = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {renewed}"})
    assert r2.status_code == 200 and not r2.headers.get("x-renewed-token")   # 新令牌不再续期
    # 正常登录的新令牌：不触发续期
    assert client.get("/api/v1/auth/me", headers=H).headers.get("x-renewed-token") is None
    client.__exit__(None, None, None)
