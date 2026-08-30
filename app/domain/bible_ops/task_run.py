"""人物谱生成任务体、录制器与 start/cancel 入口。

从 app/domain/bible_ops.py 按原样搬移。
"""
from __future__ import annotations

import asyncio
import json

from app import (
    errors,
    task_registry,
)
from app.db import (
    get_conn,
    get_setting,
    rows_to_dicts,
)
from app.domain.common import (
    BIBLE_INTERRUPTED_ERROR,
    BIBLE_TASK_TIMEOUT_S,
    _as_body_dict,
    _bible_task_active,
    _project_or_404,
    _require_harness_engine,
    router,
)
from app.harness.context import ContextPack
from app.orchestration.engine import (
    WorkflowRecorder,
    fingerprint,
)
from app.stages import (
    StageError,
    generate_bible,
)
from fastapi import (
    Body,
    HTTPException,
)

from .precheck import (
    _compute_bible_generate_precheck,
    _purge_for_style_change,
)
from .primitives import (
    _consume_payment_quote,
    _normalize_visual_style_name,
    _payment_confirm_required,
    _project_columns,
    _supports_bible_style_name,
    _validate_payment_quote,
    _visual_style_prompt_or_default,
)
from .refs_generation import _start_refs_generation
from .scene_bible_prep import _start_scene_bible_preparation


