"""统一叙事连续性契约的核心原语：锚点、来源证据、命题、状态事实、悬念问题。

这些模型刻意描述关系与可观察增量，而非题材/剧情关键词表；LLM 可用开放的
``other``/自由语义字段表达契约未预见的概念，确定性校验只检查身份、来源、
所有权与状态交接（见原 app/schemas.py 对应区段说明）。
"""
from __future__ import annotations

from pydantic import BaseModel, Field

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
