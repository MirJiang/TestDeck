"""数据库连接层：URL 解析优先级与多方言引擎构建（不真正连接）。"""
from app import config
config.set("TD_DB", ":memory:")

from app.db import resolve_database_url, make_engine


def test_default_sqlite_file(monkeypatch, tmp_path):
    monkeypatch.setitem(config._values, "TD_DATABASE_URL", "")
    monkeypatch.setitem(config._values, "TD_DB", str(tmp_path / "t.db"))
    assert resolve_database_url() == f"sqlite:///{tmp_path / 't.db'}"


def test_url_takes_precedence(monkeypatch):
    monkeypatch.setitem(config._values, "TD_DATABASE_URL", "postgresql+psycopg2://u:p@db:5432/testdeck")
    assert resolve_database_url() == "postgresql+psycopg2://u:p@db:5432/testdeck"


def test_memory_sqlite(monkeypatch):
    monkeypatch.setitem(config._values, "TD_DATABASE_URL", "")
    monkeypatch.setitem(config._values, "TD_DB", ":memory:")
    assert resolve_database_url() == "sqlite://"


def test_dialects_build():
    e = make_engine("postgresql+psycopg2://u:p@localhost:5432/t")
    assert e.dialect.name == "postgresql"
    e = make_engine("mysql+pymysql://u:p@localhost:3306/t?charset=utf8mb4")
    assert e.dialect.name == "mysql"
