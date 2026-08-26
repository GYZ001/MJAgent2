"""video.* / reference.review Command Handlers（生成台、镜头版本与参考图画廊）。"""
from __future__ import annotations

from app.capabilities import inputs as I
from app.capabilities.handlers.common import call_guarded, failed, succeeded
from app.capabilities.schemas import CommandResult


async def generate_episode(args: I.VideoGenerateEpisodeInput) -> CommandResult:
    from app import api
    from app.capabilities.preflight import video_generate_episode
    from app.completion_grant import authorize_episode_video_budget_increment

    from app.capabilities.bus import canonical_command_request_fingerprint
    from app.video_command_operations import (
        VideoCommandOperationConflict,
        VideoCommandOperationInProgress,
        claim_video_command_operation,
        finish_video_command_operation,
    )

    command = "video.generate_episode"
    request_fingerprint = canonical_command_request_fingerprint(
        command, args.model_dump(mode="json"),
    )
    try:
        operation_owner, recovered = claim_video_command_operation(
            command=command,
            idempotency_key=str(args.idempotency_key or ""),
            request_fingerprint=request_fingerprint,
            scope_type="episode",
            scope_id=args.episode_id,
        )
    except VideoCommandOperationConflict as exc:
        return failed(str(exc), error_code="idempotency_request_mismatch")
    except VideoCommandOperationInProgress as exc:
        return succeeded(str(exc), data={"idempotency_in_progress": True})
    if recovered is not None:
        return succeeded(
            f"已提交 {len(recovered.get('enqueued') or [])} 个镜头的视频生成任务",
            data=recovered,
            resource_uris=[f"manju://episodes/{args.episode_id}/storyboard"],
        )

    approved_cost = float(video_generate_episode(args).estimated_cost_cny or 0)
    if approved_cost > 0:
        if not args.idempotency_key:
            return failed(
                "付费视频命令必须提供稳定的 idempotency_key",
                error_code="idempotency_key_required",
            )
        authorize_episode_video_budget_increment(
            args.episode_id,
            approved_cost,
            source="capability:video.generate_episode",
            operation_id=f"{command}:{args.idempotency_key}",
            request_fingerprint=request_fingerprint,
        )
    outcome = await call_guarded(
        api._generate_episode_core,
        args.episode_id,
        {
            "authorized_video_cost_cny": approved_cost,
            "idempotency_key": args.idempotency_key,
            "request_id": args.request_id,
            "operation_request_fingerprint": request_fingerprint,
            "operation_claim_token": operation_owner,
            "only_incomplete": args.only_incomplete,
            "qualification_version": args.qualification_version,
        },
    )
    if isinstance(outcome, CommandResult):
        return outcome
    finish_video_command_operation(
        command=command,
        idempotency_key=str(args.idempotency_key or ""),
        request_fingerprint=request_fingerprint,
        claim_token=str(operation_owner or ""),
        result=outcome,
    )
    enqueued = outcome.get("enqueued") or []
    return succeeded(
        f"已提交 {len(enqueued)} 个镜头的视频生成任务",
        data=outcome,
        resource_uris=[f"manju://episodes/{args.episode_id}/storyboard"],
    )


