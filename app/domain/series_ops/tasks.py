"""``series_tasks`` 表读写：切分计划、生成、列表/详情投影、删除、队列状态转移。

完成判据挂产物（``merge.merge_is_current``），不挂本表的 ``status`` 列——
``status`` 只是队列调度/观测用的记账字段（five/six-value：idle/queued/running/
succeeded/failed/cancelled），contract 明确「paused 不是任务状态」。
"""
from __future__ import annotations

from fastapi import HTTPException

from app.db import new_id, now

from . import merge, state

SERIES_MAX_SPAN = 10
DEFAULT_GROUP_SIZE = 10
_LIST_LIMIT_MAX = 200
_PLAN_GROUPS_MAX = 200

_STATUS_VALUES = ("idle", "queued", "running", "succeeded", "failed", "cancelled")


# ------------------------------------------------------------------- episodes

def episodes_summary(conn, project_id: str) -> dict:
    row = conn.execute(
        "SELECT COUNT(*) AS n, MIN(episode_no) AS lo, MAX(episode_no) AS hi "
        "FROM episodes WHERE project_id=?",
        (project_id,),
    ).fetchone()
    return {
        "total": int(row["n"] or 0),
        "min_no": row["lo"],
        "max_no": row["hi"],
    }


def fetch_range_episodes(
    conn, project_id: str, episode_from: int, episode_to: int,
) -> tuple[list[dict], list[int]]:
    """按 episode_no 取闭区间内已存在的集，并如实报告缺失的集号（升序）。"""
    rows = conn.execute(
        """SELECT id, episode_no, title FROM episodes
           WHERE project_id=? AND episode_no BETWEEN ? AND ?
           ORDER BY episode_no""",
        (project_id, episode_from, episode_to),
    ).fetchall()
    found = {int(row["episode_no"]): dict(row) for row in rows}
    wanted = range(episode_from, episode_to + 1)
    missing = [no for no in wanted if no not in found]
    ordered = [found[no] for no in wanted if no in found]
    return ordered, missing


# --------------------------------------------------------------- 切分/生成

def _default_title(episode_from: int, episode_to: int) -> str:
    if episode_from == episode_to:
        return f"第 {episode_from} 集"
    return f"第 {episode_from}-{episode_to} 集"


def _numeric_groups(min_no: int, max_no: int, group_size: int) -> list[tuple[int, int]]:
    groups = []
    start = min_no
    while start <= max_no:
        end = min(start + group_size - 1, max_no)
        groups.append((start, end))
        start = end + 1
    return groups


def _existing_ranges(conn, project_id: str) -> set[tuple[int, int]]:
    rows = conn.execute(
        "SELECT episode_from, episode_to FROM series_tasks WHERE project_id=?",
        (project_id,),
    ).fetchall()
    return {(row["episode_from"], row["episode_to"]) for row in rows}


def _validate_group_size(group_size: int) -> None:
    if not isinstance(group_size, int) or group_size < 1 or group_size > SERIES_MAX_SPAN:
        raise HTTPException(422, f"group_size 必须在 1-{SERIES_MAX_SPAN} 之间")


def plan_groups(conn, project_id: str, group_size: int) -> dict:
    """``group_size`` 切分预览，不落库。"""
    _validate_group_size(group_size)
    episodes = episodes_summary(conn, project_id)
    groups: list[tuple[int, int]] = []
    if episodes["total"]:
        groups = _numeric_groups(episodes["min_no"], episodes["max_no"], group_size)
    existing = _existing_ranges(conn, project_id)
    out_groups = []
    for episode_from, episode_to in groups[:_PLAN_GROUPS_MAX]:
        exists = (episode_from, episode_to) in existing
        _ordered, missing = fetch_range_episodes(conn, project_id, episode_from, episode_to)
        out_groups.append({
            "episode_from": episode_from, "episode_to": episode_to,
            "exists": exists, "missing_episode_nos": missing,
        })
    existing_count = sum(1 for g in groups if (g[0], g[1]) in existing)
    new_count = len(groups) - existing_count
    return {
        "group_size": group_size, "total_groups": len(groups),
        "new_groups": new_count, "existing_groups": existing_count,
        "episodes": episodes,
        "groups": out_groups,
        "truncated": len(groups) > _PLAN_GROUPS_MAX,
    }


