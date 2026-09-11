"""EP-01 内置角色权限点同步（L5，见 app/LAYERS.toml::app.orgs.bootstrap）。

``app.orgs.schema.ensure_schema()`` 只建 org_default、5 个内置角色的元数据行
（key/name/description）与 ``users``/``projects`` 的 ``org_id`` 回填——那条
链路在运行时 Command Registry 加载**之前**执行（``app/main.py::lifespan``
先 ``ensure_orgs_schema()`` 再 ``ensure_catalog_loaded()``；测试同理，见
``tests/conftest.py::_initialize_database_template``），所以角色元数据行
本身不带任何权限点。

本模块把 5 个内置模板的 ``role_permissions`` 与当前 Command Registry 对齐，
必须在 ``ensure_catalog_loaded()`` 之后单独调用——``app.main`` 在
lifespan 里紧跟 ``ensure_catalog_loaded()`` 调用一次；测试在
``tests/conftest.py::_initialize_database_template`` 里同样紧跟着调用一次，
写进一次性的数据库模板，每个测试克隆到的库已经带着同步好的权限点，不需要
每个测试各跑一次。

依赖 ``app.authz.catalog``（L5，需要读 ``app.capabilities.registry``，见该
模块文档）与 ``app.orgs.store``（L2，向下依赖合法），因此本文件本身也只能
声明 L5——它是 ``app.orgs`` 包内唯一需要碰 Command Registry 的子模块，见
``app/LAYERS.toml`` 里 ``app.orgs.bootstrap`` 那条覆盖声明的注释。
"""
from __future__ import annotations

import sqlite3

from app.authz.catalog import build_builtin_role_permissions
from app.authz.policy import BUILTIN_ROLE_KEYS
from app.orgs import store


def sync_builtin_role_permissions(conn: sqlite3.Connection) -> None:
    """把 5 个内置角色模板的 role_permissions 与当前 Command Registry 全量对齐。

    幂等、可重复调用；每次都是"按当前 registry 状态整体覆盖"而不是增量 diff
    ——新增命令因此会自动被相应模板收进对应角色（EP-01 §5"新增命令自动
    出现"），不会因为只在某次旧快照跑过一次而永久缺失。
    """
    for key in BUILTIN_ROLE_KEYS:
        role = store.get_role_by_key(conn, None, key)
        if role is None:
            # schema.ensure_schema() 还没建出角色元数据行（比如脱离
            # app.main lifespan 单独调用本函数做单元测试）：没有 role_id
            # 可写，跳过而不是报错。
            continue
        permission_keys = build_builtin_role_permissions(key)
        store.set_role_permissions(conn, role["id"], permission_keys)
    conn.commit()
