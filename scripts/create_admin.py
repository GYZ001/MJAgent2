"""创建系统管理员账号（RBAC 第一阶段的唯一开户入口）。

init_db() 本身绝不建账号——账号必须由运维在这里显式、可审计地创建，避免出现
不受控的默认口令。默认只允许存在一个系统管理员；确需追加时传 --force-add。

用法：
    .venv/bin/python scripts/create_admin.py --username admin
    .venv/bin/python scripts/create_admin.py --username admin2 --force-add
"""
from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.auth.passwords import hash_password
from app.db import get_conn, init_db, new_id, now
# init_db() looks up its per-table bootstrap steps by name through
# app.db_schema instead of importing these business modules directly (P0-3
# dependency inversion, see docs/coupling_review_2026-08-29.md 第2步). This
# standalone script never otherwise imports them, so without this its bare
# init_db() call would raise KeyError on the unconditional
# "builtin_models_migration" lookup.
import app.artifacts  # noqa: F401
import app.completion_grant  # noqa: F401
import app.delivery  # noqa: F401
import app.model_migration  # noqa: F401
import app.production.certificate  # noqa: F401
import app.production.grant  # noqa: F401
import app.production.revision  # noqa: F401
import app.production.shot_uid  # noqa: F401


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--username", required=True, help="登录用户名，唯一")
    parser.add_argument("--password", help="口令；不提供则交互式输入")
    parser.add_argument("--display-name", help="展示名，默认与用户名相同")
    parser.add_argument(
        "--force-add",
        action="store_true",
        help="已存在系统管理员时仍追加一个新的系统管理员账号",
    )
    args = parser.parse_args()

    init_db()
    conn = get_conn()

    existing_admin = conn.execute(
        "SELECT id, username FROM users WHERE is_system_admin=1 LIMIT 1"
    ).fetchone()
    if existing_admin and not args.force_add:
        print(
            f"已存在系统管理员账号（{existing_admin['username']}），拒绝创建。"
            "如确需追加第二个系统管理员，请显式传 --force-add。",
            file=sys.stderr,
        )
        return 2

    username = args.username.strip()
    if not username:
        print("用户名不能为空。", file=sys.stderr)
        return 2
    if conn.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
        print(f"用户名已存在：{username}", file=sys.stderr)
        return 2

    password = args.password
    if not password:
        password = getpass.getpass(f"为 {username} 设置口令：")
    if not password:
        print("口令不能为空。", file=sys.stderr)
        return 2

    ts = now()
    user_id = new_id("user")
    conn.execute(
        """INSERT INTO users(
               id, username, display_name, password_hash, auth_provider,
               status, is_system_admin, must_change_password, created_at,
               password_changed_at
           ) VALUES(?,?,?,?,'local','active',1,0,?,?)""",
        (
            user_id,
            username,
            (args.display_name or username).strip(),
            hash_password(password),
            ts,
            ts,
        ),
    )
    conn.execute(
        "INSERT OR IGNORE INTO workspace_members(workspace_id, user_id, role, created_at) "
        "VALUES('ws_default', ?, 'workspace_admin', ?)",
        (user_id, ts),
    )
    conn.commit()
    print(f"已创建系统管理员：{username}（id={user_id}），并加入 ws_default 为 workspace_admin。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
