"""项目回收站生命周期：软删除、恢复、彻底清理（单个/全部/到期自动）。"""
from __future__ import annotations

import shutil

from fastapi import HTTPException

from app import config, task_registry, worker
from app.db import get_conn, now
from app.domain.common import _assert_principal_owns, _project_or_404, router
from app.domain.projects.constants import PROJECT_RECYCLE_BIN_RETENTION_S
from app.domain.projects.evidence import _delete_project_evidence
from app.domain.projects.listing import _listing_owner_scope


def _deleted_project_or_404(project_id: str) -> dict:
    """回收站专用存在性校验：只认已软删除（``deleted_at`` 非空）的项目。

    与 ``_project_or_404``（app.domain.common）互补而非重复：那个函数只认
    "正常"（未删除）项目，这个只认"回收站里"的项目——恢复/彻底清理必须落在
    这条判据上，否则一个还没删除的项目也能被拿来"彻底清理"。
    """
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM projects WHERE id=? AND deleted_at IS NOT NULL", (project_id,)
    ).fetchone()
    if not row:
        raise HTTPException(404, f"回收站中不存在该项目：{project_id}")
    # 与 ``_project_or_404`` 同一条归属判据，理由也相同：HTTP 边界的
    # ``require_project_owner_access`` 只在请求经 ASGI 路由时执行，Agent/MCP
    # 工具调用、内部脚本、测试直接进 domain 函数会完全绕过它。缺了这一行，
    # 「恢复」与「彻底清理」两个端点在非 HTTP 路径上就能按 id 操作**别人**
    # 回收站里的项目——而彻底清理是不可逆的（删库行 + rmtree 产物目录）。
    # 静态 SQL 守卫看不见这个洞：它对 ``WHERE id=?`` 按「主键锚定」放行，
    # 前提是「这个 id 进系统时已过归属闸门」，而这条路径上没有。
    _assert_principal_owns(
        row["owner_user_id"], not_found_detail=f"回收站中不存在该项目：{project_id}"
    )
    return dict(row)


async def _delete_project_core(project_id: str) -> dict:
    """软删除的领域逻辑，供 REST 路由与 ``project.delete`` Command Handler 共用。

    只把项目标记进回收站（``deleted_at``），不删除任何数据库行、不碰磁盘文件。
    24 小时后由 ``sweep_expired_deleted_projects`` 自动彻底清理，用户也可以
    随时在回收站里手动恢复或彻底清理——见 ``_restore_project_core`` /
    ``_purge_project_core``。

    在途任务的处理：与旧版硬删除一致，先核对供应商付费任务是否已到终态、
    再取消项目级后台协程——回收站里的项目不应该继续烧算力。这一步失败
    （``ProviderTasksNotTerminalError``）会整体拒绝这次软删除，用户需要等
    任务到终态或去控制台核对后重试；不提供强制忽略。
    """
    from app.completion_grant import (
        assert_provider_tasks_clearable,
        prepare_provider_tasks_for_clear,
        reconcile_project_provider_tasks_for_clear,
    )

    _project_or_404(project_id)
    provider_reconciliation = await reconcile_project_provider_tasks_for_clear(
        project_id,
        conn=get_conn(),
        evidence_source="project_soft_delete_terminal_reconcile",
    )
    # Fast preflight before cancelling any producer. The authoritative check is
    # repeated inside the update transaction after all local writers stop.
    assert_provider_tasks_clearable(
        project_id=project_id,
        conn=get_conn(),
    )
    # 先停止并等待所有项目级后台协程退出，防止回收站里的项目继续跑生成/烧算力；
    # 被取消的任务各自的 CancelledError 处理器会把 bible_status 等字段翻成终态
    # （见 app/domain/bible_ops/task_run.py），启动恢复扫描因此不会把它当成
    # "重启丢失的在途任务"再拉起来。
    cancelled_tasks = await task_registry.cancel_project(project_id)
    conn = get_conn()
    stamp = now()
    try:
        prepare_provider_tasks_for_clear(
            project_id=project_id,
            conn=conn,
        )
        cur = conn.execute(
            "UPDATE projects SET deleted_at=? WHERE id=? AND deleted_at IS NULL",
            (stamp, project_id),
        )
        conn.commit()
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise
    if cur.rowcount != 1:
        # 与另一次并发的软删除请求赛跑输了：对方已经先把它标进回收站。
        raise HTTPException(409, "项目已在回收站中")
    return {
        "deleted": project_id,
        "deleted_at": stamp,
        "purge_at": stamp + PROJECT_RECYCLE_BIN_RETENTION_S,
        "cancelled_tasks": cancelled_tasks,
        "provider_reconciliation": provider_reconciliation,
    }


