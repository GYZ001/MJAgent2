"""单集剧本契约：EpisodeScreenplay 及其来自 episode_prep_pack 的身份透传资产。

normalize_screenplay_json_shape 修复模型输出里可机械识别的、无损的剧本结构
漂移（如 plot_spine 缺收尾括号导致后续字段被错误吞入），不做语义猜测。
"""
from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field

from .narrative_plan import NarrativeContinuityPlan
from .screenplay_outline import (
    InformationItem,
    KeyDialogueChain,
    PlotSpine,
    ScriptScene,
    SourceCoverageDecision,
    StoryEvent,
    VoiceCanonical,
)

class PrepPackCharacterAsset(BaseModel):
    """Identity triad carried verbatim from episode_prep_pack's
    asset_manifest.characters[] (screenplay contract 6.0.0+) into the legacy
    EpisodeScreenplay projection used by the storyboard stage.

    This is a lossless passthrough only -- no downstream prompt/consumption
    logic reads it yet (that is explicitly the next phase). Its purpose is to
    guarantee the projection layer cannot silently drop the character
    identity binding (visual_entity_id / portrait_id / display_appellation)
    that the screenplay stage already resolved.
    """

    identity_id: str = ""
    display_name: str = ""
    display_appellation: str = ""
    visual_entity_id: str = ""
    portrait_id: str | None = None
    event_ids: list[str] = Field(default_factory=list)


class PrepPackSceneAsset(BaseModel):
    """Scene identity carried verbatim from episode_prep_pack's
    asset_manifest.scenes[]; sibling of ``PrepPackCharacterAsset``."""

    scene_id: str = ""
    display_name: str = ""
    scene_reference_id: str | None = None
    event_ids: list[str] = Field(default_factory=list)


class EpisodeScreenplay(BaseModel):
    model_config = ConfigDict(extra="forbid")

    episode_no: int
    # 完整剧本源数据（新格式）
    id: str | None = None
    mode: str = "full_script"
    title: str = ""
    source_text_range: str = ""
    logline: str = ""
    script_format_note: str = ""
    # 单集戏剧契约（对齐调研文档 §3.4/§3.5）：用于把"故事为什么发生、主角要什么、阻力与代价"
    # 显式锁定，避免压缩成 50s 时把方向性信息一起丢掉。
    dramatic_question: str = ""      # 本集观众心里追问的那个问题（§3.4）
    protagonist_goal: str = ""       # 主角本集外在目标（看得见、可完成）（§3.5）
    obstacle: str = ""               # 外部+内部阻力（§3.5）
    stakes: str = ""                 # 失败代价/成功代价（§3.5）
    # 主线台词/剧情点（Renderability First）：只保留推动 spine 的内容，禁止全量原文台词入库。
    key_lines: list[str] = Field(default_factory=list)        # 由 dialogue_chains 按话轮顺序确定性派生
    # 新生成合同：结构化对白链是 key_lines 的权威来源；key_lines 由后端按 turns 顺序回填。
    dialogue_chains: list[KeyDialogueChain] = Field(default_factory=list)
    key_plot_points: list[str] = Field(default_factory=list)  # 与 spine 对齐的局势变化
    plot_spine: PlotSpine | None = None
    source_coverage: list[SourceCoverageDecision] = Field(default_factory=list)
    scene_outline: list[ScriptScene] = Field(default_factory=list)
    full_script_text: str = ""
    character_state_changes: list[str] = Field(default_factory=list)
    emotional_curve: str = ""
    ending_hook: str = ""
    source_basis: str = ""
    adaptation_direction: str = ""
    opening: str = ""
    development: str = ""
    conflict: str = ""
    climax: str = ""
    # PRD：信息唯一归属契约（下游只消费结构化事件/台账，不把散文剧本当公共上下文）
    episode_premise: str = ""
    events: list[StoryEvent] = Field(default_factory=list)
    information_ledger: list[InformationItem] = Field(default_factory=list)
    voice_bible: list[VoiceCanonical] = Field(default_factory=list)
    approved_adaptations: list[str] = Field(default_factory=list)
    forbidden_additions: list[str] = Field(default_factory=list)
    # One authoritative graph shared by screenplay, storyboard and blind review.
    # Optional only so legacy published artifacts remain readable; every newly
    # generated artifact is hard-gated by app.narrative.validate_*.
    narrative_plan: NarrativeContinuityPlan | None = None
    # episode_prep_pack (screenplay contract 6.0.0+) identity passthrough --
    # populated only by app.production.screenplay_authority's projection of
    # an episode_prep_pack payload into this legacy shape; empty for every
    # screenplay produced by the retired heavy blueprint pipeline.
    prep_pack_character_assets: list[PrepPackCharacterAsset] = Field(default_factory=list)
    prep_pack_scene_assets: list[PrepPackSceneAsset] = Field(default_factory=list)
    created_at: float | None = None
    updated_at: float | None = None