def _validate_ranges(ranges: list[dict]) -> list[tuple[int, int]]:
    parsed = []
    for item in ranges:
        try:
            episode_from = int(item["episode_from"])
            episode_to = int(item["episode_to"])
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(422, "ranges 里每一项必须有 episode_from/episode_to") from exc
        if episode_to < episode_from:
            raise HTTPException(422, f"区间倒置：{episode_from}-{episode_to}")
        if episode_to - episode_from + 1 > SERIES_MAX_SPAN:
            raise HTTPException(422, f"单个任务最多 {SERIES_MAX_SPAN} 集：{episode_from}-{episode_to}")
        parsed.append((episode_from, episode_to))
    return parsed


def generate_tasks(conn, project_id: str, body: dict) -> dict:
    group_size = body.get("group_size")
    ranges = body.get("ranges")
    if group_size is None and not ranges:
        raise HTTPException(422, "group_size 与 ranges 必须二选一")
    if group_size is not None and ranges:
        raise HTTPException(422, "group_size 与 ranges 不能同时传")
    if group_size is not None:
        _validate_group_size(int(group_size))
        episodes = episodes_summary(conn, project_id)
        candidates = (
            _numeric_groups(episodes["min_no"], episodes["max_no"], int(group_size))
            if episodes["total"] else []
        )
    else:
        candidates = _validate_ranges(ranges)
    existing = _existing_ranges(conn, project_id)
    created = 0
    for episode_from, episode_to in candidates:
        if (episode_from, episode_to) in existing:
            continue
        _insert_task(conn, project_id, episode_from, episode_to)
        created += 1
    conn.commit()
    total = conn.execute(
        "SELECT COUNT(*) AS n FROM series_tasks WHERE project_id=?", (project_id,)
    ).fetchone()["n"]
    return {"created": created, "existing": len(candidates) - created, "tasks_total": int(total)}


def _insert_task(conn, project_id: str, episode_from: int, episode_to: int) -> str:
    task_id = new_id("st")
    ts = now()
    conn.execute(
        """INSERT INTO series_tasks(
               id, project_id, title, episode_from, episode_to, status,
               progress_json, created_at, updated_at
           ) VALUES (?,?,?,?,?,'idle','{}',?,?)""",
        (task_id, project_id, "", episode_from, episode_to, ts, ts),
    )
    return task_id


# --------------------------------------------------------------------- 读取

def get_task_row(conn, project_id: str, task_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM series_tasks WHERE id=? AND project_id=?", (task_id, project_id),
    ).fetchone()
    return dict(row) if row else None


def _queue_positions(conn, project_id: str) -> dict[str, int]:
    rows = conn.execute(
        "SELECT id FROM series_tasks WHERE project_id=? AND status='queued' ORDER BY queue_seq ASC",
        (project_id,),
    ).fetchall()
    return {row["id"]: i + 1 for i, row in enumerate(rows)}


