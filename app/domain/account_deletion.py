"""账号删除：自删（立即级联彻底清空）与管理员软删（30 天保留、可恢复）。

两条路径共用同一批"级联清空/软删/恢复某账号名下项目"的辅助函数，唯一的领域
层入口，供 ``app.auth.api``（自删）与 ``app.auth.admin_api``（管理员软删/恢复/
到期清理）调用。这里要复用的项目彻底清理/软删/恢复逻辑（供应商付费任务核对、
后台协程取消、证据与磁盘产物删除）只存在于 ``app.domain.projects``（L5），没有
安全的办法在别处重新实现一份而不重复其正确性风险。

（本模块初版落地时 ``app.auth.api``/``app.auth.admin_api`` 声明为 L2，调用本
模块被算成上行边，一度在 ``app/LAYERS.toml`` 登记了两条 ``allowed_exceptions``。
2026-08-30 查明那是层号判据挂错了：这两个模块和 ``app.api``/``app.system_api``
一样是只被 ``app.main`` 引用的 HTTP 路由模块，按"碰 app.db"判成 L2，而
``app.api`` 同样碰 db 却是 L5。改成 L5 后这两条边本就是合法的同层调用，豁免
已删除——违规总数不变，白名单少了两条。）

层号：本文件在 ``app.domain`` 包前缀下，继承 ``app/LAYERS.toml`` 里
``"app.domain" = 5`` 的声明，无需单独再声明一条更具体的 key。

两种保留期的区分见 ``app.domain.projects``：
``PROJECT_RECYCLE_BIN_RETENTION_S``（24 小时，用户自己删单个项目）与
``ACCOUNT_DELETE_RETENTION_S``（30 天，账号级联软删除带出的项目，与账号自身
的保留期绑定一致）。账号恢复只恢复"这次账号级联"标记过 30 天保留期的项目，
用户此前自己放进回收站的项目保留原判——账号恢复不应该顺带撤销用户自己独立
做过的删除操作。

自删是 fail-closed 全有全无：任何一个项目还有未到终态的供应商付费任务，整单
直接拒绝，不做"删掉一半"——不可逆操作没有人能在事后为用户补救半途状态。
管理员软删/恢复则复用既有回收站的"单项失败不阻塞其余"惯例（同
``app.domain.projects._purge_all_deleted_projects_core``），因为它本身是可
逆的，个别项目的失败可以留到下一轮重试。
"""
from __future__ import annotations

from fastapi import HTTPException

from app.auth.principal import get_current_principal
from app.capabilities import inputs as I
from app.capabilities.handlers.common import call_guarded, succeeded
from app.capabilities.schemas import CommandResult
from app.db import get_conn, now


def _last_system_admin_guard(conn, user_id: str) -> None:
    """拒绝删除会让系统归零管理员的账号——自删、互删都要拦。

    只统计"当前仍然活跃"的管理员（``deleted_at IS NULL``），与
    ``app.auth.admin_api.update_user`` 里取消管理员身份时的同款判据一致。
    """
    row = conn.execute("SELECT is_system_admin FROM users WHERE id=?", (user_id,)).fetchone()
    if not row or not bool(row["is_system_admin"]):
        return
    remaining = conn.execute(
        "SELECT COUNT(*) c FROM users WHERE is_system_admin=1 AND deleted_at IS NULL AND id!=?",
        (user_id,),
    ).fetchone()["c"]
    if remaining == 0:
        raise HTTPException(422, "系统至少保留一个系统管理员账号，不能删除最后一个管理员")


def _reraise_provider_lock_as_409(exc: Exception) -> None:
    """把 ``ProviderTasksNotTerminalError``（继承自 ``ValueError``）转成 409。

    本模块的路由不经 Command Bus（``app.capabilities.dispatch``），没有
    ``dispatch()`` 那道自动的 ValueError -> 409 翻译（CLAUDE.md：同一个异常在
    不同入口表现不一致就是缺口）。在这里统一兜底转换，而不是指望每个调用方
    各自记得处理。
    """
    from app.completion_grant import ProviderTasksNotTerminalError

    if isinstance(exc, ProviderTasksNotTerminalError):
        raise HTTPException(409, exc.detail) from exc
    raise exc


