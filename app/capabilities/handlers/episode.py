"""episode.* Command Handlers（分集规划）。"""
from __future__ import annotations

from app.capabilities import inputs as I
from app.capabilities.handlers.common import call_guarded, succeeded
from app.capabilities.schemas import CommandResult


async def plan(args: I.EpisodePlanInput) -> CommandResult:
    from app import planning

    outcome = await call_guarded(
        planning.start_plan,
        args.project_id,
        replace_existing=args.replace_existing,
    )
    if isinstance(outcome, CommandResult):
        return outcome
    return succeeded(
        "分集规划已启动：按章节 1:1 切分为剧集" + ("（将替换现有剧集）" if args.replace_existing else ""),
        data=outcome,
        resource_uris=[f"manju://projects/{args.project_id}/episodes"],
    )
