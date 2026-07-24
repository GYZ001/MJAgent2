"""兼容入口：领域 Handler 已全部迁至同目录模块化文件。

生产路径由 ``app.capabilities.catalog`` 直接绑定 ``handlers.{project,bible,...}``。
本文件仅保留空 ``HANDLER_MAP``，供测试补丁与 ``catalog._bind_handlers`` 回填缺省 handler。
"""
from __future__ import annotations

from typing import Any, Callable

# 故意为空：勿再在此添加实现。新增命令请写到对应 handlers/*.py 并在 catalog 声明 handler=。
HANDLER_MAP: dict[str, Callable[..., Any]] = {}
