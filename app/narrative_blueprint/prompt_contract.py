"""The generation prompt contract text: blueprint_prompt_contract."""
from __future__ import annotations

from typing import Any

from .constants import BLUEPRINT_MAX_SOURCE_SEGMENTS_PER_NODE, BLUEPRINT_VERSION
from .models_core import NarrativeNode


def blueprint_prompt_contract() -> dict[str, Any]:
    return {
        "format_version": BLUEPRINT_VERSION,
        "node_source_limit": BLUEPRINT_MAX_SOURCE_SEGMENTS_PER_NODE,
        "time_relations": list(
            NarrativeNode.model_fields["time_relation"].annotation.__args__
        ),
        "required_source_semantics": {
            "fields": [
                "narrative_layer",
                "event_priority",
                "render_policy",
            ],
            "story": {
                "event_priority": "causal",
                "render_policy": "standalone",
                "meaning": "可表演、可形成画面状态变化的剧情语义",
            },
            "paratext": {
                "event_priority": "connective",
                "render_policy": "exclude_from_spine",
                "meaning": (
                    "仅保留完整来源审计，不生成 scene/event/beat/"
                    "scene_outline/shot，也不注入剧情上下文"
                ),
            },
        },
        "program_derived": [
            "scene_plans",
            "scene_heading",
            "scene_order",
            "source_scene_owners",
            "source_semantics.disposition",
            "source_semantics.projection_policy",
            "source_audit_annotations",
            "scene_derivations",
        ],
        "source_ownership": {
            "contract": "each source_id has exactly one scene owner",
            "node_split_boundary": (
                "nodes may split only between source_ids; one source_id "
                "must never be split even when it crosses locations"
            ),
            "single_primary_location": (
                "location_key and location_label identify exactly one primary "
                "location; movement inside one source_id stays in transition "
                "semantics and never becomes a composite location"
            ),
            "cross_scene_context": (
                "state/setup/transition information uses scene_derivations "
                "and never consumes the original source_id again"
            ),
        },
        "participant_evidence_required": {
            "fields": [
                "identity_key",
                "source_segment_ids",
                "source_unit_keys",
                "usage",
            ],
            "usage": ["visible", "voice", "mentioned", "state_subject"],
            "ownership": "source_segment_ids must be owned by the same node",
            "participant_identity_contract": (
                "every participants identity has either an evidence object "
                "with the exact same identity_key and non-empty owned "
                "source_segment_ids, or an exact-unit joint assignment"
            ),
            "dialogue_voice_contract": (
                "every audible source_unit_delivery has exactly one "
                "usage=voice evidence whose identity_key equals performer_key"
            ),
            "non_dialogue_voice_contract": (
                "written_text, sound_effect and unspoken_reference delivery "
                "must not carry voice evidence"
            ),
            "state_subject_contract": (
                "every prose/action source unit must have exactly one "
                "usage=state_subject evidence with an exact source_unit_key, "
                "or its source_unit_key must be listed in "
                "environment_source_unit_keys; missing or ambiguous ownership "
                "is a hard failure"
            ),
            "environment_contract": (
                "environment_source_unit_keys is reserved for genuinely "
                "non-character establishing, weather, place or object state; "
                "never use it for a person's thought, reaction, question or action"
            ),
        },
        "source_unit_delivery_required": {
            "surface_authority": (
                "SourceFact projection=quoted records syntax only"
            ),
            "fields": [
                "source_unit_key",
                "mode",
                "content_owner_key",
                "performer_key",
            ],
            "modes": [
                "spoken_dialogue",
                "offscreen_voice",
                "written_text",
                "sound_effect",
                "unspoken_reference",
            ],
            "contract": (
                "every quoted unit in a picture node has exactly one semantic "
                "delivery decision; quotation marks never imply speech"
            ),
        },
    }
