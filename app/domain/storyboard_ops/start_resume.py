"""分镜生成的发起、续跑、整项目批量发起与取消。

从 app/domain/storyboard_ops.py 按原样搬移；依赖 mutation_primitives/resume_state/task_run。
"""
from __future__ import annotations

from app import (
    errors,
    task_registry,
)
from app.db import (
    get_conn,
    new_id,
    now,
)
from app.domain.common import (
    _as_body_dict,
    _episode_or_404,
    _project_or_404,
    _require_harness_engine,
    _screenplay_ready,
    router,
)
from fastapi import (
    Body,
    HTTPException,
)

from .mutation_primitives import _screenplay_rebuild_block
from .resume_state import (
    _storyboard_checkpoint_matches_screenplay,
    _storyboard_has_material,
    _storyboard_has_persisted_work,
    _storyboard_resume_decision,
)
from .task_run import (
    _new_storyboard_recorder,
    _storyboard_generation_is_live,
    _storyboard_guarded_recorded,
)


@router.post("/episodes/{episode_id}/storyboard")
async def start_storyboard(episode_id: str, body: dict | None = Body(None)):
    from app.capabilities.dispatch import ui_route
    body_was_explicit = isinstance(body, dict)
    body = _as_body_dict(body)
    ep = _episode_or_404(episode_id)
    resume_existing = _storyboard_has_persisted_work(episode_id, dict(ep))
    payload = {"episode_id": episode_id, **body}
    routed = await ui_route("storyboard.generate", payload)
    if routed is not None:
        return routed
    if resume_existing:
        return await resume_storyboard(episode_id, body if body_was_explicit else None)
    if body_was_explicit:
        from app.storyboard_workspace import require_preview
        require_preview(body.get("preflight_token"), "start:create", episode_id, consume=True)
    _require_harness_engine(ep["project_id"])
    if ep["screenplay_publish_fence"]:
        raise HTTPException(409, "剧本正在安全发布，暂不能启动新分镜任务")
    if _storyboard_generation_is_live(ep):
        return {
            "status": "scripting",
            "run_id": ep["active_storyboard_run_id"],
            "deduplicated": True,
        }
    if not _screenplay_ready(ep):
        rebuild_block = _screenplay_rebuild_block(get_conn(), ep)
        if rebuild_block is not None:
            raise HTTPException(409, rebuild_block)
        raise HTTPException(409, "请先在映射台生成本集可拍剧本")
    conn = get_conn()
    previous = {
        "status": ep["status"],
        "script_error": ep["script_error"],
        "active_storyboard_run_id": ep["active_storyboard_run_id"],
    }
    start_claim = f"starting:{int(now())}:{new_id('storyboard')}"
    cursor = conn.execute(
        """UPDATE episodes
              SET status='scripting', script_error=NULL, active_storyboard_run_id=?
            WHERE id=? AND screenplay_publish_fence=0
              AND active_storyboard_run_id IS ?""",
        (start_claim, episode_id, previous["active_storyboard_run_id"]),
    )
    if cursor.rowcount != 1:
        conn.rollback()
        raise HTTPException(409, "分镜状态已被其他请求抢占，请刷新后查看当前任务")
    conn.commit()
    recorder = None
    coro = None
    try:
        recorder = _new_storyboard_recorder(episode_id)
        owned = conn.execute(
            "UPDATE episodes SET active_storyboard_run_id=? "
            "WHERE id=? AND active_storyboard_run_id=?",
            (recorder.run_id, episode_id, start_claim),
        )
        if owned.rowcount != 1:
            conn.rollback()
            raise RuntimeError("分镜启动所有权已变化")
        conn.commit()
        coro = _storyboard_guarded_recorded(
            episode_id,
            recorder,
            resume=False,
            new_activation=False,
            priority=0,
        )
        task_registry.spawn(
            "storyboard", episode_id, coro, project_id=ep["project_id"],
        )
    except Exception as exc:
        if coro is not None:
            coro.close()
        current_owner = recorder.run_id if recorder is not None else start_claim
        conn.execute(
            """UPDATE episodes
                  SET status=?, script_error=?, active_storyboard_run_id=?
                WHERE id=? AND active_storyboard_run_id=?""",
            (
                previous["status"],
                previous["script_error"],
                previous["active_storyboard_run_id"],
                episode_id,
                current_owner,
            ),
        )
        conn.commit()
        if recorder is not None:
            try:
                recorder.cancel("分镜任务未能启动，剧集状态已回滚", conn=None)
            except Exception:  # noqa: BLE001
                pass
        raise HTTPException(503, {
            "code": "STORYBOARD_START_SPAWN_FAILED",
            "message": "分镜任务未能启动，剧本和原状态已保留，请重试",
            "recovery_action": "重新点击生成分镜；尚未开始逐镜生成",
            "episode_id": episode_id,
            "rolled_back": True,
            "recoverable": True,
        }) from exc
    return {
        "status": "scripting",
        "run_id": recorder.run_id,
        "action": "create",
        "resource_uri": f"manju://runs/{recorder.run_id}",
    }

