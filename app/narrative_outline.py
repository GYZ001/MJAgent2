"""Deterministic ShotTask projection from an approved narrative graph."""
from __future__ import annotations

from collections import defaultdict
import re
from typing import Any

from app import config
from app.narrative import (
    action_participant_delivery_errors,
)
from app.schemas import (
    Bible,
    EpisodeScreenplay,
    StoryboardOutline,
    StoryboardOutlineShot,
)


from .narrative_outline_finalize import _finalize_outline_windows_and_shots
from .narrative_outline_identity import _prepare_outline_identity_context
from .narrative_outline_index import (
    _build_outline_base_shot_indices,
    _build_outline_event_order,
    _build_outline_key_line_catalog,
    _canonicalize_outline_event_ids,
)
from .narrative_outline_nodes import _build_outline_nodes
from .narrative_outline_prep import (
    _build_outline_audience_path_index,
    _build_outline_event_lookup_indices,
    _resolve_outline_key_event_mapping,
)
from .narrative_outline_project import _project_outline_shots
from .narrative_outline_state import _build_outline_projection_state, _assign_outline_shot_numbers

def _relation_text(value: object) -> str:
    """Normalize text only for exact graph-relation joins."""
    return "".join(
        character.casefold()
        for character in str(value or "")
        if character.isalnum()
    )


def _narrative_key_line_catalog(
    screenplay: EpisodeScreenplay,
) -> dict[str, tuple[str, str, str]]:
    """Return stable key-line IDs with their canonical speaker and line."""
    catalog: dict[str, tuple[str, str, str]] = {}
    for position, raw_line in enumerate(screenplay.key_lines or [], start=1):
        text = str(raw_line or "").strip()
        speaker, separator, spoken = text.partition("：")
        if not separator:
            speaker, separator, spoken = text.partition(":")
        if (
            separator
            and speaker.strip()
            and spoken.strip()
            and speaker.strip() != "旁白"
        ):
            catalog[f"KL{position:02d}"] = (
                speaker.strip(),
                spoken.strip(),
                text,
            )
    return catalog


def _action_key_line_ids(
    action_ids: list[str],
    actions: dict[str, Any],
    catalog: dict[str, tuple[str, str, str]],
) -> list[str]:
    """Join dialogue to actions by exact speaker and spoken-text relations."""
    action_parts = [
        text
        for action_id in action_ids
        for action in [actions.get(action_id)]
        if action is not None
        for text in (
            str(action.semantic_intent or "").strip(),
            str(action.completion_condition or "").strip(),
        )
        if text
    ]
    action_text = _relation_text("；".join(action_parts))
    if not action_text:
        return []
    quoted_lines = {
        _relation_text(match)
        for part in action_parts
        for match in re.findall(r"[「“『\"]([^」”』\"]+)[」”』\"]", part)
        if _relation_text(match)
    }
    catalog_entries = [
        (key_id, _relation_text(speaker), _relation_text(spoken))
        for key_id, (speaker, spoken, _canonical) in catalog.items()
        if _relation_text(speaker) and _relation_text(spoken)
    ]
    matched_modes: dict[str, str] = {}
    for quoted_line in quoted_lines:
        exact_ids = [
            key_id
            for key_id, speaker_relation, spoken_relation in catalog_entries
            if speaker_relation in action_text and spoken_relation == quoted_line
        ]
        if exact_ids:
            matched_modes.update((key_id, "exact") for key_id in exact_ids)
            continue
        # A long canonical turn may be split into adjacent key lines. Accept
        # fragments only when the complete contiguous sequence rebuilds it.
        for start, (_key_id, speaker_relation, _spoken_relation) in enumerate(
            catalog_entries
        ):
            if speaker_relation not in action_text:
                continue
            combined = ""
            fragment_ids: list[str] = []
            for key_id, candidate_speaker, spoken_relation in catalog_entries[start:]:
                if candidate_speaker != speaker_relation:
                    break
                combined += spoken_relation
                if not quoted_line.startswith(combined):
                    break
                fragment_ids.append(key_id)
                if combined == quoted_line:
                    if len(fragment_ids) >= 2:
                        for fragment_id in fragment_ids:
                            matched_modes.setdefault(fragment_id, "contiguous_fragments")
                    break
    return [
        key_id
        for key_id, _speaker, _spoken in catalog_entries
        if key_id in matched_modes
    ]


