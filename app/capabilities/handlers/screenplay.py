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
        body={},
    )
    if isinstance(outcome, CommandResult):
        return outcome
    run_id = outcome.get("run_id")
    return succeeded(
        "可交付剧本流程已启动（人物解析 → 时空因果蓝图 → 场次写作 → 全局编译 → 结构校验 → 评分 → 发布）",
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
        (
            "已从安全检查点继续首版场次生成；validated 分片不会重复调用"
            if outcome.get("mode") == "baseline"
            else "完整剧本工作副本已继续执行结构校验、评分与发布"
        ),
        data=outcome,
        run_id=run_id,
        resource_uris=[f"manju://runs/{run_id}"] if run_id else [],
    )


async def repair_draft(args: I.ScreenplayRepairDraftInput) -> CommandResult:
    from app import api

    outcome = await call_guarded(
        api.repair_screenplay_draft,
        args.episode_id,
        body={
            "screenplay": args.screenplay,
            "expected_version": args.expected_version,
        },
    )
    if isinstance(outcome, CommandResult):
        return outcome
    run_id = outcome.get("run_id")
    return succeeded(
        "工作草稿已进入独立 Repair 环节；修复后会重新执行 QA",
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
        "当前剧本及其下游产物已删除",
        data=outcome,
        resource_uris=[f"manju://episodes/{args.episode_id}"],
    )


async def patch(args: I.ScreenplayPatchInput) -> CommandResult:
    from app.db import get_conn
    from app.portraits import load_screenplay_character_resolutions
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
        character_resolutions=load_screenplay_character_resolutions(
            get_conn(), args.episode_id,
        ),
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
    """结构化保存剧本；发布前由领域层执行只读 QA。"""
    from app import api

    body: dict = {"screenplay": args.screenplay}
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
