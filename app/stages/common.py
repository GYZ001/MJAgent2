"""剧本流水线共用基础设施：StageError 异常、AgentLoop 通用驱动器与故事板候选归一化。"""
from __future__ import annotations

from copy import deepcopy
import hashlib
from typing import Any, Callable

from pydantic import BaseModel

from app import config
from app.db import log_provider_call
from app.evaluations.issues import issues_from_messages
from app.harness import model_gateway
from app.harness.types import Issue
from app.continuity import adaptation_hook_errors  # noqa: F401 -- re-exported: app/production/screenplay_repair.py does `from app.stages import adaptation_hook_errors`
from app.loops import AgentLoop, AgentLoopFailure
from app.narrative_blueprint import (
    NarrativeBlueprint,
    normalize_blueprint_raw_json,
    recover_complete_blueprint_prefix,
)
from app.schemas import (Bible, EpisodeScreenplay,
                         StoryboardOutline, StoryboardOutlineShot, extract_json, normalize_screenplay_json_shape,
                         schema_errors)
from app.screenplay_ir import (
    IR_COMPILER_VERSION,
    IR_VERSION,
    ScreenplayGenerationIR,
    normalize_screenplay_ir_payload,
    recover_complete_screenplay_ir_prefix,
)

from .bible_shared import _bible_short_json_call_meta
from .constants import SYSTEM_PREFIX


class StageError(Exception):
    """阶段失败：errors 面向 UI 展示（PRD 原则 P2：失败要响）。"""

    def __init__(
        self,
        stage: str,
        errors: list[str],
        *,
        exit_reason: str | None = None,
        iterations: int | None = None,
        issues: list[Issue] | None = None,
    ):
        self.stage = stage
        self.errors = errors
        self.exit_reason = exit_reason
        self.iterations = iterations
        self.issues = list(issues or [])
        super().__init__(f"[{stage}] " + "；".join(errors[:5]))


