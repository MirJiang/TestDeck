"""一次性把 SQLite 数据迁移到 MySQL / PostgreSQL。

用法：
  python -m app.cli.db_migrate --to "postgresql+psycopg2://user:pass@host:5432/testdeck"
  python -m app.cli.db_migrate --to "mysql+pymysql://user:pass@host:3306/testdeck?charset=utf8mb4"

目标表已有数据时自动跳过，不会覆盖。
"""
import argparse

from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker

from ..db import Base, SessionLocal
from .. import models  # noqa: F401  注册所有表

ORDER = ["users", "projects", "project_members", "envs", "test_cases", "test_plans",
         "schedules", "test_runs", "flows", "git_repos", "commit_syncs",
         "notify_channels", "llm_configs", "llm_logs"]


def main():
    ap = argparse.ArgumentParser(description="把 SQLite 数据迁移到 MySQL / PostgreSQL")
    ap.add_argument("--to", required=True, help="目标库连接串，如 postgresql+psycopg2://user:pass@host/db")
    a = ap.parse_args()

    target = create_engine(a.to, pool_pre_ping=True)
    Base.metadata.create_all(target)
    src, ts = SessionLocal(), sessionmaker(bind=target)()
    insp = inspect(target)
    total = 0
    for name in ORDER:
        if name not in insp.get_table_names():
            print(f"- {name}: 目标库无此表，跳过")
            continue
        table = Base.metadata.tables[name]
        rows = [dict(r) for r in src.execute(table.select()).mappings().all()]
        if not rows:
            print(f"- {name}: 源库为空，跳过")
            continue
        if ts.execute(table.select()).first():
            print(f"- {name}: 目标已有数据，不覆盖，跳过")
            continue
        ts.execute(table.insert(), rows)
        ts.commit()
        total += len(rows)
        print(f"OK {name}: {len(rows)} 行")
    print(f"完成，共迁移 {total} 行")


if __name__ == "__main__":
    main()
