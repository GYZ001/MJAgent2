"""单集彻底删除：取消在途任务、清证据、删数据库行/磁盘产物，再压缩集号。"""
from __future__ import annotations

import shutil

from fastapi import HTTPException

from app import config, task_registry, worker
from app.db import get_conn
from app.domain.common import _episode_or_404, router
from app.domain.projects.episode_renumber import _compact_project_episode_numbers
from app.domain.projects.evidence import _delete_episode_evidence


def _assert_no_other_episode_work(project_id: str, deleting_episode_id: str) -> None:
    """Avoid renumbering paths while another episode is actively writing them."""
    from app.planning import ACTIVE_MEDIA_JOB_STATUSES

    conn = get_conn()
    marks = ",".join("?" for _ in ACTIVE_MEDIA_JOB_STATUSES)
    active_job = conn.execute(
        f"""SELECT id FROM jobs
             WHERE project_id=? AND episode_id!=? AND status IN ({marks})
             LIMIT 1""",
        (project_id, deleting_episode_id, *sorted(ACTIVE_MEDIA_JOB_STATUSES)),
    ).fetchone()
    other_episode_ids = [
        row["id"] for row in conn.execute(
            "SELECT id FROM episodes WHERE project_id=? AND id!=?",
            (project_id, deleting_episode_id),
        ).fetchall()
    ]
    active_task = any(
        task_registry.active(kind, episode_id)
        for episode_id in other_episode_ids
        for kind in ("screenplay", "storyboard", "video_completion")
    )
    if active_job or active_task:
        raise HTTPException(
            409,
            "项目内其他分集仍在生成，请先等待完成或停止任务，再删除并自动重排集号",
        )


async def _delete_episode_core(episode_id: str) -> dict:
    """Permanently remove one episode and every downstream production asset."""
    from app.completion_grant import (
        ProviderTasksNotTerminalError,
        reconcile_provider_tasks_for_clear,
    )

    ep = dict(_episode_or_404(episode_id))
    project = get_conn().execute(
        "SELECT plan_status FROM projects WHERE id=?", (ep["project_id"],)
    ).fetchone()
    if task_registry.active("plan", ep["project_id"]) or (
        project and project["plan_status"] == "running"
    ):
        raise HTTPException(409, "分集规划正在运行，请等待完成后再删除单集")
    _assert_no_other_episode_work(ep["project_id"], episode_id)
    # 与 _delete_project_core 同一个理由：删除前先做一次只读式核对，把供应商
    # 自己已经确认终态、只是本地还没结算的任务先落定，减少用户被
    # PROVIDER_TASKS_NOT_TERMINAL 挡住却无法自愈的情况（真正仍在途的任务
    # 依旧原样挡下，不受影响）。
    await reconcile_provider_tasks_for_clear(
        episode_id=episode_id,
        conn=get_conn(),
        evidence_source="episode_delete_terminal_reconcile",
    )

    cancelled_tasks = 0
    for kind in ("screenplay", "storyboard", "video_completion"):
        cancelled_tasks += int(await task_registry.cancel_and_wait(kind, episode_id))

    conn = get_conn()
    # Cancellation finalizers may have refreshed the episode projection. Recheck
    # existence before deleting its immutable evidence and generated media.
    ep = conn.execute(
        "SELECT id,project_id,episode_no,title FROM episodes WHERE id=?",
        (episode_id,),
    ).fetchone()
    if not ep:
        raise HTTPException(404, f"分集不存在：{episode_id}")
    # 与 _delete_project_core 同一个理由、同一种写法：_delete_episode_evidence →
    # worker.delete_episode_shots → DELETE episodes → commit 中途任何未捕获异常
    # 冒到 app/main.py 的全局处理器都会调 errors.log_error，而 log_error 目前在
    # 调用方的 task 缓存连接上隐式 commit——谁先提交谁定型，回滚必须在那之前，
    # 且必须是 except 分支的第一条语句（同一顺序要求见 _storyboard_task 顶层
    # except 分支上方的大注释）。这里只加回滚兜底，不改变任何拦截判定：内层
    # ProviderTasksNotTerminalError 分支仍然优先命中并把 409 转换好，外层
    # except 只在它重新抛出的 HTTPException 上做一次空操作回滚（此时事务已经
    # 不在途）再原样重新抛出，不会把 409 吞成别的状态码。
    try:
        evidence_removed = _delete_episode_evidence(conn, episode_id)
        try:
            worker.delete_episode_shots(episode_id)
        except ProviderTasksNotTerminalError as exc:
            if conn.in_transaction:
                conn.rollback()
            raise HTTPException(409, exc.detail) from exc
        conn.execute("DELETE FROM episodes WHERE id=?", (episode_id,))
        conn.commit()
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise

    shutil.rmtree(
        config.PROJECTS_DIR / ep["project_id"] / "episodes" / str(ep["episode_no"]),
        ignore_errors=True,
    )
    compaction = _compact_project_episode_numbers(conn, ep["project_id"])
    return {
        "deleted": episode_id,
        "project_id": ep["project_id"],
        "episode_no": ep["episode_no"],
        "title": ep["title"],
        "cancelled_tasks": cancelled_tasks,
        "evidence_removed": evidence_removed,
        **compaction,
    }


@router.delete("/episodes/{episode_id}")
async def delete_episode(episode_id: str):
    return await _delete_episode_core(episode_id)
