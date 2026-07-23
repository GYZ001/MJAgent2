"""screenplay.* Command Handlers（可拍剧本）。"""
from __future__ import annotations

from app.capabilities import inputs as I
from app.capabilities.handlers.common import call_guarded, succeeded
from app.capabilities.schemas import CommandResult


async def generate(args: I.ScreenplayGenerateInput) -> CommandResult:
    from app import api

    outcome = await call_guarded(api.start_screenplay, args.episode_id, body={"force": args.force})
    if isinstance(outcome, CommandResult):
        return outcome
    run_id = outcome.get("run_id")
    return succeeded(
        "剧本生成已启动" + ("（将清空本集现有分镜/媒体）" if args.force else ""),
        data=outcome,
        run_id=run_id,
        resource_uris=[f"manju://runs/{run_id}"] if run_id else [],
    )


async def generate_batch(args: I.SelectorInput) -> CommandResult:
    from app import api

    outcome = await call_guarded(api.start_screenplay_all, args.project_id)
    if isinstance(outcome, CommandResult):
        return outcome
    return succeeded(f"已批量启动 {outcome.get('started', 0)} 个剧集的剧本生成", data=outcome)


async def cancel(args: I.EpisodeScopedInput) -> CommandResult:
    from app import api

    outcome = await call_guarded(api.cancel_screenplay, args.episode_id)
    if isinstance(outcome, CommandResult):
        return outcome
    return succeeded("剧本生成已停止", data=outcome)


async def update(args: I.ScreenplayUpdateInput) -> CommandResult:
    """结构化保存剧本。写命令语义即「确认覆盖」，因此始终按 force 处理下游清理。"""
    from app import api

    outcome = await call_guarded(
        api.edit_screenplay, args.episode_id, {"screenplay": args.screenplay, "force": True}
    )
    if isinstance(outcome, CommandResult):
        return outcome
    return succeeded(
        "剧本已保存" + ("；已清空本集现有分镜/媒体" if outcome.get("downstream_cleared") else ""),
        data=outcome,
        resource_uris=[f"manju://episodes/{args.episode_id}/screenplay"],
    )
