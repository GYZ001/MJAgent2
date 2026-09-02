"""Capability Registry：人物谱 / 定妆领域命令声明。

从 ``app/capabilities/catalog.py`` 的 ``_register_commands``（原 1028 代码行单
函数）按领域拆出，与 ``app/capabilities/handlers/`` 已有的按领域分文件结构对齐。
本文件只声明 ``CommandSpec`` 列表，不做注册——``catalog.py`` 的
``_register_commands`` 统一调用每个领域模块的 ``commands()`` 再逐条
``registry.register_command()``。
"""
from __future__ import annotations

from app.capabilities import inputs as I
from app.capabilities.commands import build_command as _cmd
from app.capabilities.handlers import bible as h_bible
from app.capabilities.registry import CommandSpec
from app.capabilities.schemas import ConfirmationPolicy, IdempotencyPolicy, RiskLevel


def commands() -> list[CommandSpec]:
    return [*_bible_card_commands(), *_portrait_commands()]


def _bible_card_commands() -> list[CommandSpec]:
    """人物谱整体生成/修订 + 单角色提名建卡（不含定妆照，见 ``_portrait_commands``）。"""
    return [
        _cmd(
            "bible.generate",
            title="生成人物谱",
            description="生成人物谱与定妆照，创建长 Run；feedback 可选，用于「打回重写」时携带修订要求",
            input_model=I.BibleGenerateInput,
            risk=RiskLevel.R2_MATERIAL,
            confirmation=ConfirmationPolicy.WHEN_IMPACT,
            idempotency=IdempotencyPolicy.REQUIRED,
            scopes={"manju:generation-text", "manju:generation-media"},
            side_effect="creates_run_and_may_invalidate_downstream",
            handler=h_bible.generate,
            rest_routes=("POST /api/projects/{project_id}/bible",),
            supports_cancel=True,
            tags=("bible",),
        ),
        _cmd(
            "bible.cancel",
            title="停止人物谱生成",
            description="取消进行中的人物谱任务",
            input_model=I.ProjectScopedInput,
            risk=RiskLevel.R1_REVERSIBLE,
            confirmation=ConfirmationPolicy.NEVER,
            idempotency=IdempotencyPolicy.RECOMMENDED,
            scopes={"manju:generation-text"},
            side_effect="cancels_run",
            handler=h_bible.cancel,
            rest_routes=("POST /api/projects/{project_id}/bible/cancel",),
            tags=("bible",),
        ),
        _cmd(
            "bible.update",
            title="修订人物谱",
            description="保存人物谱修订；若使下游失效需确认",
            input_model=I.BibleUpdateInput,
            risk=RiskLevel.R2_MATERIAL,
            confirmation=ConfirmationPolicy.NEVER,
            idempotency=IdempotencyPolicy.RECOMMENDED,
            scopes={"manju:project-write"},
            side_effect="updates_bible_may_invalidate_downstream",
            handler=h_bible.update,
            rest_routes=("PUT /api/projects/{project_id}/bible",),
            tags=("bible",),
        ),
        _cmd(
            "bible.set_style",
            title="配置统一画风",
            description=(
                "人物谱与场景库共用的统一画风配置：不重新生成角色外观/性格/关系，仅切换画风字段；"
                "画风未变化时幂等短路直接返回，实际变化时先返回人物+场景合并报价，确认后同一次"
                "调用内依次发起定妆照与场景图两条全量重生成"
            ),
            input_model=I.BibleSetStyleInput,
            risk=RiskLevel.R2_MATERIAL,
            confirmation=ConfirmationPolicy.WHEN_IMPACT,
            idempotency=IdempotencyPolicy.REQUIRED,
            scopes={"manju:generation-media"},
            side_effect="creates_paid_image_jobs_and_may_invalidate_downstream",
            handler=h_bible.set_style,
            rest_routes=("POST /api/projects/{project_id}/bible/style",),
            tags=("bible", "portrait", "scene"),
        ),
        _cmd(
            "bible.nominate_character",
            title="提名角色",
            description=(
                "用户提名一个原文称呼：命中人物谱已有角色则登记别名，都没命中则按既有建卡判据"
                "（原文证据检索/subject_kind=person 硬闸/外观生成）尝试新建人物卡；命中多个角色"
                "时 fail closed，不猜测归属"
            ),
            input_model=I.CharacterNominateInput,
            risk=RiskLevel.R2_MATERIAL,
            confirmation=ConfirmationPolicy.NEVER,
            idempotency=IdempotencyPolicy.RECOMMENDED,
            scopes={"manju:generation-media"},
            side_effect="may_create_character_card_or_register_alias",
            handler=h_bible.nominate_character,
            rest_routes=("POST /api/projects/{project_id}/characters/nominate",),
            tags=("bible", "portrait"),
        ),
    ]


def _portrait_commands() -> list[CommandSpec]:
    """定妆照生命周期：改描述、生成/取消整批、单视角重做。"""
    return [
        _cmd(
            "portrait.update_prompt",
            title="修改定妆描述",
            description="更新单角色画像 Prompt（可逆编辑）",
            input_model=I.PortraitUpdatePromptInput,
            risk=RiskLevel.R1_REVERSIBLE,
            confirmation=ConfirmationPolicy.OPTIONAL,
            idempotency=IdempotencyPolicy.RECOMMENDED,
            scopes={"manju:project-write"},
            side_effect="updates_portrait_prompt",
            handler=h_bible.portrait_update_prompt,
            rest_routes=("PUT /api/projects/{project_id}/characters/{character_name}/portrait",),
            tags=("portrait",),
        ),
        _cmd(
            "portrait.generate",
            title="生成定妆照",
            description="生成全部或单角色定妆图（付费图片）",
            input_model=I.PortraitGenerateInput,
            risk=RiskLevel.R2_MATERIAL,
            confirmation=ConfirmationPolicy.NEVER,
            idempotency=IdempotencyPolicy.REQUIRED,
            scopes={"manju:generation-media"},
            side_effect="creates_paid_image_jobs",
            handler=h_bible.portrait_generate,
            rest_routes=("POST /api/projects/{project_id}/refs",),
            supports_cancel=True,
            tags=("portrait",),
        ),
        _cmd(
            "portrait.cancel",
            title="停止定妆生成",
            description="取消定妆图任务",
            input_model=I.ProjectScopedInput,
            risk=RiskLevel.R1_REVERSIBLE,
            confirmation=ConfirmationPolicy.NEVER,
            idempotency=IdempotencyPolicy.RECOMMENDED,
            scopes={"manju:generation-media"},
            side_effect="cancels_image_jobs",
            handler=h_bible.portrait_cancel,
            rest_routes=("POST /api/projects/{project_id}/refs/cancel",),
            tags=("portrait",),
        ),
        _cmd(
            "portrait.regenerate_view",
            title="重做人物单视角",
            description="生成候选视角，通过单图与整包 QA 后原子替换当前视角",
            input_model=I.PortraitViewRegenerateInput,
            risk=RiskLevel.R2_MATERIAL,
            confirmation=ConfirmationPolicy.NEVER,
            idempotency=IdempotencyPolicy.REQUIRED,
            scopes={"manju:generation-media"},
            side_effect="creates_paid_character_view_and_may_replace_ready_view",
            handler=h_bible.portrait_regenerate_view,
            rest_routes=(
                "POST /api/projects/{project_id}/characters/{character_name}/portraits/{portrait_id}/views/{view_role}/regenerate",
            ),
            tags=("portrait", "multiview"),
        ),
    ]
