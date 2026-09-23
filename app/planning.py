"""Deterministic regex-based episode planning.

Novel headings are split by ``ingest.CHAPTER_RE`` during ingestion; this module
maps each resulting chapter to exactly one episode without an LLM.
"""
from __future__ import annotations

import asyncio
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
    "waiting",
    "waiting_human",
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


def _insert_regex_plan_episodes(conn, project_id: str, chapters: list[dict]) -> None:
    """按章节顺序整体替换本项目的剧集行；从 ``_replace_regex_plan`` 拆出纯粹是
    为了单函数不超 50 代码行，不是独立的事务边界——调用方已持有 ``BEGIN
    IMMEDIATE`` 写锁，这里不再自行开事务/提交。"""
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


def _finalize_regex_plan_failure(conn, project_id: str, exc: Exception) -> None:
    """两条失败分支共用的收尾：调用方已经把 ``conn.rollback()`` 作为异常处理器
    第一条语句执行过，这里只负责把 plan_status 写回 failed 并独立提交——这条
    UPDATE 不属于被回滚的那个事务。"""
    if isinstance(exc, ReplanActiveWorkError):
        message = (
            "重新分集未执行：检测到仍可继续或正在运行的下游任务。"
            "请先在对应工作台或任务中心结束、取消任务后重试；原分集和媒体均已保留。"
        )
    else:
        # Episode inserts and the final project status share one transaction.
        # Never expose a failed plan together with a partial episode list.
        message = errors.record_and_format(
            exc, action="plan_generate", context={"project_id": project_id}
        )
    conn.execute(
        "UPDATE projects SET plan_status='failed', plan_error=? WHERE id=?",
        (message, project_id),
    )
    conn.commit()


def _replace_regex_plan(conn, project_id: str) -> bool:
    """一次 ``BEGIN IMMEDIATE`` 事务内校验没有下游在跑、再整体替换分集。
    返回是否提交成功；失败时已经把 plan_status 写回 failed 并各自提交。"""
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
        _insert_regex_plan_episodes(conn, project_id, chapters)
        conn.commit()
        return True
    except Exception as exc:  # noqa: BLE001 -- task failures (incl. ReplanActiveWorkError) must be persisted for the UI
        conn.rollback()
        _finalize_regex_plan_failure(conn, project_id, exc)
        return False


def _cleanup_regex_plan_episode_media(conn, project_id: str) -> None:
    """数据库替换已提交后才清理旧集媒体目录；失败不影响新分集本身，只在项目
    上追加一句可重试的提示。"""
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


def _run_regex_plan_sync(project_id: str) -> None:
    """``run_regex_plan`` 的同步重活本体，离开事件循环线程执行（照抄
    ``app/capabilities/handlers/delivery.py::_concatenate_in_thread`` 的写法）。
    此前这个函数整段是 ``async def`` 但体内一次 ``await`` 都没有——协程一旦被
    ``task_registry.spawn`` 调度就会不间断跑到底，等价于把「整项目分集替换
    事务 + 递归 rmtree」直接焊在事件循环线程上，与 2026-09-15 合成冻结后端
    122 秒同一类根因。

    连接归属：``get_conn()`` 在这个工作线程里没有正在运行的 asyncio task，走
    线程本地分支，拿到的是**这个线程自己的连接**；调用方 ``run_regex_plan``
    在进入线程前没有任何数据库动作，不存在"调用方连接遗留未提交写"的问题。
    这条连接从获取到最终提交/回滚全程只在本函数（及其拆出的 helper）内使用。
    """
    conn = get_conn()
    if _replace_regex_plan(conn, project_id):
        _cleanup_regex_plan_episode_media(conn, project_id)


async def run_regex_plan(project_id: str) -> None:
    """Replace a project's plan with one episode per regex-split chapter.

    整个函数体是同步的 DB 事务 + 文件清理，``await asyncio.to_thread`` 把它
    移出事件循环线程；实现与连接归属见 ``_run_regex_plan_sync``。
    """
    await asyncio.to_thread(_run_regex_plan_sync, project_id)


def recover_plan_tasks() -> int:
    conn = get_conn()
    resumed = 0
    for row in conn.execute(
        "SELECT id FROM projects -- ALL_OWNERS: startup recovery scans every "
        "project for orphaned running episode-planning tasks after a process "
        "reload/restart; runs before traffic is accepted, no request context\n"
        "WHERE plan_status='running' AND deleted_at IS NULL"
    ).fetchall():
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
