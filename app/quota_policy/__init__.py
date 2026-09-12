"""EP-04 第一阶段：配额策略表化 + 三级（org/team/user）取最紧判定。

``schema.py``（L2，lazy 建表/种子）、``plans.py``（L2，quota_plans 读写）、
``allocation.py``（L2，三级取最紧判定——``app.quota.effective_limits`` 的唯一
取值来源）、``usage_query.py``（L2，用量聚合与 80%/95% 预警）各自独立；
``api.py``（L5，REST 路由）是唯一需要读运行时 Principal/组织归属的模块。

本 ``__init__.py`` 故意不做聚合 re-export、不 import 任何子模块——与
``app.orgs`` 同一惯例（见该包 ``__init__.py`` 文档）：``app.main`` 按需从具体
子模块 import，包前缀本身不承担真实 import 边，层号声明
（``app/LAYERS.toml::app.quota_policy`` = 2）不会被 ``app.quota_policy.api``
（L5）污染。

**架构红线（派单原文）**：本包任何模块都不得 ``import app.quota``——
``app.quota`` 反过来 import 本包（``effective_limits`` 调用
``allocation.resolve_effective_limits``），方向必须单向，否则会在既有的
``db ↔ quota ↔ quota_addon ↔ quota_tiers`` 循环团上再叠一层。需要档位默认值
时 import ``app.quota_tiers``（纯数据，不是 ``app.quota`` 本体，无环）。
"""
from __future__ import annotations
