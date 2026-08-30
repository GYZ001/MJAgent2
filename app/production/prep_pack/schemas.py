"""Model response schemas (structured-output Pydantic models) for episode
asset-mapping chunk extraction.

Split out of app/production/prep_pack.py.
"""
from __future__ import annotations

from pydantic import (
    BaseModel,
    ConfigDict,
)
from typing import Any


class _ModelCharacterMention(BaseModel):
    model_config = ConfigDict(extra="forbid")
    display_name: str
    # 1.5.0 (kept in 2.0.0): model-declared prior-knowledge hypothesis (real
    # EP5 finding: outright banning this discarded a genuinely CORRECT guess
    # -- see _prep_pack_verify_true_name_hypothesis below). display_name
    # must still be the verbatim in-episode term of address; this field is
    # never used to replace it, only as an unverified candidate for _pass to
    # check.
    suspected_true_name: str | None
    # 2.0.0: this mention's own claim of which segments (global 1-based,
    # same numbering the model was shown in this chunk) it is actually
    # ON-SCREEN in -- not merely named/recalled/heard-of elsewhere. This
    # replaces the old event_id/source_span indirection: segment_indexes IS
    # now the segment-attribution claim (see _prep_pack_gate_segment_indexes
    # for the deterministic per-segment literal-evidence gate every
    # declared index must clear before being trusted).
    segment_indexes: list[int]


class _ModelSceneMention(BaseModel):
    model_config = ConfigDict(extra="forbid")
    display_name: str
    suspected_true_name: str | None  # isomorphic to the character field above
    segment_indexes: list[int]
    # 2.0.2 (real regression fix, see PREP_PACK_VERSION's 2.0.2 note above):
    # a verbatim excerpt from one of this mention's own segment_indexes that
    # supports "this is that place" -- isomorphic to the old, now-removed
    # event_chain[].source_evidence[].quote, just declared at mention grain
    # instead of event grain. Required (not Optional) matching this module's
    # strict-schema convention; legal to be "" when this mention genuinely
    # has no excerptable evidence in this chunk (never fabricate one). This
    # is the sole reason the field exists: display_name/canonical scene
    # names are frequently model-synthesized labels that never appear
    # verbatim in the source text (real EP1: "大青山山顶" vs source "这青山
    # 顶端"), so they cannot themselves serve as independent local-text-
    # anchor evidence for resolution/discovery scene bindings -- see
    # _prep_pack_local_text_anchor's "同义反复" note and _pass()'s scene
    # anchor-candidate section below for how this flows into anchor_phrase.
    quote: str


class _ModelPropMention(BaseModel):
    """2.0.0, new: a physical object/item the episode actually shows on
    screen. No bible image library exists for props (unlike characters/
    scenes) -- this is a text-only asset, ``description`` is its only
    payload, never a portrait_id/scene_reference_id/visual_entity_id."""
    model_config = ConfigDict(extra="forbid")
    label: str
    description: str
    segment_indexes: list[int]


class _ChunkResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    characters: list[_ModelCharacterMention]
    scenes: list[_ModelSceneMention]
    props: list[_ModelPropMention]
    # 1.4.1 introduced a model-self-reported `paratext_segments` field here
    # (chapter title / author's note segments, own wording+temperature,
    # independent of app.source_paratext.PARATEXT_RULE). Retired 2026-08-27
    # (logs/paratext_single_source_plan.md): paratext is now a deterministic
    # projection of chapters.paratext_json (persisted per-chapter offsets,
    # same PARATEXT_RULE the world bible uses) onto this episode's segments
    # -- see _generate_prep_pack_once's paratext_regions/deterministic_
    # paratext_segments. The model is no longer asked to judge this at all;
    # asking it twice (once here, once for the world bible) with two
    # different wordings/temperatures for the same underlying question was
    # the exact duplication the retirement plan targeted.


def _response_format(model_type: type[BaseModel], name: str) -> dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": name,
            "strict": True,
            "schema": model_type.model_json_schema(),
        },
    }


# ---------------------------------------------------------------------------
# Deterministic helpers
# ---------------------------------------------------------------------------