async def _bible_task(
    project_id: str,
    feedback: str = "",
    *,
    trigger_full_refs: bool = True,
    style_name: str | None = None,
):
    conn = get_conn()
    try:
        if style_name is None and _supports_bible_style_name(conn):
            style_row = conn.execute(
                "SELECT bible_style_name FROM projects WHERE id=?", (project_id,),
            ).fetchone()
            style_name = style_row["bible_style_name"] if style_row else None
        style_name = _normalize_visual_style_name(style_name)
        style_prompt = _visual_style_prompt_or_default(style_name)
        chapters = rows_to_dicts(conn.execute(
            "SELECT * FROM chapters WHERE project_id=? ORDER BY idx", (project_id,)).fetchall())
        timeout_s = max(int(get_setting("bible_task_timeout_s") or BIBLE_TASK_TIMEOUT_S), 60)
        # 重新谱写时按角色名保留已有定妆照（重生圣经不应丢失一致性锚点）
        old_row = conn.execute("SELECT bible_json FROM projects WHERE id=?", (project_id,)).fetchone()
        old_style = None
        old_bible = None
        if old_row and old_row["bible_json"]:
            old_bible = json.loads(old_row["bible_json"])
        from app import model_registry
        from app.harness.text_provider_scope import stage_text_provider

        resolved_text_provider = None
        if "bible_text_provider" in _project_columns(conn):
            provider_row = conn.execute(
                "SELECT bible_text_provider FROM projects WHERE id=?", (project_id,),
            ).fetchone()
            resolved_text_provider = model_registry.resolve_stage_text_provider(
                provider_row["bible_text_provider"] if provider_row else None
            )
        with stage_text_provider(resolved_text_provider):
            bible = await asyncio.wait_for(
                generate_bible(
                    chapters, feedback=feedback, previous_bible=old_bible,
                    project_id=project_id, visual_style_prompt=style_prompt,
                ),
                timeout=timeout_s,
            )
        if old_bible:
            old_style = (old_bible.get("world") or {}).get("visual_style_canonical")
            old_refs = {c.get("name"): c.get("ref_image_path")
                        for c in old_bible.get("characters", [])}
            for c in bible.characters:
                c.ref_image_path = old_refs.get(c.name) or None
        # 重谱后画风变化 → 旧画风定妆照与旧视频全部作废（否则图像信号会把新画风拉回旧画风）
        if old_style and bible.world.visual_style_canonical != old_style:
            _purge_for_style_change(project_id, bible)
        residual = list(getattr(bible, "residual_errors", []) or [])
        artifact_id = getattr(bible, "evidence_artifact_id", None)
        bible_status = "warning" if residual else "ready"
        bible_error = (
            "人物谱存在阻塞问题，允许人工修订，但不会进入下游：" + "；".join(residual[:8])
            if residual else None
        )
        # A few unit tests intentionally use a minimal legacy schema.  Production
        # databases always receive the incremental migration in app.db, while the
        # fallback keeps the stage function independently testable.
        project_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(projects)").fetchall()
        }
        final_project_status = "bible_ready"
        if "plan_status" in project_columns:
            plan_row = conn.execute(
                "SELECT plan_status FROM projects WHERE id=?", (project_id,)
            ).fetchone()
            if plan_row and plan_row["plan_status"] == "ready":
                final_project_status = "planned"
        if "bible_artifact_id" in project_columns:
            conn.execute(
                "UPDATE projects SET bible_json=?, bible_version=bible_version+1, bible_status=?, "
                "bible_error=?, bible_artifact_id=?, status=? WHERE id=?",
                (
                    bible.model_dump_json(), bible_status, bible_error, artifact_id,
                    final_project_status if not residual else "created", project_id,
                ))
        else:
            conn.execute(
                "UPDATE projects SET bible_json=?, bible_version=bible_version+1, bible_status=?, "
                "bible_error=?, status=? WHERE id=?",
                (
                    bible.model_dump_json(), bible_status, bible_error,
                    final_project_status if not residual else "created", project_id,
                ))
        conn.commit()
        if trigger_full_refs and not residual:
            try:
                _start_refs_generation(project_id, None)
            except Exception as exc:  # noqa: BLE001 bible remains deliverable
                public = errors.record_and_format(
                    exc, action="refs_spawn_after_bible", context={"project_id": project_id},
                )
                conn.execute(
                    "UPDATE projects SET refs_status='failed',refs_error=? WHERE id=?",
                    (f"人物谱已完成，但定妆任务未能启动，可直接重试定妆。{public}", project_id),
                )
                conn.commit()
            if "scene_refs_status" in project_columns:
                # 场景清单还不存在（首次谱写）或即将被这次 free 重生成覆盖（重谱换
                # 风格）：两种情况场景图都要在清单就绪后自动继续，不能停下来等用户
                # 之后碰巧访问场景库页。票据写在这里、由 _scene_bible_task 消费。
                if "pending_scene_regen" in project_columns:
                    conn.execute(
                        "UPDATE projects SET pending_scene_regen=1 WHERE id=?", (project_id,),
                    )
                    conn.commit()
                try:
                    _start_scene_bible_preparation(project_id)
                except Exception as exc:  # noqa: BLE001 bible remains deliverable
                    public = errors.record_and_format(
                        exc, action="scene_bible_spawn_after_bible",
                        context={"project_id": project_id},
                    )
                    conn.execute(
                        "UPDATE projects SET scene_refs_status='failed',scene_refs_error=? WHERE id=?",
                        (f"人物谱已完成，但场景设定未能启动，可在场景库重试。{public}", project_id),
                    )
                    conn.commit()
    except asyncio.TimeoutError:
        conn.execute(
            "UPDATE projects SET bible_status='failed', bible_error=? WHERE id=?",
            (f"人物谱解析/修复超时（超过 {timeout_s} 秒），请重新谱写。", project_id),
        )
        conn.commit()
    except asyncio.CancelledError:
        if task_registry.shutdown_in_progress():
            raise
        row = conn.execute("SELECT bible_status FROM projects WHERE id=?", (project_id,)).fetchone()
        if row and row["bible_status"] == "running":
            conn.execute(
                "UPDATE projects SET bible_status='failed', bible_error=? WHERE id=?",
                (BIBLE_INTERRUPTED_ERROR, project_id),
            )
            conn.commit()
        raise
    except (StageError, Exception) as exc:  # noqa: BLE001
        # 回滚必须在 errors.record_and_format() 之前（同一根因见
        # app.domain.storyboard_ops._storyboard_task 顶层 except 上方的大注释：
        # app.db.insert_error_log 在这同一个 task 缓存连接上落一条 error_logs 行
        # 并 conn.commit()，谁先调用谁就先把此刻挂起的写入定型）。这条 try 里画风
        # 变更时会走到 _purge_for_style_change → worker.purge_project_video_
        # artifacts，后者对全项目逐镜头 DELETE shot_versions/shot_scenes/jobs、
        # 逐集回退状态，整段过程故意不提交，只在处理完全部镜头后 commit 一次；
        # 中途任何一步失败（文件 I/O、约束冲突等）都会把尚未提交的部分 DELETE
        # 留在这个连接上。这里如果先记日志再回滚，日志落库的隐式 commit 会把这份
        # 半成品（部分镜头的视频记录已删、其余镜头未处理）直接定型进库，且波及的
        # 是整个项目而不止一集——这正是本文件同类 purge 调用（_refs_task 的
        # errors.record_and_format 同理）必须先回滚的原因。回滚只丢弃这次失败尝试
        # 自己产生的未提交写入，不影响更早已经各自 commit 过的检查点。
        if conn.in_transaction:
            conn.rollback()
        public = errors.record_and_format(exc, action="bible_generate", context={"project_id": project_id})
        conn.execute("UPDATE projects SET bible_status='failed', bible_error=? WHERE id=?", (public, project_id))
        conn.commit()

