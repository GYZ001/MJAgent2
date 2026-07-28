"""Deterministic regex-based episode planning.

Novel headings are split by ``ingest.CHAPTER_RE`` during ingestion; this module
maps each resulting chapter to exactly one episode without an LLM.
"""
from __future__ import annotations

import json
import re

from fastapi import APIRouter, HTTPException

from app import config, errors, task_registry, worker
from app.db import get_conn, new_id, now, rows_to_dicts
from app.ingest import dedupe_stub_chapters

router = APIRouter(prefix="/api")
PLAN_PREVIEW_CHARS = 100


def chapter_preview(content: str | None, limit: int = PLAN_PREVIEW_CHARS) -> str:
    return re.sub(r"\s+", " ", (content or "")).strip()[:limit]


async def run_regex_plan(project_id: str) -> None:
    """Replace a project's plan with one episode per regex-split chapter."""
    conn = get_conn()
    try:
        chapters = rows_to_dicts(conn.execute(
            "SELECT * FROM chapters WHERE project_id=? ORDER BY idx", (project_id,)
        ).fetchall())
        if not chapters:
            raise ValueError("没有可分集的章节，请先上传小说")
        # Existing projects may predate ingestion-time stub deduplication. Filter
        # adjacent title-only duplicates during an explicit replan while preserving
        # the original chapter idx values used by the reader/source mapping.
        chapters, _ = dedupe_stub_chapters(chapters, reindex=False)
        # Replace the relational plan atomically. Media files are removed only
        # after commit, so an insert failure leaves the previous plan usable.
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM episodes WHERE project_id=?", (project_id,))
        for episode_no, chapter in enumerate(chapters, start=1):
            conn.execute(
                "INSERT INTO episodes(id, project_id, episode_no, title, hook, cliffhanger, synopsis, "
                "source_chapters, target_duration_s, status, created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?, 'planned', ?)",
                (
                    new_id("ep"), project_id, episode_no,
                    chapter["title"] or f"第{chapter['idx']}章", "", "",
                    chapter_preview(chapter["content"]), json.dumps([chapter["idx"]]),
                    config.EPISODE_TARGET_DEFAULT_S, now(),
                ),
            )
        conn.execute(
            "UPDATE projects SET plan_status='ready', plan_error=NULL, key_timeline='[]', "
            "status='planned' WHERE id=?", (project_id,)
        )
        conn.commit()
        import shutil

        episode_dir = config.PROJECTS_DIR / project_id / "episodes"
        if episode_dir.exists():
            shutil.rmtree(episode_dir, ignore_errors=True)
    except Exception as exc:  # noqa: BLE001 -- task failures must be persisted for the UI
        # Episode inserts and the final project status share one transaction.
        # Never expose a failed plan together with a partial episode list.
        conn.rollback()
        public = errors.record_and_format(
            exc, action="plan_generate", context={"project_id": project_id}
        )
        conn.execute(
            "UPDATE projects SET plan_status='failed', plan_error=? WHERE id=?",
            (public, project_id),
        )
        conn.commit()


def recover_plan_tasks() -> int:
    conn = get_conn()
    resumed = 0
    for row in conn.execute("SELECT id FROM projects WHERE plan_status='running'").fetchall():
        project_id = row["id"]
        if task_registry.active("plan", project_id):
            continue
        try:
            task_registry.spawn(
                "plan", project_id, run_regex_plan(project_id), project_id=project_id
            )
            resumed += 1
        except Exception as exc:
            public = errors.record_and_format(
                exc, action="plan_recovery_spawn", context={"project_id": project_id}
            )
            conn.execute(
                "UPDATE projects SET plan_status='failed', plan_error=? WHERE id=?",
                (f"分集恢复任务未能启动，原文已保留，可在分集页重试：{public}", project_id),
            )
            conn.commit()
    return resumed


async def start_plan(project_id: str) -> dict:
    """启动分集规划的领域逻辑，供 REST 路由与 ``episode.plan`` Command Handler 共用。"""
    conn = get_conn()
    project = conn.execute("SELECT id, plan_status FROM projects WHERE id=?", (project_id,)).fetchone()
    if not project:
        raise HTTPException(404, f"项目不存在：{project_id}")
    episode_ids = [
        row["id"] for row in conn.execute(
            "SELECT id FROM episodes WHERE project_id=?", (project_id,)
        ).fetchall()
    ]
    active_tasks = [
        {"episode_id": episode_id, "kind": kind}
        for episode_id in episode_ids
        for kind in ("screenplay", "storyboard", "video_completion")
        if task_registry.active(kind, episode_id)
    ]
    active_jobs = int(conn.execute(
        """SELECT COUNT(*) AS c FROM jobs
           WHERE project_id=? AND status IN (
             'queued','running','waiting_provider','waiting_retry','paused'
           ) AND cancellation_requested=0 AND abandoned=0""",
        (project_id,),
    ).fetchone()["c"])
    if active_tasks or active_jobs:
        raise HTTPException(409, detail={
            "code": "REPLAN_ACTIVE_WORK",
            "message": "项目仍有剧本、分镜或视频任务，重新分集前必须先停止这些任务",
            "active_tasks": active_tasks,
            "active_media_jobs": active_jobs,
            "recovery_action": "在对应工作台停止运行中的任务后，再重新规划分集",
        })
    task_id = f"plan:{project_id}"
    if task_registry.active("plan", project_id):
        if project["plan_status"] != "running":
            conn.execute(
                "UPDATE projects SET plan_status='running', plan_error=NULL WHERE id=?",
                (project_id,),
            )
            conn.commit()
        return {
            "status": "running",
            "task_id": task_id,
            "already_running": True,
            "planner": "regex",
            "rule": "one_chapter_one_episode",
        }
    resumed = project["plan_status"] == "running"
    conn.execute(
        "UPDATE projects SET plan_status='running', plan_error=NULL WHERE id=?", (project_id,)
    )
    conn.commit()
    try:
        task_registry.spawn("plan", project_id, run_regex_plan(project_id), project_id=project_id)
    except Exception as exc:
        public = errors.record_and_format(
            exc, action="plan_spawn", context={"project_id": project_id}
        )
        conn.execute(
            "UPDATE projects SET plan_status='failed', plan_error=? WHERE id=?",
            (f"分集任务未能启动，项目和原文已保留，可直接重试：{public}", project_id),
        )
        conn.commit()
        raise HTTPException(
            503,
            "分集任务未能启动，项目和原文已保留，请在分集页重试",
        ) from exc
    return {
        "status": "running",
        "task_id": task_id,
        "resumed": resumed,
        "planner": "regex",
        "rule": "one_chapter_one_episode",
    }


@router.post("/projects/{project_id}/plan")
async def start_plan_route(project_id: str):
    from app.capabilities.dispatch import dispatch, respond_ui

    result = await dispatch("episode.plan", {"project_id": project_id}, initiator="ui")
    return respond_ui(result)