def normalize_screenplay_json_shape(obj: dict) -> tuple[dict, list[str]]:
    """Repair lossless, mechanically recognizable screenplay shape drift.

    A missing close brace after ``plot_spine.drop_list`` leaves all following
    root fields inside ``plot_spine``.  The payload can become syntactically
    valid after its final brace is closed, while Pydantic would silently ignore
    those misplaced extras.  Only fields declared by ``EpisodeScreenplay`` and
    not declared by ``PlotSpine`` are eligible; explicit root values win.

    Some providers also serialize free-form familiarity assumptions as strings
    even though the contract requires objects.  Preserve the text under a
    descriptive key instead of spending another full screenplay generation on
    a schema-only correction.
    """
    normalized = dict(obj)
    changes: list[str] = []
    spine = obj.get("plot_spine")
    if isinstance(spine, dict):
        misplaced = [
            key
            for key in spine
            if key in EpisodeScreenplay.model_fields and key not in PlotSpine.model_fields
        ]
        if misplaced:
            normalized_spine = dict(spine)
            for key in misplaced:
                value = normalized_spine.pop(key)
                normalized.setdefault(key, value)
            normalized["plot_spine"] = normalized_spine
            changes.extend(misplaced)

    plan = normalized.get("narrative_plan")
    if isinstance(plan, dict):
        priors = plan.get("audience_priors")
        if isinstance(priors, list):
            normalized_priors: list[object] = []
            priors_changed = False
            for prior_index, prior in enumerate(priors):
                if not isinstance(prior, dict):
                    normalized_priors.append(prior)
                    continue
                assumptions = prior.get("familiarity_assumptions")
                if not isinstance(assumptions, list):
                    normalized_priors.append(prior)
                    continue
                normalized_assumptions: list[object] = []
                prior_changed = False
                for assumption_index, assumption in enumerate(assumptions):
                    if isinstance(assumption, str) and assumption.strip():
                        normalized_assumptions.append({
                            "description": assumption.strip(),
                        })
                        changes.append(
                            "narrative_plan.audience_priors"
                            f"[{prior_index}].familiarity_assumptions"
                            f"[{assumption_index}]"
                        )
                        prior_changed = True
                    else:
                        normalized_assumptions.append(assumption)
                if prior_changed:
                    normalized_prior = dict(prior)
                    normalized_prior["familiarity_assumptions"] = normalized_assumptions
                    normalized_priors.append(normalized_prior)
                    priors_changed = True
                else:
                    normalized_priors.append(prior)
            if priors_changed:
                normalized_plan = dict(plan)
                normalized_plan["audience_priors"] = normalized_priors
                normalized["narrative_plan"] = normalized_plan

        current_plan = normalized.get("narrative_plan")
        audits = (
            current_plan.get("action_relation_audits")
            if isinstance(current_plan, dict)
            else None
        )
        if isinstance(audits, list):
            normalized_audits: list[object] = []
            audits_changed = False
            for audit_index, audit in enumerate(audits):
                if not isinstance(audit, dict):
                    normalized_audits.append(audit)
                    continue
                normalized_audit = dict(audit)
                if (
                    "action_relation_audit_id" not in normalized_audit
                    and normalized_audit.get("audit_id")
                ):
                    normalized_audit["action_relation_audit_id"] = normalized_audit.pop(
                        "audit_id"
                    )
                    changes.append(
                        "narrative_plan.action_relation_audits"
                        f"[{audit_index}].action_relation_audit_id"
                    )
                relation = str(normalized_audit.get("relation") or "").strip().lower()
                if "semantically_equivalent" not in normalized_audit:
                    if relation in {"sequential_distinct", "distinct", "different"}:
                        normalized_audit["semantically_equivalent"] = False
                    elif relation in {"equivalent", "duplicate", "semantic_equivalent"}:
                        normalized_audit["semantically_equivalent"] = True
                    if "semantically_equivalent" in normalized_audit:
                        changes.append(
                            "narrative_plan.action_relation_audits"
                            f"[{audit_index}].semantically_equivalent"
                        )
                if "reason" not in normalized_audit and normalized_audit.get("rationale"):
                    normalized_audit["reason"] = normalized_audit.pop("rationale")
                    changes.append(
                        "narrative_plan.action_relation_audits"
                        f"[{audit_index}].reason"
                    )
                if normalized_audit != audit:
                    audits_changed = True
                normalized_audits.append(normalized_audit)
            if audits_changed:
                normalized_plan = dict(current_plan)
                normalized_plan["action_relation_audits"] = normalized_audits
                normalized["narrative_plan"] = normalized_plan

        current_plan = normalized.get("narrative_plan")
        audience_states = (
            current_plan.get("audience_states")
            if isinstance(current_plan, dict)
            else None
        )
        if isinstance(audience_states, list):
            normalized_states: list[object] = []
            states_changed = False
            for state_index, state in enumerate(audience_states):
                if not isinstance(state, dict):
                    normalized_states.append(state)
                    continue
                working_memory = state.get("working_memory")
                if not isinstance(working_memory, list):
                    normalized_states.append(state)
                    continue
                normalized_memory: list[object] = []
                state_changed = False
                for memory_index, memory in enumerate(working_memory):
                    if isinstance(memory, str) and memory.strip():
                        normalized_memory.append({
                            "proposition_id": memory.strip(),
                            "retention_confidence": 1.0,
                        })
                        changes.append(
                            "narrative_plan.audience_states"
                            f"[{state_index}].working_memory[{memory_index}]"
                        )
                        state_changed = True
                    else:
                        normalized_memory.append(memory)
                if state_changed:
                    normalized_state = dict(state)
                    normalized_state["working_memory"] = normalized_memory
                    normalized_states.append(normalized_state)
                    states_changed = True
                else:
                    normalized_states.append(state)
            if states_changed:
                normalized_plan = dict(current_plan)
                normalized_plan["audience_states"] = normalized_states
                normalized["narrative_plan"] = normalized_plan

        current_plan = normalized.get("narrative_plan")
        intents = (
            current_plan.get("experience_intents")
            if isinstance(current_plan, dict)
            else None
        )
        if isinstance(intents, list):
            normalized_intents: list[object] = []
            intents_changed = False
            for intent_index, intent in enumerate(intents):
                if not isinstance(intent, dict):
                    normalized_intents.append(intent)
                    continue
                withheld = intent.get("withheld_propositions")
                if not isinstance(withheld, list):
                    normalized_intents.append(intent)
                    continue
                normalized_withheld: list[object] = []
                intent_changed = False
                for withheld_index, item in enumerate(withheld):
                    if isinstance(item, str) and item.strip():
                        normalized_withheld.append({
                            "proposition_id": item.strip(),
                            "reason": "",
                        })
                        changes.append(
                            "narrative_plan.experience_intents"
                            f"[{intent_index}].withheld_propositions"
                            f"[{withheld_index}]"
                        )
                        intent_changed = True
                    elif isinstance(item, dict) and "reason" not in item:
                        normalized_withheld.append({**item, "reason": ""})
                        changes.append(
                            "narrative_plan.experience_intents"
                            f"[{intent_index}].withheld_propositions"
                            f"[{withheld_index}].reason"
                        )
                        intent_changed = True
                    else:
                        normalized_withheld.append(item)
                if intent_changed:
                    normalized_intent = dict(intent)
                    normalized_intent["withheld_propositions"] = normalized_withheld
                    normalized_intents.append(normalized_intent)
                    intents_changed = True
                else:
                    normalized_intents.append(intent)
            if intents_changed:
                normalized_plan = dict(current_plan)
                normalized_plan["experience_intents"] = normalized_intents
                normalized["narrative_plan"] = normalized_plan

    coverage = normalized.get("source_coverage")
    if isinstance(coverage, list):
        normalized_coverage: list[object] = []
        coverage_changed = False
        allowed_dispositions = {
            "deliver", "merge", "context", "duplicate", "audit_only",
        }
        merged_list_pattern = re.compile(
            r"^(?P<disposition>[a-z]+)\s*[,，;；]\s*"
            r"(?P<field>[A-Za-z_][A-Za-z0-9_]*)\s*:\s*"
            r"\[(?P<items>[^\]]*)\]\s*$"
        )
        for coverage_index, item in enumerate(coverage):
            if not isinstance(item, dict):
                normalized_coverage.append(item)
                continue
            disposition = str(item.get("disposition") or "").strip()
            match = merged_list_pattern.fullmatch(disposition)
            if match is None or match.group("disposition") not in allowed_dispositions:
                normalized_coverage.append(item)
                continue
            sibling_field = match.group("field")
            if sibling_field not in SourceCoverageDecision.model_fields:
                normalized_coverage.append(item)
                continue
            sibling_value = item.get(sibling_field)
            merged_items = [
                value.strip().strip("\"'")
                for value in match.group("items").split(",")
                if value.strip()
            ]
            if (
                sibling_value is not None
                and (
                    not isinstance(sibling_value, list)
                    or not all(isinstance(value, str) for value in sibling_value)
                    or merged_items != sibling_value
                )
            ):
                normalized_coverage.append(item)
                continue
            normalized_item = dict(item)
            normalized_item["disposition"] = match.group("disposition")
            if sibling_value is None:
                normalized_item[sibling_field] = merged_items
            normalized_coverage.append(normalized_item)
            coverage_changed = True
            changes.append(f"source_coverage[{coverage_index}].disposition")
        if coverage_changed:
            normalized["source_coverage"] = normalized_coverage

    return normalized, changes
