"""身份操作契约与整份叙事连续性方案（NarrativeContinuityPlan 的聚合根）。

NarrativeIdentityContract 是"这个身份在本集怎么被渲染"的类型化策略（不依赖
角色名白名单）；NarrativeContinuityPlan 把上面几个narrative_*模块的所有原语
汇总成剧本/分镜/盲审共用的单一权威图。
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

from .common import NARRATIVE_CONTRACT_VERSION
from .narrative_action import ActionSemanticRelationAudit, AtomicAction, NarrativeEvent
from .narrative_audience import (
    AssimilationTask,
    AudiencePriorContract,
    AudienceStateSnapshot,
    CharacterBeliefSnapshot,
    CharacterDramaticState,
    ExperienceIntent,
)
from .narrative_capacity import (
    NarrativeArcContract,
    ReadabilityWindow,
    SceneDramaticContract,
    SetupPayoffContract,
)
from .narrative_core import (
    AdaptationDecision,
    DramaticQuestion,
    NarrativeEvidence,
    NarrativeProposition,
    SourceEvidence,
    StateFact,
)

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
