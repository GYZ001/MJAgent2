"""RBAC 运维面：账号与团队成员管理（系统管理员专用）。

用户已定「管理员开户，无自助注册」——这句话必须落地成一个真正能用的入口，
否则系统管理员只能登服务器跑 ``scripts/create_admin.py``，日常运营不现实。
这里补的就是那个入口：开户、改密、启停账号、建团队、管成员角色，全部只对
系统管理员开放。

不做的事（有意）：不做自助注册、不做角色自定义（五档角色固定）、不做跨租户
管理——这些都在 PRD 里明确排除，此处不重复造。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app.auth.deps import require_system_admin
from app.auth.passwords import hash_password
from app.auth.principal import ROLE_SCOPES, Principal
from app.db import get_conn, new_id, now

router = APIRouter(prefix="/api/system", tags=["admin"])

_VALID_ROLES = tuple(ROLE_SCOPES.keys())


def _user_payload(conn, user_row) -> dict:
    members = conn.execute(
        "SELECT wm.workspace_id, wm.role, w.name AS workspace_name "
        "FROM workspace_members wm JOIN workspaces w ON w.id = wm.workspace_id "
        "WHERE wm.user_id=? ORDER BY w.created_at",
        (user_row["id"],),
    ).fetchall()
    return {
        "id": user_row["id"],
        "username": user_row["username"],
        "display_name": user_row["display_name"],
        "status": user_row["status"],
        "is_system_admin": bool(user_row["is_system_admin"]),
        "must_change_password": bool(user_row["must_change_password"]),
        "created_at": user_row["created_at"],
        "last_login_at": user_row["last_login_at"],
        "workspaces": [
            {"id": m["workspace_id"], "name": m["workspace_name"], "role": m["role"]}
            for m in members
        ],
    }


@router.get("/users", dependencies=[Depends(require_system_admin)])
def list_users():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM users ORDER BY created_at DESC").fetchall()
    return {"items": [_user_payload(conn, r) for r in rows]}


@router.post("/users", dependencies=[Depends(require_system_admin)])
def create_user(body: dict):
    username = str(body.get("username") or "").strip()
    password = str(body.get("password") or "")
    if not username:
        raise HTTPException(422, "用户名不能为空")
    if len(password) < 8:
        raise HTTPException(422, "密码至少 8 位")
    conn = get_conn()
    if conn.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
        raise HTTPException(409, "用户名已存在")

    workspace_id = str(body.get("workspace_id") or "").strip() or None
    role = str(body.get("role") or "readonly")
    if workspace_id:
        if role not in _VALID_ROLES:
            raise HTTPException(422, f"角色必须是：{', '.join(_VALID_ROLES)}")
        if not conn.execute("SELECT 1 FROM workspaces WHERE id=?", (workspace_id,)).fetchone():
            raise HTTPException(404, "团队不存在")

    user_id = new_id("user")
    conn.execute(
        "INSERT INTO users(id, username, display_name, password_hash, status, "
        "is_system_admin, must_change_password, created_at) VALUES(?,?,?,?,?,?,?,?)",
        (
            user_id, username, str(body.get("display_name") or username).strip() or username,
            hash_password(password), "active",
            1 if body.get("is_system_admin") else 0,
            1 if body.get("must_change_password", True) else 0,
            now(),
        ),
    )
    if workspace_id:
        conn.execute(
            "INSERT INTO workspace_members(workspace_id, user_id, role, created_at) VALUES(?,?,?,?)",
            (workspace_id, user_id, role, now()),
        )
    conn.commit()
    row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    return _user_payload(conn, row)


@router.put("/users/{user_id}", dependencies=[Depends(require_system_admin)])
def update_user(user_id: str, body: dict, actor: Principal = Depends(require_system_admin)):
    conn = get_conn()
    row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if not row:
        raise HTTPException(404, "用户不存在")

    is_self = user_id == actor.user_id
    fields: list[str] = []
    values: list[object] = []

    if "display_name" in body:
        fields.append("display_name=?")
        values.append(str(body["display_name"] or "").strip() or row["username"])

    if "status" in body:
        status = str(body["status"])
        if status not in ("active", "disabled"):
            raise HTTPException(422, "status 必须是 active 或 disabled")
        if is_self and status == "disabled":
            raise HTTPException(422, "不能禁用自己当前登录的账号")
        fields.append("status=?")
        values.append(status)
        if status == "disabled":
            # 立即吊销该账号的全部会话，不等 12 小时滑动过期自然失效。
            conn.execute(
                "UPDATE user_sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL",
                (now(), user_id),
            )

    if "is_system_admin" in body:
        want_admin = bool(body["is_system_admin"])
        if is_self and not want_admin:
            raise HTTPException(422, "不能取消自己的系统管理员身份")
        if not want_admin and bool(row["is_system_admin"]):
            remaining = conn.execute(
                "SELECT COUNT(*) c FROM users WHERE is_system_admin=1 AND id!=?", (user_id,)
            ).fetchone()["c"]
            if remaining == 0:
                raise HTTPException(422, "系统至少保留一个系统管理员账号")
        fields.append("is_system_admin=?")
        values.append(1 if want_admin else 0)

    if body.get("password"):
        password = str(body["password"])
        if len(password) < 8:
            raise HTTPException(422, "密码至少 8 位")
        fields.append("password_hash=?")
        values.append(hash_password(password))
        fields.append("password_changed_at=?")
        values.append(now())
        fields.append("must_change_password=?")
        values.append(1 if body.get("must_change_password", True) else 0)
        # 改密强制下线该账号已有会话，含当前这一个——管理员重置别人密码后，
        # 对方需要用新密码重新登录；重置自己密码同理。
        conn.execute(
            "UPDATE user_sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL",
            (now(), user_id),
        )

    if not fields:
        raise HTTPException(422, "没有可更新的字段")
    values.append(user_id)
    conn.execute(f"UPDATE users SET {', '.join(fields)} WHERE id=?", values)
    conn.commit()
    row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    return _user_payload(conn, row)


@router.get("/workspaces", dependencies=[Depends(require_system_admin)])
def list_workspaces():
    conn = get_conn()
    rows = conn.execute(
        "SELECT w.*, "
        "(SELECT COUNT(*) FROM workspace_members m WHERE m.workspace_id=w.id) AS member_count, "
        "(SELECT COUNT(*) FROM projects p WHERE p.workspace_id=w.id) AS project_count "
        "FROM workspaces w ORDER BY w.created_at"
    ).fetchall()
    return {"items": [dict(r) for r in rows]}


@router.post("/workspaces", dependencies=[Depends(require_system_admin)])
def create_workspace(body: dict):
    name = str(body.get("name") or "").strip()
    if not name:
        raise HTTPException(422, "团队名称不能为空")
    conn = get_conn()
    ws_id = new_id("ws")
    conn.execute(
        "INSERT INTO workspaces(id, tenant_id, name, status, created_at) "
        "VALUES(?, 'tenant_default', ?, 'active', ?)",
        (ws_id, name, now()),
    )
    conn.commit()
    return {"id": ws_id, "name": name}


@router.put("/workspaces/{workspace_id}/members/{user_id}", dependencies=[Depends(require_system_admin)])
def set_member_role(workspace_id: str, user_id: str, body: dict):
    role = str(body.get("role") or "")
    if role not in _VALID_ROLES:
        raise HTTPException(422, f"角色必须是：{', '.join(_VALID_ROLES)}")
    conn = get_conn()
    if not conn.execute("SELECT 1 FROM workspaces WHERE id=?", (workspace_id,)).fetchone():
        raise HTTPException(404, "团队不存在")
    if not conn.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone():
        raise HTTPException(404, "用户不存在")
    existing = conn.execute(
        "SELECT 1 FROM workspace_members WHERE workspace_id=? AND user_id=?",
        (workspace_id, user_id),
    ).fetchone()
    if existing:
        conn.execute(
            "UPDATE workspace_members SET role=? WHERE workspace_id=? AND user_id=?",
            (role, workspace_id, user_id),
        )
    else:
        conn.execute(
            "INSERT INTO workspace_members(workspace_id, user_id, role, created_at) VALUES(?,?,?,?)",
            (workspace_id, user_id, role, now()),
        )
    conn.commit()
    return {"ok": True}


@router.delete("/workspaces/{workspace_id}/members/{user_id}", dependencies=[Depends(require_system_admin)])
def remove_member(workspace_id: str, user_id: str, actor: Principal = Depends(require_system_admin)):
    conn = get_conn()
    if user_id == actor.user_id:
        raise HTTPException(422, "不能把自己移出团队，请先请另一位系统管理员操作")
    conn.execute(
        "DELETE FROM workspace_members WHERE workspace_id=? AND user_id=?",
        (workspace_id, user_id),
    )
    conn.commit()
    return {"ok": True}
