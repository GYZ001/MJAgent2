"""EP-03 第一阶段：CSV 批量导入与离职资产移交
（PRD/enterprise/EP-03_用户生命周期与批量导入.md §4、§5）。

``csv_parse.py``（L1，零 db 依赖的纯解析）、``schema.py``（L2，lazy 建表）、
``importer.py``（L2，导入三段式的业务动作）、``handover.py``（L2，资产查询/
移交/删除前置校验）各自独立；本 ``__init__.py`` 故意不做聚合 re-export、
不 import 任何子模块——与 ``app/orgs/__init__.py`` 同一条理由：``api.py``
（L5，需要 ``app.auth.deps.require_system_admin`` 与 FastAPI ``APIRouter``）
由 ``app.main`` 直接 ``from app.provisioning.api import router``引用，不经
包 ``__init__``，这样 ``app.provisioning`` 包前缀本身不需要承担任何真实
import 边，层号声明（``app/LAYERS.toml::app.provisioning`` = 2）不会被
``app.provisioning.api``（L5）污染。
"""
from __future__ import annotations
