"""LLM 输出合同（PRD 原则 P5：一切 LLM 输出有 Schema）。对应 docs/PROMPT_SPEC.md。"""
from __future__ import annotations

import json
import re

from typing import Any, Literal, TypeVar

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

SHOT_SIZES = {"远景", "全景", "中景", "近景", "特写"}
CAMERA_MOVES = {"固定", "推近", "拉远", "横摇", "跟随"}
TRANSITIONS = {
    "硬切",
    "叠化",
    "淡出淡入",
    "黑场",
    "闪黑",
    "闪白",
    "甩镜",
    "遮挡转场",
    "匹配剪辑",
    "声音延续+叠化",
    "声音先行+淡入",
}
EMOTIONS = {"平静", "愤怒", "悲伤", "惊恐", "喜悦", "讥讽", "坚定"}

# 镜头连续性模式；除 scene_change 外均使用上一条采用视频的真实尾帧作首帧。
CONTINUITY_MODES = {
    "action_continuation",  # 同一人物同一动作跨镜延续
    "same_scene_cut",       # 同场景换景别/构图
    "reaction_cut",         # 切到另一人物或人群反应
    "reverse_angle",        # 正反打
    "insert_detail",        # 道具/手部/文字特写
    "scene_change",         # 时间或地点改变
}

DELIVERY_OWNERS = {
    "visual_action",
    "spoken_dialogue",
    "offscreen_voice",
    "narration",
    "on_screen_text",
    "ambient_sound",
}

AUDIO_TIMELINE_TYPES = {
    "spoken_dialogue",
    "offscreen_voice",
    "narration",
    "ambient_sound",
}

PROMPT_CONTRACT_VERSION = "video_cinematic_continuity_v6"
NARRATIVE_CONTRACT_VERSION = "narrative-continuity.v2"

# 主线节拍 ID（S*）与剧本事件 ID（E*）长得像但语义不同，历史数据把 S07 写进了 story_event_id。
# 这两个正则是四类 ID 分离（PRD VAL-422 §4.4.1）的判定底座。
SPINE_BEAT_ID_RE = re.compile(r"^S\d{1,3}$", re.I)
STORY_EVENT_ID_RE = re.compile(r"^E\d{1,3}(?:\.\d{1,3})?$", re.I)
KEY_LINE_ID_RE = re.compile(r"^KL\d{1,3}$", re.I)

_IdCarrier = TypeVar("_IdCarrier", bound=BaseModel)


def _normalize_information_ids(model: _IdCarrier) -> _IdCarrier:
    """把 `new_information_ids` 归一到 `information_ids`，两侧保持同一份内容。

    PRD §7.1 要求兼容旧字段一个版本周期，但内部立即以 `information_ids` 为准。
    双向同步而不是单向覆盖，才能让仍在读旧字段的调用方继续正确工作。
    """
    new_ids = [str(x).strip() for x in (model.new_information_ids or []) if str(x).strip()]
    info_ids = [str(x).strip() for x in (model.information_ids or []) if str(x).strip()]
    merged = list(dict.fromkeys([*info_ids, *new_ids]))
    if merged != info_ids:
        model.information_ids = merged
    if merged != new_ids:
        model.new_information_ids = merged
    return model


class Relationship(BaseModel):
    to: str
    relation: str


class Character(BaseModel):
    name: str
    role: str
    appearance_canonical: str
    personality: str = ""
    speech_style: str = ""
    relationships: list[Relationship] = Field(default_factory=list)
    # 定妆照（圣经定稿后由 Seedream 生成，跨集一致性的视觉锚点；LLM 输出中不含以下字段）
    ref_image_path: str | None = None
    # 画像描述覆盖：人工编辑的定妆照生成词；为空时用 锚点串+画风 合成的默认描述（refs.portrait_prompt）
    portrait_prompt_override: str | None = None


class World(BaseModel):
    era: str = ""
    genre: str = ""
    visual_style_canonical: str


class Scene(BaseModel):
    """规范场景（场景图素材库的一条）：跨集场景一致性的视觉锚点（与 Character 同构）。
    name 是稳定短标签（如"宗门广场"），分镜的 scene_setting 收敛到它；scene_canonical 是
    固定场景锚点串（地点/时间/光线/陈设/氛围）；ref_image_path 是 Seedream 生成的定场图。"""

    name: str
    scene_canonical: str
    location_kind: str = ""        # 室内/室外/其他（可选，仅作分类提示）
    # 场景图（圣经定稿后由 Seedream 生成，跨集复用的环境锚点；LLM 输出中不含以下字段）
    ref_image_path: str | None = None
    # 场景图生成词覆盖：人工编辑值；为空时用 锚点串+画风 合成的默认描述（scenes.scene_ref_prompt）
    scene_prompt_override: str | None = None
    # 渐进式结构化锚点与自动发现血缘；旧数据缺字段时保持兼容。
    space: str = ""
    time_of_day: str = ""
    lighting: str = ""
    landmarks: list[str] = Field(default_factory=list)
    first_episode: int | None = None
    required_views: list[str] = Field(default_factory=list)
    discovery_sources: list[str] = Field(default_factory=list)
    # 剧本场次标题可能使用同一地点的简称/旧称。别名只用于把剧本地点稳定解析到
    # 同一规范场景，避免为了一个称谓差异重复建场景或误借其它场景图。
    aliases: list[str] = Field(default_factory=list)
    # 待审状态变化获批后先记录目标锚点和生效集；完成费用确认与整包重绘后才转为正式锚点。
    pending_state_canonical: str | None = None
    pending_state_ep_start: int | None = None


class Bible(BaseModel):
    characters: list[Character]
    world: World
    scenes: list[Scene] = Field(default_factory=list)


class ScriptScene(BaseModel):
    scene_no: int
    scene_heading: str
    story_function: str
    characters: list[str] = Field(default_factory=list)
    summary: str
    conflict: str = ""
    turn: str = ""
    source_basis: str = ""
    previous_scene_exit_state: str = ""
    opening_image: str = ""
    agency_contracts: list[dict[str, str]] = Field(default_factory=list)
    entry_state: str = ""
    exit_state: str = ""
    context_requirements: list[str] = Field(default_factory=list)


class StoryEvent(BaseModel):
    """剧情事件台账：描述一次可见/可听的状态变化，而非原文摘要。"""

    event_id: str
    source_span: str = ""
    source_fact: str = ""
    state_in: str = ""
    trigger: str = ""
    visible_change: str = ""
    state_out: str = ""
    must_keep: bool = True
    narrative_layer: Literal["story", "paratext"] = "story"
    event_priority: Literal["causal", "supporting", "connective"] = "causal"
    render_policy: Literal["standalone", "merge_adjacent", "exclude_from_spine"] = "standalone"
    adaptation_addition: bool = False
    adaptation_reason: str = ""
    approved: bool = False


class InformationItem(BaseModel):
    """信息交付台账：每项观众必须获得的信息只能有一个主交付镜头。"""

    info_id: str
    event_id: str = ""
    content: str = ""
    delivery_owner: str = "visual_action"
    speaker_id: str | None = None
    exact_text: str | None = None
    reinforcement_allowed: bool = False
    status: str = "unassigned"  # unassigned | assigned | delivered
    assigned_shot_no: int | None = None


class VoiceCanonical(BaseModel):
    speaker_id: str
    voice_canonical: str
    language: str = "普通话"
    role_type: str = "named_character"  # named_character | functional_character | narrator


