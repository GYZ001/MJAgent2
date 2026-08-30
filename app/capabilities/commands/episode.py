"""Capability Registry：分集规划领域命令声明。

从 ``app/capabilities/catalog.py`` 的 ``_register_commands``（原 1028 代码行单
函数）按领域拆出，与 ``app/capabilities/handlers/`` 已有的按领域分文件结构对齐。
本文件只声明 ``CommandSpec`` 列表，不做注册——``catalog.py`` 的
``_register_commands`` 统一调用每个领域模块的 ``commands()`` 再逐条
``registry.register_command()``。
"""
from __future__ import annotations

from app.capabilities import inputs as I
from app.capabilities.commands import build_command as _cmd
from app.capabilities.handlers import episode as h_episode
from app.capabilities.registry import CommandSpec
from app.capabilities.schemas import ConfirmationPolicy, IdempotencyPolicy, RiskLevel


def commands() -> list[CommandSpec]:
    return [
        _cmd(
            "episode.plan",
            title="分集规划",
            description="创建或替换项目分集；replace_existing 会清空剧集链",
            input_model=I.EpisodePlanInput,
            risk=RiskLevel.R2_MATERIAL,
            confirmation=ConfirmationPolicy.WHEN_IMPACT,
            idempotency=IdempotencyPolicy.REQUIRED,
            scopes={"manju:project-write"},
            side_effect="creates_or_replaces_episodes",
            handler=h_episode.plan,
            rest_routes=("POST /api/projects/{project_id}/plan",),
            tags=("episode",),
        ),
    ]
