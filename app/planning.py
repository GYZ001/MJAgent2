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
        worker.delete_project_episodes(project_id)
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
    except Exception as exc:  # noqa: BLE001 -- task failures must be persisted for the UI
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
        from app import auto
        if auto.is_running(project_id):
            continue
        if not task_registry.active("plan", project_id):
            task_registry.spawn(
                "plan", project_id, run_regex_plan(project_id), project_id=project_id
            )
            resumed += 1
    return resumed


async def start_plan(project_id: str) -> dict:
    """启动分集规划的领域逻辑，供 REST 路由与 ``episode.plan`` Command Handler 共用。"""
    conn = get_conn()
    project = conn.execute("SELECT id, plan_status FROM projects WHERE id=?", (project_id,)).fetchone()
    if not project:
        raise HTTPException(404, f"项目不存在：{project_id}")
    if project["plan_status"] == "running":
        raise HTTPException(409, "剧集规划正在生成")
    conn.execute(
        "UPDATE projects SET plan_status='running', plan_error=NULL WHERE id=?", (project_id,)
    )
    conn.commit()
    task_registry.spawn("plan", project_id, run_regex_plan(project_id), project_id=project_id)
    return {"status": "running", "planner": "regex", "rule": "one_chapter_one_episode"}


@router.post("/projects/{project_id}/plan")
async def start_plan_route(project_id: str):
    from app.capabilities.dispatch import dispatch, respond_ui

    result = await dispatch("episode.plan", {"project_id": project_id}, initiator="ui")
    return respond_ui(result)
