"""project.* Command Handlers（导入与删除）。"""
from __future__ import annotations

from app.capabilities import inputs as I
from app.capabilities.handlers.common import call_guarded, failed, succeeded
from app.capabilities.schemas import CommandResult


async def import_novel(args: I.ProjectImportNovelInput) -> CommandResult:
    from app import api, planning
    from app.capabilities import attachments

    try:
        filename, raw = attachments.consume(args.attachment_token)
    except KeyError as exc:
        return failed(str(exc), error_code="attachment_invalid")
    outcome = await call_guarded(api._create_project_core, args.name, filename, raw)
    if isinstance(outcome, CommandResult):
        return outcome
    planning_generation = await call_guarded(planning.start_plan, outcome["project_id"])
    if isinstance(planning_generation, CommandResult):
        outcome["episode_planning"] = {
            "status": "failed_to_start",
            "error": planning_generation.summary,
        }
    else:
        outcome["episode_planning"] = planning_generation
    # Import bootstraps a usable asset library. Bible generation is asynchronous
    # and already fans out into character and scene multiview generation after
    # its validation gate passes.
    generation = await call_guarded(api._start_bible_core, outcome["project_id"], "")
    if isinstance(generation, CommandResult):
        outcome["asset_generation"] = {
            "status": "failed_to_start",
            "error": generation.summary,
        }
    else:
        outcome["asset_generation"] = generation
    chapter_count = outcome.get("ingestion", {}).get("chapter_count", 0)
    return succeeded(
        f"已导入小说，共切分出 {chapter_count} 章",
        data=outcome,
        resource_uris=[f"manju://projects/{outcome['project_id']}"],
    )


async def delete_project(args: I.ProjectDeleteInput) -> CommandResult:
    from app import api

    outcome = await call_guarded(api._delete_project_core, args.project_id)
    if isinstance(outcome, CommandResult):
        return outcome
    return succeeded(f"项目 {args.project_id} 已删除", data=outcome)