def film_state(row: dict, film: dict | None) -> tuple[str, bool]:
    """返回 ``(对外展示的状态, 成片是否已过期)``——完成与否挂产物，不挂状态字段。

    两个方向都要掰正，否则界面会在两头各撒一次谎：

    - 从没跑过（``idle``）但成片已在盘上且未过期 → 显示「已完成」。这种任务真实
      存在：旧单例连播台留下的成片、重新切分后与旧区间重合的任务。照字段显示会让
      列表说「未开始」，而点开始时入队判据又判「已完成，成片未过期」把它跳过。
    - 跑成功过（``succeeded``）但某一集的成片后来重做了（``film.report.json`` 记的
      输入指纹对不上）→ ``film_stale`` 为真。此时它确实可以重新入队（入队判据用的
      是同一个 ``merge_is_current``），列表必须让用户看见这件事，否则「已完成」三个
      字会盖住「其实要重合一次」。

    只提升 ``idle`` 一档：``running``/``queued`` 是此刻的事实，``failed``/
    ``cancelled`` 带着用户需要看到的原因，都不该被一个恰好存在的产物盖掉。
    """
    if film is None:
        return row["status"], False
    episode_nos = list(range(row["episode_from"], row["episode_to"] + 1))
    current = merge.merge_is_current(
        row["project_id"], row["episode_from"], row["episode_to"], episode_nos,
    )
    if not current:
        return row["status"], True
    return ("succeeded" if row["status"] == "idle" else row["status"]), False


def task_summary(
    row: dict, index: int, queue_positions: dict[str, int], missing_episode_nos: list[int],
) -> dict:
    progress = state.load_progress(row)
    episode_count = row["episode_to"] - row["episode_from"] + 1
    film = merge.film_for_range(row["project_id"], row["episode_from"], row["episode_to"])
    status, film_stale = film_state(row, film)
    return {
        "task_id": row["id"], "index": index,
        "title": row["title"] or _default_title(row["episode_from"], row["episode_to"]),
        "episode_from": row["episode_from"], "episode_to": row["episode_to"],
        "episode_count": episode_count,
        "missing_episode_nos": missing_episode_nos,
        "status": status, "film_stale": film_stale,
        "queue_position": queue_positions.get(row["id"]),
        "current_episode_no": progress.get("current_episode_no"),
        "current_stage": progress.get("current_stage"),
        "steps_done": state.steps_done(progress), "steps_total": episode_count * 5,
        "error": row.get("error") or progress.get("error"),
        "film": film,
        "updated_at": row["updated_at"], "finished_at": row.get("finished_at"),
    }


def list_tasks(conn, project_id: str, offset: int, limit: int) -> dict:
    offset = max(0, int(offset))
    limit = min(_LIST_LIMIT_MAX, max(1, int(limit)))
    rows = conn.execute(
        "SELECT * FROM series_tasks WHERE project_id=? ORDER BY episode_from LIMIT ? OFFSET ?",
        (project_id, limit, offset),
    ).fetchall()
    queue_positions = _queue_positions(conn, project_id)
    tasks_out = []
    for i, row in enumerate(rows):
        row = dict(row)
        _ordered, missing = fetch_range_episodes(conn, project_id, row["episode_from"], row["episode_to"])
        tasks_out.append(task_summary(row, offset + i + 1, queue_positions, missing))
    totals_rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM series_tasks WHERE project_id=? GROUP BY status",
        (project_id,),
    ).fetchall()
    totals = {status: 0 for status in _STATUS_VALUES}
    for row in totals_rows:
        if row["status"] in totals:
            totals[row["status"]] = int(row["n"])
    totals["all"] = sum(totals.values())
    return {
        "queue": queue_snapshot(conn, project_id),
        "totals": totals,
        "episodes": episodes_summary(conn, project_id),
        "max_span": SERIES_MAX_SPAN, "default_group_size": DEFAULT_GROUP_SIZE,
        "offset": offset, "limit": limit,
        "tasks": tasks_out,
    }


def queue_snapshot(conn, project_id: str) -> dict:
    row = conn.execute(
        "SELECT paused, stop_reason FROM series_queue_state WHERE project_id=?", (project_id,),
    ).fetchone()
    running_rows = conn.execute(
        "SELECT id FROM series_tasks WHERE project_id=? AND status='running' ORDER BY queue_seq ASC",
        (project_id,),
    ).fetchall()
    running = running_rows[0] if running_rows else None
    queued_count = conn.execute(
        "SELECT COUNT(*) AS n FROM series_tasks WHERE project_id=? AND status='queued'", (project_id,),
    ).fetchone()["n"]
    return {
        "paused": bool(row["paused"]) if row else False,
        "running_task_id": running["id"] if running else None,
        "running_task_ids": [r["id"] for r in running_rows],
        "queued_count": int(queued_count),
        "stop_reason": (row["stop_reason"] if row else None),
    }