async def _cascade_purge_owner_projects(owner_user_id: str) -> dict:
    """立即彻底清空某账号名下**全部**项目（自删专用）：不论项目此刻是活跃还是
    已经在用户自己的回收站里，一律硬删数据库行与磁盘产物。不接收部分失败——
    调用方必须先用 ``assert_provider_tasks_clearable`` 把关，这里只负责执行。
    """
    from app.domain.projects import _delete_project_core, _purge_project_core

    conn = get_conn()
    project_ids = [
        row["id"] for row in conn.execute(
            "SELECT id FROM projects WHERE owner_user_id=?", (owner_user_id,)
        ).fetchall()
    ]
    purged: list[str] = []
    for pid in project_ids:
        row = conn.execute("SELECT deleted_at FROM projects WHERE id=?", (pid,)).fetchone()
        if row is None:
            continue
        try:
            if row["deleted_at"] is None:
                await _delete_project_core(pid)
            await _purge_project_core(pid)
        except Exception as exc:  # noqa: BLE001 - 统一翻译后原样上抛，终止整单自删
            _reraise_provider_lock_as_409(exc)
        purged.append(pid)
    return {"purged": purged, "purged_count": len(purged)}


async def _cascade_soft_delete_owner_projects(owner_user_id: str, *, stamp: float) -> dict:
    """管理员软删账号：把该账号名下"当前活跃"的项目一并移入回收站，标记 30 天
    保留期。已经在用户自己回收站里的项目保留原有 24 小时时钟，不被这次账号
    级联改动——那是用户自己独立做过的删除操作。

    单个项目失败（多半是供应商付费任务未到终态）不阻塞其余项目，收集进
    ``failed`` 返回，与 ``app.domain.projects._purge_all_deleted_projects_core``
    同一惯例：这是可逆操作，下一轮可以重试。
    """
    from app.domain.projects import ACCOUNT_DELETE_RETENTION_S, _delete_project_core

    conn = get_conn()
    active_ids = [
        row["id"] for row in conn.execute(
            "SELECT id FROM projects WHERE owner_user_id=? AND deleted_at IS NULL",
            (owner_user_id,),
        ).fetchall()
    ]
    soft_deleted: list[str] = []
    failed: list[dict] = []
    for pid in active_ids:
        # 保留期标记必须写在软删**之前**。两步各自提交、无法合成一个事务
        # （_delete_project_core 自带连接与提交，还要删磁盘产物），所以中间必然
        # 有一个崩溃窗口——能选的只是这个窗口落在哪个状态上：
        #   先删后标记：崩溃后项目 deleted_at 已置、retention 为 NULL，被
        #     sweep 的 COALESCE 判成 24 小时，30 天可恢复期缩水成 1 天。丢数据。
        #   先标记后删：崩溃后项目仍是活跃的（deleted_at IS NULL），只是多带一个
        #     retention 值，而该值只在 deleted_at 非空时才被 sweep 读到——完全无害。
        # 破坏性操作的原子性做不到时，把不可逆的那一步放最后（CLAUDE.md）。
        conn.execute(
            "UPDATE projects SET recycle_bin_retention_s=? WHERE id=? AND owner_user_id=?",
            (ACCOUNT_DELETE_RETENTION_S, pid, owner_user_id),
        )
        conn.commit()
        try:
            await _delete_project_core(pid)
        except Exception as exc:  # noqa: BLE001 - 单项失败不阻塞账号软删的其余项目
            # 回滚必须排在任何日志/recorder 调用之前——它们各自都可能隐式提交。
            conn.rollback()
            # 这个项目没被删成，把刚打上的标记撤掉，免得用户日后自己删它时
            # 意外拿到 30 天而不是 24 小时保留期。
            conn.execute(
                "UPDATE projects SET recycle_bin_retention_s=NULL "
                "WHERE id=? AND owner_user_id=? AND deleted_at IS NULL",
                (pid, owner_user_id),
            )
            conn.commit()
            from app.errors import log_error
            rec = log_error(
                exc, action="account_soft_delete_cascade", context={"project_id": pid},
                meta={"stage": "account_soft_delete", "owner_user_id": owner_user_id},
            )
            failed.append({"project_id": pid, "error_id": rec.error_id, "error": str(exc)})
            continue
        soft_deleted.append(pid)
    return {
        "soft_deleted": soft_deleted, "soft_deleted_count": len(soft_deleted), "failed": failed,
    }