async def resume_storyboard(episode_id: str, body: dict | None = Body(None)):
    """内部从 Supervisor Checkpoint / 已验证前缀恢复；对外统一走 POST /storyboard。"""
    body_was_explicit = isinstance(body, dict)
    body = _as_body_dict(body)
    preview_payload: dict = {}
    if body_was_explicit:
        from app.storyboard_workspace import require_preview
        preview_payload = require_preview(
            body.get("preflight_token"),
            "start:resume",
            episode_id,
            consume=True,
        )
    ep = _episode_or_404(episode_id)
    _require_harness_engine(ep["project_id"])
    if ep["screenplay_publish_fence"]:
        raise HTTPException(409, "剧本正在安全发布，暂不能继续分镜")
    if _storyboard_generation_is_live(ep):
        return {
            "status": "scripting",
            "run_id": ep["active_storyboard_run_id"],
            "deduplicated": True,
        }
    if not _screenplay_ready(ep):
        rebuild_block = _screenplay_rebuild_block(get_conn(), ep)
        if rebuild_block is not None:
            raise HTTPException(409, rebuild_block)
        raise HTTPException(409, "请先在映射台生成本集可拍剧本")
    conn = get_conn()
    saved = conn.execute(
        "SELECT COUNT(*) AS c FROM shots WHERE episode_id=?", (episode_id,)
    ).fetchone()["c"]
    from app.storyboard_supervisor import load_latest_checkpoint
    cp = load_latest_checkpoint(episode_id)
    if (
        cp is not None
        and not _storyboard_checkpoint_matches_screenplay(cp, dict(ep))
        and not _storyboard_has_material(episode_id, dict(ep))
    ):
        # The old checkpoint is historical evidence for a screenplay version
        # whose downstream projection has already been cleared.  It cannot be
        # resumed as shot N+1 of the new screenplay.
        cp = None
    if not saved and cp is None:
        raise HTTPException(409, "当前没有可恢复的 Supervisor / 逐镜 checkpoint，请重新生成分镜")
    resume_decision = _storyboard_resume_decision(episode_id, dict(ep))
    if not resume_decision["allowed"]:
        raise HTTPException(409, resume_decision["blocking_reason"])
    prepared_published_repair = False
    if preview_payload.get("resume_mode") == "repair_existing":
        from app.storyboard_supervisor import prepare_published_storyboard_repair

        prepare_published_storyboard_repair(
            episode_id,
            [
                str(message)
                for message in preview_payload.get("current_gate_issues") or []
                if str(message).strip()
            ],
        )
        prepared_published_repair = True
    parent = conn.execute(
        """SELECT id FROM workflow_runs
           WHERE workflow_type='storyboard' AND scope_type='episode' AND scope_id=?
           ORDER BY updated_at DESC LIMIT 1""",
        (episode_id,),
    ).fetchone()
    previous = {
        "status": ep["status"],
        "script_error": ep["script_error"],
        "active_storyboard_run_id": ep["active_storyboard_run_id"],
    }
    start_claim = f"starting:{int(now())}:{new_id('storyboard')}"
    cursor = conn.execute(
        """UPDATE episodes
              SET status='scripting', script_error=NULL, active_storyboard_run_id=?
            WHERE id=? AND screenplay_publish_fence=0
              AND active_storyboard_run_id IS ?""",
        (start_claim, episode_id, previous["active_storyboard_run_id"]),
    )
    if cursor.rowcount != 1:
        conn.rollback()
        raise HTTPException(409, "分镜状态已被其他请求抢占，请刷新后查看当前任务")
    conn.commit()
    recorder = None
    coro = None
    try:
        recorder = _new_storyboard_recorder(
            episode_id,
            resume=True,
            trigger_type="resume",
            parent_run_id=parent["id"] if parent else None,
        )
        # 任务注册前持久化指针，避免 Run 已启动但页面无法轮询或控制。
        owned = conn.execute(
            "UPDATE episodes SET active_storyboard_run_id=? "
            "WHERE id=? AND active_storyboard_run_id=?",
            (recorder.run_id, episode_id, start_claim),
        )
        if owned.rowcount != 1:
            conn.rollback()
            raise RuntimeError("分镜续跑所有权已变化")
        conn.commit()
        coro = _storyboard_guarded_recorded(
            episode_id,
            recorder,
            resume=True,
            new_activation=not prepared_published_repair,
            priority=0,
        )
        task_registry.spawn(
            "storyboard", episode_id, coro, project_id=ep["project_id"],
        )
    except Exception as exc:
        if coro is not None:
            coro.close()
        conn.execute(
            """UPDATE episodes
               SET active_storyboard_run_id=?, status=?, script_error=?
               WHERE id=? AND active_storyboard_run_id IS ?""",
            (
                previous["active_storyboard_run_id"],
                previous["status"],
                previous["script_error"],
                episode_id,
                recorder.run_id if recorder is not None else start_claim,
            ),
        )
        conn.commit()
        if recorder is not None:
            try:
                recorder.cancel("分镜继续任务未能启动，状态已回滚", conn=None)
            except Exception:  # noqa: BLE001
                pass
        raise HTTPException(503, {
            "code": "STORYBOARD_RESUME_SPAWN_FAILED",
            "message": "分镜继续任务未能启动，已回滚到可重试状态",
            "recovery_action": "请稍后重试；已通过镜头和 checkpoint 均已保留",
            "episode_id": episode_id,
            "run_id": recorder.run_id if recorder is not None else None,
            "rolled_back": True,
            "recoverable": True,
        }) from exc
    checkpoint_saved = int(cp.validated_prefix_end or 0) if cp else 0
    resumed_from_shot = checkpoint_saved if cp else int(saved)
    checkpoint_next = int(cp.next_shot_no or 0) if cp else 0
    return {
        "status": "scripting",
        "run_id": recorder.run_id,
        "action": "resume",
        "resumed_from_shot": resumed_from_shot,
        "next_shot_no": checkpoint_next or resumed_from_shot + 1,
        "checkpoint_only": bool(not saved and cp is not None),
    }

