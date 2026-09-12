"""RBAC 运维面：账号管理（系统管理员专用）。

账号即项目空间：1 个账号 = 1 个独立项目空间，不再有「团队/工作空间」这个中间
概念，因此开户不再需要选团队、指角色——只有「是不是系统管理员」这一个维度。
用户已定「管理员开户，无自助注册」——这句话必须落地成一个真正能用的入口，
否则系统管理员只能登服务器跑 ``scripts/create_admin.py``，日常运营不现实。
这里补的就是那个入口：开户、改密、启停账号，全部只对系统管理员开放。

不做的事（有意）：不做自助注册、不做角色自定义（只有系统管理员/普通用户两档）、
不做跨租户管理——这些都在 PRD 里明确排除，此处不重复造。

以下一处「运维时必须绕开产品」是**已知且有意**的，不是遗漏：

1. **首个系统管理员靠 scripts/create_admin.py 直接写库**。先有鸡后有蛋：没有管理员
   就没人能调用本模块的开户接口。这条无法也不该在产品内解决。

账号删除（2026-08-30 起支持，见 ``app.domain.account_deletion``）：
管理员删除用户账号是**软删除 + 30 天保留期**，不是历史上「只能停用不能删除」
的旧约定——账号与其当前活跃的项目一并移入回收站，期间可在 ``POST
/users/{user_id}/restore`` 恢复，到期由后台巡检彻底清理（数据库行 + 磁盘产
物），不再保留旧版「代价是用户名被永久占用」的说法：真正物理删除之后用户
名会被释放。用户名唯一性约束在 30 天保留窗口内仍然生效（软删除的行还在
表里），与项目回收站同一套让步。删除前会拦最后一个系统管理员（自删/互删
都拦），避免系统归零管理员；账号本身的**自删**入口在 ``app.auth.api``。
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException

from app.audit.queries import apply_activity_summary
from app.auth import password_policy, session_policy
from app.auth.deps import require_system_admin
from app.auth.passwords import hash_password
from app.auth.principal import Principal
from app.db import get_conn, new_id, now
from app.provisioning import schema as provisioning_schema
from app.quota import VALID_TIERS
from app.quota_addon import (
    ADDON_PACKAGE_PRICE_CNY,
    ADDON_PACKAGE_SECONDS,
    addon_video_seconds_balance,
    grant_video_addon_seconds,
)

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
        "deleted_at": (
            user_row["deleted_at"] if "deleted_at" in user_row.keys() else None
        ),
        # 「最近活跃」：由 list_users() 用 apply_activity_summary() 原地填充，
        # 恒带这两个键（不是可选字段）——界面承诺展示的字段必须永远存在。
        "last_active_at": None,
        "last_action": None,
    }


@router.get("/users", dependencies=[Depends(require_system_admin)])
def list_users():
    """活跃账号列表——软删除（回收站）中的账号不在此列，见
    ``GET /users/deleted``。必须注册在 ``GET /users/deleted`` 之前，理由与
    ``app.domain.projects.list_deleted_projects`` 相同：都是同一静态前缀段。
    """
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM users WHERE deleted_at IS NULL ORDER BY created_at DESC"
    ).fetchall()
    items = [_user_payload(r) for r in rows]
    apply_activity_summary(items)
    return {"items": items}


@router.get("/users/deleted", dependencies=[Depends(require_system_admin)])
def list_deleted_users():
    """回收站：已软删除但还没到 30 天保留期（或还没被后台彻底清理）的账号。"""
    # 只经 app.domain.account_deletion 这一个出口（见该模块 docstring）：保留期
    # 常量的唯一来源在那里，不直接 import app.domain.projects，避免同一个判据
    # 在两处各有一份。（原注释说这是为了避开 LAYERS.toml 的 allowed_exceptions
    # ——2026-08-30 本模块按其真实职责改声明为 L5，那两条豁免已删除，本模块调
    # app.domain 是同层边，不再需要豁免。）
    from app.domain.account_deletion import _account_delete_retention_s

    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM users WHERE deleted_at IS NOT NULL ORDER BY deleted_at DESC"
    ).fetchall()
    stamp = now()
    items = []
    for r in rows:
        payload = _user_payload(r)
        purge_at = float(r["deleted_at"]) + _account_delete_retention_s()
        payload["purge_at"] = purge_at
        payload["retention_seconds_remaining"] = max(0.0, purge_at - stamp)
        items.append(payload)
    return {"items": items}


@router.post("/users", dependencies=[Depends(require_system_admin)])
def create_user(body: dict):
    """管理员开户：新建账号。默认要求首次登录改密。"""
    username = str(body.get("username") or "").strip()
    password = str(body.get("password") or "")
    if not username:
        raise HTTPException(422, "用户名不能为空")
    conn = get_conn()
    # 全新账号（user_id=None）：只做强度校验，没有历史口令可比对。
    password_policy.enforce_password_change(conn, user_id=None, new_password=password, current_password_hash=None)
    tier = str(body.get("tier") or "free").strip()
    if tier not in VALID_TIERS:
        raise HTTPException(422, f"tier 必须是 {'/'.join(sorted(VALID_TIERS))} 之一")
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
    """管理员修改账号资料或状态。字段包括显示名/档位/启停/改密/管理员标记。"""
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM users WHERE id=? AND deleted_at IS NULL", (user_id,)
    ).fetchone()
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

    old_password_hash = row["password_hash"]
    if body.get("password"):
        extra_fields, extra_values = _apply_password_reset(conn, user_id, row, body)
        fields.extend(extra_fields)
        values.extend(extra_values)

    if not fields:
        raise HTTPException(422, "没有可更新的字段")
    values.append(user_id)
    conn.execute(f"UPDATE users SET {', '.join(fields)} WHERE id=?", values)
    conn.commit()
    if body.get("password"):
        # 归档**被替换掉**的旧哈希，必须在新密码真正落库之后（见
        # app.auth.password_policy.record_password_change 模块文档）。
        password_policy.record_password_change(conn, user_id=user_id, old_password_hash=old_password_hash)
    row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    return _user_payload(row)


def _apply_password_reset(conn, user_id: str, row, body: dict) -> tuple[list[str], list[object]]:
    """校验新密码（强度 + 历史重用）、吊销该账号现有会话、返回要拼进
    ``UPDATE users`` 的 ``(fields, values)`` 片段。"""
    password = str(body["password"])
    password_policy.enforce_password_change(
        conn, user_id=user_id, new_password=password, current_password_hash=row["password_hash"],
    )
    # 改密强制下线该账号已有会话，含当前这一个——管理员重置别人密码后，
    # 对方需要用新密码重新登录；重置自己密码同理。
    conn.execute(
        "UPDATE user_sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL",
        (now(), user_id),
    )
    return (
        ["password_hash=?", "password_changed_at=?", "must_change_password=?"],
        [hash_password(password), now(), 1 if body.get("must_change_password", True) else 0],
    )


@router.post(
    "/users/{user_id}/video-addons", dependencies=[Depends(require_system_admin)]
)
async def grant_video_addon_route(
    user_id: str, body: dict, actor: Principal = Depends(require_system_admin)
):
    """REST 入口：先过 Command Bus（``quota.grant_video_addon`` 在 catalog 里登
    记 ``confirmation=ALWAYS``），落回本模块同名领域函数执行。此前这个端点直接
    调 ``grant_video_addon``、完全绕过总线——是 catalog 里唯一一个声明了
    "confirm=always" 却在真实 REST 路径上没有任何确认/幂等/审计闸门的能力
    （2026-08-30 排查修复；``account.self_delete``/``admin_soft_delete`` 之类同样
    绕过总线的账号操作各自有自己的等价确认闸门或本就 confirm=never，不在此列）。
    """
    from app.capabilities.dispatch import ui_route

    routed = await ui_route(
        "quota.grant_video_addon",
        {
            "user_id": user_id,
            "packages": body.get("packages"),
            "idempotency_key": str(body.get("idempotency_key") or ""),
        },
    )
    if routed is not None:
        return routed
    return grant_video_addon(user_id, body, actor=actor)


def grant_video_addon(
    user_id: str, body: dict, actor: Principal = Depends(require_system_admin)
):
    """领域实现：由 ``grant_video_addon_route``（REST）与
    ``app.capabilities.handlers.system.quota_grant_video_addon``（Command Bus
    handler）共用同一份逻辑，两条入口不得各自实现一份（用户存在性、packages
    取值域、审计落账都在这里）。

    本次不接真实支付（见 app/quota.py 模块文档）：每包 ``ADDON_PACKAGE_SECONDS``
    秒，¥``ADDON_PACKAGE_PRICE_CNY``/包，不随该账号的 30 天配额周期重置。
    ``idempotency_key`` 由调用方提供时用它做幂等标识（未来接真实支付应传订单
    号）；不提供则生成一次性 key——这种情况下重复调用本函数会重复发放，责任在
    调用方（人工操作，未接支付系统前无法从请求本身识别"这是不是同一笔购买"）。
    """
    conn = get_conn()
    row = conn.execute("SELECT id FROM users WHERE id=?", (user_id,)).fetchone()
    if not row:
        raise HTTPException(404, "用户不存在")

    packages = body.get("packages")
    if not isinstance(packages, int) or isinstance(packages, bool) or packages < 1:
        raise HTTPException(422, "packages 必须是正整数（每包 10 分钟视频）")

    idem_key = str(body.get("idempotency_key") or "").strip() or new_id("addon")
    result = grant_video_addon_seconds(conn, user_id, packages=packages, attempt_key=idem_key)
    conn.commit()

    # 直接写 monitor_audit（不经 app.monitoring.audit()）：monitor_audit 表结构
    # 简单（id/ts/action/object_type/object_id/outcome/detail_json），直接 INSERT
    # 不需要那层封装，且这条 INSERT 与上面的额度发放共用同一个 conn/同一次提交，
    # 走便捷函数反而会引入第二条连接、把"发了额度但没落审计"变成可能。
    # （原注释说本模块是 L2、跨层引入会撞上行边闸门——2026-08-30 已把本模块按其
    # 真实职责改声明为 L5，与 app.api/app.system_api 同层，那个理由不再成立。）
    conn.execute(
        "INSERT INTO monitor_audit(id,ts,action,object_type,object_id,outcome,"
        "detail_json) VALUES(?,?,?,?,?,?,?)",
        (
            new_id("audit"), now(), "grant", "quota_addon", user_id, "ok",
            json.dumps(
                {
                    "admin_id": actor.user_id, "packages": packages,
                    "seconds_granted": result["granted_s"],
                    "idempotent_replay": result["idempotent_replay"],
                    "attempt_key": idem_key,
                    "price_cny": packages * ADDON_PACKAGE_PRICE_CNY,
                },
                ensure_ascii=False,
            ),
        ),
    )
    conn.commit()
    return {
        "user_id": user_id,
        "packages": packages,
        "package_seconds": ADDON_PACKAGE_SECONDS,
        "price_cny": packages * ADDON_PACKAGE_PRICE_CNY,
        "attempt_key": idem_key,
        "seconds_granted": result["granted_s"],
        "idempotent_replay": result["idempotent_replay"],
        "addon_balance_s": addon_video_seconds_balance(conn, user_id),
    }


@router.delete("/users/{user_id}", dependencies=[Depends(require_system_admin)])
async def delete_user(user_id: str):
    """管理员删除用户账号：软删除，30 天保留期，期间可 ``restore`` 恢复。

    EP-03 §5 前置闸门：名下若仍有**活跃**项目（未处置资产），拒绝删除并
    返回 409 + 资产清单 + ``handover`` 所需的全部参数（CLAUDE.md「拦住用户
    时必须给出路」）——EP-01 落地协作后，直接把项目连同账号一起丢进回收站
    会让全部协作者一起失联，必须先移交给同事或团队，见
    ``app.provisioning.handover`` 模块文档「现状缺口」一节。移交后名下活跃
    项目清零，本函数下面这段既有的软删/30 天保留逻辑不变。
    """
    from app.domain.account_deletion import admin_soft_delete_account_core
    from app.provisioning.handover import has_unresolved_assets, list_user_assets

    if has_unresolved_assets(user_id):
        raise HTTPException(
            409,
            {
                "message": "该账号名下仍有未处置的活跃项目，请先移交后再删除",
                "assets": list_user_assets(user_id),
                "handover_endpoint": f"/api/system/users/{user_id}/handover",
                "handover_params": {
                    "to_user_id": "接收账号的 user_id；与 to_team_id 二选一",
                    "to_team_id": "接收团队的 team_id；与 to_user_id 二选一",
                },
            },
        )

    outcome = await admin_soft_delete_account_core(user_id)
    return {"ok": True, **outcome}


@router.post("/users/{user_id}/restore", dependencies=[Depends(require_system_admin)])
async def restore_user(user_id: str):
    """30 天保留期内恢复被软删除的用户账号，级联恢复其被本次删除带出的项目。"""
    from app.domain.account_deletion import admin_restore_account_core

    outcome = await admin_restore_account_core(user_id)
    return {"ok": True, **outcome}


@router.get("/users/{user_id}/sessions", dependencies=[Depends(require_system_admin)])
def list_user_sessions(user_id: str):
    """EP-03 第二阶段会话策略：查看某账号当前的活跃会话，供管理员判断要不要
    强制下线其中某一个。只返回未撤销且未过期的行，不返回 ``secret_hash``。
    ``kind`` 与 ``last_seen_at`` 恒存在（第二轮起）：前者供界面单独标出服务
    会话，后者供管理员判断"这枚服务凭证是不是已经没人用了"。"""
    conn = get_conn()
    provisioning_schema.ensure_tables_on_connection(conn)  # kind 补列，同连接不抢锁
    rows = conn.execute(
        "SELECT id, created_at, last_seen_at, expires_at, ip, user_agent, kind FROM user_sessions"
        " WHERE user_id=? AND revoked_at IS NULL AND expires_at>? ORDER BY created_at DESC",
        (user_id, now()),
    ).fetchall()
    return {"items": [dict(r) for r in rows]}


@router.post("/users/{user_id}/service-sessions", dependencies=[Depends(require_system_admin)])
def issue_service_session_route(user_id: str, body: dict):
    """管理员签发一枚长期服务会话（EP-03 第二阶段第二轮）：供回归/驱动脚本
    等自动化使用，豁免空闲超时/并发上限，但必须显式给定有限的 ``ttl_days``
    （见 ``app.auth.session_policy.issue_service_session``）。返回体带明文
    token，只这一次——前端必须当场展示，供管理员复制去替换
    ``data/regression_session_token.txt`` 之类的既有凭证文件。"""
    conn = get_conn()
    if conn.execute("SELECT 1 FROM users WHERE id=? AND deleted_at IS NULL", (user_id,)).fetchone() is None:
        raise HTTPException(404, "用户不存在")
    ttl_days = body.get("ttl_days")
    if not isinstance(ttl_days, (int, float)) or isinstance(ttl_days, bool):
        raise HTTPException(422, "ttl_days 必须是数值")
    token, expires_at = session_policy.issue_service_session(user_id, ttl_days=float(ttl_days))
    return {"session_token": token, "header": "X-Manju-Session", "expires_at": expires_at}


@router.post("/users/{user_id}/sessions/{session_id}/revoke", dependencies=[Depends(require_system_admin)])
def revoke_user_session(user_id: str, session_id: str):
    """管理员强制下线单个会话；下一次该 token 发起请求会看到具体原因
    （"管理员已强制下线此会话"），不是笼统的会话失效（CLAUDE.md「拦住用户时
    必须给出路」）。"""
    conn = get_conn()
    provisioning_schema.ensure_tables_on_connection(conn)  # revoked_reason 补列，同连接不抢锁
    cur = conn.execute(
        "UPDATE user_sessions SET revoked_at=?, revoked_reason=? WHERE id=? AND user_id=? AND revoked_at IS NULL",
        (now(), "admin_revoked", session_id, user_id),
    )
    conn.commit()
    if cur.rowcount == 0:
        raise HTTPException(404, "会话不存在或已失效")
    return {"ok": True}