async def _restore_project_core(project_id: str) -> dict:
    """把项目从回收站恢复成正常项目：清空 ``deleted_at``，不改动其余任何数据。"""
    _deleted_project_or_404(project_id)
    conn = get_conn()
    try:
        cur = conn.execute(
            "UPDATE projects SET deleted_at=NULL WHERE id=? AND deleted_at IS NOT NULL",
            (project_id,),
        )
        conn.commit()
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise
    if cur.rowcount != 1:
        raise HTTPException(404, f"回收站中不存在该项目：{project_id}")
    return {"restored": project_id}


async def _purge_project_core(project_id: str) -> dict:
    """彻底清理：只对已在回收站的项目生效，物理删除数据库行与磁盘产物。

    破坏性操作的原子性：数据库删除全部提交成功之后才执行 ``shutil.rmtree``；
    数据库提交失败（异常/回滚）时磁盘上一个文件都不会被动。反过来的顺序
    （先删文件）一旦中途失败，会把仍在数据库里的行指向已经消失的文件——
    比"删除慢了一步但数据完好"更危险。
    """
    from app.completion_grant import (
        assert_provider_tasks_clearable,
        prepare_provider_tasks_for_clear,
        reconcile_project_provider_tasks_for_clear,
    )

    project = _deleted_project_or_404(project_id)
    provider_reconciliation = await reconcile_project_provider_tasks_for_clear(
        project_id,
        conn=get_conn(),
        evidence_source="project_purge_terminal_reconcile",
    )
    assert_provider_tasks_clearable(
        project_id=project_id,
        conn=get_conn(),
    )
    # 软删除时已经取消过一轮；这里再取消一次是防御性的（例如用户在软删除后
    # 短暂恢复、又发起新任务、又再次软删除的场景），不是重复劳动的赘余。
    cancelled_tasks = await task_registry.cancel_project(project_id)
    conn = get_conn()
    try:
        prepare_provider_tasks_for_clear(
            project_id=project_id,
            conn=conn,
        )
        evidence_removed = _delete_project_evidence(conn, project_id)
        # 文件和衍生产物由同一权威清理函数处理；数据库级联负责关系完整性。
        worker.delete_project_episodes(
            project_id,
            conn=conn,
            commit=False,
            check_provider=False,
        )
        conn.execute("DELETE FROM chapters WHERE project_id=?", (project_id,))
        conn.execute("DELETE FROM jobs WHERE project_id=?", (project_id,))
        conn.execute("DELETE FROM character_portraits WHERE project_id=?", (project_id,))
        conn.execute("DELETE FROM scene_references WHERE project_id=?", (project_id,))
        # deleted_at IS NOT NULL：只删仍在回收站里的这一行，防止跟一次并发的
        # 恢复请求赛跑——真撞上了，物理清理就整体失败，磁盘文件原封不动
        # （下面的 rmtree 不会执行），下一轮清理再来。
        purge_cur = conn.execute(
            "DELETE FROM projects WHERE id=? AND deleted_at IS NOT NULL", (project_id,)
        )
        if purge_cur.rowcount != 1:
            raise HTTPException(409, "项目已被恢复，取消本次彻底清理")
        conn.commit()
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise
    shutil.rmtree(config.PROJECTS_DIR / project_id, ignore_errors=True)
    return {
        "purged": project_id,
        "name": project["name"],
        "cancelled_tasks": cancelled_tasks,
        "evidence_removed": evidence_removed,
        "provider_reconciliation": provider_reconciliation,
    }