def _new_bible_recorder(
    project_id: str,
    *,
    requested_by: str = "user",
    trigger_type: str = "manual",
    parent_run_id: str | None = None,
    style_name: str | None = None,
) -> WorkflowRecorder:
    conn = get_conn()
    chapters = rows_to_dicts(conn.execute(
        "SELECT idx, title, content FROM chapters WHERE project_id=? ORDER BY idx", (project_id,)
    ).fetchall())
    if style_name is None and _supports_bible_style_name(conn):
        row = conn.execute("SELECT bible_style_name FROM projects WHERE id=?", (project_id,)).fetchone()
        style_name = row["bible_style_name"] if row else None
    style_name = _normalize_visual_style_name(style_name)
    project = conn.execute(
        "SELECT bible_version, bible_feedback FROM projects WHERE id=?", (project_id,)
    ).fetchone()
    return WorkflowRecorder.create(
        workflow_type="character_bible",
        scope_type="project",
        scope_id=project_id,
        input_fingerprint=fingerprint(
            chapters, project["bible_version"] if project else 0,
            project["bible_feedback"] if project else None, style_name,
        ),
        requested_by=requested_by,
        trigger_type=trigger_type,
        policy_snapshot={"max_iterations": 4, "warning_blocks_downstream": True},
        config_snapshot={"style_name": style_name},
        parent_run_id=parent_run_id,
    )

async def _recorded_bible_task(
    project_id: str,
    feedback: str,
    recorder: WorkflowRecorder,
    *,
    trigger_full_refs: bool,
    style_name: str | None = None,
) -> None:
    recorder.start()
    try:
        conn = get_conn()
        chapters = rows_to_dicts(conn.execute(
            "SELECT idx, title, content FROM chapters WHERE project_id=? ORDER BY idx", (project_id,)
        ).fetchall())
        context = ContextPack(goal="生成可追溯人物圣经")
        context.add_text("chapters", "\n\n".join(ch["content"] for ch in chapters), limit=60000)
        await recorder.step(
            "character_bible",
            lambda: _bible_task(
                project_id, feedback, trigger_full_refs=trigger_full_refs,
                style_name=style_name,
            ),
            contract_key="character_bible",
            agent_name="character_bible",
            context_manifest=context.manifest(),
        )
        row = conn.execute("SELECT bible_status, bible_error FROM projects WHERE id=?", (project_id,)).fetchone()
        if row and row["bible_status"] == "ready":
            recorder.succeed("人物谱已通过确定性门禁", conn=None)
        elif row and row["bible_status"] == "warning":
            recorder.partial(row["bible_error"] or "人物谱需要人工修订", conn=None)
        else:
            recorder.fail(RuntimeError(row["bible_error"] if row else "人物谱生成失败"), conn=None)
    except asyncio.CancelledError:
        if task_registry.shutdown_in_progress():
            recorder.pause_external("服务重启，人物谱任务等待自动恢复", conn=None)
        else:
            recorder.cancel(conn=None)
        raise
    except Exception as exc:
        recorder.fail(exc, conn=None)
        raise

