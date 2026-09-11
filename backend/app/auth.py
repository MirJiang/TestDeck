import time
import hashlib
import bcrypt
from jose import jwt, JWTError
from fastapi import Depends, HTTPException, Response
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session

from . import config
config.load_dotenv_file()
from .db import get_db
from .models import User


import time as _time
_login_attempts: dict = {}   # {username: [最近失败时间戳]}，登录限速用


_SECRET_FILE = __import__("pathlib").Path(__file__).resolve().parent.parent / ".secret_key"


def _secret() -> str:
    """JWT 签名密钥：TD_SECRET 优先；未配置时自动生成随机密钥并持久化到 backend/.secret_key（跨重启稳定）。

    不再回退到公开的默认常量——那等于密钥人人可知。只读环境写不了文件时退回进程内随机
    （重启后令牌失效，但绝不使用可预测值）。"""
    import secrets as _secrets
    v = (config.get("TD_SECRET") or "").strip()
    if v:
        return v
    try:
        v = _SECRET_FILE.read_text(encoding="utf-8").strip()
    except Exception:
        v = ""
    if v:
        return v
    v = _secrets.token_urlsafe(48)
    try:
        _SECRET_FILE.write_text(v, encoding="utf-8")
        print("[TestDeck] TD_SECRET 未配置：已自动生成随机密钥并持久化到 backend/.secret_key；"
              "生产多实例/重建容器场景请在 .env 显式配置 TD_SECRET")
    except Exception:
        pass   # 只读环境：本进程内保持一致即可
    return v


_EPOCH_FILE = __import__("pathlib").Path(__file__).resolve().parent.parent / ".token_epoch"


def _token_version() -> int:
    """token 版本：写进签名；改密/重置密码后 +1，所有旧 token 立即失效。"""
    v = config.get("TD_TOKEN_EPOCH")
    if v:
        try:
            return int(v)
        except ValueError:
            return 0
    try:
        return int(_EPOCH_FILE.read_text(encoding="utf-8").strip() or 0)
    except Exception:
        return 0


def bump_token_version() -> int:
    v = _token_version() + 1
    try:
        _EPOCH_FILE.write_text(str(v), encoding="utf-8")
    except Exception:
        config.set("TD_TOKEN_EPOCH", str(v))
    return v


TOKEN_TTL = 7 * 24 * 3600
bearer = HTTPBearer(auto_error=False)


def hash_pw(pw: str) -> str:
    return bcrypt.hashpw(pw.encode()[:72], bcrypt.gensalt()).decode()


def verify_pw(pw: str, hashed: str) -> bool:
    if hashed.startswith("$2"):
        return bcrypt.checkpw(pw.encode()[:72], hashed.encode())
    return hashed == hashlib.sha256(("td$" + pw).encode()).hexdigest()  # 兼容旧数据


def login_throttled(username: str) -> bool:
    """同一用户名 1 分钟内失败 ≥5 次则临时锁定（简单内存限速，重启即清）。"""
    now = _time.time()
    recent = [t for t in _login_attempts.get(username, []) if now - t < 60]
    _login_attempts[username] = recent
    return len(recent) >= 5


def record_login_fail(username: str) -> None:
    _login_attempts.setdefault(username, []).append(_time.time())


def make_token(user: User) -> str:
    payload = {"sub": user.id, "name": user.username, "role": user.role,
               "epoch": _token_version(), "exp": int(time.time()) + TOKEN_TTL}
    return jwt.encode(payload, _secret(), algorithm="HS256")


MCP_TOKEN_TTL = 10 * 365 * 24 * 3600   # MCP 专用长时效令牌：10 年


def make_mcp_token(user: User) -> str:
    """MCP 接入令牌：长时效（不受登录态过期影响），epoch 随改密/重置密码递增即可整体吊销。"""
    payload = {"sub": user.id, "name": user.username, "role": user.role,
               "scope": "mcp", "epoch": _token_version(),
               "exp": int(time.time()) + MCP_TOKEN_TTL}
    return jwt.encode(payload, _secret(), algorithm="HS256")


def resolve_token(token: str, db) -> User | None:
    """直接解析令牌串返回用户；供 WebSocket 等无法携带 Authorization 头的场景使用。"""
    if not token:
        return None
    try:
        payload = jwt.decode(token, _secret(), algorithms=["HS256"])
        if payload.get("epoch", 0) != _token_version():
            return None
    except JWTError:
        return None
    return db.get(User, payload["sub"])


def current_user(cred: HTTPAuthorizationCredentials = Depends(bearer), db: Session = Depends(get_db),
                 response: Response = None) -> User:
    if cred is None:
        raise HTTPException(401, "未登录")
    try:
        payload = jwt.decode(cred.credentials, _secret(), algorithms=["HS256"])
        if payload.get("epoch", 0) != _token_version():
            raise HTTPException(401, "登录已失效，请重新登录")
    except JWTError:
        raise HTTPException(401, "登录已失效")
    user = db.get(User, payload["sub"])
    if not user:
        raise HTTPException(401, "用户不存在")
    # 滑动续期：剩余有效期不足一半时换发新令牌，活跃用户无感续命（前端 api.js 拦截
    # X-Renewed-Token 更新本地存储与 td_token Cookie）；MCP 长时效令牌走 resolve_token，不在此续。
    if response is not None and payload.get("exp", 0) - time.time() < TOKEN_TTL * 0.5:
        response.headers["X-Renewed-Token"] = make_token(user)
    return user


def require_admin(user: User = Depends(current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(403, "需要管理员权限")
    return user