class PlotSpineBeat(BaseModel):
    """主线骨架节拍：谁做了什么 → 局势变化（Renderability First）。"""

    beat_id: str
    who: str = ""
    does: str = ""
    turn: str = ""
    must_keep: bool = True
    narrative_layer: Literal["story", "paratext"] = "story"
    event_priority: Literal["causal", "supporting", "connective"] = "causal"
    render_policy: Literal["standalone", "merge_adjacent", "exclude_from_spine"] = "standalone"
    source_segment_ids: list[str] = Field(default_factory=list)
    purpose: str = ""
    # VAL-422 §4.4.3：可选绑定信息原子/关键台词；跨镜聚合校验时按这些 ID 核对交付。
    information_ids: list[str] = Field(default_factory=list)
    key_line_ids: list[str] = Field(default_factory=list)


class PlotSpine(BaseModel):
    """嵌入剧本输出的主线骨架：只保改变局势的事件，显式列出不拍内容。"""

    episode_premise: str = ""
    spine_beats: list[PlotSpineBeat] = Field(default_factory=list)
    must_keep_ending: str = ""
    drop_list: list[str] = Field(default_factory=list)


class SourceCoverageDecision(BaseModel):
    """One explicit disposition for a deterministically indexed source segment."""

    source_segment_id: str
    disposition: Literal["deliver", "merge", "context", "duplicate"]
    beat_ids: list[str] = Field(default_factory=list)
    duplicate_of: str | None = None
    reason: str = ""

    @model_validator(mode="after")
    def _validate_disposition(self) -> "SourceCoverageDecision":
        if not self.source_segment_id.strip():
            raise ValueError("source_segment_id 不能为空")
        if self.disposition in {"deliver", "merge"} and not self.beat_ids:
            raise ValueError("deliver/merge 必须绑定至少一个 beat_id")
        if self.disposition == "duplicate" and not (self.duplicate_of or "").strip():
            raise ValueError("duplicate 必须指向 duplicate_of")
        if self.disposition in {"context", "duplicate"} and len(self.reason.strip()) < 4:
            raise ValueError("context/duplicate 必须说明保留方式或重复依据")
        return self


class KeyDialogueTurn(BaseModel):
    """One source-grounded turn in an ordered main-dialogue chain."""

    speaker: str = ""
    line: str = ""
    function: str = "statement"  # trigger|announcement|question|response|decision|statement
    source_text: str = ""         # exact source utterance used as adaptation evidence


class KeyDialogueChain(BaseModel):
    """A trigger and its dependent replies; downstream must preserve the whole chain."""

    chain_id: str = ""
    topic: str = ""
    turns: list[KeyDialogueTurn] = Field(default_factory=list)


# ---------- Unified narrative-continuity contract ----------
#
# These models deliberately describe relations and observable deltas instead of
# genre/story keyword lists.  The LLM may use ``other``/free semantic fields for
# concepts that the contract did not anticipate; deterministic validation only
# checks identity, provenance, ownership and state hand-offs.


class NarrativeAnchor(BaseModel):
    type: str
    id: str


class SourceSpan(BaseModel):
    chapter_id: str = ""
    start: int = 0
    end: int = 0


class SourceEvidence(BaseModel):
    source_evidence_id: str
    source_span: SourceSpan = Field(default_factory=SourceSpan)
    verbatim_excerpt: str
    confidence: float = 1.0


class NarrativeProposition(BaseModel):
    proposition_id: str
    semantic_identity_key: str = ""
    canonical_statement: str
    narrative_domain: str  # source_canon | adapted_story
    entity_ids: list[str] = Field(default_factory=list)
    direct_source_evidence_ids: list[str] = Field(default_factory=list)
    domain_truth_status: str = "true"


class AdaptationDecision(BaseModel):
    adaptation_decision_id: str
    source_proposition_ids: list[str] = Field(default_factory=list)
    adapted_proposition_ids: list[str] = Field(default_factory=list)
    relation: str = "preserve"
    custom_relation: str | None = None
    creative_reason: str = ""
    protected_causal_effect_ids: list[str] = Field(default_factory=list)
    affected_event_ids: list[str] = Field(default_factory=list)
    uncertainty: str | None = None


class StateFactValue(BaseModel):
    kind: str = "text"
    data: object = ""


class StateFact(BaseModel):
    fact_id: str
    proposition_id: str
    subject_id: str
    predicate_id: str
    value: StateFactValue = Field(default_factory=StateFactValue)
    time_scope: str = ""
    visibility: str = "unknown"
    provenance: str = "screenplay"
    confidence: float = 1.0


class NarrativeEvidence(BaseModel):
    evidence_id: str
    anchor: NarrativeAnchor
    observable_claim: str
    perceivable_by: list[str] = Field(default_factory=list)
    supports_proposition_ids: list[str] = Field(default_factory=list)
    planned_salience: float = 0.0
    planned_duration_s: float | None = None
    competing_attention_ids: list[str] = Field(default_factory=list)


class DramaticQuestion(BaseModel):
    dramatic_question_id: str
    question_text: str
    target_proposition_ids: list[str] = Field(default_factory=list)
    open_anchor: NarrativeAnchor
    intended_resolution_scope_id: str = ""
    desired_state_while_open: str = "unknown"
    resolution_anchor: NarrativeAnchor | None = None
    status: str = "open"


class AtomicActionPhase(BaseModel):
    phase_id: str
    start_condition: str = ""
    end_condition: str = ""
    estimated_min_s: float = 0.0


class ActionParticipantDelivery(BaseModel):
    """Structured proof that one offscreen action participant reaches viewers."""

    action_id: str
    participant_id: str
    evidence_ids: list[str] = Field(default_factory=list)
    audible: bool = False
    visible_effect: bool = False
    visible_reaction: bool = False

    @property
    def is_perceivable(self) -> bool:
        return self.audible or self.visible_effect or self.visible_reaction


class ActionAgency(BaseModel):
    """Open semantic agency plus machine-checkable identity/source provenance."""

    kind: str = "character"
    identity_bearing: bool = True
    source_segment_ids: list[str] = Field(default_factory=list)

    @field_validator("kind", mode="before")
    @classmethod
    def _normalize_kind(cls, value: object) -> str:
        return str(value or "").strip() or "unattributed"

    @property
    def is_character_agency(self) -> bool:
        return self.kind == "character" or self.kind.startswith("character_")

    @model_validator(mode="after")
    def _validate_character_identity_bearing(self) -> "ActionAgency":
        if self.is_character_agency and not self.identity_bearing:
            raise ValueError(
                "character action_agency 必须声明 identity_bearing=true"
            )
        return self


class TextProvenance(BaseModel):
    """Compiler-owned attribution for authored text and its frozen sources."""

    model_config = ConfigDict(extra="forbid")

    kind: str = "creative_action"
    identity_keys: list[str] = Field(default_factory=list)
    source_segment_ids: list[str] = Field(default_factory=list)

    @field_validator("kind", mode="before")
    @classmethod
    def _normalize_kind(cls, value: object) -> str:
        return str(value or "").strip() or "creative_action"

    @field_validator("identity_keys", "source_segment_ids", mode="before")
    @classmethod
    def _normalize_keys(cls, value: object) -> list[str]:
        values = value if isinstance(value, (list, tuple, set)) else [value]
        return list(dict.fromkeys(
            normalized
            for item in values
            if (normalized := str(item or "").strip())
        ))


