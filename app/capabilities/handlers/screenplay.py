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


async def cancel(args: I.ScreenplayCancelInput) -> CommandResult:
    from app import api
    from app.capabilities.handlers.common import failed

    if args.project_id and not args.episode_id:
        outcome = await call_guarded(api.cancel_screenplay_all, args.project_id)
        if isinstance(outcome, CommandResult):
            return outcome
        return succeeded(f"已停止本项目 {outcome.get('stopped', 0)} 个剧本任务", data=outcome)
    if not args.episode_id:
        return failed("必须提供 episode_id 或 project_id", error_code="invalid_input")
    outcome = await call_guarded(api.cancel_screenplay, args.episode_id)
    if isinstance(outcome, CommandResult):
        return outcome
    return succeeded("剧本生成已停止", data=outcome)


async def update(args: I.ScreenplayUpdateInput) -> CommandResult:
    """结构化保存剧本。页面经 REST 传入 ``force``；Agent/MCP 批准后应传 ``force=True``。"""
    from app import api

    body: dict = {"screenplay": args.screenplay, "force": bool(args.force)}
    if args.expected_version is not None:
        body["expected_version"] = args.expected_version
    outcome = await call_guarded(api.edit_screenplay, args.episode_id, body)
    if isinstance(outcome, CommandResult):
        return outcome
    return succeeded(
        "剧本已保存" + ("；已清空本集现有分镜/媒体" if outcome.get("downstream_cleared") else ""),
        data=outcome,
        resource_uris=[f"manju://episodes/{args.episode_id}/screenplay"],
    )
