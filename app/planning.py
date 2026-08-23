"""Deterministic regex-based episode planning.

Novel headings are split by ``ingest.CHAPTER_RE`` during ingestion; this module
maps each resulting chapter to exactly one episode without an LLM.
"""
from __future__ import annotations

import json
import re
import shutil
from typing import Any

from fastapi import APIRouter, Body, HTTPException

from app import config, errors, task_registry
from app.db import get_conn, new_id, now, rows_to_dicts
from app.evidence.repository import ACTIVE_RUN_STATUSES
from app.ingest import dedupe_stub_chapters

router = APIRouter(prefix="/api")
PLAN_PREVIEW_CHARS = 100
ACTIVE_MEDIA_JOB_STATUSES = {
    "queued",
    "reserved",
    "running",
    "waiting_provider",
    "waiting_retry",
    "waiting_budget",
    "waiting",
    "waiting_human",
    "paused_budget",
    "paused",
}


class ReplanActiveWorkError(RuntimeError):
    def __init__(self, blockers: dict[str, Any]) -> None:
        super().__init__("项目仍有未结束的下游任务")
        self.blockers = blockers


def chapter_preview(content: str | None, limit: int = PLAN_PREVIEW_CHARS) -> str:
    return re.sub(r"\s+", " ", (content or "")).strip()[:limit]


def replan_blockers(conn, project_id: str) -> dict[str, Any]:
    """Return work that would be orphaned if the current episodes were replaced."""
    episode_ids = [
        row["id"]
        for row in conn.execute(
            "SELECT id FROM episodes WHERE project_id=?",
            (project_id,),
        ).fetchall()
    ]
    active_tasks = [
        {"episode_id": episode_id, "kind": kind}
        for episode_id in episode_ids
        for kind in ("screenplay", "storyboard", "video_completion")
        if task_registry.active(kind, episode_id)
    ]
    if task_registry.active("video_completion_project", project_id):
        active_tasks.append({
            "project_id": project_id,
            "kind": "video_completion_project",
        })

    run_marks = ",".join("?" for _ in ACTIVE_RUN_STATUSES)
    active_runs = rows_to_dicts(conn.execute(
        f"""SELECT id, workflow_type, scope_type, scope_id, status
            FROM workflow_runs
            WHERE recovered_by_run_id IS NULL
              AND status IN ({run_marks})
              AND (
                (scope_type='episode' AND scope_id IN (
                    SELECT id FROM episodes WHERE project_id=?
                ))
                OR (scope_type='shot' AND scope_id IN (
                    SELECT s.id FROM shots s
                    JOIN episodes e ON e.id=s.episode_id
                    WHERE e.project_id=?
                ))
                OR (
                    scope_type='project' AND scope_id=?
                    AND workflow_type='project_video_completion_queue'
                )
              )
            ORDER BY updated_at, id""",
        (*sorted(ACTIVE_RUN_STATUSES), project_id, project_id, project_id),
    ).fetchall())

    job_marks = ",".join("?" for _ in ACTIVE_MEDIA_JOB_STATUSES)
    active_jobs = rows_to_dicts(conn.execute(
        f"""SELECT id, episode_id, kind, status
            FROM jobs
            WHERE project_id=? AND status IN ({job_marks})
              AND cancellation_requested=0 AND abandoned=0
            ORDER BY created_at, id""",
        (project_id, *sorted(ACTIVE_MEDIA_JOB_STATUSES)),
    ).fetchall())
    return {
        "active_tasks": active_tasks,
        "active_runs": active_runs,
        "active_media_jobs": len(active_jobs),
        "active_job_details": active_jobs,
        "blocked": bool(active_tasks or active_runs or active_jobs),
    }


def _raise_replan_active_work(blockers: dict[str, Any]) -> None:
    if not blockers["blocked"]:
        return
    raise HTTPException(409, detail={
        "code": "REPLAN_ACTIVE_WORK",
        "message": "项目仍有剧本、分镜、视频或交付任务，不能重新分集",
        **blockers,
        "recovery_action": "请先在对应工作台或任务中心结束、取消这些任务，再重新规划分集",
    })