def reconcile_narrative_outline_action_deliveries(
    outline: StoryboardOutline,
    screenplay: EpisodeScreenplay,
    *,
    infer_unprojected_event_actions: bool = False,
) -> list[dict[str, Any]]:
    """Keep event dialogue IDs bound to the actions that explicitly own them.

    Plot-spine delivery IDs are a compatibility projection and can drift from
    the newer narrative graph.  When an atomic action explicitly contains both
    a canonical speaker and canonical spoken line, that exact relation is the
    authority.  Dialogue-only capacity splits retain ownership; all other
    stale IDs are removed from the event before duration and identity fields
    are derived.
    """
    plan = screenplay.narrative_plan
    catalog = _narrative_key_line_catalog(screenplay)
    if plan is None or not outline.shots or not catalog:
        return []
    actions = {
        action.action_id: action
        for action in plan.atomic_actions
    }
    event_actions = {
        event.event_id: list(event.action_ids)
        for event in plan.events
    }

    def shot_event_ids(shot: StoryboardOutlineShot) -> list[str]:
        return list(dict.fromkeys(
            event_id
            for event_id in [
                *(str(value or "").strip() for value in shot.event_ids),
                str(shot.story_event_id or "").strip(),
            ]
            if event_id
        ))

    shots_by_event: defaultdict[str, list[StoryboardOutlineShot]] = defaultdict(list)
    for shot in outline.shots:
        for event_id in shot_event_ids(shot):
            shots_by_event[event_id].append(shot)

    authoritative_ids_by_event = {
        event_id: _action_key_line_ids(action_ids, actions, catalog)
        for event_id, action_ids in event_actions.items()
    }
    authoritative_events_by_key: defaultdict[str, set[str]] = defaultdict(set)
    for event_id, key_ids in authoritative_ids_by_event.items():
        for key_id in key_ids:
            authoritative_events_by_key[key_id].add(event_id)
    exclusive_authority = {
        key_id: next(iter(event_ids))
        for key_id, event_ids in authoritative_events_by_key.items()
        if len(event_ids) == 1
    }

    changes: list[dict[str, Any]] = []
    for event_id, event_shots in shots_by_event.items():
        authoritative_ids = authoritative_ids_by_event.get(event_id, [])
        authoritative_set = set(authoritative_ids)

        explicit_action_ids = {
            id(shot): list(dict.fromkeys(filter(None, [
                shot.primary_action_id,
                *shot.supporting_action_ids,
            ])))
            for shot in event_shots
        }
        dialogue_owners: set[str] = set()
        for shot in event_shots:
            is_unprojected_action_owner = bool(
                infer_unprojected_event_actions
                and not explicit_action_ids[id(shot)]
                and not str(shot.shot_id or "").strip()
                and shot.shot_contribution is None
                and event_actions.get(event_id)
            )
            if explicit_action_ids[id(shot)] or is_unprojected_action_owner:
                continue
            dialogue_owners.update(
                str(key_id or "").strip().upper()
                for key_id in shot.key_line_ids
                if str(key_id or "").strip().upper() in authoritative_set
            )

        assigned_action_ids: set[str] = set(dialogue_owners)
        for shot in event_shots:
            action_ids = explicit_action_ids[id(shot)]
            if (
                not action_ids
                and infer_unprojected_event_actions
                and not str(shot.shot_id or "").strip()
                and shot.shot_contribution is None
            ):
                action_ids = event_actions.get(event_id, [])
            matched_ids = _action_key_line_ids(action_ids, actions, catalog)
            if action_ids and authoritative_set:
                desired_ids = [
                    key_id
                    for key_id in matched_ids
                    if key_id not in assigned_action_ids
                ]
                assigned_action_ids.update(desired_ids)
            else:
                desired_ids = [
                    str(key_id or "").strip().upper()
                    for key_id in shot.key_line_ids
                    if (
                        exclusive_authority.get(
                            str(key_id or "").strip().upper(),
                            event_id,
                        ) == event_id
                    )
                ]
            current_ids = [
                str(key_id or "").strip().upper()
                for key_id in shot.key_line_ids
                if str(key_id or "").strip()
            ]
            if current_ids == desired_ids:
                continue
            shot.key_line_ids = desired_ids
            shot.audio_cast = list(dict.fromkeys(
                catalog[key_id][0]
                for key_id in desired_ids
            ))
            changes.append({
                "shot_no": shot.shot_no,
                "event_id": event_id,
                "field": "key_line_ids",
                "from": current_ids,
                "to": desired_ids,
                "reason": "atomic_action_dialogue_relation",
            })
    return changes