async def complete_episode(args: I.VideoCompleteEpisodeInput) -> CommandResult:
    from app import api
    from app.capabilities.bus import canonical_command_request_fingerprint
    from app.video_command_operations import (
        VideoCommandOperationConflict,
        VideoCommandOperationFailed,
        VideoCommandOperationInProgress,
        claim_video_command_operation,
        fail_video_command_operation,
        finish_video_command_operation,
    )

    command = "video.complete_episode"
    request_fingerprint = canonical_command_request_fingerprint(
        command, args.model_dump(mode="json"),
    )
    try:
        operation_owner, recovered = claim_video_command_operation(
            command=command,
            idempotency_key=str(args.idempotency_key or ""),
            request_fingerprint=request_fingerprint,
            scope_type="episode",
            scope_id=args.episode_id,
        )
    except VideoCommandOperationConflict as exc:
        return failed(str(exc), error_code="idempotency_request_mismatch")
    except VideoCommandOperationFailed as exc:
        return failed(str(exc), error_code=exc.error_code)
    except VideoCommandOperationInProgress as exc:
        return succeeded(str(exc), data={"idempotency_in_progress": True})
    if recovered is not None and recovered.get("_resume_prepared") is not True:
        return succeeded(
            "已恢复原全片视频补齐运行",
            data=recovered,
            resource_uris=[
                f"manju://episodes/{args.episode_id}/storyboard",
                recovered.get("resource_uri") or f"manju://runs/{recovered.get('run_id')}",
            ],
        )

    body = {
        "mode": args.mode,
        "budget_cap_cny": args.budget_cap_cny,
        "wall_clock_cap_s": args.wall_clock_cap_s,
        "allow_fallback_adopt": args.allow_fallback_adopt,
        "max_fallback_shots": args.max_fallback_shots,
        "allow_storyboard_edit": args.allow_storyboard_edit,
        "completion_grant_id": args.completion_grant_id,
        "add_budget_cny": args.add_budget_cny,
        "add_wall_clock_s": args.add_wall_clock_s,
        "qualification_version": args.qualification_version,
        "idempotency_key": args.idempotency_key,
        "request_id": args.request_id,
        "operation_request_fingerprint": request_fingerprint,
        "operation_claim_token": operation_owner,
        "operation_command": command,
    }
    if recovered is not None:
        outcome = await call_guarded(
            api._resume_prepared_complete_episode_operation,
            args.episode_id,
            body,
            recovered,
        )
    else:
        outcome = await call_guarded(api._complete_episode_core, args.episode_id, body)
    if isinstance(outcome, CommandResult):
        fail_video_command_operation(
            command=command,
            idempotency_key=str(args.idempotency_key or ""),
            request_fingerprint=request_fingerprint,
            claim_token=str(operation_owner or ""),
        )
        return outcome
    finish_video_command_operation(
        command=command,
        idempotency_key=str(args.idempotency_key or ""),
        request_fingerprint=request_fingerprint,
        claim_token=str(operation_owner or ""),
        result=outcome,
    )
    return succeeded(
        "已启动全片视频补齐 Supervisor",
        data=outcome,
        resource_uris=[
            f"manju://episodes/{args.episode_id}/storyboard",
            outcome.get("resource_uri") or f"manju://runs/{outcome.get('run_id')}",
        ],
    )


async def complete_project(args: I.VideoCompleteProjectInput) -> CommandResult:
    from app import api
    from app.capabilities.bus import canonical_command_request_fingerprint
    from app.video_command_operations import (
        VideoCommandOperationConflict,
        VideoCommandOperationInProgress,
        claim_video_command_operation,
        fail_video_command_operation,
        finish_video_command_operation,
    )

    command = "video.complete_project"
    request_fingerprint = canonical_command_request_fingerprint(
        command, args.model_dump(mode="json"),
    )
    try:
        operation_owner, recovered = claim_video_command_operation(
            command=command,
            idempotency_key=str(args.idempotency_key or ""),
            request_fingerprint=request_fingerprint,
            scope_type="project",
            scope_id=args.project_id,
        )
    except VideoCommandOperationConflict as exc:
        return failed(str(exc), error_code="idempotency_request_mismatch")
    except VideoCommandOperationInProgress as exc:
        return succeeded(str(exc), data={"idempotency_in_progress": True})
    if recovered is not None:
        return succeeded(
            "已恢复原跨集补齐编排",
            data=recovered,
            resource_uris=[f"manju://projects/{args.project_id}"],
        )

    body = {
        "episode_ids": args.episode_ids,
        "global_budget_cap_cny": args.global_budget_cap_cny,
        "per_episode_cap_cny": args.per_episode_cap_cny,
        "wall_clock_cap_s": args.wall_clock_cap_s,
        "allow_fallback_adopt": args.allow_fallback_adopt,
        "allow_storyboard_edit": args.allow_storyboard_edit,
        "idempotency_key": args.idempotency_key,
        "request_id": args.request_id,
        "operation_request_fingerprint": request_fingerprint,
        "operation_claim_token": operation_owner,
        "operation_command": command,
    }
    outcome = await call_guarded(api._complete_project_videos_core, args.project_id, body)
    if isinstance(outcome, CommandResult):
        fail_video_command_operation(
            command=command,
            idempotency_key=str(args.idempotency_key or ""),
            request_fingerprint=request_fingerprint,
            claim_token=str(operation_owner or ""),
        )
        return outcome
    finish_video_command_operation(
        command=command,
        idempotency_key=str(args.idempotency_key or ""),
        request_fingerprint=request_fingerprint,
        claim_token=str(operation_owner or ""),
        result=outcome,
    )
    started = outcome.get("started") or []
    return succeeded(
        f"已启动跨集补齐编排（立即启动 {len(started)} 集）",
        data=outcome,
        resource_uris=[f"manju://projects/{args.project_id}"],
    )


