"""storyboard.* / shot.update Command Handlers（分镜与逐镜编辑）。"""
from __future__ import annotations

from app.capabilities import inputs as I
from app.capabilities.handlers.common import call_guarded, failed, succeeded
from app.capabilities.schemas import CommandResult


async def generate(args: I.StoryboardGenerateInput) -> CommandResult:
    from app import api

    # 开始与继续严格分离；领域层会拒绝用 create 覆盖任何已有分镜数据。
    mode = "resume" if args.mode == "resume" else "create"
    if mode == "resume":
        resume_body = None if args.preflight_token is None else {
                "preflight_token": args.preflight_token,
            }
        outcome = await call_guarded(
            api.resume_storyboard,
            args.episode_id,
            body=resume_body,
        )
    else:
        create_body = None if args.preflight_token is None else {
                "preflight_token": args.preflight_token,
            }
        outcome = await call_guarded(
            api.start_storyboard,
            args.episode_id,
            body=create_body,
        )
    if isinstance(outcome, CommandResult):
        return outcome
    run_id = outcome.get("run_id")
    verb = "继续" if mode == "resume" else "开始"
    goal = outcome.get("goal") or "generate_ready"
    return succeeded(
        f"分镜任务已{verb}（{goal}）",
        data=outcome,
        run_id=run_id,
        resource_uris=[f"manju://runs/{run_id}"] if run_id else [],
    )


async def generate_batch(args: I.SelectorInput) -> CommandResult:
    from app import api

    outcome = await call_guarded(api.start_storyboard_all, args.project_id)
    if isinstance(outcome, CommandResult):
        return outcome
    return succeeded(f"已批量启动 {outcome.get('started', 0)} 个剧集的分镜生成", data=outcome)


async def cancel(args: I.EpisodeScopedInput) -> CommandResult:
    from app import api

    outcome = await call_guarded(api.cancel_storyboard, args.episode_id)
    if isinstance(outcome, CommandResult):
        return outcome
    return succeeded("分镜生成已停止", data=outcome)


async def clear(args: I.EpisodeScopedInput) -> CommandResult:
    from app import api

    outcome = await call_guarded(api.clear_storyboard, args.episode_id)
    if isinstance(outcome, CommandResult):
        return outcome
    return succeeded(
        "本集分镜及其全部可恢复数据已清空；剧本已保留",
        data=outcome,
        resource_uris=[f"manju://episodes/{args.episode_id}"],
    )


async def confirm(args: I.StoryboardConfirmInput) -> CommandResult:
    """人工门禁：确认分镜并解锁付费视频阶段。``confirm_episode_core`` 抛 ``ValueError`` 表示
    确定性校验未通过（映射为 422），而不是「服务器出错」。"""
    from app import api
    from app.capabilities.handlers.common import from_http_exception
    from fastapi import HTTPException

    try:
        api._episode_or_404(args.episode_id)
        outcome = api.confirm_episode_core(
            args.episode_id,
            preview_token=args.preview_token,
            force=args.force,
            force_reason=args.force_reason,
            reason=args.reason,
        )
    except HTTPException as exc:
        return from_http_exception(exc)
    except ValueError as exc:
        return failed(str(exc), error_code="invalid_input")
    summary = "分镜已带风险强行确认，进入付费视频阶段" if outcome.get("forced") else "分镜已确认，进入付费视频阶段"
    return succeeded(summary, data=outcome, resource_uris=[f"manju://episodes/{args.episode_id}"])


async def set_shot_adoption(args: I.StoryboardShotAdoptionInput) -> CommandResult:
    from app import api

    outcome = await call_guarded(
        api._set_storyboard_shot_adoption_core,
        args.shot_id,
        {"adopted": args.adopted, "reason": args.reason},
    )
    if isinstance(outcome, CommandResult):
        return outcome
    label = "恢复采纳" if args.adopted else "取消采纳"
    return succeeded(
        f"分镜已{label}",
        data=outcome,
        resource_uris=[f"manju://shots/{args.shot_id}"],
    )


async def shot_update(args: I.ShotUpdateInput) -> CommandResult:
    from app import api

    patch = dict(args.patch or {})
    if args.expected_version is not None:
        patch["expected_version"] = args.expected_version
    if args.edit_session_token is not None:
        patch["edit_session_token"] = args.edit_session_token
    if args.preview_token is not None:
        patch["preview_token"] = args.preview_token
    if args.baseline_content_hash is not None:
        patch["baseline_content_hash"] = args.baseline_content_hash
    patch["change_source"] = args.change_source
    if args.source_binding is not None:
        patch["source_binding"] = args.source_binding
    outcome = await call_guarded(api.edit_shot, args.shot_id, patch)
    if isinstance(outcome, CommandResult):
        return outcome
    return succeeded(
        "镜头已保存" + (f"；已失效 {outcome['invalidated']} 个媒体产物" if outcome.get("invalidated") else ""),
        data=outcome,
        resource_uris=[f"manju://shots/{args.shot_id}"],
    )
