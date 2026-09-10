"""系统管理员跨账号访问的审计硬证明（原属 tests/test_rbac_project_isolation.py）。

判据只挂在「是管理员 且 owner 不是他本人」这一个事实上，不是路由白名单；写入用
独立连接读盘校验——不用同一连接读自己刚写的行，那样即便本该独立提交的写入其实
还挂在调用方未提交的事务里也会「看起来」通过。
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.auth.principal import Principal, set_current_principal
from app.db import get_conn

from tests.rbac_isolation_helpers import (  # noqa: F401 -- pytest fixture 按名字注入
    _clear_principal_after,
    _mk_project,
    seed,
)


# ---------------------------------------------------------------------------
# P0-2：系统管理员跨账号访问必须落审计（谁/何时/访问了哪个账号的哪个对象），
# 判据只挂在"是管理员 且 owner 不是他本人"这一个事实上（不是路由白名单），
# 用独立连接读盘上数据验证——不用同一连接读自己刚写的行，那样即便本该独立
# 提交的写入其实还挂在调用方未提交的事务里也会"看起来"通过。
# ---------------------------------------------------------------------------


def _monitor_audit_count(where: str = "", params: tuple = ()) -> int:
    import sqlite3

    from app import db as db_module

    read_conn = sqlite3.connect(db_module.DB_PATH)
    try:
        sql = "SELECT COUNT(*) FROM monitor_audit"
        if where:
            sql += f" WHERE {where}"
        return read_conn.execute(sql, params).fetchone()[0]
    finally:
        read_conn.close()


def test_admin_cross_account_project_access_is_audited_on_independent_connection(
    seed, _clear_principal_after,
):
    import json
    import sqlite3

    from app import db as db_module
    from app.domain.common import _project_or_404

    set_current_principal(
        Principal(user_id=seed.admin, username="sys-admin", is_system_admin=True)
    )
    before = _monitor_audit_count("action='admin_cross_account_access'")

    _project_or_404("proj_a")  # 管理员访问 user_a 的项目

    after = _monitor_audit_count("action='admin_cross_account_access'")
    assert after == before + 1, "跨账号访问必须恰好新增一条审计行"

    read_conn = sqlite3.connect(db_module.DB_PATH)
    try:
        row = read_conn.execute(
            "SELECT object_type, object_id, outcome, detail_json FROM monitor_audit "
            "WHERE action='admin_cross_account_access' ORDER BY ts DESC LIMIT 1"
        ).fetchone()
    finally:
        read_conn.close()
    object_type, object_id, outcome, detail_json = row
    assert object_type == "project"
    assert object_id == "proj_a"
    assert outcome == "ok"
    detail = json.loads(detail_json)
    assert detail["admin_user_id"] == seed.admin
    assert detail["target_owner_user_id"] == seed.user_a


def test_same_account_access_writes_no_audit_row(seed, _clear_principal_after):
    """绝大多数请求（本人访问本人项目）必须零额外写入——这是热路径开销要求。"""
    from app.domain.common import _project_or_404

    set_current_principal(
        Principal(user_id=seed.user_a, username="user-a", is_system_admin=False)
    )
    before = _monitor_audit_count()
    _project_or_404("proj_a")
    after = _monitor_audit_count()
    assert after == before


def test_admin_own_account_access_writes_no_audit_row(seed, _clear_principal_after):
    """管理员访问自己名下的项目不算"跨账号"，同样不应该产生审计行。"""
    from app.domain.common import _project_or_404

    conn = get_conn()
    _mk_project(conn, "proj_admin_own", seed.admin)
    conn.commit()

    set_current_principal(
        Principal(user_id=seed.admin, username="sys-admin", is_system_admin=True)
    )
    before = _monitor_audit_count()
    _project_or_404("proj_admin_own")
    after = _monitor_audit_count()
    assert after == before


def test_regular_user_denied_cross_account_writes_no_audit_row(seed, _clear_principal_after):
    """普通用户跨账号访问被拒绝（404），这不是"管理员访问"，不应该被计入这条
    管理员审计——判据是"是管理员 且 owner 不是他本人"，不是"owner 不是访问者"。
    """
    from app.domain.common import _project_or_404

    set_current_principal(
        Principal(user_id=seed.user_a, username="user-a", is_system_admin=False)
    )
    before = _monitor_audit_count()
    with pytest.raises(HTTPException):
        _project_or_404("proj_b")
    after = _monitor_audit_count()
    assert after == before


def test_admin_cross_account_episode_and_shot_access_audited_with_correct_object_type(
    seed, _clear_principal_after,
):
    """object_type 精确到实际被访问的对象（episode/shot），不是一律记成
    project——审计要能回答"访问了哪个账号的哪个对象"，不能只回答"哪个账号"。
    """
    from app.domain.common import owned_episode_row, owned_shot_row

    set_current_principal(
        Principal(user_id=seed.admin, username="sys-admin", is_system_admin=True)
    )

    before = _monitor_audit_count("action='admin_cross_account_access'")
    owned_episode_row("ep_a")
    after_ep = _monitor_audit_count("action='admin_cross_account_access'")
    assert after_ep == before + 1

    owned_shot_row("shot_a")
    after_shot = _monitor_audit_count("action='admin_cross_account_access'")
    assert after_shot == after_ep + 1

    import sqlite3

    from app import db as db_module

    read_conn = sqlite3.connect(db_module.DB_PATH)
    try:
        rows = read_conn.execute(
            "SELECT object_type, object_id FROM monitor_audit "
            "WHERE action='admin_cross_account_access' ORDER BY ts DESC LIMIT 2"
        ).fetchall()
    finally:
        read_conn.close()
    by_type = {t: i for t, i in rows}
    assert by_type.get("shot") == "shot_a"
    assert by_type.get("episode") == "ep_a"
