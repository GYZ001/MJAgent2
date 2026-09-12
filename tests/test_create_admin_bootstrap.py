"""``scripts/create_admin.py --from-env``：EP-06 容器首次启动引导分支。

判据挂"真的建出了可登录的管理员"，不是"脚本跑过不报错"：直接调用
``_bootstrap_from_env()``（与 CLI 入口 main() 走同一段逻辑），核对建出来的
账号 ``is_system_admin=1`` 且能用同一份口令过 ``app.auth.passwords`` 校验。
"""
from __future__ import annotations

from scripts import create_admin
from app.auth.passwords import verify_password
from app.db import get_conn, init_db


def test_from_env_noop_when_env_vars_missing(monkeypatch):
    monkeypatch.delenv("MJ_BOOTSTRAP_ADMIN_USER", raising=False)
    monkeypatch.delenv("MJ_BOOTSTRAP_ADMIN_PASSWORD", raising=False)
    init_db()
    before = get_conn().execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
    assert create_admin._bootstrap_from_env() == 0
    after = get_conn().execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
    assert after == before


def test_from_env_creates_admin_when_users_table_empty(monkeypatch):
    monkeypatch.setenv("MJ_BOOTSTRAP_ADMIN_USER", "bootadmin")
    monkeypatch.setenv("MJ_BOOTSTRAP_ADMIN_PASSWORD", "bootpass123")
    init_db()
    conn = get_conn()
    assert conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"] == 0

    assert create_admin._bootstrap_from_env() == 0

    row = conn.execute(
        "SELECT is_system_admin, password_hash FROM users WHERE username='bootadmin'"
    ).fetchone()
    assert row is not None
    assert bool(row["is_system_admin"]) is True
    assert verify_password("bootpass123", row["password_hash"])


def test_from_env_skips_when_users_table_non_empty(monkeypatch):
    monkeypatch.setenv("MJ_BOOTSTRAP_ADMIN_USER", "second-admin")
    monkeypatch.setenv("MJ_BOOTSTRAP_ADMIN_PASSWORD", "whatever123")
    init_db()
    conn = get_conn()
    from app.auth.passwords import hash_password
    from app.db import new_id, now
    conn.execute(
        "INSERT INTO users(id, username, password_hash, status, is_system_admin, "
        "must_change_password, created_at) VALUES(?,?,?,'active',0,0,?)",
        (new_id("usr"), "already-here", hash_password("x"), now()),
    )
    conn.commit()

    assert create_admin._bootstrap_from_env() == 0

    assert conn.execute(
        "SELECT COUNT(*) AS n FROM users WHERE username='second-admin'"
    ).fetchone()["n"] == 0