async def _cascade_restore_owner_projects(owner_user_id: str) -> dict:
    """管理员恢复账号：只恢复"这次账号级联"标记过 30 天保留期的项目。"""
    from app.domain.projects import ACCOUNT_DELETE_RETENTION_S, _restore_project_core

    conn = get_conn()
    cascade_ids = [
        row["id"] for row in conn.execute(
            "SELECT id FROM projects WHERE owner_user_id=? AND deleted_at IS NOT NULL "
            "AND recycle_bin_retention_s=?",
            (owner_user_id, ACCOUNT_DELETE_RETENTION_S),
        ).fetchall()
    ]
    restored: list[str] = []
    failed: list[dict] = []
    for pid in cascade_ids:
        try:
            await _restore_project_core(pid)
        except Exception as exc:  # noqa: BLE001 - 单项失败不阻塞账号恢复的其余项目
            from app.errors import log_error
            rec = log_error(
                exc, action="account_restore_cascade", context={"project_id": pid},
                meta={"stage": "account_restore", "owner_user_id": owner_user_id},
            )
            failed.append({"project_id": pid, "error_id": rec.error_id, "error": str(exc)})
            continue
        conn.execute(
            "UPDATE projects SET recycle_bin_retention_s=NULL WHERE id=? AND owner_user_id=?",
            (pid, owner_user_id),
        )
        conn.commit()
        restored.append(pid)
    return {"restored": restored, "restored_count": len(restored), "failed": failed}


