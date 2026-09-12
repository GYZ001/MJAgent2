"""创建系统管理员账号（RBAC 第一阶段的唯一开户入口）。

init_db() 本身绝不建账号——账号必须由运维在这里显式、可审计地创建，避免出现
不受控的默认口令。默认只允许存在一个系统管理员；确需追加时传 --force-add。

用法：
    .venv/bin/python scripts/create_admin.py --username admin
    .venv/bin/python scripts/create_admin.py --username admin2 --force-add

EP-06 容器首次启动引导（``--from-env``）：
    .venv/bin/python scripts/create_admin.py --from-env
读 ``MJ_BOOTSTRAP_ADMIN_USER``/``MJ_BOOTSTRAP_ADMIN_PASSWORD`` 两个环境变量，
仅当 ``users`` 表**一行都没有**时才建号（不是"没有管理员"，是"整表为空"——
容器重启每次都会跑这条入口，任何一个已存在的账号，不管是不是管理员，都说明
这不是首次启动，必须原样跳过，否则会在数据已经存在的库上重复尝试建号）。
两个环境变量任一缺失都视为"本次部署不做自动引导"，静默跳过、退出码 0——
不是每个部署都需要这条捷径，缺失不是错误。
"""
from __future__ import annotations

import argparse
import getpass
import os
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


def _insert_admin(conn, username: str, password: str, display_name: str | None) -> str:
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
            (display_name or username).strip(),
            hash_password(password),
            ts,
            ts,
        ),
    )
    conn.commit()
    # 账号即项目空间：系统管理员不需要加入任何团队/工作空间（该模型已退场），
    # is_system_admin=1 本身就隐式跨账号可见，见 app/auth/principal.py。
    return user_id


def _bootstrap_from_env() -> int:
    """容器首次启动引导：见模块 docstring「EP-06 容器首次启动引导」一段。"""
    username = os.environ.get("MJ_BOOTSTRAP_ADMIN_USER", "").strip()
    password = os.environ.get("MJ_BOOTSTRAP_ADMIN_PASSWORD", "")
    if not username or not password:
        print("MJ_BOOTSTRAP_ADMIN_USER/_PASSWORD 未同时设置，跳过自动引导。")
        return 0
    init_db()
    conn = get_conn()
    if conn.execute("SELECT 1 FROM users LIMIT 1").fetchone():
        print("users 表非空（非首次启动），跳过自动引导，不重复建号。")
        return 0
    user_id = _insert_admin(conn, username, password, None)
    print(f"已从环境变量引导系统管理员：{username}（id={user_id}）。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--username", help="登录用户名，唯一（与 --from-env 二选一）")
    parser.add_argument("--password", help="口令；不提供则交互式输入")
    parser.add_argument("--display-name", help="展示名，默认与用户名相同")
    parser.add_argument(
        "--force-add",
        action="store_true",
        help="已存在系统管理员时仍追加一个新的系统管理员账号",
    )
    parser.add_argument(
        "--from-env",
        action="store_true",
        help="容器首次启动引导：读 MJ_BOOTSTRAP_ADMIN_USER/_PASSWORD，仅当 users 表为空时生效",
    )
    args = parser.parse_args()

    if args.from_env:
        return _bootstrap_from_env()
    if not args.username:
        parser.error("--username 是必填项（或改用 --from-env）")

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

    user_id = _insert_admin(conn, username, password, args.display_name)
    print(f"已创建系统管理员：{username}（id={user_id}）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
