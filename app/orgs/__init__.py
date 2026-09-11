"""EP-01 组织/团队/角色/项目授权（PRD/enterprise/EP-01_权限模型与自定义角色.md）。

``schema.py``（L2，lazy 建表/种子/回填）、``store.py``（L2，纯 db 读写）与
``service.py``（L2，业务动作）各自独立；本 ``__init__.py`` 故意不做聚合
re-export、不 import 子模块——``app.orgs.bootstrap``（L5，需要读运行时
Command Registry，见该模块文档）由 ``app.main`` 直接
``from app.orgs.bootstrap import sync_builtin_role_permissions`` 引用，不经
包 ``__init__``，这样 ``app.orgs`` 包前缀本身不需要承担任何真实 import 边，
层号声明（``app/LAYERS.toml::app.orgs`` = 2）不会被 ``app.orgs.bootstrap``
污染。
"""
from __future__ import annotations
