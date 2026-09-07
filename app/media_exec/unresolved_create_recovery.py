"""开机自愈：重启打断 create 后留下的「供应商可能已接单、本地没有 task id」作业。

2026-09-07 部署重启实测：15 个视频作业停在 ``VIDEO_PROVIDER_CREATE_UNRESOLVED``，跨 11 集。
这个 fail-closed 状态本身是对的（不知道供应商收没收，自动重发有重复计费风险），但在无人值守
的连播里，它等的那个「人」不会来——集子就一直挂着，直到墙钟收口。

这里只做**零风险**的一件事：同一镜头已经另有活动作业（排队/在途/已成功）时，这条 unresolved
作业就是纯残留——镜头本身在正常推进，它既不代表待办也拦不住任何人，标成 abandoned 让看板干净。
真正需要重新提交的那些（镜头没有其它活动作业）**不动**：那要么由运维在页面确认，要么把
``video_provider_create_auto_resubmit`` 设成 1 由 ``resubmittable_unresolved_jobs`` 交给调用方，
判断权留给部署方——本模块不替任何人决定要不要冒重复提交的风险。
"""
from __future__ import annotations

import logging

from app.db import get_conn, now

_LOGGER = logging.getLogger(__name__)
ACTIVE_SIBLING_STATUSES = ("queued", "running", "waiting_provider", "waiting_retry", "succeeded")
UNRESOLVED_REASON = "VIDEO_PROVIDER_CREATE_UNRESOLVED"


def _unresolved_jobs(conn) -> list:
    return conn.execute(
        """SELECT id, shot_id FROM jobs
            WHERE kind='video' AND status='waiting_human' AND reason_code=?
            ORDER BY updated_at""",
        (UNRESOLVED_REASON,),
    ).fetchall()


def _shot_has_active_sibling(conn, job_id: str, shot_id: str) -> bool:
    marks = ",".join("?" * len(ACTIVE_SIBLING_STATUSES))
    row = conn.execute(
        f"SELECT 1 FROM jobs WHERE shot_id=? AND id!=? AND kind='video' AND status IN ({marks}) LIMIT 1",
        (shot_id, job_id, *ACTIVE_SIBLING_STATUSES),
    ).fetchone()
    return row is not None


def resolve_redundant_unresolved_creates() -> int:
    """把「同镜已有活动作业」的 unresolved 残留标成 abandoned；返回清理条数。"""
    conn = get_conn()
    cleared = 0
    for row in _unresolved_jobs(conn):
        if not _shot_has_active_sibling(conn, str(row["id"]), str(row["shot_id"])):
            continue
        conn.execute(
            "UPDATE jobs SET status='abandoned', error=?, updated_at=? WHERE id=? AND status='waiting_human'",
            ("同镜头另有活动视频任务，本条 create 未确认记录已作废", now(), str(row["id"])),
        )
        conn.commit()
        cleared += 1
    if cleared:
        _LOGGER.info("unresolved-create-cleanup 清理冗余残留 %d 条", cleared)
    return cleared


def resubmittable_unresolved_jobs() -> list[str]:
    """镜头没有任何活动作业、真正需要重新提交的 unresolved 作业 id（只报告，不动手）。"""
    conn = get_conn()
    return [
        str(row["id"])
        for row in _unresolved_jobs(conn)
        if not _shot_has_active_sibling(conn, str(row["id"]), str(row["shot_id"]))
    ]