async def self_delete_account_core() -> dict:
    """自删的领域逻辑：确认后立即级联清空全部项目 + 硬删除账号行本身。

    账号行改成真删除（而不是历史上"只能停用不能删除"的既有约定）：这是本次
    功能明确要落地的行为——用户要求"抹掉自己全部作品"，projects 全部硬删后
    ``owner_user_id`` 也不再需要指向任何人。``user_sessions`` 通过
    ``FOREIGN KEY ... ON DELETE CASCADE`` 自动一并清空，不需要额外撤销会话。
    """
    principal = get_current_principal()
    if principal is None:
        raise HTTPException(401, "缺少或无效的本机会话凭证")
    user_id = principal.user_id
    conn = get_conn()
    row = conn.execute("SELECT id FROM users WHERE id=? AND deleted_at IS NULL", (user_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "账号不存在")
    _last_system_admin_guard(conn, user_id)

    project_ids = [
        r["id"] for r in conn.execute(
            "SELECT id FROM projects WHERE owner_user_id=?", (user_id,)
        ).fetchall()
    ]
    # Fail-closed 预检：任何一个项目还有未到终态的供应商付费任务，整单直接
    # 拒绝，不做"删掉一半"。真正的权威校验在下面每个项目各自的删除/清理调用
    # 内部还会再查一次；这里只是提前给用户一个完整、明确的拒绝理由。
    from app.completion_grant import assert_provider_tasks_clearable
    try:
        for pid in project_ids:
            assert_provider_tasks_clearable(project_id=pid, conn=conn)
    except Exception as exc:  # noqa: BLE001 - 统一翻译成 409
        _reraise_provider_lock_as_409(exc)

    project_outcome = await _cascade_purge_owner_projects(user_id)

    conn = get_conn()
    try:
        cur = conn.execute("DELETE FROM users WHERE id=? AND deleted_at IS NULL", (user_id,))
        conn.commit()
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise
    if cur.rowcount != 1:
        raise HTTPException(409, "账号已被删除")
    return {"deleted_user_id": user_id, "projects": project_outcome}


async def admin_soft_delete_account_core(target_user_id: str) -> dict:
    """管理员软删账号：账号与其当前活跃的项目一并进入 30 天保留期，期间可恢复。"""
    from app.domain.projects import ACCOUNT_DELETE_RETENTION_S

    conn = get_conn()
    row = conn.execute(
        "SELECT id FROM users WHERE id=? AND deleted_at IS NULL", (target_user_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(404, "用户不存在")
    _last_system_admin_guard(conn, target_user_id)

    stamp = now()
    project_outcome = await _cascade_soft_delete_owner_projects(target_user_id, stamp=stamp)

    conn = get_conn()
    try:
        cur = conn.execute(
            "UPDATE users SET deleted_at=?, status='disabled' WHERE id=? AND deleted_at IS NULL",
            (stamp, target_user_id),
        )
        conn.commit()
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise
    if cur.rowcount != 1:
        raise HTTPException(409, "账号已被删除")
    # 立即吊销该账号全部会话——软删除等同"此人不能再登录"，不等 7 天滑动
    # 过期自然失效，与 update_user() 禁用账号时的既有处理一致。
    from app.auth.sessions import revoke_all_for_user
    revoke_all_for_user(target_user_id)
    return {
        "deleted_user_id": target_user_id,
        "deleted_at": stamp,
        "purge_at": stamp + ACCOUNT_DELETE_RETENTION_S,
        "projects": project_outcome,
    }


async def admin_restore_account_core(target_user_id: str) -> dict:
    """管理员在 30 天保留期内恢复账号：清空 ``deleted_at``，账号状态改回可用，
    并把这次账号级联软删除带出的项目一并恢复。"""
    conn = get_conn()
    row = conn.execute(
        "SELECT id FROM users WHERE id=? AND deleted_at IS NOT NULL", (target_user_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(404, "回收站中不存在该账号")

    try:
        cur = conn.execute(
            "UPDATE users SET deleted_at=NULL, status='active' WHERE id=? AND deleted_at IS NOT NULL",
            (target_user_id,),
        )
        conn.commit()
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise
    if cur.rowcount != 1:
        raise HTTPException(404, "回收站中不存在该账号")
    project_outcome = await _cascade_restore_owner_projects(target_user_id)
    return {"restored_user_id": target_user_id, "projects": project_outcome}


async def sweep_expired_deleted_accounts() -> dict:
    """30 天保留期到期的账号自动彻底清理；由周期性系统任务调用（见
    ``app.recovery.account_recycle_bin_sweep_loop``）。判据是 ``users.deleted_at``
    时间戳，不依赖内存计时器。清理前先兜底清空该账号名下**任何**仍然残留的
    项目（正常情况下账号软删除时已经把活跃项目都标进了回收站、由项目级
    sweep 按同一个 30 天时钟单独清理；这里的兜底只覆盖两个 sweep 节奏错开的
    边界窗口），保证账号行硬删除时不会留下指向它的项目行。
    """
    conn = get_conn()
    cutoff = now() - _account_delete_retention_s()
    user_ids = [
        row["id"] for row in conn.execute(
            "SELECT id FROM users WHERE deleted_at IS NOT NULL AND deleted_at < ?",
            (cutoff,),
        ).fetchall()
    ]
    purged: list[str] = []
    failed: list[dict] = []
    for uid in user_ids:
        try:
            await _cascade_purge_owner_projects(uid)
            cur = conn.execute("DELETE FROM users WHERE id=? AND deleted_at IS NOT NULL", (uid,))
            conn.commit()
            if cur.rowcount != 1:
                continue
            purged.append(uid)
        except Exception as exc:  # noqa: BLE001 - 单个账号失败不阻塞其余到期账号
            from app.errors import log_error
            rec = log_error(
                exc, action="account_recycle_bin_sweep", context={"user_id": uid},
                meta={"stage": "account_recycle_bin_sweep"},
            )
            failed.append({"user_id": uid, "error_id": rec.error_id, "error": str(exc)})
    return {"purged": purged, "purged_count": len(purged), "failed": failed}


def _account_delete_retention_s() -> float:
    from app.domain.projects import ACCOUNT_DELETE_RETENTION_S
    return ACCOUNT_DELETE_RETENTION_S


async def self_delete_handler(args: I.AccountSelfDeleteInput) -> CommandResult:
    outcome = await call_guarded(self_delete_account_core)
    if isinstance(outcome, CommandResult):
        return outcome
    return succeeded(
        f"账号 {outcome['deleted_user_id']} 已删除，级联清空 {outcome['projects']['purged_count']} 个项目",
        data=outcome,
    )


async def admin_soft_delete_handler(args: I.AccountAdminDeleteInput) -> CommandResult:
    outcome = await call_guarded(admin_soft_delete_account_core, args.user_id)
    if isinstance(outcome, CommandResult):
        return outcome
    return succeeded(f"账号 {args.user_id} 已软删除，30 天内可恢复", data=outcome)


async def admin_restore_handler(args: I.AccountAdminRestoreInput) -> CommandResult:
    outcome = await call_guarded(admin_restore_account_core, args.user_id)
    if isinstance(outcome, CommandResult):
        return outcome
    return succeeded(f"账号 {args.user_id} 已恢复", data=outcome)


__all__ = [name for name in globals() if not name.startswith("__")]
