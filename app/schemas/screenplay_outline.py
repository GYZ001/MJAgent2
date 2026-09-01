"""剧本大纲台账：场次、剧情事件、信息交付、配音表与主线骨架/对白链。

ScriptScene/StoryEvent/InformationItem/VoiceCanonical/PlotSpineBeat/PlotSpine
是 Renderability First 的主线台账；SourceCoverageDecision/KeyDialogueTurn/
KeyDialogueChain 是源文本覆盖判定与结构化对白链（对应 VAL-422 §4.4）。
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

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
    display_name: str = ""
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
    disposition: Literal[
        "deliver", "merge", "context", "duplicate", "audit_only",
    ]
    projection_policy: Literal[
        "picture", "context_only", "audit_only",
    ] = "picture"
    beat_ids: list[str] = Field(default_factory=list)
    duplicate_of: str | None = None
    reason: str = ""

    @model_validator(mode="before")
    @classmethod
    def _derive_projection_policy(cls, value: object) -> object:
        if not isinstance(value, dict) or "projection_policy" in value:
            return value
        normalized = dict(value)
        disposition = str(normalized.get("disposition") or "")
        normalized["projection_policy"] = (
            "picture"
            if disposition in {"deliver", "merge"}
            else "audit_only"
            if disposition == "audit_only"
            else "context_only"
        )
        return normalized

    @model_validator(mode="after")
    def _validate_disposition(self) -> "SourceCoverageDecision":
        if not self.source_segment_id.strip():
            raise ValueError("source_segment_id 不能为空")
        if self.disposition in {"deliver", "merge"} and not self.beat_ids:
            raise ValueError("deliver/merge 必须绑定至少一个 beat_id")
        if self.disposition == "duplicate" and not (self.duplicate_of or "").strip():
            raise ValueError("duplicate 必须指向 duplicate_of")
        if (
            self.disposition in {"context", "duplicate", "audit_only"}
            and len(self.reason.strip()) < 4
        ):
            raise ValueError(
                "context/duplicate/audit_only 必须说明保留方式或重复依据"
            )
        expected_projection = (
            "picture"
            if self.disposition in {"deliver", "merge"}
            else "audit_only"
            if self.disposition == "audit_only"
            else "context_only"
        )
        if self.projection_policy != expected_projection:
            raise ValueError(
                f"{self.disposition} 必须使用 "
                f"projection_policy={expected_projection}"
            )
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
    scene_id: str = ""
    topic: str = ""
    turns: list[KeyDialogueTurn] = Field(default_factory=list)