class AtomicAction(BaseModel):
    action_id: str
    actor_ids: list[str] = Field(default_factory=list)
    target_ids: list[str] = Field(default_factory=list)
    action_agency: ActionAgency
    text_provenance: TextProvenance
    dialogue_text: str = ""
    required_text: str = ""
    prop_text: str = ""
    on_screen_text: str = ""
    participant_deliveries: list[ActionParticipantDelivery] = Field(
        default_factory=list
    )
    semantic_intent: str
    precondition_fact_ids: list[str] = Field(default_factory=list)
    effects_add: list[str] = Field(default_factory=list)
    effects_remove: list[str] = Field(default_factory=list)
    completion_condition: str
    decision_requirement: str = "applies"
    decision_not_applicable_reason: str | None = None
    temporal_phases: list[AtomicActionPhase] = Field(default_factory=list)
    splittable_boundaries: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _derive_missing_action_agency(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        actor_ids = list(normalized.get("actor_ids") or [])
        target_ids = list(normalized.get("target_ids") or [])
        identity_bearing = bool(actor_ids or target_ids)
        if normalized.get("action_agency") is None:
            normalized["action_agency"] = {
                "kind": "character" if identity_bearing else "unattributed",
                "identity_bearing": identity_bearing,
                "source_segment_ids": list(
                    normalized.get("source_segment_ids") or []
                ),
            }
        if normalized.get("text_provenance") is None:
            agency = normalized["action_agency"]
            source_segment_ids = (
                list(agency.source_segment_ids)
                if isinstance(agency, ActionAgency)
                else list(agency.get("source_segment_ids") or [])
            )
            if str(normalized.get("dialogue_text") or "").strip():
                provenance_kind = "dialogue"
            elif str(normalized.get("required_text") or "").strip():
                provenance_kind = "required_text"
            elif str(normalized.get("prop_text") or "").strip():
                provenance_kind = "prop_text"
            elif str(normalized.get("on_screen_text") or "").strip():
                provenance_kind = "on_screen_text"
            else:
                provenance_kind = "creative_action"
            normalized["text_provenance"] = {
                "kind": provenance_kind,
                "identity_keys": (
                    []
                    if provenance_kind not in ("creative_action", "dialogue")
                    else list(dict.fromkeys([*actor_ids, *target_ids]))
                ),
                "source_segment_ids": source_segment_ids,
            }
        return normalized

    @model_validator(mode="after")
    def _validate_action_agency_owner(self) -> "AtomicAction":
        identity_bearing = bool(self.actor_ids or self.target_ids)
        if self.action_agency.identity_bearing != identity_bearing:
            raise ValueError(
                "action_agency.identity_bearing 必须与 actor_ids/target_ids 等价"
            )
        if self.action_agency.is_character_agency and not identity_bearing:
            raise ValueError(
                "character action_agency 必须由 actor_ids/target_ids 承载"
            )
        explicit_text_kinds = [
            kind
            for kind, content in (
                ("dialogue", self.dialogue_text),
                ("required_text", self.required_text),
                ("prop_text", self.prop_text),
                ("on_screen_text", self.on_screen_text),
            )
            if content.strip()
        ]
        if len(explicit_text_kinds) > 1:
            raise ValueError(
                "dialogue/required_text/prop_text/on_screen_text "
                "每个 action 最多声明一种"
            )
        expected_provenance_kind = (
            explicit_text_kinds[0]
            if explicit_text_kinds
            else "creative_action"
        )
        expected_identity_keys = (
            []
            if expected_provenance_kind in (
                "required_text", "prop_text", "on_screen_text",
            )
            else list(dict.fromkeys([*self.actor_ids, *self.target_ids]))
        )
        if self.text_provenance.kind != expected_provenance_kind:
            raise ValueError(
                "text_provenance.kind 必须由显式文字结构字段确定"
            )
        if self.text_provenance.identity_keys != expected_identity_keys:
            raise ValueError(
                "text_provenance.identity_keys 必须由 actor_ids/target_ids 确定"
            )
        if (
            self.text_provenance.source_segment_ids
            != self.action_agency.source_segment_ids
        ):
            raise ValueError(
                "text_provenance.source_segment_ids 必须与 action agency 来源等价"
            )
        return self


class ActionSemanticRelationAudit(BaseModel):
    """AI semantic comparison for actions that may repeat across different IDs."""

    action_relation_audit_id: str
    action_ids: list[str] = Field(default_factory=list)
    semantically_equivalent: bool
    functional_repeat: bool | None = None
    added_target_delta_ids: list[str] = Field(default_factory=list)
    added_character_state_ids: list[str] = Field(default_factory=list)
    added_evidence_ids: list[str] = Field(default_factory=list)
    causal_basis_event_ids: list[str] = Field(default_factory=list)
    decision: str = "needs_review"
    reason: str = ""


class NarrativeEvent(BaseModel):
    event_id: str
    proposition_ids: list[str] = Field(default_factory=list)
    causal_parent_ids: list[str] = Field(default_factory=list)
    precondition_fact_ids: list[str] = Field(default_factory=list)
    action_ids: list[str] = Field(default_factory=list)
    # Identities that are physically present and may be rendered while this
    # event is delivered.  This is deliberately distinct from
    # NarrativeEvidence.perceivable_by: an observer, addressee, or person
    # mentioned in dialogue is not thereby a visual subject.
    onscreen_entity_ids: list[str] = Field(default_factory=list)
    effects_add: list[str] = Field(default_factory=list)
    effects_remove: list[str] = Field(default_factory=list)
    character_goal_effects: list[dict] = Field(default_factory=list)
    downstream_dependency_event_ids: list[str] = Field(default_factory=list)
    salience: float = 0.0
    irreversibility: float = 0.0
    must_keep: bool = True
    narrative_layer: Literal["story", "paratext"] = "story"
    event_priority: Literal["causal", "supporting", "connective"] = "causal"
    render_policy: Literal["standalone", "merge_adjacent", "exclude_from_spine"] = "standalone"
    delivery_scope_id: str = "episode"
    delivery_policy: str = "deliver"
    primary_delivery_window_id: str | None = None


class CharacterDramaticState(BaseModel):
    character_state_id: str
    character_id: str
    anchor: NarrativeAnchor
    goal_proposition_ids: list[str] = Field(default_factory=list)
    stakes_proposition_ids: list[str] = Field(default_factory=list)
    relationship_state: dict = Field(default_factory=dict)
    emotion: dict = Field(default_factory=dict)
    pressure: float = 0.0
    tactic: str = ""


class BeliefItem(BaseModel):
    proposition_id: str
    stance: str = "unknown"
    confidence: float = 0.0
    evidence_ids: list[str] = Field(default_factory=list)


class CharacterBeliefSnapshot(BaseModel):
    character_belief_id: str
    character_id: str
    anchor: NarrativeAnchor
    perceived_evidence_ids: list[str] = Field(default_factory=list)
    beliefs: list[BeliefItem] = Field(default_factory=list)
    misbelief_proposition_ids: list[str] = Field(default_factory=list)
    decision_proposition_ids: list[str] = Field(default_factory=list)
    decision_basis_ids: list[str] = Field(default_factory=list)
    decision_action_ids: list[str] = Field(default_factory=list)


class AudienceStateSnapshot(BaseModel):
    audience_state_id: str
    audience_prior_id: str
    anchor: NarrativeAnchor
    beliefs: list[BeliefItem] = Field(default_factory=list)
    causal_hypotheses: list[dict | str] = Field(default_factory=list)
    character_goal_hypotheses: dict = Field(default_factory=dict)
    spatial_model: dict = Field(default_factory=dict)
    temporal_model: dict = Field(default_factory=dict)
    active_question_ids: list[str] = Field(default_factory=list)
    working_memory: list[dict] = Field(default_factory=list)
    attention_residue_ids: list[str] = Field(default_factory=list)
    affective_state: dict = Field(default_factory=dict)


class AudiencePriorContract(BaseModel):
    audience_prior_id: str
    scope_id: str = "episode"
    audience_description: str
    assumed_known_proposition_ids: list[str] = Field(default_factory=list)
    assumed_unknown_proposition_ids: list[str] = Field(default_factory=list)
    familiarity_assumptions: list[dict] = Field(default_factory=list)
    language_and_context_assumptions: list[str] = Field(default_factory=list)
    attention_memory_assumptions: dict = Field(default_factory=dict)
    calibration_source: str = "needs_review"


class TargetDelta(BaseModel):
    target_delta_id: str
    dimension: str
    proposition_ids: list[str] = Field(default_factory=list)
    description: str
    from_state: dict = Field(default_factory=dict)
    to_state: dict = Field(default_factory=dict)
    target_confidence: float | None = None
    required_processing_s: float = 0.0
    deadline_event_id: str
    primary_delivery_window_id: str | None = None
    custom_dimension: str | None = None


class AudiencePath(BaseModel):
    audience_path_id: str
    audience_prior_id: str
    audience_state_in_id: str
    audience_state_out_target_id: str
    target_deltas: list[TargetDelta] = Field(default_factory=list)


class WithheldProposition(BaseModel):
    proposition_id: str
    reason: str
    future_disclosure_anchor: NarrativeAnchor | None = None
    carried_question_id: str | None = None


class ExperienceIntent(BaseModel):
    experience_intent_id: str
    scope_id: str
    anchor_event_ids: list[str] = Field(default_factory=list)
    director_objective: str
    attention_target_ids: list[str] = Field(default_factory=list)
    audience_paths: list[AudiencePath] = Field(default_factory=list)
    withheld_propositions: list[WithheldProposition] = Field(default_factory=list)
    forbidden_misconceptions: list[str] = Field(default_factory=list)


class AssimilationTask(BaseModel):
    assimilation_task_id: str
    experience_intent_id: str
    audience_path_id: str
    target_delta_id: str
    required_prior_proposition_ids: list[str] = Field(default_factory=list)
    downstream_dependency_event_ids: list[str] = Field(default_factory=list)
    satisfaction_criteria: str
    status: str = "open"


class ShotContribution(BaseModel):
    shot_contribution_id: str
    experience_intent_ids: list[str] = Field(default_factory=list)
    target_delta_ids: list[str] = Field(default_factory=list)
    assimilation_task_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    story_delta_fact_ids: list[str] = Field(default_factory=list)
    character_state_delta_ids: list[str] = Field(default_factory=list)
    audience_state_delta_ids: list[str] = Field(default_factory=list)
    affective_delta: dict = Field(default_factory=dict)
    spatial_temporal_delta: dict = Field(default_factory=dict)
    dramatic_pressure_delta: float = 0.0


class ShotCapacityBudget(BaseModel):
    """Joint single-shot time budget proposed by AI and relation-checked.

    The dimensions describe viewing work, not story categories.  Deterministic
    validation derives lower bounds from action phases, spoken/on-screen text,
    evidence and target deltas, then verifies the joint total against the shot.
    """

    action_phase_s: float = 0.0
    spoken_and_text_s: float = 0.0
    attention_switch_s: float = 0.0
    inference_processing_s: float = 0.0
    reaction_registration_s: float = 0.0
    spatial_reorientation_s: float = 0.0
    entry_exit_settle_s: float = 0.0
    other_s: float = 0.0
    other_reason: str | None = None


class ReadabilityWindow(BaseModel):
    readability_window_id: str
    event_ids: list[str] = Field(default_factory=list)
    proposition_ids: list[str] = Field(default_factory=list)
    target_delta_ids: list[str] = Field(default_factory=list)
    shot_ids: list[str] = Field(default_factory=list)
    attention_target_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    scheduled_processing_s: float = 0.0
    planned_available_s: float = 0.0
    competing_attention_ids: list[str] = Field(default_factory=list)
    readability_reason: str = ""
    status: str = "planned"


class SetupPayoffContract(BaseModel):
    setup_payoff_id: str
    setup_proposition_ids: list[str] = Field(default_factory=list)
    setup_event_ids: list[str] = Field(default_factory=list)
    payoff_event_ids: list[str] = Field(default_factory=list)
    intended_inference_ids: list[str] = Field(default_factory=list)
    retention_deadline_event_id: str = ""
    minimum_retention_confidence: float = 0.0
    recall_needed: bool | None = None
    status: str = "open"


class AudienceStatePathRef(BaseModel):
    audience_prior_id: str
    audience_state_in_id: str
    audience_state_out_target_id: str


class SceneDramaticContract(BaseModel):
    scene_id: str
    applicability: str = "applies"
    not_applicable_reason: str | None = None
    alternative_dramatic_function: str | None = None
    scene_question_id: str | None = None
    point_of_view_character_id: str | None = None
    audience_state_paths: list[AudienceStatePathRef] = Field(default_factory=list)
    character_state_in_ids: list[str] = Field(default_factory=list)
    goal_proposition_ids: list[str] = Field(default_factory=list)
    obstacle_proposition_ids: list[str] = Field(default_factory=list)
    stakes_proposition_ids: list[str] = Field(default_factory=list)
    pressure_curve: list[dict] = Field(default_factory=list)
    turn_event_ids: list[str] = Field(default_factory=list)
    value_polarity_in: str = ""
    value_polarity_out: str = ""
    relationship_deltas: list[dict] = Field(default_factory=list)
    character_state_out_ids: list[str] = Field(default_factory=list)
    scene_button: str = ""


class NarrativeArcContract(BaseModel):
    arc_id: str
    scope: str = "episode"
    applicability: str = "applies"
    not_applicable_reason: str | None = None
    alternative_dramatic_function: str | None = None
    core_question_ids: list[str] = Field(default_factory=list)
    promise_proposition_ids: list[str] = Field(default_factory=list)
    escalation_event_ids: list[str] = Field(default_factory=list)
    climax_event_ids: list[str] = Field(default_factory=list)
    payoff_contract_ids: list[str] = Field(default_factory=list)
    pressure_curve: list[dict] = Field(default_factory=list)
    information_density_curve: list[dict] = Field(default_factory=list)
    processing_beats: list[dict] = Field(default_factory=list)
    ending_hook_question_ids: list[str] = Field(default_factory=list)
    resolved_question_ids: list[str] = Field(default_factory=list)
    carried_question_ids: list[str] = Field(default_factory=list)


class IdentityContractEvidence(BaseModel):
    """Auditable basis for one AI-resolved narrative identity.

    The lists point back into the same narrative plan.  ``rationale`` explains
    the semantic decision (persistent person, transient visible role,
    collective, or voice-only presence) without relying on a vocabulary of
    accepted names.
    """

    source_evidence_ids: list[str] = Field(default_factory=list)
    proposition_ids: list[str] = Field(default_factory=list)
    adaptation_decision_ids: list[str] = Field(default_factory=list)
    rationale: str = ""


class NarrativeIdentityContract(BaseModel):
    """Typed operational policy for an identity used by this episode.

    ``kind`` intentionally remains an open semantic label selected by the AI.
    Rendering and asset behaviour are controlled only by the typed policy
    fields, never by matching a display name against a role-name whitelist.
    """

    identity_id: str
    display_name: str
    kind: str
    visual_policy: Literal[
        "canonical", "contextual", "collective", "offscreen_only",
    ]
    visual_canonical: str = ""
    asset_requirement: Literal["required", "optional", "forbidden"]
    voice_ids: list[str] = Field(default_factory=list)
    evidence: IdentityContractEvidence = Field(default_factory=IdentityContractEvidence)

    @model_validator(mode="after")
    def _validate_operational_policy(self) -> "NarrativeIdentityContract":
        if not self.identity_id.strip():
            raise ValueError("identity_id 不能为空")
        if not self.display_name.strip():
            raise ValueError("display_name 不能为空")
        if not self.kind.strip():
            raise ValueError("kind 不能为空")
        if self.visual_policy == "offscreen_only":
            if self.asset_requirement != "forbidden":
                raise ValueError("offscreen_only 身份的 asset_requirement 必须为 forbidden")
        elif not self.visual_canonical.strip():
            raise ValueError("可见身份必须提供 visual_canonical")
        if self.visual_policy == "canonical" and self.asset_requirement != "required":
            raise ValueError("canonical 身份的 asset_requirement 必须为 required")
        normalized_voice_ids = [value.strip() for value in self.voice_ids]
        if any(not value for value in normalized_voice_ids):
            raise ValueError("voice_ids 不能包含空值")
        if len(normalized_voice_ids) != len(set(normalized_voice_ids)):
            raise ValueError("voice_ids 不能重复")
        return self


class NarrativeContinuityPlan(BaseModel):
    contract_version: str = NARRATIVE_CONTRACT_VERSION
    scope_id: str
    source_evidence: list[SourceEvidence] = Field(default_factory=list)
    propositions: list[NarrativeProposition] = Field(default_factory=list)
    adaptation_decisions: list[AdaptationDecision] = Field(default_factory=list)
    state_facts: list[StateFact] = Field(default_factory=list)
    initial_state_fact_ids: list[str] = Field(default_factory=list)
    evidence: list[NarrativeEvidence] = Field(default_factory=list)
    dramatic_questions: list[DramaticQuestion] = Field(default_factory=list)
    events: list[NarrativeEvent] = Field(default_factory=list)
    atomic_actions: list[AtomicAction] = Field(default_factory=list)
    action_relation_audits: list[ActionSemanticRelationAudit] = Field(default_factory=list)
    character_states: list[CharacterDramaticState] = Field(default_factory=list)
    character_beliefs: list[CharacterBeliefSnapshot] = Field(default_factory=list)
    audience_priors: list[AudiencePriorContract] = Field(default_factory=list)
    audience_states: list[AudienceStateSnapshot] = Field(default_factory=list)
    experience_intents: list[ExperienceIntent] = Field(default_factory=list)
    assimilation_tasks: list[AssimilationTask] = Field(default_factory=list)
    readability_windows: list[ReadabilityWindow] = Field(default_factory=list)
    setup_payoff_contracts: list[SetupPayoffContract] = Field(default_factory=list)
    scene_contracts: list[SceneDramaticContract] = Field(default_factory=list)
    arc_contracts: list[NarrativeArcContract] = Field(default_factory=list)
    identity_contracts: list[NarrativeIdentityContract] = Field(default_factory=list)


class EpisodeScreenplay(BaseModel):
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
        allowed_dispositions = {"deliver", "merge", "context", "duplicate"}
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


class Dialogue(BaseModel):
    speaker: str
    line: str
    emotion: str = "平静"
    # 画外角色发声：保留 speaker 身份，但不要求画面口型
    delivery: str = "spoken_dialogue"  # spoken_dialogue | offscreen_voice | narration


class AudioTimelineItem(BaseModel):
    start_s: float = 0.0
    end_s: float = 0.0
    type: str = "ambient_sound"
    speaker_id: str | None = None
    text: str = ""
    lip_sync: bool = False
    emotion: str = "平静"
    voice_canonical: str = ""


class SceneContinuityState(BaseModel):
    scene_revision_id: str = ""
    time_of_day: str = ""
    lighting_state: str = ""
    axis_id: str = ""
    landmarks: dict[str, str] = Field(default_factory=dict)


class CharacterContinuityState(BaseModel):
    look_revision_id: str = ""
    outfit_revision_id: str = ""
    visibility: str = "visible"
    visible_in_frame: bool | None = None
    screen_side: str = ""
    pose: str = ""
    facing: str = ""
    gaze_target: str = ""
    left_hand: str = ""
    right_hand: str = ""


class PropContinuityState(BaseModel):
    canonical_name: str = ""
    revision_id: str = ""
    owner: str = ""
    location: str = ""
    form: str = ""
    visibility: str = "optional"
    text_state: str = "none"
    required: bool = False


class ContinuityState(BaseModel):
    """可比较的镜头状态快照；自然语言 state_in/state_out 仍作为生成提示。"""

    scene: SceneContinuityState = Field(default_factory=SceneContinuityState)
    characters: dict[str, CharacterContinuityState] = Field(default_factory=dict)
    props: dict[str, PropContinuityState] = Field(default_factory=dict)


class RequiredOnScreenText(BaseModel):
    surface: str = ""
    exact_text: str = ""
    strategy: str = "deterministic_insert"
    delivery_owner_shot_no: int | None = None
    appear_start_s: float = 0.0
    stable_until_s: float | None = None
    style: str = ""
    allow_other_text: bool = False
    max_other_text: int = 0
    font_role: str = "classical_serif"
    reading_priority: str = "plot_critical"


class Shot(BaseModel):
    shot_no: int
    shot_uid: str = ""
    duration_s: int
    shot_size: str
    camera_move: str
    # 时间与场景图身份是两个独立维度。scene_time 允许早/中/晚/黄昏/具体时刻。
    scene_time: str = ""
    # 旧的「时间，地点」混合字段，只作兼容显示；新流程不再用它选场景图。
    scene_setting: str = ""
    # 库内规范场景名，与场景图一一对应。模糊输入命中后必须回填成该规范名。
    scene_name: str = ""
    characters: list[str] = Field(default_factory=list)
    action_desc: str
    # 生成起点与结束目标：同场景起点继承上一条采用视频真实尾帧；换场起点只依赖人物谱/场景库。
    # last_frame_desc 是视频结束状态目标，不代表另行生成一张静态尾帧参考图。
    first_frame_desc: str = ""
    last_frame_desc: str = ""
    source_excerpt: str = ""
    narration: str | None = None
    dialogues: list[Dialogue] = Field(default_factory=list)
    transition: str = "硬切"
    continuity_from_prev: bool = False
    # ---- PRD 连续性生产契约（缺省时由 first/last_frame / action_desc / characters 回填）----
    story_event_id: str = ""
    purpose: str = ""
    # PRD VAL-422 §4.4.1：E/S/I/KL 四类 ID 分属四个空间，禁止互相混写。
    # story_event_id 只放剧本事件 E*；主线节拍走 spine_beat_ids；关键台词走 key_line_ids。
    spine_beat_ids: list[str] = Field(default_factory=list)
    key_line_ids: list[str] = Field(default_factory=list)
    information_ids: list[str] = Field(default_factory=list)
    # 兼容旧字段一个版本周期；内部立即归一到 information_ids（见 _sync_information_ids）。
    new_information_ids: list[str] = Field(default_factory=list)
    reinforcement_info_ids: list[str] = Field(default_factory=list)
    spoken_contract_status: str = "legacy"  # coherent | conflict | legacy
    state_in: str = ""
    primary_action: str = ""
    emotion_beat: str = ""
    state_out: str = ""
    observed_state_out: str = ""
    continuity_mode: str = ""
    characters_visible: list[str] = Field(default_factory=list)
    audio_cast: list[str] = Field(default_factory=list)
    audio_timeline: list[AudioTimelineItem] = Field(default_factory=list)
    required_text: RequiredOnScreenText | None = None
    continuity_state_in: ContinuityState = Field(default_factory=ContinuityState)
    continuity_state_out: ContinuityState = Field(default_factory=ContinuityState)
    reference_roles: list[str] = Field(default_factory=list)
    do_not_repeat: list[str] = Field(default_factory=list)
    risk_tags: list[str] = Field(default_factory=list)
    prompt_contract_version: str = ""
    legacy_unvalidated: bool = False
    camera_angle: str = ""
    spatial_anchor: str = ""
    is_final: bool = False
    context_requirement_ids: list[str] = Field(default_factory=list)
    resulting_change: str = ""
    readability_focus: str = ""
    camera_motivation: str = ""
    repeat_of_shot_id: str | None = None
    repeat_gain: str = ""
    # Narrative task.  A reaction/establishing/processing shot may have no
    # primary action, but it must still own a non-empty evidence contribution.
    shot_id: str = ""
    scene_id: str = ""
    event_ids: list[str] = Field(default_factory=list)
    primary_action_id: str | None = None
    supporting_action_ids: list[str] = Field(default_factory=list)
    action_phase_ids: list[str] = Field(default_factory=list)
    visible_entity_ids: list[str] = Field(default_factory=list)
    offscreen_action_actor_ids: list[str] = Field(default_factory=list)
    offscreen_action_target_ids: list[str] = Field(default_factory=list)
    action_participant_deliveries: list[ActionParticipantDelivery] = Field(
        default_factory=list
    )
    capacity_budget: ShotCapacityBudget | None = None
    shot_contribution: ShotContribution | None = None
    audience_state_paths: list[AudienceStatePathRef] = Field(default_factory=list)
    planned_state_in_fact_ids: list[str] = Field(default_factory=list)
    planned_delta_add_fact_ids: list[str] = Field(default_factory=list)
    planned_delta_remove_fact_ids: list[str] = Field(default_factory=list)
    planned_state_out_fact_ids: list[str] = Field(default_factory=list)
    completed_before_action_ids: list[str] = Field(default_factory=list)
    completed_before_action_phase_ids: list[str] = Field(default_factory=list)
    reserved_future_event_ids: list[str] = Field(default_factory=list)
    readability_window_ids: list[str] = Field(default_factory=list)
    narrative_boundary_from_previous: "NarrativeBoundaryContract | None" = None

    @model_validator(mode="after")
    def _sync_information_ids(self) -> "Shot":
        return _normalize_information_ids(self)


class Storyboard(BaseModel):
    episode_no: int
    shots: list[Shot]


class StoryboardOutlineShot(BaseModel):
    """分镜大纲里的一条镜头节拍：按状态变化一次规划原子镜头。
    逐镜填充阶段据此把整集剧情均匀铺满，避免多镜停留在同一情绪/同一句原文。"""

    shot_no: int
    scene_time: str = ""      # 独立时间标签，可为具体时刻
    scene_name: str = ""      # 场景图素材库的规范名
    scene_setting: str = ""   # 旧数据/显示兼容字段
    beat: str = ""            # 本镜推进的剧情（一句话：谁做了什么 / 局势如何变化 / 与上一镜的区别）
    covers: str = ""          # 本镜落实的必保留关键台词/剧情点（可空）
    # 原子分镜规划字段（PRD §7）：大纲阶段即锁定状态链与连续性模式
    story_event_id: str = ""
    # PRD VAL-422 §4.2：大纲阶段就把关键台词/主线节拍按稳定 ID 分配到镜头，
    # 容量预检据此判断「这一镜的必保留口播是否念得完」。
    spine_beat_ids: list[str] = Field(default_factory=list)
    key_line_ids: list[str] = Field(default_factory=list)
    information_ids: list[str] = Field(default_factory=list)
    new_information_ids: list[str] = Field(default_factory=list)
    state_in: str = ""
    primary_action: str = ""
    emotion_beat: str = ""
    state_out: str = ""
    continuity_mode: str = ""
    duration_s: int | None = None
    characters_visible: list[str] = Field(default_factory=list)
    audio_cast: list[str] = Field(default_factory=list)
    purpose: str = ""
    context_requirement_ids: list[str] = Field(default_factory=list)
    resulting_change: str = ""
    readability_focus: str = ""
    camera_size: str = ""
    camera_angle: str = ""
    camera_movement: str = ""
    camera_motivation: str = ""
    repeat_of_shot_id: str | None = None
    repeat_gain: str = ""
    shot_id: str = ""
    scene_id: str = ""
    event_ids: list[str] = Field(default_factory=list)
    primary_action_id: str | None = None
    supporting_action_ids: list[str] = Field(default_factory=list)
    action_phase_ids: list[str] = Field(default_factory=list)
    visible_entity_ids: list[str] = Field(default_factory=list)
    offscreen_action_actor_ids: list[str] = Field(default_factory=list)
    offscreen_action_target_ids: list[str] = Field(default_factory=list)
    action_participant_deliveries: list[ActionParticipantDelivery] = Field(
        default_factory=list
    )
    capacity_budget: ShotCapacityBudget | None = None
    shot_contribution: ShotContribution | None = None
    audience_state_paths: list[AudienceStatePathRef] = Field(default_factory=list)
    planned_state_in_fact_ids: list[str] = Field(default_factory=list)
    planned_delta_add_fact_ids: list[str] = Field(default_factory=list)
    planned_delta_remove_fact_ids: list[str] = Field(default_factory=list)
    planned_state_out_fact_ids: list[str] = Field(default_factory=list)
    completed_before_action_ids: list[str] = Field(default_factory=list)
    completed_before_action_phase_ids: list[str] = Field(default_factory=list)
    reserved_future_event_ids: list[str] = Field(default_factory=list)
    readability_window_ids: list[str] = Field(default_factory=list)
    narrative_boundary_from_previous: "NarrativeBoundaryContract | None" = None

    @model_validator(mode="after")
    def _sync_information_ids(self) -> "StoryboardOutlineShot":
        return _normalize_information_ids(self)


class StoryboardOutline(BaseModel):
    """整集分镜大纲：一次性把剧本铺成有序的 N 条镜头节拍，先定全局节奏再逐镜填充。"""

    episode_no: int
    shots: list[StoryboardOutlineShot] = Field(default_factory=list)
    scene_contexts: list["StoryboardSceneContext"] = Field(default_factory=list)
    readability_windows: list[ReadabilityWindow] = Field(default_factory=list)
    cognitive_bridge_plans: list["CognitiveBridgePlan"] = Field(default_factory=list)


class StoryboardContextRequirement(BaseModel):
    requirement_id: str
    description: str
    required_before_shot_no: int | None = None


class StoryboardSceneContext(BaseModel):
    scene_id: str
    scene_no: int
    scene_name: str = ""
    scene_time: str = ""
    entry_state: str
    exit_state: str
    transition_from_previous: str = ""
    spatial_axis: str = ""
    context_requirements: list[StoryboardContextRequirement] = Field(default_factory=list)


class StoryboardScenePack(BaseModel):
    episode_no: int
    scene_id: str
    shots: list[Shot] = Field(default_factory=list)


class BoundaryStateTransition(BaseModel):
    """Auditable reason for a world-state difference across a cut.

    The basis describes a structural relation (timeline, viewpoint, spatial
    model or an action phase), never a story object/category.  Unknown
    relations remain representable through ``other`` but require human review
    before narrative-ready publication.
    """

    transition_id: str
    basis_type: str
    source_fact_id: str | None = None
    target_fact_id: str | None = None
    basis_action_phase_id: str | None = None
    custom_basis: str | None = None
    reason: str = ""


class NarrativeBoundaryContract(BaseModel):
    boundary_id: str
    previous_shot_id: str
    next_shot_id: str
    narrative_relation: str
    required_state_invariants: list[str] = Field(default_factory=list)
    allowed_state_deltas: list[str] = Field(default_factory=list)
    state_delta_transitions: list[BoundaryStateTransition] = Field(default_factory=list)
    forbidden_replay_action_ids: list[str] = Field(default_factory=list)
    handoff_action_phase_id: str | None = None
    spatial_orientation_contract: dict = Field(default_factory=dict)
    temporal_orientation_contract: dict = Field(default_factory=dict)
    audience_state_handoffs: list[dict] = Field(default_factory=list)
    affective_handoff: dict = Field(default_factory=dict)
    cut_motivation: str


class CognitiveBridgePlan(BaseModel):
    bridge_plan_id: str
    assimilation_task_ids: list[str] = Field(default_factory=list)
    candidate_changes: list[dict] = Field(default_factory=list)
    expected_audience_delta: dict = Field(default_factory=dict)
    affected_shot_ids: list[str] = Field(default_factory=list)
    added_shot_ids: list[str] = Field(default_factory=list)
    removed_shot_ids: list[str] = Field(default_factory=list)
    estimated_screen_time_delta: float = 0.0
    deletion_test_result: dict = Field(default_factory=dict)
    marginal_gain_result: dict = Field(default_factory=dict)
    selection_reason: str = ""


class BlindSpontaneousRecall(BaseModel):
    """Closed model-output contract; unknown fields fail schema validation."""

    model_config = ConfigDict(extra="forbid")

    recognized_entities: list[Any]
    inferred_propositions: list[Any]
    causal_hypotheses: list[Any]
    character_goal_hypotheses: list[Any]
    active_question_ids: list[str]


class BlindAudienceObservation(BaseModel):
    observation_id: str
    audience_prior_id: str
    anchor: NarrativeAnchor
    spontaneous_recall: BlindSpontaneousRecall
    neutral_followup_observations: list[dict | str] = Field(default_factory=list)
    noticed_attention_target_ids: list[str] = Field(default_factory=list)
    spatial_temporal_model: dict = Field(default_factory=dict)
    felt_affective_state: dict = Field(default_factory=dict)
    perceived_relationship_deltas: list[dict] = Field(default_factory=list)
    perceived_stakes: list[str] = Field(default_factory=list)
    experienced_pressure_curve: list[dict] = Field(default_factory=list)
    experienced_rhythm: dict = Field(default_factory=dict)
    next_event_expectations: list[str] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    # Evidence handles explicitly present in the frozen, unprompted first pass.
    # Follow-up observations may add to supporting_evidence_ids but can never
    # retroactively populate this ledger.
    spontaneous_supporting_evidence_ids: list[str] = Field(default_factory=list)
    supporting_evidence_ids: list[str] = Field(default_factory=list)
    confidence: float = 0.0


class TargetDeltaResult(BaseModel):
    audience_prior_id: str
    target_delta_id: str
    result: str
    predicted_score: float | None = None
    # The comparator must make every conclusion auditable.  Observation IDs
    # point at frozen first-pass recalls; evidence IDs point at opaque handles
    # the cold reader actually saw.  They are deliberately absent from the
    # director-facing target contract and therefore cannot prompt the answer.
    supporting_observation_ids: list[str] = Field(default_factory=list)
    supporting_evidence_ids: list[str] = Field(default_factory=list)
    reason: str = ""


class NarrativeReviewReport(BaseModel):
    narrative_review_report_id: str
    scope_id: str
    experience_intent_ids: list[str] = Field(default_factory=list)
    observation_ids: list[str] = Field(default_factory=list)
    target_delta_results: list[TargetDeltaResult] = Field(default_factory=list)
    character_goal_readability_result: dict = Field(default_factory=dict)
    attention_alignment_result: dict = Field(default_factory=dict)
    spatial_temporal_orientation_result: dict = Field(default_factory=dict)
    affective_alignment_result: dict = Field(default_factory=dict)
    relationship_change_result: dict = Field(default_factory=dict)
    stakes_readability_result: dict = Field(default_factory=dict)
    pressure_rhythm_result: dict = Field(default_factory=dict)
    action_functional_repetition_result: dict = Field(default_factory=dict)
    next_expectation_result: dict = Field(default_factory=dict)
    intentional_ambiguity_result: dict = Field(default_factory=dict)
    low_percentile_result: dict = Field(default_factory=dict)
    inference_variance: float = 0.0
    evidence_gap_ids: list[str] = Field(default_factory=list)
    unintended_inference_ids: list[str] = Field(default_factory=list)
    decision: str = "needs_human_review"
    reason: str = ""


# Resolve the forward reference used by Shot/StoryboardOutlineShot without
# moving the existing public classes (many callers import them by location).
Shot.model_rebuild()
StoryboardOutlineShot.model_rebuild()
StoryboardOutline.model_rebuild()


def _escape_unescaped_inner_quotes(text: str) -> str:
    """只修复能明确判定为 JSON 字符串内容的裸双引号。

    模型偶尔把原文弯引号改成 ASCII 双引号，例如
    ``"这个"天才"仍在原地"``。字符串真正的结束引号后只能跟
    ``:``, ``,``、``]``、``}`` 或文本结束；其他位置的裸引号可安全
    视为字符串内容并转义。缺逗号、括号错误等结构问题不会被放行。
    """
    repaired: list[str] = []
    in_string = False
    escaped = False
    length = len(text)
    for index, char in enumerate(text):
        if not in_string:
            repaired.append(char)
            if char == '"':
                in_string = True
            continue
        if escaped:
            repaired.append(char)
            escaped = False
            continue
        if char == "\\":
            repaired.append(char)
            escaped = True
            continue
        if char != '"':
            repaired.append(char)
            continue

        next_index = index + 1
        while next_index < length and text[next_index].isspace():
            next_index += 1
        next_char = text[next_index] if next_index < length else ""
        if next_char in {":", ",", "]", "}"} or not next_char:
            repaired.append(char)
            in_string = False
        else:
            repaired.extend(("\\", char))
    return "".join(repaired)


def _close_missing_root_object(text: str) -> str:
    """Close one unambiguous missing root ``}`` at end-of-output.

    Providers occasionally emit an otherwise complete object but stop after the
    final child value (commonly an array), omitting only the outermost closing
    brace.  This repair is intentionally narrower than a general JSON repairer:
    strings must be closed, every nested container must already be balanced,
    and the root object must be the sole remaining open container.
    """
    expected_closers: list[str] = []
    in_string = False
    escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            expected_closers.append("}")
        elif char == "[":
            expected_closers.append("]")
        elif char in "}]":
            if not expected_closers or expected_closers[-1] != char:
                return text
            expected_closers.pop()

    if not in_string and not escaped and expected_closers == ["}"]:
        return text + "}"
    return text


def _repair_trailing_container_closure(text: str) -> str:
    """Replace one wrong EOF closer with the uniquely required close sequence."""
    expected_closers: list[str] = []
    in_string = False
    escaped = False
    last_nonspace = len(text.rstrip()) - 1
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            expected_closers.append("}")
        elif char == "[":
            expected_closers.append("]")
        elif char in "}]":
            if not expected_closers:
                return text
            if expected_closers[-1] != char:
                if index != last_nonspace or in_string or escaped:
                    return text
                return (
                    text[:index]
                    + "".join(reversed(expected_closers))
                    + text[index + 1:]
                )
            expected_closers.pop()
    return text


def _repair_singleton_string_object_fields(
    text: str,
    field_names: tuple[str, ...],
) -> str:
    json_string = r'"(?:\\.|[^"\\])*"'
    for field_name in field_names:
        pattern = re.compile(
            rf'"{re.escape(field_name)}"\s*:\s*\{{\s*({json_string})\s*\}}'
        )
        text = pattern.sub(
            rf'"{field_name}":{{"description":\1}}',
            text,
        )
    return text


def _repair_structural_json_delimiters(text: str) -> str:
    """Repair delimiter omissions that are uniquely implied by JSON nesting."""
    repaired: list[str] = []
    expected_closers: list[str] = []
    in_string = False
    escaped = False
    previous_significant = ""

    def next_token_is_object_key(start: int) -> bool:
        index = start
        while index < len(text) and text[index].isspace():
            index += 1
        if index >= len(text) or text[index] != '"':
            return False
        index += 1
        local_escaped = False
        while index < len(text):
            char = text[index]
            if local_escaped:
                local_escaped = False
            elif char == "\\":
                local_escaped = True
            elif char == '"':
                index += 1
                break
            index += 1
        while index < len(text) and text[index].isspace():
            index += 1
        return index < len(text) and text[index] == ":"

    for index, char in enumerate(text):
        if in_string:
            repaired.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
                previous_significant = '"'
            continue
        if char == '"':
            in_string = True
            repaired.append(char)
            continue
        if (
            char in "[{"
            and previous_significant in "}]"
            and expected_closers
            and expected_closers[-1] == "]"
        ):
            repaired.append(",")
        if char == "{":
            expected_closers.append("}")
        elif char == "[":
            expected_closers.append("]")
        elif char in "}]":
            if expected_closers and expected_closers[-1] == char:
                expected_closers.pop()
        elif (
            char == ","
            and expected_closers
            and expected_closers[-1] == "]"
            and next_token_is_object_key(index + 1)
        ):
            repaired.append("]")
            expected_closers.pop()
        repaired.append(char)
        if not char.isspace():
            previous_significant = char
    return "".join(repaired)


def _repair_merged_object_string_entry(
    text: str,
    error: json.JSONDecodeError,
) -> str:
    """Split one object key accidentally merged into the preceding string value."""
    if error.msg != "Expecting ',' delimiter" or error.pos >= len(text):
        return text
    if text[error.pos] != ":":
        return text

    prefix = text[:error.pos]
    match = re.search(
        r':\s*"(?P<value>(?:\\.|[^"\\])*)"(?P<space>\s*)$',
        prefix,
    )
    if match is None:
        return text

    value = match.group("value")
    delimiter_index = max(
        value.rfind(","),
        value.rfind("，"),
        value.rfind(";"),
        value.rfind("；"),
    )
    if delimiter_index <= 0 or delimiter_index >= len(value) - 1:
        return text

    previous_value = value[:delimiter_index].strip()
    merged_key = value[delimiter_index + 1:].strip()
    if not previous_value or not merged_key:
        return text

    replacement = (
        f':"{previous_value}","{merged_key}"'
        f'{match.group("space")}'
    )
    return text[:match.start()] + replacement + text[error.pos:]


def _remove_unmatched_root_level_closer(
    text: str,
    error: json.JSONDecodeError,
) -> str:
    """Remove one closer that cannot belong to any open nested container."""
    if error.pos >= len(text) or text[error.pos] not in "]}":
        return text
    expected_closers: list[str] = []
    in_string = False
    escaped = False
    for char in text[:error.pos]:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            expected_closers.append("}")
        elif char == "[":
            expected_closers.append("]")
        elif char in "]}":
            if not expected_closers or expected_closers[-1] != char:
                return text
            expected_closers.pop()
    if (
        not in_string
        and expected_closers == ["}"]
        and text[error.pos] != expected_closers[-1]
    ):
        return text[:error.pos] + text[error.pos + 1:]
    return text


def _escape_json_control_chars_in_strings(text: str) -> str:
    """Escape raw control characters only while inside JSON strings."""
    replacements = {
        "\b": "\\b",
        "\f": "\\f",
        "\n": "\\n",
        "\r": "\\r",
        "\t": "\\t",
    }
    repaired: list[str] = []
    in_string = False
    escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
                repaired.append(char)
                continue
            if char == "\\":
                escaped = True
                repaired.append(char)
                continue
            if char == '"':
                in_string = False
                repaired.append(char)
                continue
            if ord(char) < 0x20:
                repaired.append(
                    replacements.get(char, f"\\u{ord(char):04x}")
                )
                continue
            repaired.append(char)
            continue
        if char == '"':
            in_string = True
        repaired.append(char)
    return "".join(repaired)


def extract_json(
    text: str,
    *,
    repair_unescaped_inner_quotes: bool = False,
    repair_singleton_string_object_fields: tuple[str, ...] = (),
) -> dict:
    """从模型输出中提取第一个完整 JSON 对象。失败抛 ValueError（含原文摘要）。"""
    cleaned = re.sub(r"```(?:json)?\s*", "", text, flags=re.IGNORECASE).replace("```", "").strip()
    think_markers = list(
        re.finditer(r"</think[^>]*>", cleaned, flags=re.IGNORECASE)
    )
    if think_markers:
        formal_payload = cleaned[think_markers[-1].end():].strip()
        if "{" in formal_payload:
            cleaned = formal_payload
    first_start = cleaned.find("{")
    if first_start == -1:
        raise ValueError(f"输出中找不到 JSON 对象。原文开头：{text[:200]}")

    first_error: json.JSONDecodeError | None = None
    for match in re.finditer(r"{", cleaned):
        start = match.start()
        # 只把形如 JSON 对象开头的花括号当候选；这样仍能跳过说明文字里的
        # “{不是 JSON}”。一旦遇到第一个真正的 JSON 根对象候选，就必须以它
        # 为准：若它因字符串内双引号未转义等原因损坏，应把解析错误回喂模型，
        # 不能继续向内扫描并误把 dialogues 中的小对象当成整份输出。
        remainder = cleaned[start + 1:].lstrip()
        if remainder and not (remainder.startswith('"') or remainder.startswith("}")):
            continue
        candidate = _repair_singleton_string_object_fields(
            cleaned[start:],
            repair_singleton_string_object_fields,
        )
        candidate = _escape_json_control_chars_in_strings(candidate)
        try:
            obj, _ = json.JSONDecoder().raw_decode(candidate)
        except json.JSONDecodeError as exc:
            candidate_error = exc
            if repair_unescaped_inner_quotes:
                repaired = _escape_unescaped_inner_quotes(candidate)
                if repaired != candidate:
                    try:
                        obj, _ = json.JSONDecoder().raw_decode(repaired)
                    except json.JSONDecodeError as repaired_exc:
                        candidate_error = repaired_exc
                    else:
                        if isinstance(obj, dict):
                            return obj
                    candidate = repaired
            repaired = _repair_structural_json_delimiters(candidate)
            if repaired != candidate:
                try:
                    obj, _ = json.JSONDecoder().raw_decode(repaired)
                except json.JSONDecodeError as repaired_exc:
                    candidate_error = repaired_exc
                else:
                    if isinstance(obj, dict):
                        return obj
                candidate = repaired
            repaired = _remove_unmatched_root_level_closer(
                candidate,
                candidate_error,
            )
            if repaired != candidate:
                try:
                    obj, _ = json.JSONDecoder().raw_decode(repaired)
                except json.JSONDecodeError as repaired_exc:
                    candidate_error = repaired_exc
                else:
                    if isinstance(obj, dict):
                        return obj
                candidate = repaired
            repaired = _repair_merged_object_string_entry(
                candidate,
                candidate_error,
            )
            if repaired != candidate:
                try:
                    obj, _ = json.JSONDecoder().raw_decode(repaired)
                except json.JSONDecodeError as repaired_exc:
                    candidate_error = repaired_exc
                else:
                    if isinstance(obj, dict):
                        return obj
                candidate = repaired
            if candidate_error.pos >= len(candidate.rstrip()) - 1:
                repaired = _repair_trailing_container_closure(candidate)
                if repaired != candidate:
                    try:
                        obj, _ = json.JSONDecoder().raw_decode(repaired)
                    except json.JSONDecodeError:
                        pass
                    else:
                        if isinstance(obj, dict):
                            return obj
            # Only an EOF failure may be eligible.  Missing commas and damaged
            # inner structure fail before EOF and must still enter the repair
            # loop instead of being silently guessed here.
            if candidate_error.pos >= len(candidate):
                repaired = _close_missing_root_object(candidate)
                if repaired != candidate:
                    try:
                        obj, _ = json.JSONDecoder().raw_decode(repaired)
                    except json.JSONDecodeError:
                        pass
                    else:
                        if isinstance(obj, dict):
                            return obj
            first_error = exc
            break
        if isinstance(obj, dict):
            return obj
        raise ValueError(f"JSON 根节点不是对象。片段：{cleaned[start:start + 200]}")

    detail = f"（{first_error}）" if first_error else ""
    raise ValueError(f"JSON 解析失败{detail}。片段：{cleaned[first_start:first_start + 200]}")


def schema_errors(model_cls: type[BaseModel], obj: dict) -> tuple[BaseModel | None, list[str]]:
    """返回 (实例, 错误列表)。错误消息具体到字段路径，供修复回路回喂。"""
    try:
        return model_cls.model_validate(obj), []
    except ValidationError as exc:
        errors = []
        for e in exc.errors():
            path = ".".join(str(p) for p in e["loc"])
            errors.append(f"字段 {path}：{e['msg']}")
        return None, errors
