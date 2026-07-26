"""video.* / reference.review Command Handlers（评审墙、镜头版本与参考图画廊）。"""
from __future__ import annotations

from app.capabilities import inputs as I
from app.capabilities.handlers.common import call_guarded, failed, succeeded
from app.capabilities.schemas import CommandResult


async def generate_episode(args: I.EpisodeScopedInput) -> CommandResult:
    from app import api

    outcome = await call_guarded(api.generate_episode, args.episode_id, {})
    if isinstance(outcome, CommandResult):
        return outcome
    enqueued = outcome.get("enqueued") or []
    return succeeded(
        f"已提交 {len(enqueued)} 个镜头的视频生成任务",
        data=outcome,
        resource_uris=[f"manju://episodes/{args.episode_id}/storyboard"],
    )


async def complete_episode(args: I.VideoCompleteEpisodeInput) -> CommandResult:
    from app import api

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
    }
    outcome = await call_guarded(api._complete_episode_core, args.episode_id, body)
    if isinstance(outcome, CommandResult):
        return outcome
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

    body = {
        "episode_ids": args.episode_ids,
        "global_budget_cap_cny": args.global_budget_cap_cny,
        "per_episode_cap_cny": args.per_episode_cap_cny,
        "wall_clock_cap_s": args.wall_clock_cap_s,
        "allow_fallback_adopt": args.allow_fallback_adopt,
        "allow_storyboard_edit": args.allow_storyboard_edit,
    }
    outcome = await call_guarded(api._complete_project_videos_core, args.project_id, body)
    if isinstance(outcome, CommandResult):
        return outcome
    started = outcome.get("started") or []
    return succeeded(
        f"已启动跨集补齐编排（立即启动 {len(started)} 集）",
        data=outcome,
        resource_uris=[f"manju://projects/{args.project_id}"],
    )


async def generate_shot(args: I.VideoGenerateShotInput) -> CommandResult:
    from app import api

    body = {
        "prompt_override": args.prompt_override,
        "reroll": args.reroll,
        "with_critique": bool(args.critique),
    }
    outcome = await call_guarded(api._generate_shot_core, args.shot_id, body)
    if isinstance(outcome, CommandResult):
        return outcome
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


async def adopt_version(args: I.VideoAdoptVersionInput) -> CommandResult:
    from app import api

    body = {"version_id": args.version_id, "reason": args.reason, "decided_by": "agent"}
    outcome = await call_guarded(api._adopt_version_core, args.shot_id, body)
    if isinstance(outcome, CommandResult):
        return outcome
    return succeeded(f"已采用版本 {args.version_id}", data=outcome, resource_uris=[f"manju://shots/{args.shot_id}"])


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
    return succeeded(f"已恢复 {outcome.get('resumed_jobs', 0)} 个暂停中的视频任务", data=outcome)


async def repair_stale_assets(args: I.VideoRepairStaleAssetsInput) -> CommandResult:
    from app import api

    outcome = await call_guarded(
        api.repair_stale_assets,
        args.episode_id,
        {"shot_ids": args.shot_ids, "confirm": args.confirm},
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