async def _start_bible_core(
    project_id: str,
    feedback: str,
    *,
    confirm: bool = False,
    quote_id: str | None = None,
    require_quote_id: bool = False,
    style_name: str | None = None,
) -> dict:
    """启动人物谱生成的领域逻辑，供 REST 路由与 ``bible.generate`` Command Handler 共用。"""
    p = _project_or_404(project_id)
    _require_harness_engine(project_id)
    if p["bible_status"] == "running" and _bible_task_active(project_id):
        raise HTTPException(409, "角色圣经正在生成中")
    if p["refs_status"] == "running":
        raise HTTPException(409, "定妆照正在生成中，请先停止后再重生人物谱")
    feedback = feedback.strip()
    if len(feedback) > 2000:
        raise HTTPException(400, "打回要求过长，请控制在 2000 字以内")
    style_name = _normalize_visual_style_name(style_name)
    precheck = _compute_bible_generate_precheck(project_id, style_name=style_name)
    if not confirm:
        raise _payment_confirm_required(precheck)
    quote_row = _validate_payment_quote(project_id, quote_id, precheck)
    if quote_row["consumed_at"] is not None:
        return {
            "status": "accepted", "idempotent_replay": True,
            "task_id": quote_row["consumed_task_id"], "run_id": quote_row["consumed_run_id"],
        }
    conn = get_conn()
    # 持久化 feedback：进程重启后 recover_bible_tasks 能用相同入参续跑，而非中断报错
    if _supports_bible_style_name(conn):
        conn.execute(
            "UPDATE projects SET bible_status='running', bible_error=NULL, "
            "bible_feedback=?, bible_style_name=? WHERE id=?",
            (feedback, style_name, project_id),
        )
    else:
        conn.execute(
            "UPDATE projects SET bible_status='running', bible_error=NULL, bible_feedback=? WHERE id=?",
            (feedback, project_id),
        )
    conn.commit()
    recorder = None
    try:
        recorder = _new_bible_recorder(project_id, style_name=style_name)
        task_registry.spawn(
            "bible",
            project_id,
            _recorded_bible_task(
                project_id, feedback, recorder, trigger_full_refs=True,
                style_name=style_name,
            ),
            project_id=project_id,
        )
    except Exception as exc:
        if _supports_bible_style_name(conn):
            conn.execute(
                "UPDATE projects SET bible_status=?, bible_error=?, "
                "bible_feedback=?, bible_style_name=? WHERE id=?",
                (
                    p["bible_status"],
                    p["bible_error"],
                    p.get("bible_feedback"),
                    p.get("bible_style_name"),
                    project_id,
                ),
            )
        else:
            conn.execute(
                "UPDATE projects SET bible_status=?, bible_error=?, bible_feedback=? WHERE id=?",
                (
                    p["bible_status"],
                    p["bible_error"],
                    p.get("bible_feedback"),
                    project_id,
                ),
            )
        conn.commit()
        if recorder is not None:
            try:
                recorder.cancel("人物谱任务未能启动，项目状态已回滚", conn=None)
            except Exception:  # noqa: BLE001
                pass
        raise HTTPException(503, {
            "code": "BIBLE_START_FAILED",
            "message": "人物谱任务未能启动，项目原状态和费用凭证均已保留，请重试",
            "action": "retry_generate",
        }) from exc
    _consume_payment_quote(
        str(quote_id), task_id=f"bible:{project_id}", run_id=recorder.run_id,
    )
    return {"status": "running", "task_id": f"bible:{project_id}", "run_id": recorder.run_id}

@router.post("/projects/{project_id}/bible")
async def start_bible(project_id: str, body: dict | None = Body(None)):
    from app.capabilities.dispatch import dispatch, respond_ui

    payload = _as_body_dict(body)
    feedback = str(payload.get("feedback") or "")
    result = await dispatch(
        "bible.generate",
        {
            "project_id": project_id,
            "feedback": feedback,
            "confirm": payload.get("confirm") is True,
            "quote_id": payload.get("quote_id"),
            "require_quote_id": True,
            "style_name": payload.get("style_name"),
            "idempotency_key": payload.get("idempotency_key") or payload.get("quote_id"),
        },
        initiator="ui",
    )
    return respond_ui(result)

async def _cancel_bible_core(project_id: str) -> dict:
    """停止人物谱生成的领域逻辑，供 REST 路由与 ``bible.cancel`` Command Handler 共用。
    若人物谱尚未完成，停止后不会继续触发后续定妆照任务。"""
    p = _project_or_404(project_id)
    stopped = await task_registry.cancel_and_wait("bible", project_id)
    conn = get_conn()
    conn.execute(
        "UPDATE projects SET bible_status='idle', bible_error=NULL, bible_feedback=NULL WHERE id=?",
        (project_id,),
    )
    conn.commit()
    was_running = p["bible_status"] == "running"
    return {"stopped": stopped or was_running}

@router.post("/projects/{project_id}/bible/cancel")
async def cancel_bible(project_id: str):
    from app.capabilities.dispatch import dispatch, respond_ui

    result = await dispatch("bible.cancel", {"project_id": project_id}, initiator="ui")
    return respond_ui(result)
