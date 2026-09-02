"""project.* Command Handlers（导入与删除）。"""
from __future__ import annotations

from app.capabilities import inputs as I
from app.capabilities.handlers.common import call_guarded, failed, succeeded
from app.capabilities.schemas import CommandResult


async def _confirm_and_start_bible(project_id: str, style_name: str | None) -> CommandResult:
    """导入即定风格：世界观写入挪到项目创建时自动发起，不再等用户回到人物谱
    页手动点「选择画风并确定世界观」（2026-08-31 用户拍板：人物谱/场景库降为
    纯展示，画风改在导入面板一次性选定）。首次生成只是把选定的画风写进
    world.visual_style_canonical，不再发起任何模型调用（同日二次拍板：画风
    已由用户选定，问模型判定 era/genre 是多余的，且曾在真实项目上触发内容
    审核把用户拦住），也不产生图片费用（见 api._compute_bible_generate_precheck
    的对应分支），比照 app.domain.bible_ops 里其余「预检后立即用 quote_id 自动
    确认」路径（2026-08-29 用户拍板删除费用确认弹窗），这里同样不停下来等人工
    点头。
    """
    from app import api

    async def _run():
        precheck = api._compute_bible_generate_precheck(project_id, style_name=style_name)
        quote = api._issue_scope_quote(precheck)
        return await api._start_bible_core(
            project_id, "", confirm=True, quote_id=quote["quote_id"], style_name=style_name,
        )

    return await call_guarded(_run)


async def import_novel(args: I.ProjectImportNovelInput) -> CommandResult:
    from app import api, planning
    from app.capabilities import attachments

    token_hash = api._novel_import_token_hash(args.attachment_token)
    outcome = api._novel_import_receipt(token_hash)
    if outcome is None:
        try:
            filename, raw = attachments.read(args.attachment_token)
        except KeyError as exc:
            return failed(str(exc), error_code="attachment_invalid")
        try:
            outcome = await call_guarded(
                api._create_project_core,
                args.name,
                filename,
                raw,
                import_token_hash=token_hash,
            )
        except BaseException:
            attachments.release(args.attachment_token)
            raise
        if isinstance(outcome, CommandResult):
            attachments.release(args.attachment_token)
            return outcome
    attachments.discard(args.attachment_token)

    project_id = outcome["project_id"]
    project_state = api.get_conn().execute(
        "SELECT plan_status,bible_status FROM projects WHERE id=?",
        (project_id,),
    ).fetchone()
    if project_state and project_state["plan_status"] == "ready":
        outcome["episode_planning"] = {
            "status": "ready",
            "existing": True,
        }
    else:
        planning_generation = await call_guarded(planning.start_plan, project_id)
        if isinstance(planning_generation, CommandResult):
            outcome["episode_planning"] = {
                "status": "failed_to_start",
                "error": planning_generation.summary,
                "error_code": planning_generation.error_code,
                "retryable": True,
                "retry_endpoint": f"/api/projects/{project_id}/plan",
            }
        else:
            outcome["episode_planning"] = planning_generation
    # Import bootstraps a usable asset library. Bible generation is asynchronous
    # and already fans out into character and scene multiview generation after
    # its validation gate passes.
    if project_state and project_state["bible_status"] in {"ready", "warning"}:
        outcome["asset_generation"] = {
            "status": project_state["bible_status"],
            "existing": True,
        }
    elif project_state and project_state["bible_status"] == "running":
        outcome["asset_generation"] = {
            "status": "running",
            "existing": True,
            "task_id": f"bible:{project_id}",
        }
    else:
        generation = await _confirm_and_start_bible(project_id, args.style_name)
        if isinstance(generation, CommandResult):
            detail = generation.data if isinstance(generation.data, dict) else {}
            if detail.get("code") == "PAYMENT_CONFIRM_REQUIRED":
                outcome["asset_generation"] = {
                    "status": "awaiting_confirmation",
                    "reason_code": "payment_confirmation_required",
                    "message": "人物谱与定妆需要确认生成范围，已保留导入结果，确认后即可继续",
                    "precheck": detail.get("precheck"),
                    "retryable": True,
                    "precheck_endpoint": f"/api/projects/{project_id}/bible/generate-precheck",
                    "start_endpoint": f"/api/projects/{project_id}/bible",
                }
            else:
                outcome["asset_generation"] = {
                    "status": "failed_to_start",
                    "error": generation.summary,
                    "error_code": generation.error_code,
                    "retryable": True,
                    "retry_endpoint": f"/api/projects/{project_id}/bible",
                }
        else:
            outcome["asset_generation"] = generation
    chapter_count = outcome.get("ingestion", {}).get("chapter_count", 0)
    planning_status = outcome["episode_planning"].get("status")
    asset_status = outcome["asset_generation"].get("status")
    if planning_status == "running" and asset_status == "running":
        summary = f"小说已导入，共 {chapter_count} 章；分集、人物谱与素材准备已启动"
    elif planning_status in {"running", "ready"} and asset_status == "awaiting_confirmation":
        planning_text = "已完成" if planning_status == "ready" else "已启动"
        summary = f"小说已导入，共 {chapter_count} 章；分集{planning_text}，人物谱与定妆等待确认"
    elif planning_status == "ready" and asset_status in {"ready", "warning"}:
        summary = f"小说导入结果已恢复，共 {chapter_count} 章；分集和人物谱均已保留"
    elif planning_status in {"running", "ready"} and asset_status == "running":
        summary = f"小说导入结果已恢复，共 {chapter_count} 章；后台准备正在继续"
    else:
        summary = f"小说已导入，共 {chapter_count} 章；后台准备可在项目内继续或重试"
    return succeeded(
        summary,
        data=outcome,
        resource_uris=[f"manju://projects/{outcome['project_id']}"],
    )


async def delete_project(args: I.ProjectDeleteInput) -> CommandResult:
    """软删除：项目移入回收站，数据与产物原样保留，24 小时后自动彻底清理。"""
    from app import api

    outcome = await call_guarded(api._delete_project_core, args.project_id)
    if isinstance(outcome, CommandResult):
        return outcome
    return succeeded(f"项目 {args.project_id} 已移入回收站", data=outcome)


async def restore_project(args: I.ProjectRestoreInput) -> CommandResult:
    from app import api

    outcome = await call_guarded(api._restore_project_core, args.project_id)
    if isinstance(outcome, CommandResult):
        return outcome
    return succeeded(f"项目 {args.project_id} 已从回收站恢复", data=outcome)


async def purge_project(args: I.ProjectPurgeInput) -> CommandResult:
    """彻底清理：仅对回收站中的项目生效，物理删除数据库行与磁盘产物，不可撤销。"""
    from app import api

    outcome = await call_guarded(api._purge_project_core, args.project_id)
    if isinstance(outcome, CommandResult):
        return outcome
    return succeeded(f"项目 {args.project_id} 已彻底删除", data=outcome)


async def purge_all_deleted_projects(args: I.ProjectPurgeAllInput) -> CommandResult:
    """清空回收站：逐个彻底清理全部已软删除的项目，不可撤销。"""
    from app import api

    outcome = await call_guarded(api._purge_all_deleted_projects_core)
    if isinstance(outcome, CommandResult):
        return outcome
    return succeeded(
        f"回收站已清空，彻底删除 {outcome['purged_count']} 个项目"
        + (f"，{len(outcome['failed'])} 个失败待重试" if outcome["failed"] else ""),
        data=outcome,
    )
