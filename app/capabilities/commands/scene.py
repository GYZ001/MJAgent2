"""Capability Registry：场景领域命令声明。

从 ``app/capabilities/catalog.py`` 的 ``_register_commands``（原 1028 代码行单
函数）按领域拆出，与 ``app/capabilities/handlers/`` 已有的按领域分文件结构对齐。
本文件只声明 ``CommandSpec`` 列表，不做注册——``catalog.py`` 的
``_register_commands`` 统一调用每个领域模块的 ``commands()`` 再逐条
``registry.register_command()``。
"""
from __future__ import annotations

from app.capabilities import inputs as I
from app.capabilities.commands import build_command as _cmd
from app.capabilities.handlers import scene as h_scene
from app.capabilities.registry import CommandSpec
from app.capabilities.schemas import ConfirmationPolicy, IdempotencyPolicy, RiskLevel


def commands() -> list[CommandSpec]:
    return [
        _cmd(
            "scene.generate_bible",
            title="生成场景圣经",
            description="生成场景圣经并启动场景图",
            input_model=I.ProjectScopedInput,
            risk=RiskLevel.R2_MATERIAL,
            confirmation=ConfirmationPolicy.WHEN_IMPACT,
            idempotency=IdempotencyPolicy.REQUIRED,
            scopes={"manju:generation-text", "manju:generation-media"},
            side_effect="creates_run",
            handler=h_scene.generate_bible,
            rest_routes=("POST /api/projects/{project_id}/scene-bible",),
            tags=("scene",),
        ),
        _cmd(
            "scene.generate_refs",
            title="生成场景图",
            description="补齐或重生成场景参考图",
            input_model=I.SceneGenerateRefsInput,
            risk=RiskLevel.R2_MATERIAL,
            confirmation=ConfirmationPolicy.NEVER,
            idempotency=IdempotencyPolicy.REQUIRED,
            scopes={"manju:generation-media"},
            side_effect="creates_paid_image_jobs",
            handler=h_scene.generate_refs,
            rest_routes=("POST /api/projects/{project_id}/scene-refs",),
            supports_cancel=True,
            tags=("scene",),
        ),
        _cmd(
            "scene.cancel_refs",
            title="停止场景图",
            description="取消场景图生成",
            input_model=I.ProjectScopedInput,
            risk=RiskLevel.R1_REVERSIBLE,
            confirmation=ConfirmationPolicy.NEVER,
            idempotency=IdempotencyPolicy.RECOMMENDED,
            scopes={"manju:generation-media"},
            side_effect="cancels_image_jobs",
            handler=h_scene.cancel_refs,
            rest_routes=("POST /api/projects/{project_id}/scene-refs/cancel",),
            tags=("scene",),
        ),
        _cmd(
            "scene.update_prompt",
            title="修改场景描述",
            description="更新场景 Prompt；重出图需另行确认",
            input_model=I.SceneUpdatePromptInput,
            risk=RiskLevel.R1_REVERSIBLE,
            confirmation=ConfirmationPolicy.OPTIONAL,
            idempotency=IdempotencyPolicy.RECOMMENDED,
            scopes={"manju:project-write"},
            side_effect="updates_scene_prompt",
            handler=h_scene.update_prompt,
            rest_routes=("PUT /api/projects/{project_id}/scenes/{scene_name}/prompt",),
            tags=("scene",),
        ),
        _cmd(
            "scene.regenerate_view",
            title="重做场景单视角",
            description="生成候选场景视角，通过单图与整包 QA 后原子替换当前视角",
            input_model=I.SceneViewRegenerateInput,
            risk=RiskLevel.R2_MATERIAL,
            confirmation=ConfirmationPolicy.NEVER,
            idempotency=IdempotencyPolicy.REQUIRED,
            scopes={"manju:generation-media"},
            side_effect="creates_paid_scene_view_and_may_replace_ready_view",
            handler=h_scene.regenerate_view,
            rest_routes=(
                "POST /api/projects/{project_id}/scenes/{scene_name}/refs/{scene_reference_id}/views/{view_role}/regenerate",
            ),
            tags=("scene", "multiview"),
        ),
        _cmd(
            "scene.adopt_candidate",
            title="采纳场景候选图",
            description="将已通过新版硬门禁的场景候选图采纳为主图；明确硬失败不可绕过",
            input_model=I.SceneAdoptCandidateInput,
            risk=RiskLevel.R2_MATERIAL,
            confirmation=ConfirmationPolicy.NEVER,
            idempotency=IdempotencyPolicy.REQUIRED,
            scopes={"manju:project-write"},
            side_effect="adopts_scene_reference_candidate",
            handler=h_scene.adopt_candidate,
            rest_routes=(
                "POST /api/projects/{project_id}/scenes/{scene_name}/candidates/{artifact_id}/adopt",
            ),
            tags=("scene",),
        ),
    ]
