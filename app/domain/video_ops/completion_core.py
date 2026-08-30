"""整集视频成片合成任务体。

从 app/domain/video_ops.py 按原样搬移；依赖 confirmation_gate。单个函数 _complete_episode_core 440 行，是成片终态
判决与资源收口的唯一权威顺序，不做拆分（移动未拆分）。
"""
from __future__ import annotations

import json

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
    _episode_or_404,
    router,
)
from app.domain.review_wall import (
    _review_assert_positive_action,
    _review_validate_authorization_number,
)
from fastapi import HTTPException

from .confirmation_gate import _assert_storyboard_generation_gate


def _ensure_video_episode_columns(conn=None) -> None:
    db = conn or get_conn()
    for stmt in (
        "ALTER TABLE episodes ADD COLUMN active_video_run_id TEXT",
        "ALTER TABLE episodes ADD COLUMN video_completion_mode TEXT NOT NULL DEFAULT 'quick'",
        "ALTER TABLE episodes ADD COLUMN video_control_json TEXT",
    ):
        try:
            db.execute(stmt)
            db.commit()
        except Exception:  # noqa: BLE001
            pass

async def _recorded_video_completion_task(
    episode_id: str,
    recorder,
    *,
    resume: bool,
    grant_id: str | None,
    budget_cap_cny: float | None = None,
    wall_clock_cap_s: float | None = None,
    allow_fallback_adopt: bool = True,
    max_fallback_shots: int | None = None,
    allow_storyboard_edit: bool = False,
):
    import asyncio
    from app.observability.tracing import bind_trace
    from app.video_supervisor import run_video_completion_resilient
    recorder.start()
    try:
        with bind_trace(recorder.run_id, None):
            result = await run_video_completion_resilient(
                episode_id,
                resume=resume,
                grant_id=grant_id,
                run_id=recorder.run_id,
                budget_cap_cny=budget_cap_cny,
                wall_clock_cap_s=wall_clock_cap_s,
                allow_fallback_adopt=allow_fallback_adopt,
                max_fallback_shots=max_fallback_shots,
                allow_storyboard_edit=allow_storyboard_edit,
            )
        if result.phase in {"SUCCEEDED_COVERED", "COMPLETED_DEADLINE_FALLBACK"}:
            recorder.succeed(result.outcome or "SUCCEEDED_COVERED", conn=None)
        elif result.phase == "CANCELLED":
            recorder.cancel(conn=None)
        else:
            coverage = result.coverage or {}
            completed_shots = int(coverage.get("adopted") or 0)
            total_shots = int(coverage.get("total") or 0)
            if result.finished_at is not None and total_shots > 0 and completed_shots == 0:
                recorder.fail_result(
                    result.outcome or result.phase,
                    failure_code="NO_COMPLETED_OUTPUT", conn=None,
                )
            else:
                recorder.partial(result.outcome or result.phase, conn=None)
        if result.phase in {
            "SUCCEEDED_COVERED", "COMPLETED_DEADLINE_FALLBACK",
            "PARTIAL_NO_USABLE_CANDIDATE", "FAILED_CLOSED", "CANCELLED",
        }:
            from app.media_exec.enqueue import reconcile_episode_generation_status
            reconcile_episode_generation_status(episode_id)
        return result
    except asyncio.CancelledError:
        # 必须在调用 recorder 之前回滚：run_video_completion_resilient 内部
        # 与本函数共用同一个 task 缓存连接（app.db.get_conn() 按 asyncio.
        # current_task() 缓存），Supervisor 主循环里的候选采用写入（例如
        # app.evidence.media.select_best_video_candidate 先 UPDATE
        # shots.adopted_version_id 与 shot_versions.adoption_reason，再调用
        # invalidate_episode_delivery_authority 写 delivery_packages，最后才
        # 一次性 conn.commit()）在这几条语句之间没有中间提交点。取消信号可能
        # 恰好落在这段还没提交的窗口里；此时 recorder.pause_external()/
        # recorder.cancel() 会经 refresh_cost()→get_conn().commit() 把这份半
        # 途的采用写入一并提交下去。回滚只丢弃这次未提交的挂起写入，不影响
        # Supervisor 自己在检查点写入时已经落盘的状态。
        conn = get_conn()
        if conn.in_transaction:
            conn.rollback()
        if task_registry.shutdown_in_progress():
            recorder.pause_external("服务重启，全片视频补齐等待自动恢复", conn=None)
        else:
            recorder.cancel(conn=None)
        raise
    except Exception as exc:
        # 同上，必须先回滚再记录。注意：多数常规异常已经在
        # run_video_completion_resilient 内部的控制面恢复循环里被捕获并转换
        # 成检查点保存，这里能看到的往往是恢复循环自身也失败之后的异常；即
        # 便如此，回滚仍是免费的安全网，不能省略。
        conn = get_conn()
        if conn.in_transaction:
            conn.rollback()
        recorder.fail(exc, conn=None)
        raise