def normalize_storyboard_outline_candidate(
    obj: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Normalize unambiguous outline serialization without weakening its schema."""
    normalized = dict(obj)
    changes: list[dict[str, Any]] = []
    raw_shots = normalized.get("shots")
    if not isinstance(raw_shots, list):
        return normalized, changes
    shots: list[Any] = []
    for index, raw_shot in enumerate(raw_shots):
        if not isinstance(raw_shot, dict):
            shots.append(raw_shot)
            continue
        shot = dict(raw_shot)
        for field_name, field_info in StoryboardOutlineShot.model_fields.items():
            if field_name not in shot or shot[field_name] is not None:
                continue
            default = field_info.get_default(call_default_factory=True)
            if not isinstance(default, (str, list, dict)):
                continue
            shot[field_name] = deepcopy(default)
            changes.append({
                "field": f"shots.{index}.{field_name}",
                "from": None,
                "to": deepcopy(default),
                "reason": "nullable_outline_field_normalization",
            })
        covers = shot.get("covers")
        if isinstance(covers, list) and all(
            isinstance(item, str) for item in covers
        ):
            merged = "；".join(
                item.strip() for item in covers if item.strip()
            )
            shot["covers"] = merged
            changes.append({
                "field": f"shots.{index}.covers",
                "from": covers,
                "to": merged,
                "reason": "join_string_list",
            })
        shots.append(shot)
    normalized["shots"] = shots
    return normalized, changes


def _render_error_history(
    error_history: list[list[str]],
    *,
    latest_keep: int = 12,
) -> str:
    """渲染历次输出的问题记录（让模型看到自己反复犯的错）。
    与上一轮完全相同的轮次折叠成一行，避免把同样的错误抄 7 遍、把 prompt 撑爆。"""
    blocks: list[str] = []
    for i, errs in enumerate(error_history):
        if i > 0 and errs == error_history[i - 1]:
            blocks.append(f"【第 {i + 1} 次输出】问题与上一次完全相同（未改进）")
            continue
        if i == len(error_history) - 1:
            keep = latest_keep
        elif i == len(error_history) - 2:
            keep = min(12, latest_keep)
        else:
            keep = 5
        lines = [f"- {e}" for e in errs[:keep]]
        if len(errs) > keep:
            lines.append(f"- ……（另有 {len(errs) - keep} 条同轮问题从略）")
        blocks.append(f"【第 {i + 1} 次输出的问题】\n" + "\n".join(lines))
    return "\n".join(blocks)


async def _run_with_agent_loop(
    stage: str,
    stage_key: str,
    user_prompt: str,
    model_cls: type[BaseModel],
    business_validate: Callable[[BaseModel], list[str | Issue]],
    *,
    loop: AgentLoop,
    temperature: float = 0.7,
    max_tokens: int = 8192,
    repair_user_prompt_limit: int | None = 3000,
    repair_candidate_limit: int | None = 6000,
    repair_context: str | None = None,
    repair_output_contract: str | None = None,
    prefill: dict | None = None,
    semantic_attempt_id: str | None = None,
) -> BaseModel:
    """Phase 2 loop adapter: structured issues, bounded repair and persisted iterations."""
    base_call_meta = {
        "stage": stage,
        "stage_key": stage_key,
        "initiator_label": stage,
        "initiator_scope": "agent_loop",
        "contract_version": loop.contract.version,
        "expected_json": True,
        **(
            {
                "generation_contract": IR_VERSION,
                "published_output_contract": "EpisodeScreenplay@4.0.0",
                "deterministic_compiler": "app.screenplay_ir.compile_screenplay_ir",
                "compiler_version": IR_COMPILER_VERSION,
                "prompt_version": loop.prompt_version,
            }
            if model_cls is ScreenplayGenerationIR
            else {}
        ),
    }
    if stage_key.startswith("character_bible"):
        base_call_meta = _bible_short_json_call_meta(base_call_meta)
    iteration_state = {"number": 0}

    async def producer(
        iteration_no: int,
        previous_raw: str | None,
        latest_issues,
        issue_history,
    ) -> str:
        iteration_state["number"] = iteration_no
        semantic_call_meta: dict[str, Any] = {}
        if semantic_attempt_id:
            digest = hashlib.sha256(
                f"{semantic_attempt_id}:inner:{iteration_no}".encode("utf-8")
            ).hexdigest()[:32]
            semantic_call_meta = {
                "semantic_attempt_id": semantic_attempt_id,
                "operation_id": f"op_sem_{digest}",
            }
        if iteration_no == 1:
            return await model_gateway.chat(
                [{"role": "system", "content": SYSTEM_PREFIX}, {"role": "user", "content": user_prompt}],
                temperature=temperature,
                max_tokens=max_tokens,
                call_meta={
                    **base_call_meta,
                    "call_role": "stage_generate",
                    "call_role_label": "主生成",
                    "repair_round": 0,
                    **semantic_call_meta,
                    # 若相同幂等 operation 已从供应商成功返回、但本地状态机在落库前
                    # 发生恢复竞态，直接复用已记录响应，禁止再次付费生成。
                    "reuse_successful_operation": True,
                },
            )
        error_history = [[issue.message for issue in issues] for issues in issue_history]
        repair_index = iteration_no - 1
        repair_temp = 0.2 if repair_index < 3 else min(0.2 + 0.15 * (repair_index - 2), 0.8)
        emphasis = (
            ""
            if repair_index < 3
            else (
                f"\n\n【第 {repair_index} 次修复】历史问题已多次未解决。"
                "必须逐条定向修改，且不得引入新的合同违规。"
            )
        )
        if repair_context is not None:
            original_task = repair_context
        else:
            original_task = (
                user_prompt
                if repair_user_prompt_limit is None
                else user_prompt[:repair_user_prompt_limit]
            )
        previous_candidate = previous_raw or ""
        if repair_candidate_limit is not None:
            previous_candidate = previous_candidate[:repair_candidate_limit]
        repair_prompt = (
            "你此前的输出未通过校验。以下问题均为结构化硬门禁，不是泛泛建议：\n"
            + _render_error_history(
                error_history,
                latest_keep=48 if loop.policy.repair_all_blockers else 12,
            )
            + emphasis
            + "\n\n只修复上述问题，然后重新输出完整 JSON（不要解释，不要 Markdown）。"
            + "\n\n原任务要求：\n"
            + original_task
            + "\n\n最近一次候选：\n"
            + previous_candidate
            + (
                "\n\n本轮输出合同（最高优先级）：\n" + repair_output_contract
                if repair_output_contract
                else ""
            )
        )
        repaired_raw = await model_gateway.chat(
            [{"role": "system", "content": SYSTEM_PREFIX}, {"role": "user", "content": repair_prompt}],
            temperature=repair_temp,
            max_tokens=max_tokens,
            call_meta={
                **base_call_meta,
                "call_role": "stage_repair",
                "call_role_label": "定向修复",
                "repair_round": repair_index,
                **semantic_call_meta,
                "latest_issue_codes": [issue.code for issue in latest_issues[:10]],
                "latest_errors": [issue.message for issue in latest_issues[:10]],
            },
        )
        return repaired_raw

    def evaluator(raw: str):
        if model_cls is NarrativeBlueprint:
            raw = normalize_blueprint_raw_json(raw)
        try:
            obj = extract_json(
                raw,
                repair_unescaped_inner_quotes=model_cls in {
                    EpisodeScreenplay,
                    NarrativeBlueprint,
                },
                repair_singleton_string_object_fields=(
                    ("attention_memory_assumptions",)
                    if model_cls is EpisodeScreenplay
                    else ()
                ),
            )
        except ValueError as exc:
            obj = (
                recover_complete_screenplay_ir_prefix(raw)
                if model_cls is ScreenplayGenerationIR
                else (
                    recover_complete_blueprint_prefix(raw)
                    if model_cls is NarrativeBlueprint
                    else None
                )
            )
            if obj is None:
                messages = [str(exc)]
                return None, issues_from_messages(
                    messages,
                    subject=f"{loop.scope_type}:{loop.scope_id}",
                    category="structural",
                )
        if model_cls is ScreenplayGenerationIR and isinstance(obj, dict):
            obj, normalizations = normalize_screenplay_ir_payload(obj)
            if normalizations:
                log_provider_call(
                    "screenplay_ir_candidate_normalization",
                    config.MODEL_TEXT,
                    "NORMALIZED",
                    None,
                    0,
                    meta={
                        "episode_id": loop.scope_id,
                        "stage": stage,
                        "generation_contract": IR_VERSION,
                        "compiler_version": IR_COMPILER_VERSION,
                        "changes": normalizations,
                    },
                )
        if model_cls is EpisodeScreenplay:
            obj, normalized_paths = normalize_screenplay_json_shape(obj)
            if normalized_paths:
                log_provider_call(
                    "screenplay_candidate_normalization", config.MODEL_TEXT,
                    "NORMALIZED", None, 0,
                    meta={
                        "episode_id": loop.scope_id,
                        "stage": stage,
                        "change": "normalize_screenplay_candidate_shape",
                        "normalized_paths": normalized_paths,
                    },
                )
        if model_cls is StoryboardOutline and isinstance(obj, dict):
            obj, normalizations = normalize_storyboard_outline_candidate(obj)
            if normalizations:
                log_provider_call(
                    "storyboard_outline_candidate_normalization",
                    config.MODEL_TEXT,
                    "NORMALIZED",
                    None,
                    0,
                    meta={
                        "episode_id": loop.scope_id,
                        "stage": stage,
                        "changes": normalizations,
                    },
                )
        if prefill and isinstance(obj, dict):
            obj.update(prefill)
        instance, messages = schema_errors(model_cls, obj)
        if instance is not None and model_cls is Bible:
            from app.refs import production_appearance_anchor

            normalizations = []
            for character in instance.characters:
                original = character.appearance_canonical
                normalized = production_appearance_anchor(original)
                if normalized != original:
                    character.appearance_canonical = normalized
                    normalizations.append({
                        "character": character.name,
                        "from": original,
                        "to": normalized,
                    })
            if normalizations:
                log_provider_call(
                    "character_bible_candidate_normalization",
                    config.MODEL_TEXT,
                    "NORMALIZED",
                    None,
                    0,
                    meta={
                        "project_id": loop.scope_id,
                        "stage": stage,
                        "changes": normalizations,
                    },
                )
        if instance is not None:
            messages = business_validate(instance)
        typed_issues = [item for item in messages if isinstance(item, Issue)]
        prose_messages = [str(item) for item in messages if not isinstance(item, Issue)]
        return (
            instance,
            [
                *issues_from_messages(
                    prose_messages,
                    subject=f"{loop.scope_type}:{loop.scope_id}",
                ),
                *typed_issues,
            ],
        )

    try:
        result = await loop.run(producer, evaluator)
    except AgentLoopFailure as exc:
        log_provider_call(
            f"{stage}_loop", config.MODEL_TEXT, "LOOP_FAILED", None, 0,
            meta={
                "stage": stage,
                "iterations": exc.iterations,
                "exit_reason": exc.exit_reason,
                "issue_codes": [issue.code for issue in exc.issues[:10]],
            },
        )
        raise StageError(
            stage,
            [issue.message for issue in exc.issues]
            + [f"Agent Loop 退出：{exc.exit_reason}（{exc.iterations} 轮）"],
            exit_reason=exc.exit_reason,
            iterations=exc.iterations,
            issues=exc.issues,
        ) from exc
    if result.status == "warning":
        object.__setattr__(result.value, "residual_errors", [issue.message for issue in result.issues])
        object.__setattr__(
            result.value, "residual_issues",
            [issue.model_dump(mode="json") for issue in result.issues],
        )
        object.__setattr__(result.value, "disposition", "WARNING")
        log_provider_call(
            f"{stage}_loop", config.MODEL_TEXT, "WARNING_CANDIDATE", None, 0,
            meta={
                "stage": stage,
                "iterations": result.iterations,
                "exit_reason": result.exit_reason,
                "artifact_id": result.artifact_id,
                "issue_codes": [issue.code for issue in result.issues[:10]],
            },
        )
    elif result.status == "baseline":
        object.__setattr__(result.value, "residual_errors", [issue.message for issue in result.issues])
        object.__setattr__(
            result.value, "residual_issues",
            [issue.model_dump(mode="json") for issue in result.issues],
        )
        object.__setattr__(result.value, "disposition", "BASELINE")
        object.__setattr__(result.value, "evidence_artifact_id", result.artifact_id)
        log_provider_call(
            f"{stage}_loop", config.MODEL_TEXT, "BASELINE_HANDOFF", None, 0,
            meta={
                "stage": stage,
                "iterations": result.iterations,
                "exit_reason": result.exit_reason,
                "artifact_id": result.artifact_id,
                "issue_codes": [issue.code for issue in result.issues[:10]],
                "call_role": "local_patch",
            },
        )
    elif result.status == "needs_replan":
        object.__setattr__(result.value, "residual_errors", [issue.message for issue in result.issues])
        object.__setattr__(
            result.value, "residual_issues",
            [issue.model_dump(mode="json") for issue in result.issues],
        )
        object.__setattr__(result.value, "disposition", "NEEDS_REPLAN")
        log_provider_call(
            f"{stage}_loop", config.MODEL_TEXT, "NEEDS_REPLAN", None, 0,
            meta={
                "stage": stage,
                "iterations": result.iterations,
                "exit_reason": result.exit_reason,
                "artifact_id": result.artifact_id,
                "issue_codes": [issue.code for issue in result.issues[:10]],
            },
        )
    else:
        object.__setattr__(result.value, "disposition", "PASS")
    object.__setattr__(result.value, "loop_exit_reason", result.exit_reason)
    object.__setattr__(result.value, "evidence_artifact_id", result.artifact_id)
    return result.value


# ---------- A. 角色圣经 ----------

BIBLE_HEAD_CHAPTERS = 20         # 首版人物谱发现窗口：只看前二十章；按一章一小请求并发
BIBLE_LOOKAHEAD_CHAPTERS = 0     # 发现窗口已扩到 60 章，不再额外扩大裁决卷宗范围
BIBLE_RECURRING_MIN_ONSTAGE_QUOTES = 2  # 至少两条经裁决闸核验的「本人在场」证据才算重要角色
# 且这些证据至少跨两个章节。只数条数分不出「跨章反复登场的人物」和「在某一章里连说
# 三句话的路人」：类别称谓（「绿袍男子」这种靠衣着指人、换个场合就指别人的说法）往往
# 在单章里就能凑满条数。人物谱的作用域是全书，进这份名单的判据也该是全书级的复现。
BIBLE_RECURRING_MIN_ONSTAGE_CHAPTERS = 2
# 全文统计通道门槛：命中量 + 章节覆盖率同时达标即可独立进入名单，不依赖模型裁决。
BIBLE_STATISTICAL_MIN_MENTIONS = 25
BIBLE_STATISTICAL_MIN_CHAPTER_RATIO = 0.15
# 真名替换门槛：真名必须至少和名单称呼一样常见，才有资格当人物谱主名。
# 旧值 0.2 会把「靠山老祖 1072 次 / 白主 344 次」改成主名「白主」，
# 更常用的原文称呼连 aliases 都进不去，检索直接落空。
BIBLE_FORMAL_NAME_MIN_RATIO = 1.0
BIBLE_MUST_COVER_MAX = 20        # 前 60 章重要角色容量；详情仍逐角色小请求生成
# 人数不设上限（见 roster_recurring._recurring_character_names）；这是失控护栏
# 不是质量门槛——真实作品不会触及，触发多半是资格裁决整体失效，会让下游详情
# 生成扇出成几百次调用。
BIBLE_ROSTER_RUNAWAY_MAX = 200
# 点名调用每个候选最多申报几条在场证据。判据只需要 BIBLE_RECURRING_MIN_ONSTAGE_QUOTES
# （=2）条核验通过的证据；这里留 1 条余量应付结构闸/裁决闸刷掉个别证据，不留更多——
# 多留的每一条对戏份多的主角都是纯浪费：一个出场上千次的主角，旧提示词「尽量都列出来」
# 会让模型老实列出十几条，既拉长点名调用本身的输出（更容易撞 max_tokens 截断，撞了就要
# 整次重试），又线性拉长下游裁决闸的调用条数。见 `_recurring_character_names` docstring。
BIBLE_ROLL_CALL_MAX_EVIDENCE_PER_CANDIDATE = 3
BIBLE_ROLL_CALL_CHUNK_CHAPTERS = 1
BIBLE_ROLL_CALL_CHUNK_INPUT_MAX_CHARS = 8000
BIBLE_ROLL_CALL_CHUNK_MAX_TOKENS = 12000
BIBLE_ROLL_CALL_CONCURRENCY = 6
BIBLE_ROLL_CALL_MAX_ATTEMPTS = 3
BIBLE_ROLL_CALL_TIMEOUT_S = 300.0
BIBLE_SMALL_VERDICT_TIMEOUT_S = 120.0
