"""project.* Command Handlers（导入与删除）。"""
from __future__ import annotations

from app.capabilities import inputs as I
from app.capabilities.handlers.common import call_guarded, failed, succeeded
from app.capabilities.schemas import CommandResult


async def import_novel(args: I.ProjectImportNovelInput) -> CommandResult:
    from app import api
    from app.capabilities import attachments

    try:
        filename, raw = attachments.consume(args.attachment_token)
    except KeyError as exc:
        return failed(str(exc), error_code="attachment_invalid")
    outcome = await call_guarded(api._create_project_core, args.name, filename, raw)
    if isinstance(outcome, CommandResult):
        return outcome
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
