"""screenplay.* Command Handlers（可拍剧本）。"""
from __future__ import annotations

from app.capabilities import inputs as I
from app.capabilities.handlers.common import call_guarded, succeeded
from app.capabilities.schemas import CommandResult


async def generate(args: I.ScreenplayGenerateInput) -> CommandResult:
    from app import api

    outcome = await call_guarded(
        api.start_screenplay,
        args.episode_id,
        body={
            "force": args.force,
            "required_dialogue_lines": args.required_dialogue_lines,
        },
    )
    if isinstance(outcome, CommandResult):
        return outcome
    run_id = outcome.get("run_id")
    return succeeded(
        "可交付剧本生产已启动（一次 Baseline + 局部自愈）",
        data=outcome,
        run_id=run_id,
        resource_uris=[f"manju://runs/{run_id}"] if run_id else [],
    )


async def resume(args: I.ScreenplayResumeInput) -> CommandResult:
    from app import api

    outcome = await call_guarded(api.resume_screenplay, args.episode_id, body={})
    if isinstance(outcome, CommandResult):
        return outcome
    run_id = outcome.get("run_id")
    return succeeded(
        "剧本工作副本已继续执行局部修复（不会再次整版生成）",
        data=outcome,
        run_id=run_id,
        resource_uris=[f"manju://runs/{run_id}"] if run_id else [],
    )


async def revise(args: I.ScreenplayReviseInput) -> CommandResult:
    from app import api

    outcome = await call_guarded(
        api.revise_screenplay,
        args.episode_id,
        body={"instruction": args.instruction},
    )
    if isinstance(outcome, CommandResult):
        return outcome
    run_id = outcome.get("run_id")
    return succeeded(
        "已从已发布剧本创建工作分支，开始按当前规则复验和局部修复",
        data=outcome,
        run_id=run_id,
        resource_uris=[f"manju://runs/{run_id}"] if run_id else [],
    )


async def delete(args: I.ScreenplayDeleteInput) -> CommandResult:
    from app import api

    outcome = await call_guarded(api.delete_screenplay, args.episode_id)
    if isinstance(outcome, CommandResult):
        return outcome
    return succeeded(
        "当前剧本及其下游产物已删除；必保留台词约束已保留",
        data=outcome,
        resource_uris=[f"manju://episodes/{args.episode_id}"],
    )


async def patch(args: I.ScreenplayPatchInput) -> CommandResult:
    from app.production.patch import PatchOperation, PatchRequest, apply_screenplay_patch
    from app.capabilities.handlers.common import failed

    result = apply_screenplay_patch(
        PatchRequest(
            production_revision_id=args.production_revision_id,
            expected_artifact_id=args.expected_artifact_id,
            expected_hash=args.expected_hash,
            issue_set_hash=args.issue_set_hash,
            operations=[PatchOperation.model_validate(op) for op in args.operations],
            idempotency_key=args.idempotency_key,
            reason=args.reason,
        ),
        episode_id=args.episode_id,
    )
    if not result.ok:
        return failed(result.error or "patch failed", error_code="patch_rejected")
    return succeeded(
        f"剧本局部 Patch 已应用，触及 {len(result.touched_node_ids)} 个节点",
        data=result.model_dump(mode="json"),
        resource_uris=[
            f"manju://episodes/{args.episode_id}/screenplay/working",
            f"manju://artifacts/{result.after_artifact_id}",
        ],
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