def narrative_outline_action_delivery_errors(
    outline: StoryboardOutline,
    screenplay: EpisodeScreenplay,
) -> list[str]:
    """Validate exact action-to-dialogue ownership after all outline rewrites."""
    plan = screenplay.narrative_plan
    catalog = _narrative_key_line_catalog(screenplay)
    if plan is None or not outline.shots or not catalog:
        return []
    actions = {action.action_id: action for action in plan.atomic_actions}
    expected_by_event = {
        event.event_id: _action_key_line_ids(
            list(event.action_ids),
            actions,
            catalog,
        )
        for event in plan.events
    }
    events_by_key: defaultdict[str, set[str]] = defaultdict(set)
    for event_id, key_ids in expected_by_event.items():
        for key_id in key_ids:
            events_by_key[key_id].add(event_id)
    errors: list[str] = []
    for event in plan.events:
        expected = expected_by_event[event.event_id]
        if not expected:
            continue
        event_shots = [
            shot
            for shot in outline.shots
            if event.event_id in {
                *(str(value or "").strip() for value in shot.event_ids),
                str(shot.story_event_id or "").strip(),
            }
        ]
        actual = list(dict.fromkeys(
            str(key_id or "").strip().upper()
            for shot in event_shots
            for key_id in shot.key_line_ids
            if str(key_id or "").strip()
        ))
        missing = [key_id for key_id in expected if key_id not in actual]
        misplaced = [
            key_id
            for key_id in actual
            if (
                len(events_by_key.get(key_id, set())) == 1
                and event.event_id not in events_by_key[key_id]
            )
        ]
        if missing or misplaced:
            errors.append(
                "[OUTLINE_ACTION_DIALOGUE_RELATION_MISMATCH] "
                f"事件 {event.event_id} 的原子动作明确绑定台词 {expected}，"
                f"当前镜头交付为 {actual}，缺失 {missing}，错属 {misplaced}；"
                "请按 action_id 的说话人和原句关系重投影"
            )
    return errors


def normalize_split_action_owner_completions(
    outline: StoryboardOutline,
    screenplay: EpisodeScreenplay,
) -> list[dict[str, Any]]:
    """Fill missing terminal-action directing prose without overwriting it.

    Action/state IDs are graph authority, while ``beat``/``covers`` and the
    visible action prose are director-owned presentation.  Replacing an
    already directed post-dialogue result with the action's completion text
    can turn a reaction/result shot into a verbatim duplicate of the dialogue
    shot.  That duplicate cannot be repaired by the later scene-pack model,
    because the scene-pack contract correctly treats outline prose as fixed.
    """
    plan = screenplay.narrative_plan
    if plan is None:
        return []

    dialogue_event_ids = {
        event_id
        for shot in outline.shots
        if shot.key_line_ids
        for event_id in (
            list(shot.event_ids)
            or ([shot.story_event_id] if shot.story_event_id else [])
        )
    }
    actions = {
        action.action_id: action
        for action in plan.atomic_actions
    }
    changes: list[dict[str, Any]] = []
    for shot in outline.shots:
        event_ids = (
            list(shot.event_ids)
            or ([shot.story_event_id] if shot.story_event_id else [])
        )
        if (
            not shot.primary_action_id
            or shot.key_line_ids
            or shot.audio_cast
            or not dialogue_event_ids.intersection(event_ids)
        ):
            continue
        action_ids = [
            shot.primary_action_id,
            *shot.supporting_action_ids,
        ]
        completion_parts = [
            str(action.completion_condition or "").strip()
            for action_id in action_ids
            for action in [actions.get(action_id)]
            if action is not None
            and str(action.completion_condition or "").strip()
        ]
        completion = "；".join(dict.fromkeys(completion_parts))
        if not completion:
            continue
        for field, value in (
            ("state_in", ""),
            ("primary_action", completion),
            ("state_out", completion),
            ("beat", completion),
            ("covers", completion),
        ):
            current = getattr(shot, field)
            if str(current or "").strip() or current == value:
                continue
            setattr(shot, field, value)
            changes.append({
                "shot_no": shot.shot_no,
                "field": field,
                "from": current,
                "to": value,
                "reason": "split_event_action_completion",
            })
    return changes