@router.post("/episodes/{episode_id}/video-completion")
async def complete_episode(episode_id: str, body: dict | None = None):
    """启动集级视频补齐 Supervisor（补齐到全片可用）。"""
    from app.capabilities.dispatch import ui_route
    payload = {"episode_id": episode_id, **(body or {})}
    routed = await ui_route("video.complete_episode", payload)
    if routed is not None:
        return routed
    return await _complete_episode_core(episode_id, body or {})

async def _complete_episode_core(
    episode_id: str,
    body: dict,
    *,
    parent_run_id: str | None = None,
    trigger_type: str = "manual",
) -> dict:
    from app.completion_grant import (
        DEFAULT_VIDEO_BUDGET_CAP_CNY,
        DEFAULT_VIDEO_WALL_CLOCK_CAP_S,
        GrantValidationError,
        default_max_fallback_shots,
        issue_video_completion_grant,
        bump_video_grant_budget,
        revoke_grant,
        validate_video_grant,
    )
    from app.orchestration.engine import WorkflowRecorder, fingerprint
    from app.video_supervisor import (
        FIRST_PASS_BUDGET_FRACTION,
        MAX_ATTEMPTS_PER_SHOT,
        MAX_CHAIN_CASCADE_DEPTH,
        MAX_REPAIR_EPOCHS,
        MIN_ATTEMPTS_PER_SHOT,
    )

    ep = _episode_or_404(episode_id)
    _review_assert_positive_action(episode_id, body.get("qualification_version"))
    # 见 _generate_episode_core 同一处注释：这里删掉的独立
    # `ep["status"] not in (...)` 复查与上面这次调用判的是同一件事，但挂的
    # 是分镜台 2.0.0 生成完成后从不会推进到的 status 白名单，会把刚判定
    # 合格的分集重新拦一次。
    _assert_storyboard_generation_gate(episode_id)
    _ensure_video_episode_columns()
    mode = body.get("mode") or "fresh"
    if mode not in {"fresh", "resume"}:
        raise HTTPException(422, "mode 只能是 fresh 或 resume")

    if task_registry.active("video_completion", episode_id):
        raise HTTPException(409, "全片补齐 Supervisor 已在运行")

    conn = get_conn()
    shots_total = conn.execute(
        "SELECT COUNT(*) AS c FROM shots WHERE episode_id=?", (episode_id,)
    ).fetchone()["c"]
    if int(shots_total or 0) <= 0:
        raise HTTPException(409, "本集尚无分镜")

    budget_cap = _review_validate_authorization_number(
        body.get("budget_cap_cny"), field="budget_cap_cny", minimum=1, maximum=100000,
    )
    wall_cap = _review_validate_authorization_number(
        body.get("wall_clock_cap_s"), field="wall_clock_cap_s", minimum=60, maximum=604800,
    )
    allow_fallback = body.get("allow_fallback_adopt", True)
    max_fallback = body.get("max_fallback_shots")
    allow_edit = bool(body.get("allow_storyboard_edit", False))
    grant_id = body.get("completion_grant_id")
    operation_fingerprint = str(body.get("operation_request_fingerprint") or "")
    operation_claim_token = str(body.get("operation_claim_token") or "")
    operation_command = str(body.get("operation_command") or "")
    operation_key = (
        f"{operation_command}:{str(body.get('idempotency_key') or '').strip()}"
        if operation_command and body.get("idempotency_key")
        else ""
    )
    grant_idempotency_key = (
        str(body.get("idempotency_key") or "").strip() or None
    )
    if mode == "resume" and not grant_id:
        raise HTTPException(422, {
            "code": "VIDEO_COMPLETION_GRANT_REQUIRED",
            "message": "继续补齐必须携带原补齐授权；如需重新开始，请选择 fresh 模式",
            "action": "start_fresh",
        })

    # resume + 追加预算
    add_budget = _review_validate_authorization_number(
        body.get("add_budget_cny"), field="add_budget_cny", minimum=1, maximum=100000,
    )
    add_wall = _review_validate_authorization_number(
        body.get("add_wall_clock_s"), field="add_wall_clock_s", minimum=60, maximum=604800,
    )
    if (add_budget is not None or add_wall is not None) and not (mode == "resume" and grant_id):
        raise HTTPException(422, "追加授权只能用于带 completion_grant_id 的 resume 模式")
    existing = None
    if mode == "resume" and grant_id:
        try:
            existing = validate_video_grant(
                grant_id,
                episode_id=episode_id,
                storyboard_artifact_id=ep["storyboard_artifact_id"],
            )
            if add_budget is not None or add_wall is not None:
                existing = bump_video_grant_budget(
                    grant_id,
                    add_cny=float(add_budget or 0),
                    add_wall_s=float(add_wall or 0),
                    idempotency_key=grant_idempotency_key,
                )
        except GrantValidationError as exc:
            raise HTTPException(409, {
                "code": exc.code,
                "message": str(exc),
                "action": "renew_authorization",
                "completion_grant_id": grant_id,
            }) from exc

    issued_new_grant = False
    if mode == "fresh":
        grant, _token = issue_video_completion_grant(
            episode_id=episode_id,
            project_id=ep["project_id"],
            storyboard_artifact_id=ep["storyboard_artifact_id"] or "",
            budget_cap_cny=(
                float(budget_cap)
                if budget_cap is not None
                else None
            ),
            wall_clock_cap_s=float(wall_cap) if wall_cap is not None else DEFAULT_VIDEO_WALL_CLOCK_CAP_S,
            allow_fallback_adopt=bool(allow_fallback),
            max_fallback_shots=(
                int(max_fallback) if max_fallback is not None
                else default_max_fallback_shots(int(shots_total))
            ),
            allow_storyboard_edit=allow_edit,
            shots_total=int(shots_total),
            impact_snapshot={
                "mode": "complete_episode_video",
                "auto_concatenate": False,
                "auto_delivery": False,
            },
            idempotency_key=grant_idempotency_key,
        )
        issued_new_grant = bool(_token)
        grant_id = grant.grant_id
        budget_cap = grant.budget_cap_cny
        wall_cap = grant.wall_clock_cap_s
        max_fallback = grant.max_fallback_shots
    else:
        if existing:
            budget_cap = existing.budget_cap_cny
            wall_cap = existing.wall_clock_cap_s
            max_fallback = existing.max_fallback_shots
            allow_fallback = existing.allow_fallback_adopt
            allow_edit = existing.allow_storyboard_edit

    cap = float(budget_cap if budget_cap is not None else DEFAULT_VIDEO_BUDGET_CAP_CNY)
    resolved_wall_cap = float(
        wall_cap if wall_cap is not None else DEFAULT_VIDEO_WALL_CLOCK_CAP_S
    )
    workflow_input_fingerprint = fingerprint(
        ep["storyboard_artifact_id"], grant_id, mode,
    )
    workflow_policy_snapshot = {
        "supervisor": "video_completion",
        "budget_cap_cny": cap,
        "wall_clock_cap_s": resolved_wall_cap,
        "first_pass_budget_fraction": FIRST_PASS_BUDGET_FRACTION,
        "min_attempts_per_shot": MIN_ATTEMPTS_PER_SHOT,
        "max_attempts_per_shot": MAX_ATTEMPTS_PER_SHOT,
        "max_repair_epochs": MAX_REPAIR_EPOCHS,
        "max_chain_cascade_depth": MAX_CHAIN_CASCADE_DEPTH,
        "allow_fallback_adopt": bool(allow_fallback),
        "max_fallback_shots": int(max_fallback or 0),
        "allow_storyboard_edit": allow_edit,
        "operation_key": operation_key,
        "operation_request_fingerprint": operation_fingerprint,
    }

    active_run_states = {
        "CREATED", "RUNNING", "WAITING_RETRY", "WAITING_HUMAN",
        "WAITING_AUTHORIZATION", "PAUSED_BUDGET", "PAUSED_EXTERNAL",
    }
    start_claim = f"starting:{int(now())}:{new_id('video_completion')}"
    reusable_run_id: str | None = None
    conn.execute("BEGIN IMMEDIATE")
    try:
        owner_row = conn.execute(
            "SELECT active_video_run_id FROM episodes WHERE id=?",
            (episode_id,),
        ).fetchone()
        previous_active_run_id = owner_row["active_video_run_id"] if owner_row else None
        active_status = None
        previous_claim_live = False
        if str(previous_active_run_id or "").startswith("starting:"):
            try:
                claim_started_at = float(str(previous_active_run_id).split(":", 2)[1])
                previous_claim_live = now() - claim_started_at <= 60
            except (TypeError, ValueError, IndexError):
                previous_claim_live = False
        elif previous_active_run_id:
            active_row = conn.execute(
                """SELECT status,input_fingerprint,policy_snapshot_json
                     FROM workflow_runs WHERE id=?""",
                (previous_active_run_id,),
            ).fetchone()
            active_status = active_row["status"] if active_row else None
            if active_row and active_status == "CREATED" and operation_key:
                try:
                    active_policy = json.loads(active_row["policy_snapshot_json"] or "{}")
                except json.JSONDecodeError:
                    active_policy = {}
                if (
                    active_row["input_fingerprint"] == workflow_input_fingerprint
                    and active_policy.get("operation_key") == operation_key
                    and active_policy.get("operation_request_fingerprint")
                    == operation_fingerprint
                ):
                    reusable_run_id = str(previous_active_run_id)
        if (
            previous_claim_live
            or (active_status in active_run_states and not reusable_run_id)
        ):
            raise HTTPException(409, {
                "code": "VIDEO_COMPLETION_ALREADY_ACTIVE",
                "message": "全片补齐任务已在启动或运行，请勿重复提交",
                "active_run_id": previous_active_run_id,
                "action": "view_progress",
            })
        if reusable_run_id is None:
            claimed = conn.execute(
                """UPDATE episodes SET active_video_run_id=?
                   WHERE id=? AND active_video_run_id IS ?""",
                (start_claim, episode_id, previous_active_run_id),
            )
            if claimed.rowcount != 1:
                raise HTTPException(409, {
                    "code": "VIDEO_COMPLETION_START_CONFLICT",
                    "message": "本集补齐状态刚刚发生变化，请刷新后重试",
                    "action": "refresh",
                })
        conn.commit()
    except Exception:
        conn.rollback()
        if issued_new_grant and grant_id:
            revoke_grant(grant_id)
        raise

    try:
        if reusable_run_id:
            recorder = WorkflowRecorder(reusable_run_id)
        else:
            orphan_rows = conn.execute(
                """SELECT id,policy_snapshot_json FROM workflow_runs
                     WHERE workflow_type='episode_video_completion'
                       AND scope_type='episode' AND scope_id=?
                       AND status='CREATED' AND input_fingerprint=?
                     ORDER BY updated_at DESC""",
                (episode_id, workflow_input_fingerprint),
            ).fetchall()
            exact_orphans = []
            for orphan in orphan_rows:
                try:
                    orphan_policy = json.loads(orphan["policy_snapshot_json"] or "{}")
                except json.JSONDecodeError:
                    continue
                if (
                    operation_key
                    and orphan_policy.get("operation_key") == operation_key
                    and orphan_policy.get("operation_request_fingerprint")
                    == operation_fingerprint
                ):
                    exact_orphans.append(str(orphan["id"]))
            if len(exact_orphans) > 1:
                raise HTTPException(409, "补齐命令存在多个同源未启动运行，需人工审核")
            recorder = (
                WorkflowRecorder(exact_orphans[0])
                if exact_orphans
                else WorkflowRecorder.create(
                    workflow_type="episode_video_completion",
                    scope_type="episode",
                    scope_id=episode_id,
                    input_fingerprint=workflow_input_fingerprint,
                    requested_by="user",
                    trigger_type=trigger_type,
                    budget_limit_cny=cap,
                    deadline_at=now() + resolved_wall_cap,
                    policy_snapshot=workflow_policy_snapshot,
                    parent_run_id=parent_run_id,
                )
            )
    except Exception:
        conn.execute(
            """UPDATE episodes SET active_video_run_id=?
               WHERE id=? AND active_video_run_id=?""",
            (previous_active_run_id, episode_id, start_claim),
        )
        conn.commit()
        if issued_new_grant and grant_id:
            revoke_grant(grant_id)
        raise
    installed = conn.execute(
        """UPDATE episodes
           SET video_completion_mode='complete',
               status='generating',
               active_video_run_id=?
           WHERE id=? AND active_video_run_id=?""",
        (
            recorder.run_id,
            episode_id,
            recorder.run_id if reusable_run_id else start_claim,
        ),
    )
    if installed.rowcount != 1:
        conn.rollback()
        recorder.cancel("补齐启动权已变化，当前运行未启动", conn=None)
        if issued_new_grant and grant_id:
            revoke_grant(grant_id)
        raise HTTPException(409, {
            "code": "VIDEO_COMPLETION_START_CONFLICT",
            "message": "本集补齐状态刚刚发生变化，请刷新后重试",
            "action": "refresh",
        })
    completion_result = {
        "status": "accepted",
        "run_id": recorder.run_id,
        "goal": "complete_episode_video",
        "completion_grant_id": grant_id,
        "resource_uri": f"manju://runs/{recorder.run_id}",
        "poll_url": f"/api/episodes/{episode_id}/video-completion",
        "message": "全片补齐任务已启动，可在生成台查看进度",
    }
    if operation_fingerprint and operation_claim_token and operation_command:
        from app.video_command_operations import bind_video_command_operation

        bind_video_command_operation(
            command=operation_command,
            idempotency_key=str(body.get("idempotency_key") or ""),
            request_fingerprint=operation_fingerprint,
            claim_token=operation_claim_token,
            binding={
                "operation_complete": False,
                "phase": "durable_run_installed",
                "run_id": recorder.run_id,
                "completion_grant_id": grant_id,
                "result": completion_result,
                "spawn": {
                    "episode_id": episode_id,
                    "project_id": ep["project_id"],
                    "resume": mode == "resume",
                    "grant_id": grant_id,
                    "budget_cap_cny": cap,
                    "wall_clock_cap_s": (
                        float(wall_cap) if wall_cap is not None else None
                    ),
                    "allow_fallback_adopt": bool(allow_fallback),
                    "max_fallback_shots": (
                        int(max_fallback) if max_fallback is not None else None
                    ),
                    "allow_storyboard_edit": allow_edit,
                },
            },
            conn=conn,
            merge=True,
        )
    conn.commit()

    completion_coro = _recorded_video_completion_task(
        episode_id, recorder,
        resume=(mode == "resume"),
        grant_id=grant_id,
        budget_cap_cny=cap,
        wall_clock_cap_s=float(wall_cap) if wall_cap is not None else None,
        allow_fallback_adopt=bool(allow_fallback),
        max_fallback_shots=int(max_fallback) if max_fallback is not None else None,
        allow_storyboard_edit=allow_edit,
    )
    try:
        task_registry.spawn(
            "video_completion", episode_id, completion_coro,
            project_id=ep["project_id"],
        )
    except Exception as exc:
        completion_coro.close()
        try:
            recorder.start()
            recorder.fail(exc, conn=None)
        except Exception as record_exc:  # noqa: BLE001
            errors.log_error(
                record_exc,
                action="video_completion_start_record_failed",
                context={"episode_id": episode_id, "run_id": recorder.run_id},
            )
        try:
            previous_mode = ep["video_completion_mode"] or "quick"
        except (KeyError, IndexError):
            previous_mode = "quick"
        conn.execute(
            """UPDATE episodes
               SET video_completion_mode=?,
                   status=?,
                   active_video_run_id=NULL
               WHERE id=? AND active_video_run_id=?""",
            (previous_mode, ep["status"], episode_id, recorder.run_id),
        )
        if operation_fingerprint and operation_claim_token and operation_command:
            from app.video_command_operations import bind_video_command_operation

            bind_video_command_operation(
                command=operation_command,
                idempotency_key=str(body.get("idempotency_key") or ""),
                request_fingerprint=operation_fingerprint,
                claim_token=operation_claim_token,
                binding={
                    "operation_complete": False,
                    "operation_failed": True,
                    "phase": "definitely_not_started",
                    "failure_code": "VIDEO_COMPLETION_START_FAILED",
                    "failure_message": "全片补齐任务未能启动，请使用新的幂等键重试",
                },
                conn=conn,
                merge=True,
            )
        conn.commit()
        raise HTTPException(503, {
            "code": "VIDEO_COMPLETION_START_FAILED",
            "message": "全片补齐任务未能启动，尚未产生生成任务，可安全重试",
            "retryable": True,
            "completion_grant_id": grant_id,
            "run_id": recorder.run_id,
        }) from exc
    if operation_fingerprint and operation_claim_token and operation_command:
        from app.video_command_operations import bind_video_command_operation

        bind_video_command_operation(
            command=operation_command,
            idempotency_key=str(body.get("idempotency_key") or ""),
            request_fingerprint=operation_fingerprint,
            claim_token=operation_claim_token,
            binding={
                "operation_complete": True,
                "phase": "spawn_registered",
                "result": completion_result,
            },
            conn=conn,
            merge=True,
        )
        conn.commit()
    return completion_result
