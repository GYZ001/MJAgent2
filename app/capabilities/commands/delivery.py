"""Capability Registry：交付领域命令声明。

从 ``app/capabilities/catalog.py`` 的 ``_register_commands``（原 1028 代码行单
函数）按领域拆出，与 ``app/capabilities/handlers/`` 已有的按领域分文件结构对齐。
本文件只声明 ``CommandSpec`` 列表，不做注册——``catalog.py`` 的
``_register_commands`` 统一调用每个领域模块的 ``commands()`` 再逐条
``registry.register_command()``。
"""
from __future__ import annotations

from app.capabilities import inputs as I
from app.capabilities.commands import build_command as _cmd
from app.capabilities.handlers import delivery as h_delivery
from app.capabilities.registry import CommandSpec
from app.capabilities.schemas import ConfirmationPolicy, IdempotencyPolicy, RiskLevel


def commands() -> list[CommandSpec]:
    return [
        _cmd(
            "delivery.concatenate",
            title="拼接成片",
            description="拼接已采用镜头为成片",
            input_model=I.EpisodeScopedInput,
            risk=RiskLevel.R2_MATERIAL,
            confirmation=ConfirmationPolicy.ALWAYS,
            idempotency=IdempotencyPolicy.REQUIRED,
            scopes={"manju:delivery"},
            side_effect="creates_concat_job",
            handler=h_delivery.concatenate,
            rest_routes=("POST /api/episodes/{episode_id}/concatenate",),
            tags=("delivery",),
        ),
        _cmd(
            "delivery.check",
            title="检查交付就绪",
            description="重新计算 Delivery Readiness（只读计算）",
            input_model=I.EpisodeScopedInput,
            risk=RiskLevel.R0_READ,
            confirmation=ConfirmationPolicy.NEVER,
            idempotency=IdempotencyPolicy.NONE,
            scopes={"manju:read", "manju:delivery"},
            side_effect="none_read_compute",
            handler=h_delivery.check,
            rest_routes=(),  # GET readiness 走 Resource；此 Tool 供 Agent 主动触发重算
            mcp_exposed=True,
            tags=("delivery",),
        ),
        _cmd(
            "delivery.create_package",
            title="生成交付候选",
            description="在 readiness 通过后创建待审交付包",
            input_model=I.DeliveryCreatePackageInput,
            risk=RiskLevel.R2_MATERIAL,
            confirmation=ConfirmationPolicy.ALWAYS,
            idempotency=IdempotencyPolicy.REQUIRED,
            scopes={"manju:delivery"},
            side_effect="creates_delivery_package",
            handler=h_delivery.create_package,
            rest_routes=("POST /api/episodes/{episode_id}/delivery/package",),
            tags=("delivery",),
        ),
        _cmd(
            "delivery.review",
            title="审批交付包",
            description="批准 / 带风险批准 / 拒绝；最高业务门禁",
            input_model=I.DeliveryReviewInput,
            risk=RiskLevel.R3_DESTRUCTIVE,
            confirmation=ConfirmationPolicy.ALWAYS,
            idempotency=IdempotencyPolicy.REQUIRED,
            scopes={"manju:delivery"},
            side_effect="human_delivery_gate",
            handler=h_delivery.review,
            rest_routes=("POST /api/episodes/{episode_id}/delivery/approve",),
            tags=("delivery", "gate"),
        ),
        _cmd(
            "delivery.submit_feedback",
            title="提交客户反馈",
            description="记录客户反馈并可发起修订 Run",
            input_model=I.DeliveryFeedbackInput,
            risk=RiskLevel.R2_MATERIAL,
            confirmation=ConfirmationPolicy.ALWAYS,
            idempotency=IdempotencyPolicy.REQUIRED,
            scopes={"manju:delivery"},
            side_effect="creates_revision_run",
            handler=h_delivery.submit_feedback,
            rest_routes=("POST /api/episodes/{episode_id}/customer-feedback",),
            tags=("delivery",),
        ),
    ]