async def generate_shot(args: I.VideoGenerateShotInput) -> CommandResult:
    from app import api
    from app.capabilities.preflight import video_generate_shot
    from app.completion_grant import authorize_episode_video_budget_increment

    from app.capabilities.bus import canonical_command_request_fingerprint
    from app.video_command_operations import (
        VideoCommandOperationConflict,
        VideoCommandOperationInProgress,
        claim_video_command_operation,
        finish_video_command_operation,
    )

    command = "video.generate_shot"
    request_fingerprint = canonical_command_request_fingerprint(
        command, args.model_dump(mode="json"),
    )
    try:
        operation_owner, recovered = claim_video_command_operation(
            command=command,
            idempotency_key=str(args.idempotency_key or ""),
            request_fingerprint=request_fingerprint,
            scope_type="shot",
            scope_id=args.shot_id,
        )
    except VideoCommandOperationConflict as exc:
        return failed(str(exc), error_code="idempotency_request_mismatch")
    except VideoCommandOperationInProgress as exc:
        return succeeded(str(exc), data={"idempotency_in_progress": True})
    if recovered is not None:
        return succeeded(
            "已复用既有成片" if recovered.get("reused") else "视频生成任务已提交",
            data=recovered,
            resource_uris=[f"manju://shots/{args.shot_id}"],
        )

    approved_cost = float(video_generate_shot(args).estimated_cost_cny or 0)
    if approved_cost > 0:
        if not args.idempotency_key:
            return failed(
                "付费视频命令必须提供稳定的 idempotency_key",
                error_code="idempotency_key_required",
            )
        from app.db import get_conn

        shot = get_conn().execute(
            "SELECT episode_id FROM shots WHERE id=?", (args.shot_id,),
        ).fetchone()
        if shot:
            authorize_episode_video_budget_increment(
                str(shot["episode_id"]),
                approved_cost,
                source="capability:video.generate_shot",
                operation_id=f"{command}:{args.idempotency_key}",
                request_fingerprint=request_fingerprint,
            )

    body = {
        "prompt_override": args.prompt_override,
        "reroll": args.reroll,
        "with_critique": args.with_critique,
        "qualification_version": args.qualification_version,
        "idempotency_key": args.idempotency_key,
        "request_id": args.request_id,
        "authorized_video_cost_cny": approved_cost,
        "operation_request_fingerprint": request_fingerprint,
        "operation_claim_token": operation_owner,
    }
    outcome = await call_guarded(api._generate_shot_core, args.shot_id, body)
    if isinstance(outcome, CommandResult):
        return outcome
    finish_video_command_operation(
        command=command,
        idempotency_key=str(args.idempotency_key or ""),
        request_fingerprint=request_fingerprint,
        claim_token=str(operation_owner or ""),
        result=outcome,
    )
    return succeeded(
        "已复用既有成片" if outcome.get("reused") else "视频生成任务已提交",
        data=outcome,
        resource_uris=[f"manju://shots/{args.shot_id}"],
    )


async def stop_shot(args: I.ShotScopedInput) -> CommandResult:
    from app import api

    outcome = await call_guarded(api.stop_shot_video, args.shot_id)
    if isinstance(outcome, CommandResult):
        return outcome
    return succeeded("已停止本镜排队中/运行中的视频任务", data=outcome)


async def stop_episode(args: I.EpisodeScopedInput) -> CommandResult:
    from app import api

    outcome = await call_guarded(api.stop_episode_video, args.episode_id)
    if isinstance(outcome, CommandResult):
        return outcome
    if int(outcome.get("paused_jobs") or 0) == 0:
        if int(outcome.get("already_paused_jobs") or 0) > 0:
            return succeeded("整集视频任务已处于暂停状态", data={**outcome, "idempotent": True})
        return failed(
            "当前没有可暂停的视频任务",
            error_code="no_active_video_jobs",
            data={**outcome, "recovery_action": "如需生成，请在生成台发起新任务"},
        )
    return succeeded("已暂停整集视频任务，可继续执行", data=outcome)


async def adopt_version(args: I.VideoAdoptVersionInput) -> CommandResult:
    from app import api

    body = {
        "version_id": args.version_id, "reason": args.reason, "decided_by": "agent",
        "playback_rate": args.playback_rate,
        "qualification_version": args.qualification_version,
        "idempotency_key": args.idempotency_key, "request_id": args.request_id,
    }
    outcome = await call_guarded(api._adopt_version_core, args.shot_id, body)
    if isinstance(outcome, CommandResult):
        return outcome
    return succeeded(f"已采用版本 {args.version_id}", data=outcome, resource_uris=[f"manju://shots/{args.shot_id}"])


