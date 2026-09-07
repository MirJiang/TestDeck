"""数据库连接层：默认 SQLite（零配置，本地开发与测试）；生产在 .env 配 TD_DATABASE_URL 切 MySQL/PostgreSQL。

常用连接串（写入 .env 的 TD_DATABASE_URL）：
  postgresql+psycopg2://user:pass@host:5432/testdeck
  mysql+pymysql://user:pass@host:3306/testdeck?charset=utf8mb4
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase

from . import config
config.load_dotenv_file()  # 先载入 .env，再解析数据库地址


def resolve_database_url() -> str:
    url = config.get("TD_DATABASE_URL").strip()
    if url:
        return url
    path = config.get("TD_DB") or "testdeck.db"
    if path == ":memory:":
        return "sqlite://"
    return f"sqlite:///{path}"


def make_engine(url: str):
    if url.startswith("sqlite"):
        # busy_timeout：TD_WORKERS>1 并发执行时写锁等待自动重试，而非立刻报 database is locked
        kwargs = {"connect_args": {"check_same_thread": False, "timeout": 30}}
        if url == "sqlite://":  # 内存库：测试用，需单连接共享
            from sqlalchemy.pool import StaticPool
            kwargs["poolclass"] = StaticPool
            kwargs["connect_args"] = {"check_same_thread": False}
        return create_engine(url, **kwargs)
    return create_engine(url, pool_pre_ping=True)  # MySQL/PG 长连接断线自愈


engine = make_engine(resolve_database_url())
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
