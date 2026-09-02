"""连播台运行状态的形状、持久化与到契约 ``SeriesRun`` 的投影。

状态整体落在 ``workflow_runs.config_snapshot_json`` 的 ``series_state`` 键下
（照 ``app.domain.video_ops.project_queue_core._persist_project_video_queue``
同一种「整棵状态树一次性覆盖写」的做法，而不是拆多列）。
"""
from __future__ import annotations

import json

from app.db import get_conn, now

WORKFLOW_TYPE = "series_film"
TASK_KIND = "series_film"

STAGE_SEQUENCE: tuple[str, ...] = ("screenplay", "storyboard", "confirm", "video", "final")

# 项目级暂停请求集合：照 project_queue_core._project_video_queue_pause_requests
# 同一种「模块级可变单例，只 add/discard，不做 global 重绑定」的写法。
_PAUSE_REQUESTS: set[str] = set()


def request_pause(project_id: str) -> None:
    _PAUSE_REQUESTS.add(project_id)


def clear_pause(project_id: str) -> None:
    _PAUSE_REQUESTS.discard(project_id)


def is_pause_requested(project_id: str) -> bool:
    return project_id in _PAUSE_REQUESTS


def persist(run_id: str, run_state: dict) -> None:
    conn = get_conn()
    conn.execute(
        "UPDATE workflow_runs SET config_snapshot_json=?, updated_at=? WHERE id=?",
        (json.dumps({"series_state": run_state}, ensure_ascii=False), now(), run_id),
    )
    conn.commit()


def load_state(row: dict) -> dict:
    """从一条 ``workflow_runs`` 行（裸 SQL dict 或 repository 投影 dict 皆可）取回状态树。"""
    try:
        snapshot = row.get("config_snapshot")
        if snapshot is None:
            snapshot = json.loads(row.get("config_snapshot_json") or "{}")
    except (TypeError, ValueError):
        return {}
    value = snapshot.get("series_state") if isinstance(snapshot, dict) else None
    return value if isinstance(value, dict) else {}


def new_episode_entry(episode_id: str, episode_no: int) -> dict:
    return {
        "episode_id": episode_id,
        "episode_no": episode_no,
        "stages": {stage: "pending" for stage in STAGE_SEQUENCE},
        "error": None,
    }


def new_state(episode_from: int, episode_to: int, episodes: list[dict]) -> dict:
    return {
        "episode_from": episode_from,
        "episode_to": episode_to,
        "episodes": episodes,
        "current_episode_no": None,
        "current_stage": None,
        "error": None,
    }


def fetch_range_episodes(
    conn, project_id: str, episode_from: int, episode_to: int,
) -> tuple[list[dict], list[int]]:
    """按 episode_no 取闭区间内已存在的集，并报告缺失的集号（保持升序）。"""
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


_TERMINAL_STATUS = {"SUCCEEDED": "succeeded", "FAILED": "failed", "CANCELLED": "cancelled"}


def run_status_label(status: str, _failure_code: str | None) -> str:
    """把内部 workflow_runs.status 映射到契约 SeriesRun.status 的五值枚举。"""
    if status in _TERMINAL_STATUS:
        return _TERMINAL_STATUS[status]
    if status == "PAUSED_EXTERNAL":
        return "paused"
    if status in {"CREATED", "RUNNING"}:
        return "running"
    # WAITING_* 在本工作流从不出现（每步失败即停，没有等待态）；出现即视为异常终态，
    # fail-closed 归入 failed 而不是静默当成 running。
    return "failed"


def project_run_view(row: dict) -> dict:
    """把一条 workflow_runs 行 + 其状态树投影成契约 ``SeriesRun`` 结构。"""
    run_state = load_state(row)
    return {
        "run_id": row["id"],
        "status": run_status_label(row["status"], row.get("failure_code")),
        "episode_from": run_state.get("episode_from"),
        "episode_to": run_state.get("episode_to"),
        "current_episode_no": run_state.get("current_episode_no"),
        "current_stage": run_state.get("current_stage"),
        "started_at": row.get("started_at"),
        "updated_at": row.get("updated_at"),
        "finished_at": row.get("finished_at"),
        "error": run_state.get("error") or row.get("failure_message"),
        "episodes": run_state.get("episodes") or [],
    }