def normalize_narrative_storyboard_outline(
    outline: StoryboardOutline,
    screenplay: EpisodeScreenplay,
    *,
    bible: Bible | None = None,
    preserve_shot_ids: bool = False,
) -> list[dict[str, Any]]:
    """Project graph-owned fields while preserving model-authored directing text.

    The model chooses the visible beat, scene presentation and legacy delivery
    fields. Event state replay, action ownership, audience staging, cumulative
    ledgers, capacity and boundaries are deterministic consequences of the
    published narrative plan.

    This is an orchestrator: each phase of the original ~1,318-line single
    function is now a standalone helper function, split across sibling
    modules by what it reads/writes (see each sibling's module docstring):
    narrative_outline_identity.py (event/action/identity indices + legacy
    visible-identity projection), narrative_outline_index.py (event-id
    canonicalization, event ordering, key-line cataloging, base-shot
    indexing), narrative_outline_prep.py (key-line-to-event resolution,
    audience-path/target-delta indexing, per-event lookup tables),
    narrative_outline_nodes.py (per-event ShotTask-node synthesis),
    narrative_outline_state.py (shot numbering + projection-state setup),
    narrative_outline_project.py (per-node shot projection -- the original
    function's second-largest loop), narrative_outline_finalize.py
    (readability-window shot-id projection + final assembly). The sequence
    and the data each phase reads/writes is unchanged from the pre-split
    source; only the decomposition into named, independently
    readable/testable steps is new.
    """
    plan = screenplay.narrative_plan
    if plan is None or not outline.shots or not plan.events:
        return []

    (
        events, actions, identity_contracts, legacy_events,
        legacy_event_text_identity_ids, legacy_event_relation_ids,
        legacy_action_relation_changes, compiler_context_identity_names,
    ) = _prepare_outline_identity_context(screenplay, plan, bible)

    _canonicalize_outline_event_ids(outline, events)
    event_order = _build_outline_event_order(plan)
    key_line_meta, chain_key_ids = _build_outline_key_line_catalog(screenplay)
    base_by_event, bases_by_event = _build_outline_base_shot_indices(
        outline, events, event_order,
    )

    already_projected = any(
        str(shot.shot_id or "").strip()
        and shot.shot_contribution is not None
        for shot in outline.shots
    )
    action_delivery_changes = reconcile_narrative_outline_action_deliveries(
        outline,
        screenplay,
        infer_unprojected_event_actions=not already_projected,
    )
    required_events = [
        item.event_id
        for item in plan.events
        if item.must_keep and item.delivery_policy == "deliver"
    ]
    if any(event_id not in base_by_event for event_id in required_events):
        return []

    key_event, chain_events, key_ids_by_event = _resolve_outline_key_event_mapping(
        screenplay, plan, event_order, bases_by_event, key_line_meta, chain_key_ids,
    )
    paths, delta_paths, _delta_destinations = _build_outline_audience_path_index(
        plan, event_order,
    )
    (
        deltas_by_event, evidence_by_event, character_state_ids_by_event,
        window_seconds_by_event,
    ) = _build_outline_event_lookup_indices(plan, delta_paths)

    nodes = _build_outline_nodes(
        plan, base_by_event, bases_by_event, already_projected,
        key_ids_by_event, key_line_meta, actions, deltas_by_event,
        delta_paths, evidence_by_event, character_state_ids_by_event,
    )
    _assign_outline_shot_numbers(nodes, preserve_shot_ids)

    state = _build_outline_projection_state(
        nodes, deltas_by_event, delta_paths, paths, plan, events, actions,
        screenplay, bible, compiler_context_identity_names,
        action_delivery_changes, legacy_action_relation_changes,
    )
    _project_outline_shots(
        nodes, event_order, state, events, actions, plan, screenplay, bible,
        identity_contracts, compiler_context_identity_names,
        legacy_event_relation_ids, legacy_event_text_identity_ids,
        key_line_meta, deltas_by_event, delta_paths, evidence_by_event,
        character_state_ids_by_event, paths, window_seconds_by_event,
    )

    _finalize_outline_windows_and_shots(
        outline, plan, state.normalized_shots, state.positions_by_event,
        state.delta_owner_position,
    )
    state.changes.extend(
        normalize_split_action_owner_completions(
            outline,
            screenplay,
        )
    )
    return state.changes




