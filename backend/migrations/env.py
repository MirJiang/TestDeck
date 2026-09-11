"""Alembic 环境：连接串与表元数据全部取自应用本体，避免两套结构定义漂移。

连接串优先级：环境变量 ALEMBIC_DATABASE_URL > 应用 TD_DATABASE_URL > TD_DB（SQLite）。
用法（在 backend/ 目录）：
    .venv/Scripts/python -m alembic upgrade head            # 应用/升级到最新结构
    .venv/Scripts/python -m alembic revision --autogenerate -m "..."
      （改了 app/models.py 后生成增量迁移；对照库用 ALEMBIC_DATABASE_URL 指向基线库）
"""
import os
import sys
from logging.config import fileConfig
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # backend/ 可 import app

from alembic import context

from app import config as app_config
from app import models  # noqa: F401  确保全部表定义注册进 Base.metadata
from app.db import Base

config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _url() -> str:
    if os.environ.get("ALEMBIC_DATABASE_URL"):
        return os.environ["ALEMBIC_DATABASE_URL"]
    from app.db import resolve_database_url
    return resolve_database_url()   # 与应用同一事实源（TD_DATABASE_URL / TD_DB / 内存库）


def run_migrations_offline() -> None:
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True,
                      compare_type=True, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    from sqlalchemy import create_engine
    engine = create_engine(_url())
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata,
                          compare_type=True, render_as_batch=True)   # batch：SQLite 改列走建表拷贝
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