def task_detail(conn, project_id: str, task_id: str) -> dict:
    row = get_task_row(conn, project_id, task_id)
    if row is None:
        raise HTTPException(404, f"连播任务不存在：{task_id}")
    ordered, missing = fetch_range_episodes(conn, project_id, row["episode_from"], row["episode_to"])
    summary = task_summary(
        row, _task_index(conn, project_id, task_id), _queue_positions(conn, project_id), missing,
    )
    progress = state.load_progress(row)
    progress_by_id = {e["episode_id"]: e for e in progress.get("episodes") or []}
    episodes_out = []
    for episode_row in ordered:
        entry = progress_by_id.get(episode_row["id"]) or state.new_episode_entry(
            episode_row["id"], episode_row["episode_no"],
        )
        episodes_out.append({
            "episode_id": episode_row["id"], "episode_no": episode_row["episode_no"],
            "title": episode_row.get("title"), "stages": entry["stages"], "error": entry.get("error"),
        })
    summary["episodes"] = episodes_out
    # film / film_stale 由 task_summary 一处算出（同一判据不留第二份实现）。
    return summary


def _task_index(conn, project_id: str, task_id: str) -> int:
    rows = conn.execute(
        "SELECT id FROM series_tasks WHERE project_id=? ORDER BY episode_from", (project_id,),
    ).fetchall()
    for i, row in enumerate(rows):
        if row["id"] == task_id:
            return i + 1
    return 0


# --------------------------------------------------------------------- 删除

def delete_task(conn, project_id: str, task_id: str) -> dict:
    row = get_task_row(conn, project_id, task_id)
    if row is None:
        raise HTTPException(404, f"连播任务不存在：{task_id}")
    if row["status"] in ("queued", "running"):
        raise HTTPException(409, "任务在队列中或正在运行，请先取消再删除")
    conn.execute("DELETE FROM series_tasks WHERE id=?", (task_id,))
    conn.commit()
    return {"ok": True, "task_id": task_id, "note": "只删任务记录，磁盘上的成片保留"}


# ------------------------------------------------------------- 队列状态转移
# 以下写入方法只改列，不碰 progress_json（进度树的持久化专属 state.py）；
# 调用方是 queue.py 的串行 runner。

def mark_running(conn, task_id: str, run_id: str) -> None:
    conn.execute(
        "UPDATE series_tasks SET status='running', run_id=?, started_at=?, updated_at=? WHERE id=?",
        (run_id, now(), now(), task_id),
    )
    conn.commit()


def mark_succeeded(conn, task_id: str) -> None:
    conn.execute(
        "UPDATE series_tasks SET status='succeeded', queue_seq=NULL, error=NULL, "
        "finished_at=?, updated_at=? WHERE id=?",
        (now(), now(), task_id),
    )
    conn.commit()


def mark_failed(conn, task_id: str, error: str) -> None:
    conn.execute(
        "UPDATE series_tasks SET status='failed', queue_seq=NULL, error=?, "
        "finished_at=?, updated_at=? WHERE id=?",
        (error[:1000], now(), now(), task_id),
    )
    conn.commit()


def mark_idle(conn, task_id: str) -> None:
    """取消：清出队列（idle，清 queue_seq），进度树原样保留。"""
    conn.execute(
        "UPDATE series_tasks SET status='idle', queue_seq=NULL, updated_at=? WHERE id=?",
        (now(), task_id),
    )
    conn.commit()


