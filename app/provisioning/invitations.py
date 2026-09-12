"""EP-03 §6 邀请链接：签发 / 列表 / 撤销 / 预览 / 接受（L2，见
app/LAYERS.toml::app.provisioning.invitations）。

token = ``secrets.token_urlsafe(32)``，只存 SHA-256（与 ``mcp_tokens``/
``user_sessions``/SSO 交换码既有做法一致）；明文只在签发那一刻返回一次，
之后任何读路径（列表/预览）都看不到它。

**邀请不绕过密码策略**：接受时用户自设的口令要过
``app.auth.password_policy.enforce_password_change`` 同一条校验
（``user_id=None``——全新账号没有历史口令可比对，只做强度校验）。

**接受时校验邀请是否仍有效**（EP-03 §6）：状态本身（过期/已用/已撤销）
优先于目标（组织被停用/角色被删）优先于口令强度——邀请已经死了就不该让
用户白白设一次密码才发现失败；目标失效时明确报错并提示联系管理员（不是
静默用一个不存在的角色建号）。

过期清理：本模块只提供 ``sweep_expired``（被动函数，不含定时器），挂载点在
``app.audit.retention`` 既有的 6 小时巡检循环（EP-03 §6，不新开定时器）。
"""
from __future__ import annotations

import hashlib
import secrets
import sqlite3

from fastapi import HTTPException

from app.auth import password_policy
from app.auth.passwords import hash_password
from app.auth.sessions import create_session
from app.db import get_conn, new_id, now
from app.orgs import schema as orgs_schema
from app.orgs import store as orgs_store
from app.provisioning import schema

DEFAULT_TTL_S = 7 * 24 * 3600.0
_SWEEP_GRACE_S = 30 * 24 * 3600.0
_INVALID_STATUS_MESSAGES = {
    "revoked": "邀请已被撤销，请联系管理员重新生成",
    "accepted": "邀请已被使用，请联系管理员重新生成",
    "expired": "邀请已过期，请联系管理员重新生成",
}


def _ensure_schemas(conn: sqlite3.Connection) -> None:
    """同连接补表，不开独立连接（2026-09-12 协调方审查修复）：本模块的函数
    接受未提交的调用方连接（``accept_invitation`` 一次调用里有多步写入），
    独立连接的 ``ensure_schema()`` 会在这种场景下去抢 ``BEGIN IMMEDIATE``，
    2 秒超时后失败又被吞掉，表现成远离病因的 ``no such table``——同一类问题
    已在 ``app.auth.sessions``/``session_policy``/``password_policy`` 修过，
    见 ``app.auth.session_policy`` 模块文档。"""
    schema.ensure_tables_on_connection(conn)
    orgs_schema.ensure_tables_on_connection(conn)


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _row_by_token(conn: sqlite3.Connection, token: str):
    return conn.execute(
        "SELECT * FROM user_invitations WHERE token_hash=?", (_hash_token(token),)
    ).fetchone()


def _status_of(row, ts: float) -> str:
    if row["revoked_at"]:
        return "revoked"
    if row["accepted_at"]:
        return "accepted"
    if float(row["expires_at"]) <= ts:
        return "expired"
    return "pending"


def create_invitation(
    *, org_id: str, username: str, display_name: str, email: str,
    team_id: str | None, role_id: str | None, created_by: str, ttl_s: float = DEFAULT_TTL_S,
) -> dict:
    """签发一次性邀请；``team_id``/``role_id`` 二者必须同时提供或同时留空
    （与 CSV 导入 ``importer._resolve_team_and_role`` 同一约束）。返回体带
    明文 ``token``，只这一次。"""
    username = username.strip()
    if not username:
        raise HTTPException(422, "username 不能为空")
    if bool(team_id) != bool(role_id):
        raise HTTPException(422, "team_id 与 role_id 必须同时提供或同时留空")
    conn = get_conn()
    _ensure_schemas(conn)
    if conn.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
        raise HTTPException(409, "用户名已存在")
    if team_id is not None and orgs_store.get_team(conn, team_id) is None:
        raise HTTPException(404, "团队不存在")
    if role_id is not None and orgs_store.get_role(conn, role_id) is None:
        raise HTTPException(404, "角色不存在")

    token = secrets.token_urlsafe(32)
    invitation_id = new_id("inv")
    ts = now()
    conn.execute(
        "INSERT INTO user_invitations(id, org_id, username, display_name, email, team_id, role_id,"
        " token_hash, expires_at, created_by, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (invitation_id, org_id, username, display_name.strip() or username, email.strip() or None,
         team_id, role_id, _hash_token(token), ts + ttl_s, created_by, ts),
    )
    conn.commit()
    _record_invitation_audit("provisioning.invitation_create", invitation_id, username, created_by)
    row = conn.execute("SELECT * FROM user_invitations WHERE id=?", (invitation_id,)).fetchone()
    return {**_admin_payload(conn, row), "token": token}


