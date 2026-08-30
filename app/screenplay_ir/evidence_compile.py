"""Compiler phase: compiles event source evidence and adaptation decisions, then computes prop order and effective render policy."""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from app.schemas import system_environment_entity_id

from .constants import ScreenplayIRFidelityError
from .identity_authorities import ATTRIBUTED_TEXT_PROVENANCE_KINDS
from .identity_resolver import _IRIdentityResolver
from .models_core import IRScene
from .models_event import ScreenplayGenerationIR
from .prompt_context import _semantic_key, _source_location


def _ir_compile_event_evidence_and_adaptation(
    value: ScreenplayGenerationIR,
    source_text: str,
    segments: dict[str, Any],
    episode: dict[str, Any],
    episode_no: int,
    format_version: str,
    typed_visual_unit_contract: bool,
    identity_resolver: _IRIdentityResolver,
    final_identity_ids: dict[str, str],
    event_speaker_keys: "defaultdict[str, list[str]]",
    scene_by_key: dict[str, IRScene],
    event_ids: dict[str, str],
    ordered_used_keys: list[str],
    compiler_audit: list[dict[str, Any]],
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[str, str],
    dict[str, list[str]],
    dict[str, list[str]],
    dict[str, str],
    dict[str, str],
    dict[str, str],
    str,
]:
    authorized_chapters = {
        str(key): str(text)
        for key, text in (
            episode.get("authorized_source_chapters")
            if isinstance(episode.get("authorized_source_chapters"), dict)
            else {}
        ).items()
        if str(text)
    }

    source_evidence: list[dict[str, Any]] = []
    propositions: list[dict[str, Any]] = []
    adaptation_decisions: list[dict[str, Any]] = []
    source_prop_by_statement: dict[str, str] = {}
    adapted_prop_by_statement: dict[str, str] = {}
    event_source_evidence_id: dict[str, str] = {}
    event_source_prop_id: dict[str, str] = {}
    event_adapted_prop_id: dict[str, str] = {}
    event_decision_id: dict[str, str] = {}
    environment_subject_id = system_environment_entity_id(
        episode.get("id") or f"episode-{episode_no}"
    )
    event_participant_ids: dict[str, list[str]] = {}
    event_state_subject_ids: dict[str, list[str]] = {}

    for position, event in enumerate(value.events, start=1):
        chapter_id, start, end, exact_excerpt = _source_location(
            event.source_excerpt,
            source_text=source_text,
            source_segment_ids=event.source_segment_ids,
            segments=segments,
            authorized_source_chapters=authorized_chapters,
        )
        evidence_id = f"SE-{position}"
        event_source_evidence_id[event.key] = evidence_id
        source_evidence.append({
            "source_evidence_id": evidence_id,
            "source_span": {
                "chapter_id": chapter_id,
                "start": start,
                "end": end,
            },
            "verbatim_excerpt": exact_excerpt,
            "confidence": 1.0,
        })

        actor_ids = [
            identity_resolver.id(token) for token in event.actor_keys
            if str(token).strip() != "audience"
        ]
        target_ids = [
            identity_resolver.id(token) for token in event.target_keys
            if str(token).strip() != "audience"
        ]
        speaker_ids = list(dict.fromkeys([
            final_identity_ids[key]
            for key in event_speaker_keys.get(event.key, [])
        ]))
        content_owner_ids = list(dict.fromkeys([
            identity_resolver.id(token)
            for token in event.text_provenance.content_owner_keys
        ]))
        if typed_visual_unit_contract:
            if event.environment_only:
                if event.state_subject_keys:
                    raise ScreenplayIRFidelityError(
                        f"IR {format_version} event {event.key} 同时声明"
                        "state_subject_keys 与 environment_only"
                    )
                state_subject_ids = [environment_subject_id]
            else:
                subject_keys = list(dict.fromkeys(event.state_subject_keys))
                if (
                    not subject_keys
                    and event.text_provenance.kind
                    in ATTRIBUTED_TEXT_PROVENANCE_KINDS
                    and content_owner_ids
                ):
                    # 归属型文字的状态主体就是它的归属方：木牌上的「杂」属于宗门，
                    # 不属于任何在场人物。下面的非 typed 分支本来就用
                    # content_owner_ids 兜底，typed 分支不该反而无路可走
                    # （生产上 EP2 每轮都卡在 bp-sc005:SRC0020:008）。
                    state_subject_ids = list(content_owner_ids)
                elif (
                    not subject_keys
                    or any(key not in event.actor_keys for key in subject_keys)
                ):
                    raise ScreenplayIRFidelityError(
                        f"IR {format_version} event {event.key} 缺少"
                        " exact-unit typed actor state_subject_keys"
                    )
                else:
                    state_subject_ids = [
                        identity_resolver.id(subject_key)
                        for subject_key in subject_keys
                    ]
        else:
            non_actor_subject_ids = list(dict.fromkeys([
                *speaker_ids,
                *content_owner_ids,
            ]))
            state_subject_ids = [(
                actor_ids
                or target_ids
                or (
                    non_actor_subject_ids
                    if len(non_actor_subject_ids) == 1
                    else []
                )
                or [environment_subject_id]
            )[0]]
        event_state_subject_ids[event.key] = state_subject_ids
        participants = list(dict.fromkeys([
            *actor_ids,
            *target_ids,
            *[
                identity_resolver.id(token) for token in event.onscreen_entity_keys
                if str(token).strip() != "audience"
            ],
            *[
                identity_resolver.id(token)
                for token in event.perceivable_by
                if str(token).strip() != "audience"
            ],
            *speaker_ids,
            *content_owner_ids,
            *[
                identity_resolver.id(delivery.participant_key)
                for delivery in event.participant_deliveries
            ],
            *[
                identity_resolver.id(token)
                for token in scene_by_key[event.scene_key].character_keys
            ],
            *state_subject_ids,
        ]))
        if not participants and not typed_visual_unit_contract:
            participants = [final_identity_ids[ordered_used_keys[0]]]
        event_participant_ids[event.key] = participants

        source_statement = event.source_statement.strip() or exact_excerpt
        source_identity = re.sub(r"\s+", "", source_statement).casefold()
        source_prop_id = source_prop_by_statement.get(source_identity)
        if source_prop_id is None:
            source_prop_id = f"P-SOURCE-{len(source_prop_by_statement) + 1}"
            source_prop_by_statement[source_identity] = source_prop_id
            propositions.append({
                "proposition_id": source_prop_id,
                "semantic_identity_key": _semantic_key(
                    "source_canon", source_statement,
                ),
                "canonical_statement": source_statement,
                "narrative_domain": "source_canon",
                "entity_ids": participants,
                "direct_source_evidence_ids": [evidence_id],
                "domain_truth_status": "true",
            })
        else:
            existing = next(
                item for item in propositions
                if item["proposition_id"] == source_prop_id
            )
            existing["entity_ids"] = list(dict.fromkeys([
                *existing["entity_ids"], *participants,
            ]))
            existing["direct_source_evidence_ids"] = list(dict.fromkeys([
                *existing["direct_source_evidence_ids"], evidence_id,
            ]))
        event_source_prop_id[event.key] = source_prop_id

        adapted_statement = event.adapted_statement.strip()
        adapted_identity = re.sub(r"\s+", "", adapted_statement).casefold()
        adapted_prop_id = adapted_prop_by_statement.get(adapted_identity)
        if adapted_prop_id is None:
            adapted_prop_id = f"P-ADAPTED-{len(adapted_prop_by_statement) + 1}"
            adapted_prop_by_statement[adapted_identity] = adapted_prop_id
            propositions.append({
                "proposition_id": adapted_prop_id,
                "semantic_identity_key": _semantic_key(
                    "adapted_story", adapted_statement,
                ),
                "canonical_statement": adapted_statement,
                "narrative_domain": "adapted_story",
                "entity_ids": participants,
                "direct_source_evidence_ids": [],
                "domain_truth_status": "true",
            })
        else:
            existing = next(
                item for item in propositions
                if item["proposition_id"] == adapted_prop_id
            )
            existing["entity_ids"] = list(dict.fromkeys([
                *existing["entity_ids"], *participants,
            ]))
        event_adapted_prop_id[event.key] = adapted_prop_id

        decision_id = f"AD-{position}"
        event_decision_id[event.key] = decision_id
        adaptation_decisions.append({
            "adaptation_decision_id": decision_id,
            "source_proposition_ids": [source_prop_id],
            "adapted_proposition_ids": [adapted_prop_id],
            "relation": (
                event.adaptation_relation
                if event.adaptation_relation in {
                    "preserve", "condense", "split", "combine", "transform",
                    "omit", "invent", "other",
                }
                else "other"
            ),
            "custom_relation": (
                None
                if event.adaptation_relation in {
                    "preserve", "condense", "split", "combine", "transform",
                    "omit", "invent",
                }
                else event.adaptation_relation
            ),
            "creative_reason": event.adaptation_reason,
            "protected_causal_effect_ids": [adapted_prop_id],
            "affected_event_ids": [event_ids[event.key]],
            "uncertainty": None,
        })
    return (
        source_evidence,
        propositions,
        adaptation_decisions,
        event_source_evidence_id,
        event_state_subject_ids,
        event_participant_ids,
        event_source_prop_id,
        event_adapted_prop_id,
        event_decision_id,
        environment_subject_id,
    )


