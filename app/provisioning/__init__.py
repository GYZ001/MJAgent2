"""EP-03：CSV 批量导入、离职资产移交、邀请链接
（PRD/enterprise/EP-03_用户生命周期与批量导入.md §4、§5、§6）。

``csv_parse.py``（L1，零 db 依赖的纯解析）、``schema.py``（L2，lazy 建表）、
``importer.py``（L2，导入三段式的业务动作）、``handover.py``（L2，资产查询/
移交/删除前置校验）、``invitations.py``（L2，邀请签发/预览/接受/撤销）各自
独立；本 ``__init__.py`` 故意不做聚合 re-export、不 import 任何子模块——与
``app/orgs/__init__.py`` 同一条理由：``api.py``/``invite_api.py``（均 L5，
分别是管理员入口与公开接受入口，需要 FastAPI ``APIRouter``）由 ``app.main``
直接 ``from app.provisioning.api import router``/``from app.provisioning.
invite_api import router`` 引用，不经包 ``__init__``，这样 ``app.provisioning``
包前缀本身不需要承担任何真实 import 边，层号声明（``app/LAYERS.toml::
app.provisioning`` = 2）不会被两个 L5 子模块污染。
"""
from __future__ import annotations