def compile_narrative_storyboard_outline(
    screenplay: EpisodeScreenplay,
) -> StoryboardOutline:
    """Compile one compact event skeleton without asking a model for ledgers.

    The screenplay already owns event order, scene boundaries, source
    ownership, dialogue IDs and state transitions. The outline model used to
    restate all of those fields in one very large JSON response. This compiler
    keeps only the directing prose needed by the later scene-pack stage; the
    existing narrative projection then derives every graph-owned field.
    """
    plan = screenplay.narrative_plan
    scenes = list(screenplay.scene_outline or [])
    events = list(plan.events if plan is not None else [])
    scene_contracts = list(plan.scene_contracts if plan is not None else [])
    if plan is None:
        raise ValueError("叙事剧本缺少 narrative_plan")
    if not events:
        raise ValueError("叙事剧本没有可编译事件")
    participant_errors = action_participant_delivery_errors(screenplay)
    if participant_errors:
        raise ValueError("；".join(participant_errors))
    if not scenes:
        raise ValueError("叙事剧本没有场次结构")
    if len(scene_contracts) != len(scenes):
        raise ValueError(
            "scene_outline 与 narrative_plan.scene_contracts 数量不一致："
            f"{len(scenes)} != {len(scene_contracts)}"
        )
    if len(events) < len(scenes):
        raise ValueError(
            "叙事事件少于场次数，无法保证每场至少一个镜头："
            f"{len(events)} < {len(scenes)}"
        )

    event_positions = {
        event.event_id: position
        for position, event in enumerate(events)
    }
    scene_by_event: dict[str, int] = {}
    cursor = 0
    for scene_index, contract in enumerate(scene_contracts):
        remaining_scenes = len(scenes) - scene_index - 1
        latest_allowed = len(events) - remaining_scenes - 1
        declared_turns = [
            event_positions[event_id]
            for event_id in contract.turn_event_ids
            if (
                event_id in event_positions
                and cursor <= event_positions[event_id] <= latest_allowed
            )
        ]
        if scene_index == len(scenes) - 1:
            end = len(events) - 1
        elif declared_turns:
            end = min(max(declared_turns), latest_allowed)
        else:
            end = min(cursor, latest_allowed)
        if end < cursor:
            raise ValueError(
                f"场次 {contract.scene_id} 没有可分配的顺序事件"
            )
        for position in range(cursor, end + 1):
            scene_by_event[events[position].event_id] = scene_index
        cursor = end + 1
    if cursor != len(events):
        raise ValueError(
            f"场次合同只覆盖 {cursor}/{len(events)} 个叙事事件"
        )

    legacy_events = {
        event.event_id: event
        for event in screenplay.events or []
    }

    def source_ids(value: str) -> set[str]:
        return set(re.findall(r"SRC\d+", str(value or "")))

    event_source_ids = {
        event.event_id: source_ids(
            legacy_events.get(event.event_id).source_span
            if event.event_id in legacy_events else ""
        )
        for event in events
    }
    beats_by_event: defaultdict[str, list[Any]] = defaultdict(list)
    beats = list(
        screenplay.plot_spine.spine_beats
        if screenplay.plot_spine is not None else []
    )
    for beat_position, beat in enumerate(beats):
        beat_sources = set(beat.source_segment_ids or [])
        _score, _distance, selected_event_id = max(
            (
                len(beat_sources & event_source_ids[event.event_id]),
                -abs(beat_position - event_position),
                event.event_id,
            )
            for event_position, event in enumerate(events)
        )
        beats_by_event[selected_event_id].append(beat)

    key_line_catalog: dict[str, str] = {}
    for key_position, line in enumerate(screenplay.key_lines or [], start=1):
        text = str(line or "").strip()
        speaker, separator, _spoken = text.partition("：")
        if not separator:
            speaker, separator, _spoken = text.partition(":")
        if text and separator and speaker.strip() != "旁白":
            key_line_catalog[f"KL{key_position:02d}"] = text
    narrative_key_lines = _narrative_key_line_catalog(screenplay)
    actions_by_id = {
        action.action_id: action
        for action in plan.atomic_actions
    }

    evidence_by_event: defaultdict[str, list[str]] = defaultdict(list)
    for evidence in plan.evidence:
        if evidence.anchor.type == "event":
            evidence_by_event[evidence.anchor.id].append(
                evidence.observable_claim
            )
    information_by_event: defaultdict[str, list[str]] = defaultdict(list)
    for item in screenplay.information_ledger or []:
        if item.event_id:
            information_by_event[item.event_id].append(item.info_id)

    def scene_parts(scene_heading: str) -> tuple[str, str]:
        heading = re.sub(
            r"^【场[^】]*】\s*",
            "",
            str(scene_heading or ""),
        ).strip()
        parts = re.split(r"\s*[／/]\s*", heading, maxsplit=1)
        if len(parts) == 2:
            return parts[0].strip(), parts[1].strip()
        return "", heading

    shots: list[StoryboardOutlineShot] = []
    for shot_no, event in enumerate(events, start=1):
        scene_index = scene_by_event[event.event_id]
        scene = scenes[scene_index]
        scene_contract = scene_contracts[scene_index]
        legacy = legacy_events.get(event.event_id)
        event_beats = beats_by_event[event.event_id]
        spine_key_line_ids = list(dict.fromkeys(
            key_line_id
            for beat in event_beats
            for key_line_id in beat.key_line_ids
            if key_line_id in key_line_catalog
        ))
        action_key_line_ids = _action_key_line_ids(
            list(event.action_ids),
            actions_by_id,
            narrative_key_lines,
        )
        event_key_line_ids = (
            action_key_line_ids
            if action_key_line_ids
            else spine_key_line_ids
        )
        event_information_ids = list(dict.fromkeys([
            *information_by_event[event.event_id],
            *[
                info_id
                for beat in event_beats
                for info_id in beat.information_ids
            ],
        ]))
        scene_time, scene_name = scene_parts(scene.scene_heading)
        observable = next(
            (
                claim.strip()
                for claim in evidence_by_event[event.event_id]
                if claim.strip()
            ),
            "",
        )
        visible_change = str(
            getattr(legacy, "visible_change", "") or ""
        ).strip()
        trigger = str(getattr(legacy, "trigger", "") or "").strip()
        beat_text = (
            visible_change
            or observable
            or trigger
            or scene.summary
        )
        covers = "；".join(dict.fromkeys(filter(None, [
            *[
                str(beat.does or "").strip()
                for beat in event_beats
            ],
            observable,
            visible_change,
        ])))
        speakers = list(dict.fromkeys(
            key_line_catalog[key_line_id].partition("：")[0].strip()
            for key_line_id in event_key_line_ids
            if key_line_id in key_line_catalog
        ))
        previous_scene_id = shots[-1].scene_id if shots else ""
        shots.append(StoryboardOutlineShot(
            shot_no=shot_no,
            scene_time=scene_time,
            scene_name=scene_name,
            scene_setting=(
                f"{scene_time}，{scene_name}"
                if scene_time and scene_name else scene_name or scene_time
            ),
            beat=beat_text,
            covers=covers or beat_text,
            story_event_id=event.event_id,
            spine_beat_ids=[
                beat.beat_id
                for beat in event_beats
                if beat.beat_id
            ],
            key_line_ids=event_key_line_ids,
            information_ids=event_information_ids,
            new_information_ids=event_information_ids,
            state_in=(
                str(getattr(legacy, "state_in", "") or "").strip()
                or scene.entry_state
            ),
            primary_action=trigger or visible_change or observable,
            state_out=(
                str(getattr(legacy, "state_out", "") or "").strip()
                or scene.exit_state
            ),
            continuity_mode=(
                "scene_change"
                if previous_scene_id != scene_contract.scene_id
                else "same_scene_cut"
            ),
            duration_s=config.DEFAULT_VIDEO_DURATION_S,
            characters_visible=[],
            audio_cast=speakers,
            purpose=observable or scene.story_function,
            resulting_change=(
                str(getattr(legacy, "state_out", "") or "").strip()
                or scene.exit_state
            ),
            readability_focus=(
                "dialogue" if event_key_line_ids else "action"
            ),
            scene_id=scene_contract.scene_id,
            event_ids=[event.event_id],
        ))

    outline = StoryboardOutline(
        episode_no=screenplay.episode_no,
        shots=shots,
    )
    object.__setattr__(
        outline,
        "_compile_audit",
        {
            "compiler": "narrative-storyboard-outline.v2",
            "event_count": len(events),
            "scene_count": len(scenes),
            "base_shot_count": len(shots),
            "model_calls": 0,
        },
    )
    return outline
