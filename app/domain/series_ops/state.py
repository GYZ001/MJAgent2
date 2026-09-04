"""连播任务进度树的形状与持久化。

进度树整棵写进 ``series_tasks.progress_json``（照旧例：整棵状态一次性覆盖写，
不拆多列）。队列/暂停相关的协调状态不在这里——那是 ``queue.py`` 的职责，本模块
只管「一个任务内部五步进行到哪了」这一件事。
"""
from __future__ import annotations

import json

from app.db import get_conn, now

STAGE_SEQUENCE: tuple[str, ...] = ("screenplay", "storyboard", "confirm", "video", "final")


def new_episode_entry(episode_id: str, episode_no: int) -> dict:
    return {
        "episode_id": episode_id,
        "episode_no": episode_no,
        "stages": {stage: "pending" for stage in STAGE_SEQUENCE},
        "error": None,
    }


def new_progress(episodes: list[dict]) -> dict:
    return {
        "episodes": episodes,
        "current_episode_no": None,
        "current_stage": None,
        "running_episode_nos": [],
        "error": None,
    }


def refresh_current(progress: dict) -> None:
    """多集并行后 ``current_*`` 从进度树推导：正在跑的集里取最小集号及其正在跑的步；
    ``running_episode_nos`` 列出全部在跑的集。没有任何集在跑时不动 ``current_*``
    （merge 阶段由编排器自己写）。"""
    running: list[tuple[int, str]] = []
    for entry in progress.get("episodes") or []:
        for stage, value in (entry.get("stages") or {}).items():
            if value == "running":
                running.append((int(entry["episode_no"]), stage))
    progress["running_episode_nos"] = sorted({no for no, _ in running})
    if running:
        progress["current_episode_no"], progress["current_stage"] = min(running)


def persist_progress(task_id: str, progress: dict) -> None:
    refresh_current(progress)
    conn = get_conn()
    conn.execute(
        "UPDATE series_tasks SET progress_json=?, updated_at=? WHERE id=?",
        (json.dumps(progress, ensure_ascii=False), now(), task_id),
    )
    conn.commit()


def load_progress(row: dict) -> dict:
    """从一条 ``series_tasks`` 行取回进度树；形状不对/解析失败一律当空处理。"""
    try:
        value = json.loads(row.get("progress_json") or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def steps_done(progress: dict) -> int:
    """已完成（done/skipped）的步骤数，供列表页 ``steps_done/steps_total`` 用。"""
    done = 0
    for entry in progress.get("episodes") or []:
        stages = entry.get("stages") or {}
        done += sum(1 for v in stages.values() if v in ("done", "skipped"))
    return done