async def _purge_all_deleted_projects_core() -> dict:
    """清空回收站：逐个彻底清理全部已软删除的项目。

    每个项目的清理各自独立提交；一个项目失败（例如供应商任务未到终态）不
    得阻塞其余项目——收集失败项返回，而不是让调用方一次报错看不到全貌。

    归属范围与 ``list_deleted_projects`` 一致：普通账号只清空自己名下的回收
    站条目，系统管理员（或无 Principal 的内部调用）才清空全部——这是
    ``DELETE /projects/deleted``（"一键清空回收站"）背后的真正查询，此前
    完全没有 owner 过滤，任何登录账号都会把其他账号回收站里的项目一并彻底
    删除；project_id 不在 ``ProjectPurgeAllInput`` 里，HTTP 边缘的
    ``require_project_owner_access`` 没有路径参数可挂，全靠这里补上。
    """
    conn = get_conn()
    owner = _listing_owner_scope()
    if owner is not None:
        project_ids = [
            row["id"] for row in conn.execute(
                "SELECT id FROM projects WHERE deleted_at IS NOT NULL AND owner_user_id=?",
                (owner,),
            ).fetchall()
        ]
    else:
        # ALL_OWNERS: same admin/internal-caller rationale as
        # list_deleted_projects() -- the marker inside the SQL text is what
        # tests/test_project_ownership_query_guard.py actually looks for.
        project_ids = [
            row["id"] for row in conn.execute(
                "SELECT id FROM projects -- ALL_OWNERS: system admin / internal caller\n"
                "WHERE deleted_at IS NOT NULL"
            ).fetchall()
        ]
    purged: list[str] = []
    failed: list[dict] = []
    for pid in project_ids:
        try:
            await _purge_project_core(pid)
            purged.append(pid)
        except Exception as exc:  # noqa: BLE001 — 单个项目失败不得阻塞其余项目清空
            from app.errors import log_error
            rec = log_error(
                exc,
                action="project_purge_all",
                context={"project_id": pid},
                meta={"stage": "recycle_bin_purge_all"},
            )
            failed.append({"project_id": pid, "error_id": rec.error_id, "error": str(exc)})
    return {"purged": purged, "purged_count": len(purged), "failed": failed}


async def sweep_expired_deleted_projects() -> dict:
    """保留期到期的项目自动彻底清理；由周期性系统任务调用（见 ``app.recovery``）。
    判据是 ``deleted_at`` + 每行 ``recycle_bin_retention_s``（NULL 时默认 24 小时；
    账号级联软删除写 30 天，见 ``ACCOUNT_DELETE_RETENTION_S``）与当前时间的差值，
    不依赖任何内存计时器；供应商任务未到终态的项目会在这一轮失败并保留，下一
    轮重试。
    """
    conn = get_conn()
    stamp = now()
    project_ids = [
        row["id"] for row in conn.execute(
            "SELECT id FROM projects -- ALL_OWNERS: periodic background sweep "
            "loop (app.recovery.project_recycle_bin_sweep_loop), no request "
            "context; retention is enforced globally by deleted_at + "
            "recycle_bin_retention_s, not per caller\n"
            "WHERE deleted_at IS NOT NULL "
            "AND deleted_at + COALESCE(recycle_bin_retention_s, ?) < ?",
            (PROJECT_RECYCLE_BIN_RETENTION_S, stamp),
        ).fetchall()
    ]
    purged: list[str] = []
    failed: list[dict] = []
    for pid in project_ids:
        try:
            await _purge_project_core(pid)
            purged.append(pid)
        except Exception as exc:  # noqa: BLE001 — 单个项目失败不得阻塞其余到期项目
            from app.errors import log_error
            rec = log_error(
                exc,
                action="project_recycle_bin_sweep",
                context={"project_id": pid},
                meta={"stage": "recycle_bin_sweep"},
            )
            failed.append({"project_id": pid, "error_id": rec.error_id, "error": str(exc)})
    return {"purged": purged, "purged_count": len(purged), "failed": failed}


@router.delete("/projects/deleted")
async def purge_all_deleted_projects():
    """一键清空回收站。必须注册在 ``DELETE /projects/{project_id}`` 之前，
    理由同 ``list_deleted_projects``：都是 "projects" 后接一个静态段。"""
    from app.capabilities.dispatch import dispatch, respond_ui

    result = await dispatch("project.purge_all", {}, initiator="ui")
    return respond_ui(result)


@router.delete("/projects/{project_id}")
async def delete_project(project_id: str):
    from app.capabilities.dispatch import dispatch, respond_ui

    result = await dispatch("project.delete", {"project_id": project_id}, initiator="ui")
    return respond_ui(result)


@router.post("/projects/{project_id}/restore")
async def restore_project(project_id: str):
    from app.capabilities.dispatch import dispatch, respond_ui

    result = await dispatch("project.restore", {"project_id": project_id}, initiator="ui")
    return respond_ui(result)


@router.delete("/projects/{project_id}/purge")
async def purge_project(project_id: str):
    from app.capabilities.dispatch import dispatch, respond_ui

    result = await dispatch("project.purge", {"project_id": project_id}, initiator="ui")
    return respond_ui(result)
