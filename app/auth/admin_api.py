"""RBAC 运维面：账号管理（系统管理员专用）。

账号即项目空间：1 个账号 = 1 个独立项目空间，不再有「团队/工作空间」这个中间
概念，因此开户不再需要选团队、指角色——只有「是不是系统管理员」这一个维度。
用户已定「管理员开户，无自助注册」——这句话必须落地成一个真正能用的入口，
否则系统管理员只能登服务器跑 ``scripts/create_admin.py``，日常运营不现实。
这里补的就是那个入口：开户、改密、启停账号，全部只对系统管理员开放。

不做的事（有意）：不做自助注册、不做角色自定义（只有系统管理员/普通用户两档）、
不做跨租户管理——这些都在 PRD 里明确排除，此处不重复造。

以下两处「运维时必须绕开产品」是**已知且有意**的，不是遗漏：

1. **首个系统管理员靠 scripts/create_admin.py 直接写库**。先有鸡后有蛋：没有管理员
   就没人能调用本模块的开户接口。这条无法也不该在产品内解决。
2. **没有「删除用户」，只有停用**。刻意如此：审计表里的 decided_by /
   archived_by 指向 users.id，删账号会让历史操作失去归属；停用已经达到「此人不能
   再登录」的效果，且可逆。代价是用户名被永久占用（UNIQUE），打错字建的账号只能
   停用不能回收——接受这个代价，换审计完整性。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app.auth.deps import require_system_admin
from app.auth.passwords import hash_password
from app.auth.principal import Principal
from app.db import get_conn, new_id, now
from app.quota import VALID_TIERS

router = APIRouter(prefix="/api/system", tags=["admin"])


def _user_payload(user_row) -> dict:
    return {
        "id": user_row["id"],
        "username": user_row["username"],
        "display_name": user_row["display_name"],
        "status": user_row["status"],
        "is_system_admin": bool(user_row["is_system_admin"]),
        "must_change_password": bool(user_row["must_change_password"]),
        "tier": user_row["tier"] if "tier" in user_row.keys() else "free",
        "quota_period_started_at": (
            user_row["quota_period_started_at"]
            if "quota_period_started_at" in user_row.keys()
            else None
        ),
        "created_at": user_row["created_at"],
        "last_login_at": user_row["last_login_at"],
    }


@router.get("/users", dependencies=[Depends(require_system_admin)])
def list_users():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM users ORDER BY created_at DESC").fetchall()
    return {"items": [_user_payload(r) for r in rows]}


@router.post("/users", dependencies=[Depends(require_system_admin)])
def create_user(body: dict):
    username = str(body.get("username") or "").strip()
    password = str(body.get("password") or "")
    if not username:
        raise HTTPException(422, "用户名不能为空")
    if len(password) < 8:
        raise HTTPException(422, "密码至少 8 位")
    tier = str(body.get("tier") or "free").strip()
    if tier not in VALID_TIERS:
        raise HTTPException(422, f"tier 必须是 {'/'.join(sorted(VALID_TIERS))} 之一")
    conn = get_conn()
    if conn.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
        raise HTTPException(409, "用户名已存在")

    user_id = new_id("user")
    stamp = now()
    conn.execute(
        "INSERT INTO users(id, username, display_name, password_hash, status, "
        "is_system_admin, must_change_password, created_at, tier, "
        "quota_period_started_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            user_id, username, str(body.get("display_name") or username).strip() or username,
            hash_password(password), "active",
            1 if body.get("is_system_admin") else 0,
            1 if body.get("must_change_password", True) else 0,
            stamp, tier, stamp,
        ),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    return _user_payload(row)


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
            # 立即吊销该账号的全部会话，不等 7 天滑动过期自然失效。
            conn.execute(
                "UPDATE user_sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL",
                (now(), user_id),
            )

    if "tier" in body:
        tier = str(body["tier"] or "").strip()
        if tier not in VALID_TIERS:
            raise HTTPException(422, f"tier 必须是 {'/'.join(sorted(VALID_TIERS))} 之一")
        fields.append("tier=?")
        values.append(tier)

    if body.get("reset_quota_period"):
        # 管理员显式重置这个账号的 30 天周期锚点（例如手工补偿）；不是自动
        # 到期触发——到期重置本身就是 period_index 前进的自然结果，不需要
        # 写库动作，见 app/quota.py::period_index 的周期算法。
        fields.append("quota_period_started_at=?")
        values.append(now())

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
    return _user_payload(row)