@router.post("/projects/{project_id}/storyboard-all")
async def start_storyboard_all(project_id: str):
    """为本项目所有【待分镜】(planned) 剧集批量生成分镜，限并发逐集触发。
    必须是 async def：sync 路由跑在无事件循环的线程池里，asyncio.create_task 会抛
    'no running event loop'，导致状态已置为 scripting 但任务从未启动（前端显示分镜中、模型却收不到请求）。
    同时回收状态卡在 scripting 但无在跑任务的孤儿集，便于一键修复。"""
    from app.generation_concurrency import PRIORITY_BATCH
    from app.capabilities.dispatch import ui_route
    routed = await ui_route("storyboard.generate_batch", {"project_id": project_id})
    if routed is not None:
        return routed
    _project_or_404(project_id)
    _require_harness_engine(project_id)
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, status, script_error, screenplay_status, screenplay_json, screenplay_publish_fence, "
        "active_storyboard_run_id "
        "FROM episodes WHERE project_id=? AND status IN ('planned','scripting','script_failed') ORDER BY episode_no",
        (project_id,)).fetchall()
    # 待分镜的；以及卡在“分镜中”却没有在跑任务的孤儿（需重新触发）
    candidates = [
        r for r in rows
        if r["screenplay_status"] == "ready" and r["screenplay_json"]
        and not r["screenplay_publish_fence"]
        and not _storyboard_generation_is_live(dict(r))
    ]
    if not candidates:
        raise HTTPException(409, "没有可展开分镜的剧集（需先生成剧本，且状态为待分镜/分镜失败/卡住的分镜中）")
    run_ids: list[str] = []
    failed_to_start: list[dict] = []
    for candidate in candidates:
        eid = candidate["id"]
        recorder = None
        try:
            recorder = _new_storyboard_recorder(eid, trigger_type="batch")
        except Exception as exc:
            public = errors.record_and_format(
                exc, action="storyboard_batch_recorder",
                context={"project_id": project_id, "episode_id": eid},
            )
            failed_to_start.append({"episode_id": eid, "error": public, "retryable": True})
            continue
        installed = conn.execute(
            """UPDATE episodes
               SET status='scripting', script_error=NULL, active_storyboard_run_id=?
               WHERE id=? AND status=? AND active_storyboard_run_id IS ?
                 AND screenplay_publish_fence=0
                 AND NOT EXISTS (
                     SELECT 1 FROM workflow_runs AS wr
                     WHERE wr.id=episodes.active_storyboard_run_id
                       AND wr.status IN ('CREATED','RUNNING')
                 )""",
            (
                recorder.run_id,
                eid,
                candidate["status"],
                candidate["active_storyboard_run_id"],
            ),
        )
        if installed.rowcount != 1:
            conn.rollback()
            recorder.cancel("批量分镜启动权已变化，当前运行未启动", conn=None)
            failed_to_start.append({
                "episode_id": eid,
                "error": "剧集状态刚刚发生变化，本次未接管",
                "retryable": True,
            })
            continue
        conn.commit()
        coro = _storyboard_guarded_recorded(
            eid,
            recorder,
            resume=True,
            new_activation=True,
            priority=PRIORITY_BATCH,
        )
        try:
            task_registry.spawn(
                "storyboard", eid, coro, project_id=project_id,
            )
        except Exception as exc:
            coro.close()
            rollback_status = (
                "script_failed" if candidate["status"] == "scripting" else candidate["status"]
            )
            rollback_error = (
                "检测到上次分镜任务已中断；本次批量任务也未能启动，可继续重试"
                if candidate["status"] == "scripting"
                else candidate["script_error"]
            )
            conn.execute(
                """UPDATE episodes
                   SET active_storyboard_run_id=NULL, status=?, script_error=?
                   WHERE id=? AND active_storyboard_run_id=?""",
                (rollback_status, rollback_error, eid, recorder.run_id),
            )
            conn.commit()
            recorder.cancel("批量分镜任务未能启动，状态已回滚", conn=None)
            public = errors.record_and_format(
                exc, action="storyboard_batch_spawn",
                context={"project_id": project_id, "episode_id": eid},
            )
            failed_to_start.append({"episode_id": eid, "error": public, "retryable": True})
            continue
        run_ids.append(recorder.run_id)
    if not run_ids:
        raise HTTPException(503, {
            "code": "STORYBOARD_BATCH_START_FAILED",
            "message": "批量分镜任务均未能启动，各集剧本和恢复点已保留，可直接重试",
            "failed_to_start": failed_to_start,
        })
    return {
        "started": len(run_ids),
        "run_ids": run_ids,
        "failed_to_start": failed_to_start,
        "retryable_failures": len(failed_to_start),
    }

@router.post("/episodes/{episode_id}/storyboard/cancel")
async def cancel_storyboard(episode_id: str, body: dict | None = Body(None)):
    """立即暂停分镜任务，保留工作镜头和安全检查点以便继续或清空。"""
    from app.capabilities.dispatch import ui_route
    routed = await ui_route("storyboard.cancel", {"episode_id": episode_id})
    if routed is not None:
        return routed
    ep = _episode_or_404(episode_id)
    if ep["status"] != "scripting":
        return {
            "status": ep["status"],
            "deduplicated": True,
            "message": "任务已自然结束或此前已停止；当前状态保持不变",
        }
    await task_registry.cancel_and_wait("storyboard", episode_id)
    await task_registry.cancel_and_wait("storyboard_assets", episode_id)
    from app.storyboard_workspace import finalize_storyboard_cancellation
    return finalize_storyboard_cancellation(
        episode_id,
        run_id=ep["active_storyboard_run_id"],
        message="已从分镜台暂停生成",
        paused=True,
    )
