"""服务重启后残留运行的收尾逻辑（WS8-B）。

真包（不是 ``exec()`` 聚合外观）：子模块之间用普通 ``from .x import y`` 互相
访问，`app.recovery` 直接 `from app.domain.orchestration_ops.stale_run_finalize
import finalize_stale_workflow_runs` 使用，不经本文件转手（本文件目前不需要
再导出任何符号）。

Layer：随 ``app.domain`` 包前缀归 L5（``app/LAYERS.toml``）。
"""
from __future__ import annotations
