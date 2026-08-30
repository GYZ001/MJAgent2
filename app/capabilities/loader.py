"""能力目录懒加载入口。

独立于 ``app/capabilities/__init__.py`` 之外，避免「包 → 子模块 catalog」
与「子模块 → 包」两条边互相咬合成环（`docs/architecture_layering_plan_2026-08-29.md`
P0-2 起，`app.capabilities` 与 `app.capabilities.dispatch` 是全后端最大强连通分量
里贡献最大的两条团内边）。``ensure_catalog_loaded`` 原本定义在 ``__init__.py``，
现搬到这里；调用方一律从本模块导入，不再从包顶层导入。
"""
from __future__ import annotations

from app.capabilities.registry import CapabilityRegistry, get_registry


def ensure_catalog_loaded() -> CapabilityRegistry:
    """幂等加载能力目录，供 API、CI 扫描与测试使用。"""
    from app.capabilities import catalog  # noqa: F401 — 触发注册副作用

    catalog.ensure_registered()
    return get_registry()
