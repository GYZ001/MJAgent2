"""bible.* / portrait.* Command Handlers（人物谱与定妆照）。"""
from __future__ import annotations

from app.capabilities import inputs as I
from app.capabilities.handlers.common import accepted, call_guarded, succeeded
from app.capabilities.schemas import CommandResult


async def generate(args: I.BibleGenerateInput) -> CommandResult:
    from app import api

    outcome = await call_guarded(
        api._start_bible_core,
        args.project_id,
        args.feedback or "",
        confirm=bool(args.confirm),
        quote_id=args.quote_id,
        require_quote_id=bool(args.require_quote_id),
        style_name=args.style_name,
    )
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

    body = {
        "bible": args.bible,
        "expected_version": args.expected_version,
        "confirm": bool(args.confirm),
        "impact_preview_fingerprint": args.impact_preview_fingerprint,
    }
    outcome = await call_guarded(api.edit_bible, args.project_id, body)
    if isinstance(outcome, CommandResult):
        return outcome
    return succeeded(
        "人物谱已保存" + ("；画风变更已清理旧画风产物" if outcome.get("style_changed") else ""),
        data=outcome,
        resource_uris=[f"manju://projects/{args.project_id}/bible"],
    )


async def set_style(args: I.BibleSetStyleInput) -> CommandResult:
    """人物谱与场景库共用的统一画风入口：不重新生成角色内容，仅换画风并按需
    重生成定妆照与场景图两条腿；未变化时幂等短路。见
    ``app.domain.bible_ops.style_and_drafts.set_bible_visual_style`` 的完整语义。
    """
    from app import api

    body = {
        "style_name": args.style_name,
        "expected_version": args.expected_version,
        "confirm": bool(args.confirm),
        "quote_id": args.quote_id,
    }
    outcome = await call_guarded(api.set_bible_visual_style, args.project_id, body)
    if isinstance(outcome, CommandResult):
        return outcome
    summary = "画风未变化，未触发任何生成" if not outcome.get("changed") else (
        "统一画风已更新" + ("；已重放为幂等确认" if outcome.get("idempotent_replay") else "，人物定妆照与场景图已发起重生成")
    )
    return succeeded(
        summary,
        data=outcome,
        resource_uris=[f"manju://projects/{args.project_id}/bible"],
    )


async def nominate_character(args: I.CharacterNominateInput) -> CommandResult:
    """用户提名一个原文称呼：命中已有角色则登记别名，否则按既有建卡判据处理。
    真实结果（exists/conflict/added/skipped_minor/card_incomplete/
    skipped_not_person/error）原样透传给调用方，不折叠成一句"提名失败"，见
    ``app.domain.bible_ops.nominate`` 的模块 docstring。

    直接从 ``app.domain.bible_ops`` 导入（不经 ``app.api``）：``app/api.py`` /
    ``app/domain/__init__.py`` 的整仓再导出列表都卡在 line_count 棘轮基线的零
    余量上，新增一个再导出符号就会把两个文件同时推过基线——``app/capabilities/
    handlers/screenplay.py`` 的 ``generate``/``patch`` 已经是这个"绕过顶层门面
    直接从 app.domain.<chunk> 导入"的先例，同一模式在这里复用，不新造写法。
    """
    from app.domain.bible_ops import nominate_character as _nominate_character

    outcome = await call_guarded(
        _nominate_character,
        args.project_id,
        {"label": args.label, "from_episode_no": args.from_episode_no},
    )
    if isinstance(outcome, CommandResult):
        return outcome
    return succeeded(outcome.get("message") or "提名已处理", data=outcome)


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
        body={
            "character": args.character,
            "characters": args.characters,
            "resume": args.resume,
            "confirm": bool(args.confirm),
            "quote_id": args.quote_id,
        },
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
        args.project_id,
        args.character_name,
        args.portrait_id,
        args.view_role,
        body={
            "confirm": bool(args.confirm),
            "quote_id": args.quote_id,
        },
    )
    if isinstance(outcome, CommandResult):
        return outcome
    if outcome.get("status") == "accepted":
        return accepted(
            f"角色视角 {args.view_role} 重做任务已受理",
            data=outcome,
            run_id=outcome.get("run_id"),
            resource_uris=[f"manju://runs/{outcome['run_id']}"] if outcome.get("run_id") else [],
        )
    return succeeded(f"角色视角 {args.view_role} 已重做并通过整包 QA", data=outcome)