async def run_regex_plan(project_id: str) -> None:
    """Replace a project's plan with one episode per regex-split chapter."""
    conn = get_conn()
    committed = False
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
        blockers = replan_blockers(conn, project_id)
        if blockers["blocked"]:
            raise ReplanActiveWorkError(blockers)
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
            "status='planned', plan_finished_at=? WHERE id=?", (now(), project_id)
        )
        conn.commit()
        committed = True
    except ReplanActiveWorkError:
        conn.rollback()
        conn.execute(
            "UPDATE projects SET plan_status='failed', plan_error=?, plan_finished_at=? WHERE id=?",
            (
                "重新分集未执行：检测到仍可继续或正在运行的下游任务。"
                "请先在对应工作台或任务中心结束、取消任务后重试；原分集和媒体均已保留。",
                now(),
                project_id,
            ),
        )
        conn.commit()
    except Exception as exc:  # noqa: BLE001 -- task failures must be persisted for the UI
        # Episode inserts and the final project status share one transaction.
        # Never expose a failed plan together with a partial episode list.
        conn.rollback()
        public = errors.record_and_format(
            exc, action="plan_generate", context={"project_id": project_id}
        )
        conn.execute(
            "UPDATE projects SET plan_status='failed', plan_error=?, plan_finished_at=? WHERE id=?",
            (public, now(), project_id),
        )
        conn.commit()
    if not committed:
        return

    episode_dir = config.PROJECTS_DIR / project_id / "episodes"
    if not episode_dir.exists():
        return
    try:
        shutil.rmtree(episode_dir)
    except OSError as exc:
        public = errors.record_and_format(
            exc,
            action="plan_media_cleanup",
            context={"project_id": project_id, "episode_dir": str(episode_dir)},
        )
        conn.execute(
            "UPDATE projects SET plan_error=? WHERE id=?",
            (
                "分集已更新，但旧媒体缓存未完全清理；新分集不受影响。"
                f"再次重新分集或清理项目缓存即可重试：{public}",
                project_id,
            ),
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
                "UPDATE projects SET plan_status='failed', plan_error=?, plan_finished_at=? WHERE id=?",
                (f"分集恢复任务未能启动，原文已保留，可在分集页重试：{public}", now(), project_id),
            )
            conn.commit()
    return resumed


async def start_plan(project_id: str, *, replace_existing: bool = False) -> dict:
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
    task_id = f"plan:{project_id}"
    if task_registry.active("plan", project_id):
        if project["plan_status"] != "running":
            conn.execute(
                # 任务已在跑，状态只是没对上；起点缺失时补一个，不覆盖已有值。
                "UPDATE projects SET plan_status='running', plan_error=NULL, "
                "plan_started_at=COALESCE(plan_started_at, ?) WHERE id=?",
                (now(), project_id),
            )
            conn.commit()
        return {
            "status": "running",
            "task_id": task_id,
            "already_running": True,
            "planner": "regex",
            "rule": "one_chapter_one_episode",
        }
    if episode_ids and not replace_existing:
        raise HTTPException(409, detail={
            "code": "REPLAN_CONFIRMATION_REQUIRED",
            "message": "项目已有分集；重新规划会清空现有剧集链，必须明确确认替换",
            "episode_count": len(episode_ids),
            "recovery_action": "确认影响后，以 replace_existing=true 重新提交",
        })
    _raise_replan_active_work(replan_blockers(conn, project_id))
    resumed = project["plan_status"] == "running"
    conn.execute(
        # plan_started_at 是任务计时的服务端起点：续跑保留原起点，新任务才重新计时。
        "UPDATE projects SET plan_status='running', plan_error=NULL, "
        "plan_started_at=CASE WHEN ? THEN COALESCE(plan_started_at, ?) ELSE ? END WHERE id=?",
        (1 if resumed else 0, now(), now(), project_id),
    )
    conn.commit()
    try:
        task_registry.spawn("plan", project_id, run_regex_plan(project_id), project_id=project_id)
    except Exception as exc:
        public = errors.record_and_format(
            exc, action="plan_spawn", context={"project_id": project_id}
        )
        conn.execute(
            "UPDATE projects SET plan_status='failed', plan_error=?, plan_finished_at=? WHERE id=?",
            (f"分集任务未能启动，项目和原文已保留，可直接重试：{public}", now(), project_id),
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
async def start_plan_route(project_id: str, body: dict | None = Body(None)):
    from app.capabilities.dispatch import dispatch, respond_ui

    payload = dict(body) if isinstance(body, dict) else {}
    result = await dispatch(
        "episode.plan",
        {
            "project_id": project_id,
            "replace_existing": bool(payload.get("replace_existing")),
            "idempotency_key": payload.get("idempotency_key"),
        },
        initiator="ui",
    )
    return respond_ui(result)
