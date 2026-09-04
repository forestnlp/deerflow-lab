"""trace 上下文 —— 一个 contextvar，让 trace_id 贯穿整个 run。
参考本体 packages/harness/deerflow/trace_context.py（同款思路：不函数传参，
用 contextvar 把 trace_id 送到任何深层调用点）。

为什么不用参数透传？trace_id 要出现在每一条日志、每一个事件、每一次子工具调用里。
若靠参数，每个函数签名都要多带一个"和业务无关"的东西；contextvar 让它在
同一执行上下文中随处可取，且天然区分并发任务。
"""

from __future__ import annotations

import contextvars
import uuid
from contextlib import contextmanager

trace_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("trace_id", default="-")
run_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("run_id", default="-")
thread_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("thread_id", default="-")


def new_trace_id() -> str:
    return uuid.uuid4().hex[:12]


@contextmanager
def trace_scope(trace_id: str, run_id: str, thread_id: str):
    """进入一个 run 的作用域：作用域内任何地方 get_trace_id() 都拿得到。"""
    t1 = trace_id_var.set(trace_id)
    t2 = run_id_var.set(run_id)
    t3 = thread_id_var.set(thread_id)
    try:
        yield
    finally:
        trace_id_var.reset(t1)
        run_id_var.reset(t2)
        thread_id_var.reset(t3)


def current() -> tuple[str, str, str]:
    return trace_id_var.get(), run_id_var.get(), thread_id_var.get()
