"""模型库落表 + 凭据加密（EP-05 第一阶段）。

真包，不是 ``exec()`` 聚合外观：``crypto``/``keyprovider``/``store``/
``migration`` 各是独立子模块，互相之间只通过 ``from app.models_registry
import x`` + ``x.func(...)``（模块限定访问）调用，不用 ``from .x import y``
把函数名拷贝进自己的命名空间——这样任何一处
``monkeypatch.setattr(app.models_registry.store, "get_credential", fake)``
都会被所有调用方看到，不会出现"包拆分后打桩静默落空"的陷阱（该陷阱与它的
标准解法见 ``tests/conftest.py::patch_models_registry_everywhere`` 与
``tests/test_models_registry_monkeypatch_guard.py`` 的模块文档）。

这个 ``__init__.py`` 本身故意不再导出任何符号：外部一律写
``app.models_registry.store.xxx`` / ``app.models_registry.crypto.xxx``，
不经这里转手，从根上消掉"包属性被子模块同名符号覆盖、getattr 静默拿错对象"
那一类陷阱（CLAUDE.md 记录过 get_conn 打桩打空、连到生产库的真实事故）。

对外兼容外观是 ``app/model_registry.py``（单数，已有 134 行），它内部转调
本包；不要把本包本身当成对外接口。
"""
from __future__ import annotations
