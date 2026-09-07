"""执行队列：可配并发的工作线程池。

默认 1 个 worker（保持天然串行，避免并发写冲突）；团队规模上来后设
TD_WORKERS=N 开启并发执行——SQLite 已配 busy_timeout，多 worker 下写等待
自动重试而非立刻报 "database is locked"（生产切 PostgreSQL/MySQL 更稳）。

支持取消：执行前注册一个 cancel token，取消接口置位后，引擎在步骤间检查并提前终止。
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor

from .. import config


def _workers() -> int:
    try:
        return max(1, min(16, int(config.get("TD_WORKERS") or 1)))
    except Exception:
        return 1


_pool = ThreadPoolExecutor(max_workers=_workers(), thread_name_prefix="td-run")
_cancelled: set = set()   # run_id 集合；执行线程在步骤间检查


def worker_count() -> int:
    return _pool._max_workers


def register(run_id: str) -> None:
    _cancelled.discard(run_id)


def cancel(run_id: str) -> None:
    _cancelled.add(run_id)


def is_cancelled(run_id: str) -> bool:
    return run_id in _cancelled


def unregister(run_id: str) -> None:
    _cancelled.discard(run_id)


async def queued(sync_fn):
    """sync_fn: 无参可调用的同步函数。在专用线程串行执行并等待结果。"""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_pool, sync_fn)
