"""Pytest shared-database-template bootstrap helpers, kept out of
``tests/conftest.py`` purely for its line-count baseline (``app/
FILE_CONVENTIONS.toml``, ratchet only tightens -- see that file's own
docstring on why baselines don't get bumped to fit new content;
``tests/patch_targets.py`` is the established precedent for this exact kind
of move).

``tests/conftest.py`` keeps ownership of the module-level state these two
functions used to close over (``_DATABASE_TEMPLATE`` / the
``_DATABASE_TEMPLATE_INITIALIZED`` flag) -- CLAUDE.md's rule that a
``global``-rebound name must live in the same module as its writer means the
old bodies can't just be pasted here verbatim with the state left behind in
``conftest.py``: a bare module-global read/write compiled into *this*
module's bytecode would resolve against *this* module's namespace, not
``conftest``'s, so the two files would silently drift apart (``conftest.py``'s
``pytest_configure`` would keep writing one copy of ``_DATABASE_TEMPLATE``
while a function relocated here read/wrote an always-empty second copy --
same class of hazard as the ``exec()`` two-namespace bug ``app/worker.py``
used to have). ``initialize_database_template`` below is therefore a pure
function that takes the template path and ``conftest.py``'s own
``_restore_isolated_runtime`` as arguments instead of reading globals;
``conftest.py``'s ``_initialize_database_template`` wrapper still owns the
``global`` statement and the already-initialized short-circuit.
``clone_database`` needs no such wrapper -- it was already a pure function of
its two path arguments, so it is re-exported as-is.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Callable


def initialize_database_template(
    db,
    *,
    database_template: Path,
    restore_isolated_runtime: Callable[..., None],
) -> None:
    """Build + seed the shared template database once.

    Body moved verbatim out of ``tests/conftest.py``'s former
    ``_initialize_database_template`` (minus the already-initialized guard,
    which stays in ``conftest.py`` alongside the flag it guards).
    """
    restore_isolated_runtime(db, database_path=database_template)
    connection = db.get_conn()
    try:
        db.init_db()
        # app.orgs.store 的函数现在只用 ensure_tables_on_connection（同连接、不
        # 做种子写入，见 app/orgs/schema.py 模块文档「两个入口」一段）——
        # org_default/5 个内置角色元数据行必须在这里显式调用完整的
        # ensure_schema()（独立连接、含种子）来种，不能再像改前那样指望
        # sync_builtin_role_permissions 内部经 store.get_role_by_key 的防御性
        # 调用顺带把种子建出来（那条调用已经不做种子了）。
        from app.orgs.schema import ensure_schema as ensure_orgs_schema
        ensure_orgs_schema()
        from app.orgs.bootstrap import sync_builtin_role_permissions
        sync_builtin_role_permissions(connection)
    finally:
        connection.close()
        db._local.conn = None


def clone_database(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_connection = sqlite3.connect(source)
    destination_connection = sqlite3.connect(destination)
    try:
        source_connection.backup(destination_connection)
    finally:
        destination_connection.close()
        source_connection.close()
