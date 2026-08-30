"""Capability Registry：分镜领域命令声明。

从 ``app/capabilities/catalog.py`` 的 ``_register_commands``（原 1028 代码行单
函数）按领域拆出，与 ``app/capabilities/handlers/`` 已有的按领域分文件结构对齐。
本文件只声明 ``CommandSpec`` 列表，不做注册——``catalog.py`` 的
``_register_commands`` 统一调用每个领域模块的 ``commands()`` 再逐条
``registry.register_command()``。
"""
from __future__ import annotations

from app.capabilities import inputs as I
from app.capabilities.commands import build_command as _cmd
from app.capabilities.handlers import storyboard as h_storyboard
from app.capabilities.registry import CommandSpec
from app.capabilities.schemas import ConfirmationPolicy, IdempotencyPolicy, RiskLevel


def commands() -> list[CommandSpec]:
    return [
        _cmd(
            "storyboard.generate",
            title="开始或继续分镜任务",
            description="无分镜时开始任务；已有镜头或检查点时只允许继续任务",
            input_model=I.StoryboardGenerateInput,
            risk=RiskLevel.R2_MATERIAL,
            confirmation=ConfirmationPolicy.WHEN_IMPACT,
            idempotency=IdempotencyPolicy.REQUIRED,
            scopes={"manju:generation-text"},
            side_effect="creates_run_local_repair",
            handler=h_storyboard.generate,
            rest_routes=("POST /api/episodes/{episode_id}/storyboard",),
            supports_cancel=True,
            tags=("storyboard", "production"),
        ),
        _cmd(
            "storyboard.generate_batch",
            title="批量生成分镜",
            description="批量启动待办分镜",
            input_model=I.SelectorInput,
            risk=RiskLevel.R2_MATERIAL,
            confirmation=ConfirmationPolicy.ALWAYS,
            idempotency=IdempotencyPolicy.REQUIRED,
            scopes={"manju:generation-text"},
            side_effect="creates_batch_runs",
            handler=h_storyboard.generate_batch,
            rest_routes=("POST /api/projects/{project_id}/storyboard-all",),
            tags=("storyboard",),
        ),
        _cmd(
            "storyboard.cancel",
            title="暂停分镜",
            description="立即暂停分镜生成并保留工作镜头与安全检查点",
            input_model=I.EpisodeScopedInput,
            risk=RiskLevel.R1_REVERSIBLE,
            confirmation=ConfirmationPolicy.NEVER,
            idempotency=IdempotencyPolicy.RECOMMENDED,
            scopes={"manju:generation-text"},
            side_effect="cancels_run",
            handler=h_storyboard.cancel,
            rest_routes=("POST /api/episodes/{episode_id}/storyboard/cancel",),
            tags=("storyboard",),
        ),
        _cmd(
            "shot.update",
            title="编辑镜头",
            description="结构化 Patch 更新镜头字段；保存前展示失效媒体数",
            input_model=I.ShotUpdateInput,
            risk=RiskLevel.R2_MATERIAL,
            confirmation=ConfirmationPolicy.WHEN_IMPACT,
            idempotency=IdempotencyPolicy.RECOMMENDED,
            scopes={"manju:project-write"},
            side_effect="updates_shot_may_invalidate_media",
            handler=h_storyboard.shot_update,
            rest_routes=("PUT /api/shots/{shot_id}",),
            tags=("storyboard", "shot"),
        ),
        _cmd(
            "storyboard.confirm",
            title="确认分镜",
            description="人工门禁：确认分镜并进入付费视频阶段",
            input_model=I.StoryboardConfirmInput,
            risk=RiskLevel.R3_DESTRUCTIVE,
            # confirm-preview already signs a one-time, state-bound approval
            # that the handler consumes. A second Command Bus approval leaves
            # the page stuck at HTTP 202 after the user has approved the modal.
            confirmation=ConfirmationPolicy.NEVER,
            idempotency=IdempotencyPolicy.REQUIRED,
            scopes={"manju:generation-media"},
            side_effect="human_gate_unlocks_paid_video",
            handler=h_storyboard.confirm,
            rest_routes=("POST /api/episodes/{episode_id}/confirm",),
            tags=("storyboard", "gate"),
        ),
    ]
