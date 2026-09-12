"""服务器 shell 侧签发一次性应急恢复码（EP-02 §6 强制 SSO 下的本地登录通道）。

``scripts/create_admin.py`` 同族：需要服务器 shell 权限才能运行，产出的
恢复码 10 分钟内有效、只能使用一次，兑换入口是
``POST /api/auth/sso/break-glass``（body: ``{"username", "code"}``）。这是
``local_login_policy=disabled`` 时唯一仍可用的本地登录通道——切换到
``disabled`` 前台管理面已经强制做过一次真实 SSO 连通性自检
（``app.sso.admin_api.set_local_login_policy``），但连通性检查不了配置本身
是否配对（比如 client_secret 错），因此必须保留这条不依赖任何 IdP 的最后
出路。

用法：
    .venv/bin/python scripts/break_glass_login.py --username admin
"""
from __future__ import annotations

import argparse
import hashlib
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.db import get_conn, init_db
# init_db() looks up its per-table bootstrap steps by name through
# app.db_schema instead of importing these business modules directly (P0-3
# dependency inversion). This standalone script never otherwise imports them,
# so without this its bare init_db() call would raise KeyError on the
# unconditional "builtin_models_migration" lookup — same reason
# scripts/create_admin.py carries the identical block.
import app.artifacts  # noqa: F401
import app.completion_grant  # noqa: F401
import app.delivery  # noqa: F401
import app.model_migration  # noqa: F401
import app.production.certificate  # noqa: F401
import app.production.grant  # noqa: F401
import app.production.revision  # noqa: F401
import app.production.shot_uid  # noqa: F401
from app.sso.store import create_break_glass_code


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--username", required=True, help="必须是已存在且启用中的系统管理员账号")
    args = parser.parse_args()

    init_db()
    conn = get_conn()
    row = conn.execute(
        "SELECT id, is_system_admin, status FROM users WHERE username=?", (args.username,)
    ).fetchone()
    if row is None:
        print(f"账号不存在：{args.username}", file=sys.stderr)
        return 2
    if not row["is_system_admin"]:
        print(f"应急通道仅限系统管理员账号，{args.username!r} 不是系统管理员。", file=sys.stderr)
        return 2
    if row["status"] != "active":
        print(f"账号 {args.username} 当前状态是 {row['status']!r}，无法使用应急通道。", file=sys.stderr)
        return 2

    code = secrets.token_urlsafe(24)
    code_hash = hashlib.sha256(code.encode("utf-8")).hexdigest()
    create_break_glass_code(conn, user_id=row["id"], code_hash=code_hash, created_by="break_glass_login.py")
    conn.commit()

    print(f"一次性恢复码（10 分钟内有效，只能使用一次）：{code}")
    print(
        "兑换方式：POST /api/auth/sso/break-glass，body = "
        f'{{"username": "{args.username}", "code": "<上面的恢复码>"}}'
    )
    print("使用后该账号会被强制要求下次登录改密，且本次使用会写入 operation_audit。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
