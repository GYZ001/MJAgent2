"""Capability Registry：人物卡固定声音领域命令声明。

风格与 ``app.capabilities.commands.bible`` 一致；单独成模块是因为
``commands/bible.py`` 的 ``_portrait_commands`` 已经卡在函数行数基线
（CLAUDE.md「新命令放新模块」）。金额概念已退场（2026-09-01），本域命令不带
``confirm``/``quote_id`` 报价流程。
"""
from __future__ import annotations

from app.capabilities import inputs as I
from app.capabilities.commands import build_command as _cmd
from app.capabilities.handlers import voice as h_voice
from app.capabilities.registry import CommandSpec
from app.capabilities.schemas import ConfirmationPolicy, IdempotencyPolicy, RiskLevel


def commands() -> list[CommandSpec]:
    return [*_generation_commands(), *_lifecycle_commands()]


def _generation_commands() -> list[CommandSpec]:
    """描述建议 + 正式生成（两者都会真的产出/消耗一次供应商调用或免费文本调用）。"""
    return [
        _cmd(
            "voice.suggest_description",
            title="生成声音描述建议",
            description="由人物卡外观/性格/语风/定位/年代生成声音描述与试听台词建议，不落库（免费文本调用）",
            input_model=I.VoiceDescriptionInput,
            risk=RiskLevel.R1_REVERSIBLE,
            confirmation=ConfirmationPolicy.NEVER,
            idempotency=IdempotencyPolicy.NONE,
            scopes={"manju:generation-text"},
            side_effect="reads_bible_and_suggests_only",
            handler=h_voice.suggest_description,
            rest_routes=("POST /api/projects/{project_id}/characters/{character_name}/voice-description",),
            tags=("voice",),
        ),
        _cmd(
            "voice.generate",
            title="生成角色声音",
            description="调用声音生成模型为角色生成一个候选声音版本，裁片、响度归一并做语音识别核验",
            input_model=I.VoiceGenerateInput,
            risk=RiskLevel.R2_MATERIAL,
            confirmation=ConfirmationPolicy.NEVER,
            idempotency=IdempotencyPolicy.REQUIRED,
            scopes={"manju:media-generate"},
            side_effect="creates_paid_voice_design_job",
            handler=h_voice.generate,
            rest_routes=("POST /api/projects/{project_id}/characters/{character_name}/voices",),
            tags=("voice",),
        ),
    ]


def _lifecycle_commands() -> list[CommandSpec]:
    """采用与批量补齐：只操作已生成的行/触发已有生成流程，不新增业务能力。"""
    return [
        _cmd(
            "voice.adopt",
            title="采用角色声音",
            description="把某个候选声音版本设为该角色的当前声音；人工采用优先级最高",
            input_model=I.VoiceAdoptInput,
            risk=RiskLevel.R1_REVERSIBLE,
            confirmation=ConfirmationPolicy.NEVER,
            idempotency=IdempotencyPolicy.RECOMMENDED,
            scopes={"manju:project-write"},
            side_effect="updates_current_voice_pointer",
            handler=h_voice.adopt,
            rest_routes=(
                "POST /api/projects/{project_id}/characters/{character_name}/voices/{voice_id}/adopt",
            ),
            tags=("voice",),
        ),
        _cmd(
            "voice.generate_missing",
            title="批量补齐缺失声音",
            description="为项目里所有还没有当前声音的具名角色后台串行生成声音，已在生成中的角色跳过",
            input_model=I.ProjectScopedInput,
            risk=RiskLevel.R2_MATERIAL,
            confirmation=ConfirmationPolicy.NEVER,
            idempotency=IdempotencyPolicy.RECOMMENDED,
            scopes={"manju:media-generate"},
            side_effect="creates_paid_voice_design_jobs_in_background",
            handler=h_voice.generate_missing,
            rest_routes=("POST /api/projects/{project_id}/voices/generate-missing",),
            tags=("voice",),
        ),
    ]