def mark_queued_again(conn, task_id: str) -> None:
    """暂停/服务重启：退回排队中，保留原 queue_seq（下次照原顺序先跑它）与进度树。"""
    conn.execute(
        "UPDATE series_tasks SET status='queued', updated_at=? WHERE id=?",
        (now(), task_id),
    )
    conn.commit()


def enqueue_many(conn, project_id: str, task_ids: list[str], *, force: bool) -> tuple[list[str], list[dict]]:
    """按数组顺序尝试入队；返回 (accepted_task_ids, skipped[{task_id,reason}])。"""
    next_seq = _next_queue_seq(conn, project_id)
    accepted: list[str] = []
    skipped: list[dict] = []
    for task_id in task_ids:
        reason = _enqueue_rejection_reason(conn, project_id, task_id, force=force)
        if reason is not None:
            skipped.append({"task_id": task_id, "reason": reason})
            continue
        conn.execute(
            "UPDATE series_tasks SET status='queued', queue_seq=?, error=NULL, updated_at=? WHERE id=?",
            (next_seq, now(), task_id),
        )
        next_seq += 1
        accepted.append(task_id)
    conn.commit()
    return accepted, skipped


def _enqueue_rejection_reason(conn, project_id: str, task_id: str, *, force: bool) -> str | None:
    row = get_task_row(conn, project_id, task_id)
    if row is None:
        return "任务不存在"
    if row["status"] in ("queued", "running"):
        return "已在队列中"
    _ordered, missing = fetch_range_episodes(conn, project_id, row["episode_from"], row["episode_to"])
    if missing:
        return "缺集：" + "、".join(str(n) for n in missing)
    if not force:
        episode_nos = list(range(row["episode_from"], row["episode_to"] + 1))
        if merge.merge_is_current(project_id, row["episode_from"], row["episode_to"], episode_nos):
            return "已完成，成片未过期"
    return None


def _next_queue_seq(conn, project_id: str) -> float:
    row = conn.execute(
        "SELECT COALESCE(MAX(queue_seq), 0) AS m FROM series_tasks WHERE project_id=?", (project_id,),
    ).fetchone()
    return float(row["m"] or 0) + 1.0


def next_queued_task(conn, project_id: str, exclude: set[str] | None = None) -> dict | None:
    """队首排队任务；``exclude`` 是调度器刚派出、子任务尚未来得及把状态改成 running 的任务。"""
    excluded = sorted(exclude or ())
    marks = ",".join("?" for _ in excluded)
    sql = "SELECT * FROM series_tasks WHERE project_id=? AND status='queued'"
    if excluded:
        sql += f" AND id NOT IN ({marks})"
    row = conn.execute(sql + " ORDER BY queue_seq ASC LIMIT 1", (project_id, *excluded)).fetchone()
    return dict(row) if row else None


def count_queued(conn, project_id: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM series_tasks WHERE project_id=? AND status='queued'", (project_id,),
    ).fetchone()
    return int(row["n"])


def consecutive_failures(conn, project_id: str) -> int:
    """最近连续失败的任务数（按 finished_at 降序数连续 'failed'，遇到非
    failed 即停）；成功/取消都会打断连续失败计数。"""
    rows = conn.execute(
        "SELECT status FROM series_tasks WHERE project_id=? AND finished_at IS NOT NULL "
        "ORDER BY finished_at DESC LIMIT 20",
        (project_id,),
    ).fetchall()
    count = 0
    for row in rows:
        if row["status"] != "failed":
            break
        count += 1
    return count


def reset_running_to_queued(conn) -> set[str]:
    """开机恢复：把因进程重启而卡在 running 的任务复位为 queued（进度保留）。"""
    rows = conn.execute("SELECT id, project_id FROM series_tasks WHERE status='running'").fetchall()
    project_ids: set[str] = set()
    for row in rows:
        conn.execute(
            "UPDATE series_tasks SET status='queued', updated_at=? WHERE id=?", (now(), row["id"]),
        )
        project_ids.add(row["project_id"])
    if rows:
        conn.commit()
    return project_ids
