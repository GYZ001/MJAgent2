"""scene.* Command Handlers（场景圣经与场景图素材库）。"""
from __future__ import annotations

from app.capabilities import inputs as I
from app.capabilities.handlers.common import call_guarded, succeeded
from app.capabilities.schemas import CommandResult


async def generate_bible(args: I.ProjectScopedInput) -> CommandResult:
    from app import api

    outcome = await call_guarded(api.start_scene_bible, args.project_id)
    if isinstance(outcome, CommandResult):
        return outcome
    return succeeded("场景圣经生成已启动，完成后自动补齐场景图", data=outcome)


async def generate_refs(args: I.SceneGenerateRefsInput) -> CommandResult:
    from app import api

    outcome = await call_guarded(api.start_scene_refs, args.project_id, body={"scene": args.scene_name})
    if isinstance(outcome, CommandResult):
        return outcome
    scope = f"场景「{args.scene_name}」" if args.scene_name else "全部场景"
    return succeeded(f"{scope}的场景图生成已启动", data=outcome)


async def cancel_refs(args: I.ProjectScopedInput) -> CommandResult:
    from app import api

    outcome = await call_guarded(api.cancel_scene_refs, args.project_id)
    if isinstance(outcome, CommandResult):
        return outcome
    return succeeded("场景图生成已停止", data=outcome)


async def update_prompt(args: I.SceneUpdatePromptInput) -> CommandResult:
    from app import api

    outcome = await call_guarded(
        api.edit_scene_prompt, args.project_id, args.scene_name, {"scene_prompt": args.prompt}
    )
    if isinstance(outcome, CommandResult):
        return outcome
    return succeeded(f"场景「{args.scene_name}」的场景图描述已保存", data=outcome)


async def regenerate_view(args: I.SceneViewRegenerateInput) -> CommandResult:
    from app import api

    outcome = await call_guarded(
        api.regenerate_scene_view_route,
        args.project_id, args.scene_name, args.scene_reference_id, args.view_role,
    )
    if isinstance(outcome, CommandResult):
        return outcome
    return succeeded(f"场景视角 {args.view_role} 已重做并通过整包 QA", data=outcome)



