"""bible.* / portrait.* Command Handlers（人物谱与定妆照）。"""
from __future__ import annotations

from app.capabilities import inputs as I
from app.capabilities.handlers.common import call_guarded, succeeded
from app.capabilities.schemas import CommandResult


async def generate(args: I.BibleGenerateInput) -> CommandResult:
    from app import api

    outcome = await call_guarded(api._start_bible_core, args.project_id, args.feedback or "")
    if isinstance(outcome, CommandResult):
        return outcome
    run_id = outcome.get("run_id")
    return succeeded(
        "人物谱生成已启动，完成后会自动进入定妆照阶段",
        data=outcome,
        run_id=run_id,
        resource_uris=[f"manju://runs/{run_id}"] if run_id else [],
    )


async def cancel(args: I.ProjectScopedInput) -> CommandResult:
    from app import api

    outcome = await call_guarded(api._cancel_bible_core, args.project_id)
    if isinstance(outcome, CommandResult):
        return outcome
    return succeeded("人物谱生成已停止", data=outcome)


async def update(args: I.BibleUpdateInput) -> CommandResult:
    from app import api

    body = dict(args.bible or {})
    if args.expected_version is not None:
        body = {"bible": args.bible, "expected_version": args.expected_version}
    else:
        body = args.bible
    outcome = await call_guarded(api.edit_bible, args.project_id, body)
    if isinstance(outcome, CommandResult):
        return outcome
    return succeeded(
        "人物谱已保存" + ("；画风变更已清理旧画风产物" if outcome.get("style_changed") else ""),
        data=outcome,
        resource_uris=[f"manju://projects/{args.project_id}/bible"],
    )


async def portrait_update_prompt(args: I.PortraitUpdatePromptInput) -> CommandResult:
    from app import api

    outcome = await call_guarded(
        api.edit_portrait_prompt, args.project_id, args.character, {"portrait_prompt": args.prompt}
    )
    if isinstance(outcome, CommandResult):
        return outcome
    return succeeded(f"角色「{args.character}」的画像描述已保存", data=outcome)


async def portrait_generate(args: I.PortraitGenerateInput) -> CommandResult:
    from app import api

    outcome = await call_guarded(
        api.start_refs,
        args.project_id,
        body={"character": args.character, "resume": args.resume},
    )
    if isinstance(outcome, CommandResult):
        return outcome
    scope = f"角色「{args.character}」" if args.character else "全部角色"
    action = "缺失定妆照补齐" if args.resume else "定妆照生成"
    return succeeded(f"{scope}的{action}已启动", data=outcome)


async def portrait_cancel(args: I.ProjectScopedInput) -> CommandResult:
    from app import api

    outcome = await call_guarded(api.cancel_refs, args.project_id)
    if isinstance(outcome, CommandResult):
        return outcome
    return succeeded("定妆照生成已停止", data=outcome)


async def portrait_regenerate_view(args: I.PortraitViewRegenerateInput) -> CommandResult:
    from app import api

    outcome = await call_guarded(
        api.regenerate_character_view_route,
        args.project_id, args.character_name, args.portrait_id, args.view_role,
    )
    if isinstance(outcome, CommandResult):
        return outcome
    return succeeded(f"角色视角 {args.view_role} 已重做并通过整包 QA", data=outcome)