def list_invitations(org_id: str) -> list[dict]:
    conn = get_conn()
    _ensure_schemas(conn)
    rows = conn.execute(
        "SELECT * FROM user_invitations WHERE org_id=? ORDER BY created_at DESC", (org_id,)
    ).fetchall()
    return [_admin_payload(conn, r) for r in rows]


def revoke_invitation(invitation_id: str, *, revoked_by: str) -> dict:
    conn = get_conn()
    _ensure_schemas(conn)
    row = conn.execute("SELECT * FROM user_invitations WHERE id=?", (invitation_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "邀请不存在")
    if row["accepted_at"]:
        raise HTTPException(409, "邀请已被接受，无法撤销")
    if not row["revoked_at"]:
        conn.execute(
            "UPDATE user_invitations SET revoked_at=?, revoked_by=? WHERE id=?",
            (now(), revoked_by, invitation_id),
        )
        conn.commit()
        _record_invitation_audit("provisioning.invitation_revoke", invitation_id, row["username"], revoked_by)
        row = conn.execute("SELECT * FROM user_invitations WHERE id=?", (invitation_id,)).fetchone()
    return _admin_payload(conn, row)


def get_invitation_preview(token: str) -> dict:
    """``GET /api/invite/{token}``：只读预览，供接受页在用户输入口令前先展示
    这个邀请是谁的、还有效吗。查无此 token 一律 404；找到但已失效时仍返回
    200 + ``status`` 字段，把"已过期/已撤销/已使用"的渲染交给前端，不在这里
    报错——用户点开一个死链接不该看到一个裸的 HTTP 错误页。"""
    conn = get_conn()
    _ensure_schemas(conn)
    row = _row_by_token(conn, token)
    if row is None:
        raise HTTPException(404, "邀请链接无效")
    return _preview_payload(conn, row)


def _preview_payload(conn: sqlite3.Connection, row) -> dict:
    team = orgs_store.get_team(conn, row["team_id"]) if row["team_id"] else None
    role = orgs_store.get_role(conn, row["role_id"]) if row["role_id"] else None
    org = orgs_store.get_org(conn, row["org_id"]) if row["org_id"] else None
    return {
        "status": _status_of(row, now()), "username": row["username"], "display_name": row["display_name"],
        "org_name": org["name"] if org else None,
        "team_name": team["name"] if team else None,
        "role_name": role["name"] if role else None,
        "expires_at": row["expires_at"],
    }


def _admin_payload(conn: sqlite3.Connection, row) -> dict:
    payload = _preview_payload(conn, row)
    payload.update({
        "id": row["id"], "email": row["email"], "team_id": row["team_id"], "role_id": row["role_id"],
        "created_by": row["created_by"], "created_at": row["created_at"],
        "accepted_user_id": row["accepted_user_id"], "revoked_by": row["revoked_by"],
    })
    return payload


def _validate_invitation_targets(conn: sqlite3.Connection, row) -> None:
    """组织被停用 / 团队被停用或删除 / 角色被删——明确报错并提示联系管理员
    （EP-03 §6），不是静默用一个失效的目标建号。"""
    if row["org_id"]:
        org = orgs_store.get_org(conn, row["org_id"])
        if org is None or org["status"] != "active":
            raise HTTPException(409, "邀请所属组织已停用，请联系系统管理员重新处理邀请")
    if row["team_id"]:
        team = orgs_store.get_team(conn, row["team_id"])
        if team is None or team["status"] != "active":
            raise HTTPException(409, "邀请所属团队已停用或不存在，请联系系统管理员重新处理邀请")
    if row["role_id"] and orgs_store.get_role(conn, row["role_id"]) is None:
        raise HTTPException(409, "邀请预置的角色已被删除，请联系系统管理员重新处理邀请")


def accept_invitation(token: str, *, password: str, user_agent: str | None, ip: str | None) -> dict:
    """``POST /api/invite/{token}/accept``：校验邀请仍有效 → 校验目标仍有效
    → 密码策略 → 建号 → 直接签发会话（口令是用户自己设的、已过 policy 校验，
    不像 CSV 导入的随机口令那样需要登录后再强制改一次）。"""
    conn = get_conn()
    _ensure_schemas(conn)
    row = _row_by_token(conn, token)
    if row is None:
        raise HTTPException(404, "邀请链接无效")
    status = _status_of(row, now())
    if status != "pending":
        raise HTTPException(410, _INVALID_STATUS_MESSAGES[status])
    _validate_invitation_targets(conn, row)

    username = row["username"]
    if conn.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
        raise HTTPException(409, "用户名已被占用，请联系系统管理员重新生成邀请")
    password_policy.enforce_password_change(
        conn, user_id=None, new_password=password, current_password_hash=None,
    )

    user_id = _create_invited_user(conn, row, password)
    stamp = now()
    conn.execute(
        "UPDATE user_invitations SET accepted_at=?, accepted_user_id=? WHERE id=?",
        (stamp, user_id, row["id"]),
    )
    conn.commit()
    _record_invitation_audit("provisioning.invitation_accept", row["id"], username, user_id)
    session_token = create_session(user_id, user_agent=user_agent, ip=ip)
    return {"user_id": user_id, "username": username, "session_token": session_token, "header": "X-Manju-Session"}


def _create_invited_user(conn: sqlite3.Connection, row, password: str) -> str:
    user_id = new_id("user")
    stamp = now()
    conn.execute(
        "INSERT INTO users(id, username, display_name, password_hash, status, is_system_admin,"
        " must_change_password, created_at, tier, quota_period_started_at, org_id, email, created_by)"
        " VALUES(?,?,?,?,'active',0,0,?,'free',?,?,?,?)",
        (user_id, row["username"], row["display_name"] or row["username"], hash_password(password),
         stamp, stamp, row["org_id"], row["email"] or None, f"invitation:{row['id']}"),
    )
    if row["team_id"] and row["role_id"]:
        orgs_store.add_team_member(
            conn, team_id=row["team_id"], user_id=user_id, role_id=row["role_id"],
            created_by=f"invitation:{row['id']}",
        )
    return user_id


def sweep_expired(*, grace_s: float = _SWEEP_GRACE_S) -> int:
    """物理删除早已失效、且已经过了 ``grace_s`` 宽限期的邀请行（过期未接受/
    已撤销/已接受）——审计已在 ``operation_audit`` 留痕，这里只回收台账体积，
    不是唯一记录来源。挂载点见 ``app.audit.retention``。"""
    conn = get_conn()
    _ensure_schemas(conn)
    cutoff = now() - grace_s
    cur = conn.execute(
        "DELETE FROM user_invitations WHERE "
        "(accepted_at IS NOT NULL AND accepted_at < ?) OR "
        "(revoked_at IS NOT NULL AND revoked_at < ?) OR "
        "(accepted_at IS NULL AND revoked_at IS NULL AND expires_at < ?)",
        (cutoff, cutoff, cutoff),
    )
    conn.commit()
    return cur.rowcount


def _record_invitation_audit(event: str, invitation_id: str, username: str, actor: str) -> None:
    from app.audit import recorder

    recorder.record_command(
        event, "邀请链接", recorder.current_source(), "ok", None,
        f"{event}：{username}（{invitation_id}）", None, None,
        {"invitation_id": invitation_id, "username": username, "actor": actor}, None,
    )
