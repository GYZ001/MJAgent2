"""防止「REST → Command Bus → Handler → 同名 REST」递归。

Handler 执行期间置位；路由入口若检测到该标志，则直接跑本函数领域逻辑，
不再二次进入 dispatch。
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar

_IN_HANDLER: ContextVar[bool] = ContextVar("capability_in_handler", default=False)


def in_handler() -> bool:
    return bool(_IN_HANDLER.get())


@contextmanager
def enter_handler():
    token = _IN_HANDLER.set(True)
    try:
        yield
    finally:
        _IN_HANDLER.reset(token)
