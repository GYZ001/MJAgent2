"""Capability Registry：剧本领域命令声明。

从 ``app/capabilities/catalog.py`` 的 ``_register_commands``（原 1028 代码行单
函数）按领域拆出，与 ``app/capabilities/handlers/`` 已有的按领域分文件结构对齐。
本文件只声明 ``CommandSpec`` 列表，不做注册——``catalog.py`` 的
``_register_commands`` 统一调用每个领域模块的 ``commands()`` 再逐条
``registry.register_command()``。
"""
from __future__ import annotations

from app.capabilities import inputs as I
from app.capabilities.commands import build_command as _cmd
from app.capabilities.handlers import screenplay as h_screenplay
from app.capabilities.registry import CommandSpec
from app.capabilities.schemas import ConfirmationPolicy, IdempotencyPolicy, RiskLevel


def commands() -> list[CommandSpec]:
    return [
        _cmd(
            "screenplay.generate",
            title="生成可交付剧本",
            description="人物解析、蓝图、全局包络与场次分片合并后建立完整 Baseline，再校验发布",
            input_model=I.ScreenplayGenerateInput,
            risk=RiskLevel.R2_MATERIAL,
            confirmation=ConfirmationPolicy.WHEN_IMPACT,
            idempotency=IdempotencyPolicy.REQUIRED,
            scopes={"manju:generation-text"},
            side_effect="creates_run_may_clear_downstream_on_publish",
            handler=h_screenplay.generate,
            rest_routes=("POST /api/episodes/{episode_id}/screenplay",),
            supports_cancel=True,
            tags=("screenplay", "production"),
        ),
        _cmd(
            "screenplay.repair_draft",
            title="送修人工剧本（生成工作文档）",
            description="把页面提交的人工剧本送入独立 Repair 环节，仅修复 JSON/Schema/上下文绑定等结构问题并产出服务端工作文档（working Artifact）；质量问题不触发修复。与页面自动保存的会话草稿（screenplay_drafts）无关，也不直接改写已发布剧本",
            input_model=I.ScreenplayRepairDraftInput,
            risk=RiskLevel.R2_MATERIAL,
            confirmation=ConfirmationPolicy.WHEN_IMPACT,
            idempotency=IdempotencyPolicy.REQUIRED,
            scopes={"manju:generation-text"},
            side_effect="creates_working_revision",
            handler=h_screenplay.repair_draft,
            rest_routes=("POST /api/episodes/{episode_id}/screenplay/repair-draft",),
            supports_cancel=True,
            tags=("screenplay", "production"),
        ),
        _cmd(
            "screenplay.delete",
            title="删除当前剧本",
            description="删除本集已发布剧本并清空其工作文档指针与分镜、媒体、交付等下游指针；保留历史证据，不影响页面会话草稿记录（screenplay_drafts）",
            input_model=I.ScreenplayDeleteInput,
            risk=RiskLevel.R3_DESTRUCTIVE,
            confirmation=ConfirmationPolicy.ALWAYS,
            idempotency=IdempotencyPolicy.REQUIRED,
            scopes={"manju:project-write"},
            side_effect="deletes_screenplay_and_downstream_products",
            handler=h_screenplay.delete,
            rest_routes=("DELETE /api/episodes/{episode_id}/screenplay",),
            tags=("screenplay", "destructive"),
        ),
        _cmd(
            "screenplay.resume",
            title="继续剧本流程",
            description="恢复未完成的首版场次分片，或从完整 working Artifact 继续校验与原子发布",
            input_model=I.ScreenplayResumeInput,
            risk=RiskLevel.R2_MATERIAL,
            confirmation=ConfirmationPolicy.ALWAYS,
            idempotency=IdempotencyPolicy.RECOMMENDED,
            scopes={"manju:generation-text"},
            side_effect="resumes_working_revision_finalization",
            handler=h_screenplay.resume,
            rest_routes=("POST /api/episodes/{episode_id}/screenplay/resume",),
            supports_cancel=True,
            tags=("screenplay", "production", "resume"),
        ),
        _cmd(
            "screenplay.patch",
            title="剧本局部 Patch",
            description="按 Issue 对工作 Artifact 做字段/节点级修补，禁止根替换",
            input_model=I.ScreenplayPatchInput,
            risk=RiskLevel.R2_MATERIAL,
            confirmation=ConfirmationPolicy.NEVER,
            idempotency=IdempotencyPolicy.REQUIRED,
            scopes={"manju:generation-text"},
            side_effect="patches_working_artifact",
            handler=h_screenplay.patch,
            rest_routes=(),
            tags=("screenplay", "production", "repair"),
        ),
        _cmd(
            "screenplay.generate_batch",
            title="批量生成剧本",
            description="批量启动无有效 revision 的剧集；warning/repairing 续跑修复而非 fresh 重生",
            input_model=I.SelectorInput,
            risk=RiskLevel.R2_MATERIAL,
            confirmation=ConfirmationPolicy.ALWAYS,
            idempotency=IdempotencyPolicy.REQUIRED,
            scopes={"manju:generation-text"},
            side_effect="creates_batch_runs",
            handler=h_screenplay.generate_batch,
            rest_routes=("POST /api/projects/{project_id}/screenplay-all",),
            supports_cancel=True,
            tags=("screenplay",),
        ),
        _cmd(
            "screenplay.cancel",
            title="取消剧本生成",
            description="取消单集或批量剧本任务",
            input_model=I.ScreenplayCancelInput,
            risk=RiskLevel.R1_REVERSIBLE,
            confirmation=ConfirmationPolicy.NEVER,
            idempotency=IdempotencyPolicy.RECOMMENDED,
            scopes={"manju:generation-text"},
            side_effect="cancels_run",
            handler=h_screenplay.cancel,
            rest_routes=(
                "POST /api/episodes/{episode_id}/screenplay/cancel",
                "POST /api/projects/{project_id}/screenplay-all/cancel",
            ),
            tags=("screenplay",),
        ),
        _cmd(
            "screenplay.update",
            title="保存剧本",
            description="结构化保存剧本；下游失效时需确认",
            input_model=I.ScreenplayUpdateInput,
            risk=RiskLevel.R2_MATERIAL,
            confirmation=ConfirmationPolicy.WHEN_IMPACT,
            idempotency=IdempotencyPolicy.RECOMMENDED,
            scopes={"manju:project-write"},
            side_effect="updates_screenplay_may_invalidate_downstream",
            handler=h_screenplay.update,
            rest_routes=("PUT /api/episodes/{episode_id}/screenplay",),
            tags=("screenplay",),
        ),
    ]
