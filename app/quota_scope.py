"""配额闸门的归属解析与并发计数：纯只读查询，不判上限、不记账。

从 ``app/quota.py`` 拆出（文件行数棘轮 ``scripts/check_file_conventions.py`` 逼
的，不是过度设计）：这几个函数是唯一一组「外部调用方通过 ``quota.X`` 使用、但
内部不依赖 ``effective_limits``/``QuotaExceeded``/ledger 原语」的纯查询，可以
在零反向依赖的前提下独立成模块。``app/quota.py`` 顶部把这几个名字重新导入进自
己的命名空间（``quota.owner_of_project`` 等调用点因此不用改），本模块反过来不
import 任何 ``app.quota`` 的东西——依赖方向单向，不构成环。
"""
from __future__ import annotations

import sqlite3


def owner_of_project(conn: sqlite3.Connection, project_id: str | None) -> str | None:
    if not project_id:
        return None
    row = conn.execute(
        "SELECT owner_user_id FROM projects WHERE id=?", (project_id,)
    ).fetchone()
    if not row or not row["owner_user_id"]:
        return None
    return str(row["owner_user_id"])


def owner_of_episode(conn: sqlite3.Connection, episode_id: str | None) -> str | None:
    if not episode_id:
        return None
    row = conn.execute(
        "SELECT project_id FROM episodes WHERE id=?", (episode_id,)
    ).fetchone()
    return owner_of_project(conn, row["project_id"]) if row else None


def count_active_workflow_runs(
    conn: sqlite3.Connection, owner_user_id: str, workflow_type: str,
    *, exclude_run_id: str | None = None,
) -> int:
    """统计某账号名下、某 workflow_type 当前处于 CREATED/RUNNING 的 run 数。
    scope_type='episode' 是 screenplay/storyboard 两类 run 的既有约定，归属
    通过 episode -> project -> owner_user_id 解析。"""
    # SQL 整条内联在 execute 调用点上（可选条件用 ``? IS NULL OR`` 并进常量，
    # 不再拼变量）：tests/test_project_ownership_query_guard.py 的静态扫描只能
    # 分析调用点上的字面量，SQL 一旦先存进变量就成了它的盲区——而这条查询正是
    # 靠 ``p.owner_user_id=?`` 做账号隔离的，必须留在它看得见的地方。
    return int(
        conn.execute(
            "SELECT COUNT(*) AS c FROM workflow_runs wr "
            "JOIN episodes e ON e.id = wr.scope_id "
            "JOIN projects p ON p.id = e.project_id "
            "WHERE wr.workflow_type=? AND wr.scope_type='episode' "
            "AND p.owner_user_id=? AND wr.status IN ('CREATED','RUNNING') "
            "AND (? IS NULL OR wr.id != ?)",
            (workflow_type, owner_user_id, exclude_run_id, exclude_run_id),
        ).fetchone()["c"]
        or 0
    )


#: jobs 表里代表"这个视频任务还在推进、没有走到头"的状态集合，与既有
#: ``reconcile_episode_generation_status`` 用的口径完全一致（不新造一套判断）。
ACTIVE_JOB_STATUSES = ("queued", "running", "waiting_provider", "waiting_retry")


def count_active_video_jobs(
    conn: sqlite3.Connection, owner_user_id: str, *, exclude_job_id: str | None = None
) -> int:
    # 同上：整条内联在调用点，可选条件并进常量。f-string 里只插入由本模块常量
    # ACTIVE_JOB_STATUSES 派生的占位符，不插入任何外部输入。
    placeholders = ",".join("?" for _ in ACTIVE_JOB_STATUSES)
    return int(
        conn.execute(
            f"SELECT COUNT(*) AS c FROM jobs WHERE kind='video' AND project_id IN "
            f"(SELECT id FROM projects WHERE owner_user_id=?) "
            f"AND status IN ({placeholders}) AND (? IS NULL OR id != ?)",
            (owner_user_id, *ACTIVE_JOB_STATUSES, exclude_job_id, exclude_job_id),
        ).fetchone()["c"]
        or 0
    )
