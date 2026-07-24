"""project.* / production.* Command Handlers（导入、删除、一键全自动）。"""
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


async def auto_start(args: I.ProductionAutoStartInput) -> CommandResult:
    from app import api

    outcome = await call_guarded(
        api._start_auto_core, args.project_id, args.directory_grant, args.mode,
    )
    if isinstance(outcome, CommandResult):
        return outcome
    run_id = outcome.get("run_id")
    mode = outcome.get("mode") or args.mode
    summary = (
        "一键全自动成片已启动（含视频）"
        if mode == "full"
        else "已启动：生成到分镜待确认（不会自动烧视频）"
    )
    return succeeded(
        summary,
        data=outcome,
        run_id=run_id,
        resource_uris=[f"manju://runs/{run_id}"] if run_id else [],
    )


async def auto_cancel(args: I.ProjectScopedInput) -> CommandResult:
    from app import api, auto

    api._project_or_404(args.project_id)
    cancelled = await call_guarded(auto.cancel, args.project_id)
    if isinstance(cancelled, CommandResult):
        return cancelled
    return succeeded("一键全自动已停止；已入队镜头可能仍继续", data={"cancelled": cancelled})
