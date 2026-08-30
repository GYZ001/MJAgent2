"""Capability Registry：Run / Job 领域命令声明。

从 ``app/capabilities/catalog.py`` 的 ``_register_commands``（原 1028 代码行单
函数）按领域拆出，与 ``app/capabilities/handlers/`` 已有的按领域分文件结构对齐。
本文件只声明 ``CommandSpec`` 列表，不做注册——``catalog.py`` 的
``_register_commands`` 统一调用每个领域模块的 ``commands()`` 再逐条
``registry.register_command()``。
"""
from __future__ import annotations

from app.capabilities import inputs as I
from app.capabilities.commands import build_command as _cmd
from app.capabilities.handlers import run as h_run
from app.capabilities.registry import CommandSpec
from app.capabilities.schemas import ConfirmationPolicy, IdempotencyPolicy, RiskLevel


def commands() -> list[CommandSpec]:
    return [
        _cmd(
            "run.control",
            title="控制 Workflow Run",
            description="cancel / resume / retry / pause / handoff 统一入口",
            input_model=I.RunControlInput,
            risk=RiskLevel.R2_MATERIAL,
            confirmation=ConfirmationPolicy.WHEN_IMPACT,
            idempotency=IdempotencyPolicy.REQUIRED,
            scopes={"manju:project-write"},
            side_effect="mutates_run_state",
            handler=h_run.control,
            rest_routes=(
                "POST /api/runs/{run_id}/cancel",
                "POST /api/runs/{run_id}/resume",
                "POST /api/runs/{run_id}/retry",
                "POST /api/runs/{run_id}/pause",
                "POST /api/runs/{run_id}/handoff",
            ),
            tags=("run",),
        ),
        _cmd(
            "job.cancel",
            title="取消媒体 Job",
            description="取消单个媒体任务",
            input_model=I.JobCancelInput,
            risk=RiskLevel.R1_REVERSIBLE,
            confirmation=ConfirmationPolicy.NEVER,
            idempotency=IdempotencyPolicy.RECOMMENDED,
            scopes={"manju:generation-media"},
            side_effect="cancels_media_job",
            handler=h_run.job_cancel,
            rest_routes=("POST /api/jobs/{job_id}/cancel",),
            tags=("job",),
        ),
    ]
