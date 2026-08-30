"""启动时恢复孤儿的人物谱/角色引用图/场景引用图任务。

从 app/domain/bible_ops.py 按原样搬移；依赖 task_run 提供的任务体与录制器，因此排在其后。
"""
from __future__ import annotations

import json

from app import (
    errors,
    task_registry,
)
from app.db import get_conn
from app.domain.common import (
    _bible_task_active,
    _refs_task_active,
    _scene_assets_task_active,
)

from .primitives import (
    _decode_refs_target,
    _supports_bible_style_name,
)
from .refs_generation import _start_refs_generation
from .scene_bible_prep import (
    _decode_scene_target,
    _start_scene_bible_preparation,
    _start_scene_refs_generation,
)
from .task_run import (
    _new_bible_recorder,
    _recorded_bible_task,
)


def recover_bible_tasks() -> int:
    """启动时恢复人物谱任务（对齐 worker.recover_and_start 的语义）：
    进程重启/reload 会丢掉内存里的 asyncio.Task，但 DB 仍是 running。
    与其在下次访问时判孤儿并报错，不如用持久化的 feedback 重新拉起任务续跑。"""
    conn = get_conn()
    style_column = "bible_style_name" if _supports_bible_style_name(conn) else "NULL AS bible_style_name"
    rows = conn.execute(
        f"SELECT id, bible_feedback, {style_column} "
        "FROM projects -- ALL_OWNERS: startup recovery scans every project for "
        "orphaned running bible tasks after a process reload/restart; runs "
        "before traffic is accepted, no request/Principal context\n"
        "WHERE bible_status='running' AND deleted_at IS NULL"
    ).fetchall()
    resumed = 0
    for r in rows:
        pid = r["id"]
        if _bible_task_active(pid):
            continue
        feedback = r["bible_feedback"] or ""
        parent = conn.execute(
            "SELECT id FROM workflow_runs WHERE workflow_type='character_bible' "
            "AND scope_type='project' AND scope_id=? AND status='PAUSED_EXTERNAL' "
            "AND recovered_by_run_id IS NULL ORDER BY updated_at DESC LIMIT 1",
            (pid,),
        ).fetchone()
        recorder = None
        try:
            recorder = _new_bible_recorder(
                pid, trigger_type="resume", requested_by="system",
                parent_run_id=parent["id"] if parent else None,
                style_name=r["bible_style_name"],
            )
            task_registry.spawn(
                "bible",
                pid,
                _recorded_bible_task(
                    pid, feedback, recorder, trigger_full_refs=True,
                    style_name=r["bible_style_name"],
                ),
                project_id=pid,
            )
            resumed += 1
        except Exception as exc:  # one project must not block all startup recovery
            public = errors.record_and_format(
                exc,
                action="bible_recovery_spawn",
                context={"project_id": pid},
            )
            conn.execute(
                "UPDATE projects SET bible_status='failed', bible_error=? WHERE id=?",
                (f"人物谱自动恢复未能启动，原文和反馈已保留，可重新发起。{public}", pid),
            )
            conn.commit()
            if recorder is not None:
                try:
                    recorder.cancel("人物谱恢复任务未能启动", conn=None)
                except Exception:  # noqa: BLE001
                    pass
    return resumed

def recover_character_ref_tasks() -> int:
    """Resume portrait batches without changing their original refresh semantics."""
    conn = get_conn()
    rows = conn.execute(
        """SELECT id, refs_target, refs_resume, refs_batch_started_at
           FROM projects p -- ALL_OWNERS: startup recovery scans every project
           -- for orphaned running portrait-batch tasks after a process
           -- reload/restart; runs before traffic is accepted, no request
           -- context
           WHERE p.deleted_at IS NULL
             AND (
               refs_status='running'
                  OR EXISTS (
                      SELECT 1 FROM workflow_runs wr
                       WHERE wr.workflow_type='character_references'
                         AND wr.scope_type='project'
                         AND wr.scope_id=p.id
                         AND wr.status='PAUSED_EXTERNAL'
                         AND wr.recovered_by_run_id IS NULL
                  )
             )"""
    ).fetchall()
    resumed = 0
    for row in rows:
        project_id = row["id"]
        if _refs_task_active(project_id):
            continue
        parent = conn.execute(
            "SELECT id FROM workflow_runs WHERE workflow_type='character_references' "
            "AND scope_type='project' AND scope_id=? AND status='PAUSED_EXTERNAL' "
            "AND recovered_by_run_id IS NULL ORDER BY updated_at DESC LIMIT 1",
            (project_id,),
        ).fetchone()
        only_character, only_characters = _decode_refs_target(row["refs_target"])
        was_gap_resume = bool(row["refs_resume"])
        fresh_after = None if was_gap_resume else row["refs_batch_started_at"]
        try:
            if _start_refs_generation(
                project_id,
                only_character,
                only_characters=only_characters,
                resume=True,
                fresh_after=fresh_after,
                parent_run_id=parent["id"] if parent else None,
            ):
                resumed += 1
        except Exception as exc:  # noqa: BLE001
            public = errors.record_and_format(
                exc, action="refs_recovery_spawn", context={"project_id": project_id},
            )
            conn.execute(
                "UPDATE projects SET refs_status='failed',refs_error=? WHERE id=?",
                (f"定妆自动恢复未能启动，已完成素材仍保留，可重试缺口。{public}", project_id),
            )
            conn.commit()
    return resumed

