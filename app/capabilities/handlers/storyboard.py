"""storyboard.* / shot.update Command Handlers（分镜与逐镜编辑）。"""
from __future__ import annotations

from app.capabilities import inputs as I
from app.capabilities.handlers.common import call_guarded, failed, succeeded
from app.capabilities.schemas import CommandResult


async def generate(args: I.StoryboardGenerateInput) -> CommandResult:
    from app import api

    if args.mode == "resume":
        outcome = await call_guarded(
            api.resume_storyboard,
            args.episode_id,
            {
                "completion_mode": args.completion_mode,
                "completion_grant_id": args.completion_grant_id,
            },
        )
    else:
        outcome = await call_guarded(
            api.start_storyboard,
            args.episode_id,
            {
                "completion_mode": args.completion_mode,
                "completion_grant_id": args.completion_grant_id,
            },
        )
    if isinstance(outcome, CommandResult):
        return outcome
    run_id = outcome.get("run_id")
    verb = "续跑" if args.mode == "resume" else "重新生成"
    goal = outcome.get("goal") or (
        "generate_and_confirm" if args.completion_mode == "auto_confirm" else "generate_ready"
    )
    return succeeded(
        f"分镜{verb}已启动（{goal}）",
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


async def confirm(args: I.StoryboardConfirmInput) -> CommandResult:
    """人工门禁：确认分镜并解锁付费视频阶段。``confirm_episode_core`` 抛 ``ValueError`` 表示
    确定性校验未通过（映射为 422），而不是「服务器出错」。"""
    from app import api
    from app.capabilities.handlers.common import from_http_exception
    from fastapi import HTTPException

    try:
        api._episode_or_404(args.episode_id)
        outcome = api.confirm_episode_core(args.episode_id)
    except HTTPException as exc:
        return from_http_exception(exc)
    except ValueError as exc:
        return failed(str(exc), error_code="invalid_input")
    return succeeded("分镜已确认，进入付费视频阶段", data=outcome, resource_uris=[f"manju://episodes/{args.episode_id}"])


async def shot_update(args: I.ShotUpdateInput) -> CommandResult:
    from app import api

    patch = dict(args.patch or {})
    if args.expected_version is not None:
        patch["expected_version"] = args.expected_version
    outcome = await call_guarded(api.edit_shot, args.shot_id, patch)
    if isinstance(outcome, CommandResult):
        return outcome
    return succeeded(
        "镜头已保存" + (f"；已失效 {outcome['invalidated']} 个媒体产物" if outcome.get("invalidated") else ""),
        data=outcome,
        resource_uris=[f"manju://shots/{args.shot_id}"],
    )
