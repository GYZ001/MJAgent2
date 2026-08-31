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
from app.schemas import character_is_portrait_eligible
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
    _issue_payment_quote,
    _normalize_visual_style_name,
    _payment_confirm_required,
    _supports_bible_style_name,
    _validate_payment_quote,
    _visual_style_prompt_or_default,
)
from .refs_generation import _start_refs_generation


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
        timeout_s = max(int(get_setting("bible_task_timeout_s") or BIBLE_TASK_TIMEOUT_S), 60)
        # 重新谱写时按角色名保留已有定妆照（重生圣经不应丢失一致性锚点）
        old_row = conn.execute("SELECT bible_json FROM projects WHERE id=?", (project_id,)).fetchone()
        old_style = None
        old_bible = None
        if old_row and old_row["bible_json"]:
            old_bible = json.loads(old_row["bible_json"])
        # generate_bible 不再读原文、不再发起任何模型调用（2026-08-31 二次
        # 拍板，见该函数 docstring）：不再查 chapters（大长篇整本查出来只为
        # 传参又原样丢弃，纯浪费）、也不再按 bible_text_provider 路由文本
        # 模型 provider——这一环节已经没有模型调用可路由。
        bible = await asyncio.wait_for(
            generate_bible(
                [], feedback=feedback, previous_bible=old_bible,
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
        # 世界观判定不再改动角色内容（generate_bible 原样带出 previous_bible 的
        # characters/scenes），只有画风真的变化才需要作废旧画风定妆照/视频——
        # 否则图像信号会把新画风拉回旧画风；画风未变时旧定妆照依旧成立，不能
        # 无差别打回重做（下面的定妆触发条件复用同一个判据，不引入新变量以
        # 免把本函数推过单函数行数基线）。
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
        # 只在「首次生成」（characters 恒为空，_start_refs_generation 本来就是
        # 无害空转，见 app.refs.generate_refs 对空选中集的早退）或「画风确实
        # 变化」（旧定妆照已被上面的 _purge_for_style_change 判定失效，需要
        # 按新画风重出）时才触发定妆任务。画风未变时，世界观判定不产生任何
        # 角色内容变化，已有定妆照依旧成立——不该被这次调用无差别打回重做，
        # 那会把「只是重新判定了一次年代/题材」变成一次隐藏的、真实产生图片
        # 费用的批量重出图（回归：previous_bible 带出角色后，characters 不再
        # 恒为空，旧的「无条件触发」假设不再成立）。
        if trigger_full_refs and not residual and (not old_bible or (old_style and bible.world.visual_style_canonical != old_style)):
            try:
                # 显式传全部具备定妆资格的角色名单，不能让 _start_refs_generation
                # 靠「没传 only_characters」自己去猜范围：上面 _purge_for_style_change
                # 已经把 character_portraits 整表清空，此时若不传范围，它内部的
                # 「已建卡角色缺口」扫描（_established_portrait_gap_names）会因为
                # 表刚被清空而查到零个已建卡角色，把整批当空选中悄悄早退——一个
                # 角色都不会重新出图，refs_status 却仍会走向 ready（实战撞到：
                # 我欲封天换画风后 5 个角色只剩 1 个有图）。名单口径与
                # compute_refs_cost_precheck 的整包生成分支同源，不重写第二份。
                _start_refs_generation(project_id, None, only_characters=[c.name for c in bible.characters if character_is_portrait_eligible(c)])
            except Exception as exc:  # noqa: BLE001 bible remains deliverable
                public = errors.record_and_format(
                    exc, action="refs_spawn_after_bible", context={"project_id": project_id},
                )
                conn.execute(
                    "UPDATE projects SET refs_status='failed',refs_error=? WHERE id=?",
                    (f"人物谱已完成，但定妆任务未能启动，可直接重试定妆。{public}", project_id),
                )
                conn.commit()
            # 场景清单批量生成（generate_scene_bible）同一批退场（2026-08-31）：
            # 不再在人物谱谱写成功后自动触发 _start_scene_bible_preparation。
            # 场景改为按需反应式发现——分镜阶段遇到未匹配的 label 时逐个跑
            # app.scenes.assess_new_scene（该路径已存在，对照
            # portraits.ensure_character_card 的新角色路径）。场景库页仍保留
            # 手动「预览并生成场景圣经」的批量入口（app/domain/bible_ops/
            # scene_refs.py 的 /scene-bible/preview 与 /scene-bible），那是用户
            # 显式触发的独立操作，不属于本次退场范围。
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
        # 未确认调用必须签发并落库一份真实报价，而不是把 precheck 内部占位的
        # quote_id（其实是 scope_fingerprint）原样递给调用方——那个值从未写进
        # character_payment_quotes，调用方按响应指引带它来确认必然 QUOTE_STALE
        # 死循环（实测 ERR：POST /bible 不带 confirm → 409 里的 quote_id 是
        # scope_fingerprint 的 64 位 hash，无法通过 _validate_payment_quote）。
        raise _payment_confirm_required(_issue_payment_quote(precheck))
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
        task = task_registry.spawn(
            "bible",
            project_id,
            _recorded_bible_task(
                project_id, feedback, recorder, trigger_full_refs=True,
                style_name=style_name,
            ),
            project_id=project_id,
        )
        # generate_bible 不再发起任何模型调用（2026-08-31 二次拍板），这一段
        # 现在总是秒级完成：不再返回一个「running」占位就撒手让调用方（导入
        # 面板/REST 端点/Agent-MCP）自己轮询，而是等这个任务真正跑完，返回值
        # 直接反映它的终态（ready/warning/failed）——用户不会再看到与实际情况
        # 脱节的「谱写中」。仍然经 task_registry.spawn 而不是直接 await 协程
        # 本身：继续复用同一把双提交互斥锁（active()/register() 撞车即
        # RuntimeError）与取消/关机语义，只是紧接着在这里等它跑完。
        await task
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
    # 任务已经跑完（见上面的 await task），这里重新查一次而不是继续沿用
    # 调用前的占位值：_bible_task 在另一个 asyncio Task 上用它自己的连接
    # 提交（app.db.get_conn 按 asyncio Task 分连接），只有重新 SELECT 才能
    # 看到它刚提交的终态。
    final_row = conn.execute("SELECT bible_status FROM projects WHERE id=?", (project_id,)).fetchone()
    return {
        "status": final_row["bible_status"] if final_row else "unknown",
        "task_id": f"bible:{project_id}",
        "run_id": recorder.run_id,
    }

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