def recover_scene_ref_tasks() -> int:
    """Resume persisted scene-asset work after a reload or process restart.

    Scene generation is idempotent: approved references are skipped, so an
    interrupted batch safely continues from the first missing scene instead of
    regenerating accepted assets.
    """
    conn = get_conn()
    rows = conn.execute(
        "SELECT id,bible_json,bible_status,scene_refs_status,scene_refs_target "
        "FROM projects -- ALL_OWNERS: startup recovery scans every project for "
        "orphaned scene-asset tasks after a process reload/restart; runs "
        "before traffic is accepted, no request context\n"
        "WHERE deleted_at IS NULL AND ("
        "scene_refs_status='running' "
        "OR (scene_refs_status='idle' AND bible_status='ready')"
        ")"
    ).fetchall()
    resumed = 0
    for row in rows:
        project_id = row["id"]
        # A recovered character-bible task will start a fresh scene pipeline
        # after committing its new Bible.  Starting from the old Bible here
        # would race it and could generate obsolete assets.
        if (row["bible_status"] == "running"
                or _scene_assets_task_active(project_id)):
            continue
        try:
            bible = json.loads(row["bible_json"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            bible = {}
        if bible.get("scenes"):
            if row["scene_refs_status"] != "running":
                continue
            parent = conn.execute(
                "SELECT id FROM workflow_runs WHERE workflow_type='scene_references' "
                "AND scope_type='project' AND scope_id=? AND status='PAUSED_EXTERNAL' "
                "AND recovered_by_run_id IS NULL ORDER BY updated_at DESC LIMIT 1",
                (project_id,),
            ).fetchone()
            try:
                if _start_scene_refs_generation(
                    project_id,
                    _decode_scene_target(row["scene_refs_target"]),
                    resume=True,
                    parent_run_id=parent["id"] if parent else None,
                ):
                    resumed += 1
            except Exception as exc:  # noqa: BLE001
                public = errors.record_and_format(
                    exc, action="scene_refs_recovery_spawn",
                    context={"project_id": project_id},
                )
                conn.execute(
                    "UPDATE projects SET scene_refs_status='failed',scene_refs_error=? WHERE id=?",
                    (f"场景图自动恢复未能启动，已完成素材仍保留，可重试缺口。{public}", project_id),
                )
                conn.commit()
            continue
        parent = conn.execute(
            "SELECT id FROM workflow_runs WHERE workflow_type='scene_bible' "
            "AND scope_type='project' AND scope_id=? AND status='PAUSED_EXTERNAL' "
            "AND recovered_by_run_id IS NULL ORDER BY updated_at DESC LIMIT 1",
            (project_id,),
        ).fetchone()
        try:
            if _start_scene_bible_preparation(
                project_id,
                parent_run_id=parent["id"] if parent else None,
                requested_by="system",
                trigger_type="resume" if row["scene_refs_status"] == "running" else "automatic",
            ):
                resumed += 1
        except Exception as exc:  # noqa: BLE001
            public = errors.record_and_format(
                exc, action="scene_bible_recovery_spawn", context={"project_id": project_id},
            )
            conn.execute(
                "UPDATE projects SET scene_refs_status='failed',scene_refs_error=? WHERE id=?",
                (f"场景清单自动恢复未能启动，可重新发起。{public}", project_id),
            )
            conn.commit()
    return resumed