async def cancel_adoption(args: I.ShotScopedInput) -> CommandResult:
    from app import api

    outcome = await call_guarded(api._cancel_shot_adoption_core, args.shot_id)
    if isinstance(outcome, CommandResult):
        return outcome
    if not outcome.get("previous_adopted_version_id"):
        return succeeded("本镜当前未采纳任何版本", data={**outcome, "idempotent": True})
    return succeeded(
        "已取消本镜采纳；真实模型候选仍保留，成片不会使用图片代替",
        data=outcome,
        resource_uris=[f"manju://shots/{args.shot_id}"],
    )


async def clear_episode(args: I.EpisodeScopedInput) -> CommandResult:
    from app import api

    outcome = await call_guarded(api.clear_episode_artifacts, args.episode_id)
    if isinstance(outcome, CommandResult):
        return outcome
    return succeeded("已清空本集全部镜头的媒体产物", data=outcome)


async def clear_shot(args: I.ShotScopedInput) -> CommandResult:
    from app import api

    outcome = await call_guarded(api.clear_shot_artifacts, args.shot_id)
    if isinstance(outcome, CommandResult):
        return outcome
    return succeeded("已清空本镜的媒体产物", data=outcome)


async def clear_episode_videos(args: I.EpisodeScopedInput) -> CommandResult:
    from app import api

    outcome = await call_guarded(api.clear_episode_videos, args.episode_id)
    if isinstance(outcome, CommandResult):
        return outcome
    return succeeded("已清空本集全部视频，参考图已保留", data=outcome)


async def clear_shot_references(args: I.ShotScopedInput) -> CommandResult:
    from app import api

    outcome = await call_guarded(api.clear_shot_references, args.shot_id)
    if isinstance(outcome, CommandResult):
        return outcome
    return succeeded("已清空本镜参考图，视频已保留", data=outcome)


async def clear_shot_videos(args: I.ShotScopedInput) -> CommandResult:
    from app import api

    outcome = await call_guarded(api.clear_shot_videos, args.shot_id)
    if isinstance(outcome, CommandResult):
        return outcome
    return succeeded("已清空本镜视频，参考图已保留", data=outcome)


async def delete_version(args: I.VersionScopedInput) -> CommandResult:
    from app import api

    outcome = await call_guarded(api.delete_version, args.version_id)
    if isinstance(outcome, CommandResult):
        return outcome
    return succeeded(f"已删除视频版本 {args.version_id}", data=outcome)


async def resume_episode(args: I.EpisodeScopedInput) -> CommandResult:
    from app import api

    outcome = await call_guarded(api.resume_episode, args.episode_id)
    if isinstance(outcome, CommandResult):
        return outcome
    enqueued = outcome.get("enqueued") or []
    created = sum(1 for item in enqueued if item.get("job_id"))
    return succeeded(
        f"已恢复 {outcome.get('resumed_jobs', 0)} 个暂停任务并补建 {created} 个未完成任务",
        data=outcome,
    )


async def repair_stale_assets(args: I.VideoRepairStaleAssetsInput) -> CommandResult:
    from app import api

    outcome = await call_guarded(
        api.repair_stale_assets,
        args.episode_id,
        {
            "shot_ids": args.shot_ids, "confirm": args.confirm,
            "preview_version": args.preview_version,
            "qualification_version": args.qualification_version,
            "idempotency_key": args.idempotency_key,
        },
    )
    if isinstance(outcome, CommandResult):
        return outcome
    return succeeded(f"已提交 {outcome.get('queued', 0)} 个陈旧镜头重生任务", data=outcome)


async def reference_review(args: I.ReferenceReviewInput) -> CommandResult:
    from app import api

    if args.action == "discard":
        outcome = await call_guarded(api.discard_reference_image, args.version_id, args.ref_id)
        summary = "参考图已废弃"
    elif args.action == "restore":
        outcome = await call_guarded(
            api.restore_reference_image,
            args.version_id,
            args.ref_id,
            {"override_reason": args.override_reason},
        )
        summary = "参考图已恢复使用"
    else:
        return failed(f"未知 action：{args.action}", error_code="invalid_input")
    if isinstance(outcome, CommandResult):
        return outcome
    return succeeded(summary, data=outcome)
