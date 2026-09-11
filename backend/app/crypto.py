"""凭据静态加密：模型 API Key、告警 webhook 等入库前 Fernet 加密，读取透明解密。

密钥从 TD_SECRET（或自动生成的 backend/.secret_key）经 SHA-256 派生——与 JWT 签名同源，
零额外配置；代价是轮换 TD_SECRET 会使已存密文不可解（读取返回空串，需重新录入）。
密文带 `enc:v1:` 前缀：无前缀的值视为存量明文原样返回，启动迁移负责补加密。
"""
import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import String, TypeDecorator

_PREFIX = "enc:v1:"


def _fernet() -> Fernet:
    from .auth import _secret   # 惰性导入：auth 依赖 models，避免 models → crypto → auth 循环
    key = hashlib.sha256(_secret().encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt(value: str) -> str:
    if not value or value.startswith(_PREFIX):
        return value
    return _PREFIX + _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt(value: str) -> str:
    if not value or not value.startswith(_PREFIX):
        return value
    try:
        return _fernet().decrypt(value[len(_PREFIX):].encode("ascii")).decode("utf-8")
    except InvalidToken:
        return ""   # 密钥已轮换：密文不可解，返回空让界面提示重新录入


class EncryptedString(TypeDecorator):
    """透明加解密的可空字符串列：写库自动加密、读库自动解密。"""

    impl = String(1024)   # Fernet 密文比明文长约 1.4 倍，放宽到 1024
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return encrypt(value) if value else value

    def process_result_value(self, value, dialect):
        return decrypt(value) if value else value
