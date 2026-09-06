"""执行串行队列：单线程池。所有测试执行（API/UI/计划）提交到同一个
单工作线程同步执行，天然串行：避免并发写冲突，也不引入任何
跨线程事件循环（此前异步引擎在池线程中会偶发死锁）。

支持取消：执行前注册一个 cancel token，取消接口置位后，引擎在步骤间检查并提前终止。
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor

_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="td-run")
_cancelled: set = set()   # run_id 集合；执行线程在步骤间检查


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