def _ir_compute_prop_order_and_render_policy(
    value: ScreenplayGenerationIR,
    event_adapted_prop_id: dict[str, str],
    strict_unit_ownership: bool,
    compiler_audit: list[dict[str, Any]],
) -> tuple[list[str], str, str, dict[str, str]]:
    adapted_ids_in_order = list(dict.fromkeys(
        event_adapted_prop_id[event.key] for event in value.events
    ))
    final_adapted_prop_id = adapted_ids_in_order[-1]
    first_adapted_prop_id = adapted_ids_in_order[0]

    effective_render_policy = {
        event.key: event.render_policy
        for event in value.events
    }
    if strict_unit_ownership:
        changed_render_policy_keys: list[str] = []
        for event_index, event in enumerate(value.events):
            if (
                event.narrative_layer != "story"
                or event.render_policy == "exclude_from_spine"
            ):
                continue
            next_event = (
                value.events[event_index + 1]
                if event_index + 1 < len(value.events)
                else None
            )
            projected_policy = (
                "merge_adjacent"
                if (
                    next_event is not None
                    and next_event.scene_key == event.scene_key
                    and next_event.narrative_layer == "story"
                    and next_event.render_policy
                    != "exclude_from_spine"
                )
                else "standalone"
            )
            effective_render_policy[event.key] = projected_policy
            if projected_policy != event.render_policy:
                changed_render_policy_keys.append(event.key)
        if changed_render_policy_keys:
            compiler_audit.append({
                "path": "events[*].render_policy",
                "operation": "project_contiguous_delivery_merge_policy",
                "count": len(changed_render_policy_keys),
                "sample_event_keys": changed_render_policy_keys[:20],
                "reason": (
                    "strict_unit_events_retain_identity_and_traceability_"
                    "while_same_scene_delivery_tasks_may_share_one_shot"
                ),
            })
    return (
        adapted_ids_in_order, final_adapted_prop_id, first_adapted_prop_id,
        effective_render_policy,
    )
