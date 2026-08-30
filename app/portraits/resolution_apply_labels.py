"""把身份决议写回剧本文本的标签级替换工具：候选标签替换、
列表分隔标签替换、叙事计划身份替换与重复 identity contract 合并。
"""

from __future__ import annotations

import re

from ._identity_tokens import (
    _IDENTITY_LIST_SEPARATOR_PATTERN,
    _project_identity_token,
)

def _replace_resolved_label(text: str, source_label: str, canonical_name: str) -> str:
    if not text or source_label == canonical_name:
        return text
    # Identity normalization can run at several durable pipeline boundaries
    # (candidate, normalized working copy, approved publication).  Preserve an
    # already-canonical occurrence before matching its source alias so mappings
    # such as ``美 -> 卢美`` cannot grow another ``卢`` on every pass.
    prefix, separator, suffix = canonical_name.partition(source_label)
    if separator:
        if prefix and suffix:
            repeated = (
                rf"(?:{re.escape(prefix)}){{2,}}"
                rf"{re.escape(source_label)}"
                rf"(?:{re.escape(suffix)}){{2,}}"
            )
            text = re.sub(repeated, canonical_name, text)
        elif prefix:
            text = re.sub(
                rf"(?:{re.escape(prefix)}){{2,}}{re.escape(source_label)}",
                canonical_name,
                text,
            )
        elif suffix:
            text = re.sub(
                rf"{re.escape(source_label)}(?:{re.escape(suffix)}){{2,}}",
                canonical_name,
                text,
            )
    pattern = re.compile(
        rf"{re.escape(canonical_name)}|{re.escape(source_label)}"
    )
    return pattern.sub(
        lambda match: (
            canonical_name
            if match.group(0) == source_label
            else match.group(0)
        ),
        text,
    )



def _replace_identity_list_label(
    value: str,
    source_label: str,
    canonical_name: str,
) -> str:
    """Apply one authority decision to exact ``who`` identity tokens."""
    parts = _IDENTITY_LIST_SEPARATOR_PATTERN.split(str(value or ""))
    return "".join(
        part
        if _IDENTITY_LIST_SEPARATOR_PATTERN.fullmatch(part or "") is not None
        else _project_identity_token(part, source_label, canonical_name)
        for part in parts
    )


def _replace_screenplay_body_label(
    text: str,
    source_label: str,
    canonical_name: str,
    *,
    replace_prose: bool = True,
    replace_speaker: bool = True,
) -> str:
    """改剧本正文中的角色身份，不改其他角色说出的台词内容。"""
    lines: list[str] = []
    speaker_pattern = re.compile(
        rf"^(?P<indent>\s*){re.escape(source_label)}(?P<emotion>[\(（][^\)）]{{0,16}}[\)）])?(?P<colon>[:：])"
    )
    any_dialogue_pattern = re.compile(
        r"^\s*[\u3400-\u9fffA-Za-z0-9_·•・·-]{1,16}(?:[\(（][^\)）]{0,16}[\)）])?[:：]"
    )
    for line in (text or "").splitlines(keepends=True):
        if replace_speaker and speaker_pattern.match(line):
            line = speaker_pattern.sub(
                lambda match: (
                    f"{match.group('indent')}{canonical_name}"
                    f"{match.group('emotion') or ''}{match.group('colon')}"
                ),
                line,
                count=1,
            )
        elif replace_prose and not any_dialogue_pattern.match(line):
            line = _replace_resolved_label(line, source_label, canonical_name)
        lines.append(line)
    return "".join(lines)


def _restore_non_dialogue_prefix(
    text: str,
    source_label: str,
    canonical_name: str,
    *,
    authoritative_lines: set[str],
) -> str:
    """Restore a structural prefix previously mistaken for a speaker."""
    prefix = re.compile(
        rf"^(?P<indent>\s*){re.escape(canonical_name)}(?P<colon>[:：])"
        r"(?P<line>.*)$"
    )
    lines: list[str] = []
    for raw_line in (text or "").splitlines(keepends=True):
        line = raw_line.rstrip("\r\n")
        ending = raw_line[len(line):]
        match = prefix.match(line)
        if (
            match is not None
            and match.group("line").strip() not in authoritative_lines
        ):
            line = (
                f"{match.group('indent')}{source_label}"
                f"{match.group('colon')}{match.group('line')}"
            )
        lines.append(line + ending)
    return "".join(lines)


def _replace_identity_value(value, source_label: str, canonical_name: str):
    """Replace exact identity values recursively without touching source spans."""
    if isinstance(value, str):
        return canonical_name if value == source_label else value
    if isinstance(value, list):
        return [
            _replace_identity_value(item, source_label, canonical_name)
            for item in value
        ]
    if isinstance(value, tuple):
        return tuple(
            _replace_identity_value(item, source_label, canonical_name)
            for item in value
        )
    if isinstance(value, dict):
        return {
            (
                canonical_name if str(key) == source_label else key
            ): _replace_identity_value(item, source_label, canonical_name)
            for key, item in value.items()
        }
    return value


def _identity_value_contains(value, identity: str) -> bool:
    if isinstance(value, str):
        return value == identity
    if isinstance(value, (list, tuple)):
        return any(_identity_value_contains(item, identity) for item in value)
    if isinstance(value, dict):
        return any(
            str(key) == identity or _identity_value_contains(item, identity)
            for key, item in value.items()
        )
    return False


def _replace_narrative_plan_identity(
    plan,
    source_label: str,
    canonical_name: str,
    *,
    replace_display_text: bool = True,
) -> bool:
    """Atomically update every authoritative entity reference in one plan.

    SourceEvidence and direct source excerpts remain immutable.  The mapping is
    AI/project supplied; this routine validates no role vocabulary and merely
    applies one resolved identity consistently across the relation graph.
    """
    if plan is None:
        return False
    before = plan.model_dump(mode="json")

    for contract in plan.identity_contracts:
        if replace_display_text and contract.display_name == source_label:
            contract.display_name = canonical_name
        contract.voice_ids = list(dict.fromkeys(
            canonical_name if voice_id == source_label else voice_id
            for voice_id in contract.voice_ids
        ))
        if replace_display_text:
            contract.evidence.rationale = _replace_resolved_label(
                contract.evidence.rationale, source_label, canonical_name,
            )
    for proposition in plan.propositions:
        proposition.entity_ids = list(dict.fromkeys(
            canonical_name if entity_id == source_label else entity_id
            for entity_id in proposition.entity_ids
        ))
        if replace_display_text:
            proposition.canonical_statement = _replace_resolved_label(
                proposition.canonical_statement, source_label, canonical_name,
            )
    for fact in plan.state_facts:
        if fact.subject_id == source_label:
            fact.subject_id = canonical_name
        fact.value.data = _replace_identity_value(
            fact.value.data, source_label, canonical_name,
        )
    for evidence in plan.evidence:
        evidence.perceivable_by = list(dict.fromkeys(
            canonical_name if entity_id == source_label else entity_id
            for entity_id in evidence.perceivable_by
        ))
        if replace_display_text:
            evidence.observable_claim = _replace_resolved_label(
                evidence.observable_claim, source_label, canonical_name,
            )
        evidence.competing_attention_ids = list(dict.fromkeys(
            canonical_name if entity_id == source_label else entity_id
            for entity_id in evidence.competing_attention_ids
        ))
    for question in plan.dramatic_questions:
        if replace_display_text:
            question.question_text = _replace_resolved_label(
                question.question_text, source_label, canonical_name,
            )
    for action in plan.atomic_actions:
        action.actor_ids = list(dict.fromkeys(
            canonical_name if entity_id == source_label else entity_id
            for entity_id in action.actor_ids
        ))
        action.target_ids = list(dict.fromkeys(
            canonical_name if entity_id == source_label else entity_id
            for entity_id in action.target_ids
        ))
        if replace_display_text:
            for field in ("semantic_intent", "completion_condition", "decision_not_applicable_reason"):
                value = getattr(action, field, None)
                if isinstance(value, str):
                    setattr(action, field, _replace_resolved_label(value, source_label, canonical_name))
            for phase in action.temporal_phases:
                phase.start_condition = _replace_resolved_label(
                    phase.start_condition, source_label, canonical_name,
                )
                phase.end_condition = _replace_resolved_label(
                    phase.end_condition, source_label, canonical_name,
                )
    for event in plan.events:
        event.character_goal_effects = _replace_identity_value(
            event.character_goal_effects, source_label, canonical_name,
        )
    for state in plan.character_states:
        if state.character_id == source_label:
            state.character_id = canonical_name
        state.relationship_state = _replace_identity_value(
            state.relationship_state, source_label, canonical_name,
        )
        state.emotion = _replace_identity_value(
            state.emotion, source_label, canonical_name,
        )
        if replace_display_text:
            state.tactic = _replace_resolved_label(
                state.tactic, source_label, canonical_name,
            )
    for belief in plan.character_beliefs:
        if belief.character_id == source_label:
            belief.character_id = canonical_name
    for prior in plan.audience_priors:
        if replace_display_text:
            prior.audience_description = _replace_resolved_label(
                prior.audience_description, source_label, canonical_name,
            )
        prior.familiarity_assumptions = _replace_identity_value(
            prior.familiarity_assumptions, source_label, canonical_name,
        )
    for state in plan.audience_states:
        for field in (
            "causal_hypotheses",
            "character_goal_hypotheses",
            "spatial_model",
            "temporal_model",
            "working_memory",
            "affective_state",
        ):
            setattr(
                state,
                field,
                _replace_identity_value(
                    getattr(state, field), source_label, canonical_name,
                ),
            )
        state.attention_residue_ids = list(dict.fromkeys(
            canonical_name if entity_id == source_label else entity_id
            for entity_id in state.attention_residue_ids
        ))
    for intent in plan.experience_intents:
        intent.attention_target_ids = list(dict.fromkeys(
            canonical_name if entity_id == source_label else entity_id
            for entity_id in intent.attention_target_ids
        ))
        if replace_display_text:
            intent.director_objective = _replace_resolved_label(
                intent.director_objective, source_label, canonical_name,
            )
            intent.forbidden_misconceptions = [
                _replace_resolved_label(value, source_label, canonical_name)
                for value in intent.forbidden_misconceptions
            ]
    for scene in plan.scene_contracts:
        if scene.point_of_view_character_id == source_label:
            scene.point_of_view_character_id = canonical_name
        scene.relationship_deltas = _replace_identity_value(
            scene.relationship_deltas, source_label, canonical_name,
        )
        if replace_display_text:
            for field in (
                "not_applicable_reason",
                "alternative_dramatic_function",
                "value_polarity_in",
                "value_polarity_out",
                "scene_button",
            ):
                value = getattr(scene, field, None)
                if isinstance(value, str):
                    setattr(scene, field, _replace_resolved_label(value, source_label, canonical_name))
    for arc in plan.arc_contracts:
        if replace_display_text:
            for field in ("not_applicable_reason", "alternative_dramatic_function"):
                value = getattr(arc, field, None)
                if isinstance(value, str):
                    setattr(arc, field, _replace_resolved_label(value, source_label, canonical_name))
        arc.pressure_curve = _replace_identity_value(
            arc.pressure_curve, source_label, canonical_name,
        )
        arc.information_density_curve = _replace_identity_value(
            arc.information_density_curve, source_label, canonical_name,
        )
        arc.processing_beats = _replace_identity_value(
            arc.processing_beats, source_label, canonical_name,
        )
    return plan.model_dump(mode="json") != before


def _merge_duplicate_narrative_identity_contracts(plan) -> list[dict]:
    """Merge aliases that resolve to one canonical display identity."""
    if plan is None:
        return []
    data = plan.model_dump(mode="json")
    contracts = list(data.get("identity_contracts") or [])
    groups: dict[str, list[tuple[int, dict]]] = {}
    for index, contract in enumerate(contracts):
        if not isinstance(contract, dict):
            continue
        display_name = str(contract.get("display_name") or "").strip()
        if display_name:
            groups.setdefault(display_name, []).append((index, contract))

    replacements: dict[str, str] = {}
    merged_by_display: dict[str, dict] = {}
    changes: list[dict] = []
    for display_name, members in groups.items():
        if len(members) < 2:
            continue
        _canonical_index, canonical = max(
            members,
            key=lambda item: (
                int(str(item[1].get("identity_id") or "") == display_name),
                int(str(item[1].get("visual_policy") or "") == "canonical"),
                int(str(item[1].get("asset_requirement") or "") == "required"),
                -item[0],
            ),
        )
        canonical_id = str(canonical.get("identity_id") or "").strip()
        if not canonical_id:
            continue
        merged = dict(canonical)
        merged_evidence = dict(merged.get("evidence") or {})
        merged_voice_ids = list(merged.get("voice_ids") or [])
        rationales = [str(merged_evidence.get("rationale") or "").strip()]
        merged_ids: list[str] = []
        for _index, contract in members:
            identity_id = str(contract.get("identity_id") or "").strip()
            if identity_id and identity_id != canonical_id:
                replacements[identity_id] = canonical_id
                merged_ids.append(identity_id)
            merged_voice_ids.extend(contract.get("voice_ids") or [])
            evidence = contract.get("evidence") or {}
            for field in (
                "source_evidence_ids",
                "proposition_ids",
                "adaptation_decision_ids",
            ):
                merged_evidence[field] = list(dict.fromkeys([
                    *(merged_evidence.get(field) or []),
                    *(evidence.get(field) or []),
                ]))
            rationale = str(evidence.get("rationale") or "").strip()
            if rationale:
                rationales.append(rationale)
        merged["voice_ids"] = list(dict.fromkeys(merged_voice_ids))
        merged_evidence["rationale"] = "；".join(dict.fromkeys(filter(
            None,
            rationales,
        )))
        merged["evidence"] = merged_evidence
        merged_by_display[display_name] = merged
        changes.append({
            "kind": "identity_contract_merge",
            "display_name": display_name,
            "canonical_identity_id": canonical_id,
            "merged_identity_ids": merged_ids,
        })

    if not replacements:
        return []

    def replace_merged_ids(value):
        if isinstance(value, str):
            return replacements.get(value, value)
        if isinstance(value, list):
            replaced = [replace_merged_ids(item) for item in value]
            if not any(
                isinstance(item, str) and item in replacements
                for item in value
            ):
                return replaced
            deduplicated: list = []
            seen_strings: set[str] = set()
            for item in replaced:
                if isinstance(item, str):
                    if item in seen_strings:
                        continue
                    seen_strings.add(item)
                deduplicated.append(item)
            return deduplicated
        if isinstance(value, tuple):
            return tuple(replace_merged_ids(item) for item in value)
        if isinstance(value, dict):
            return {
                replacements.get(str(key), key): replace_merged_ids(item)
                for key, item in value.items()
            }
        return value

    data = replace_merged_ids(data)

    retained_contracts: list[dict] = []
    emitted_displays: set[str] = set()
    for contract in contracts:
        display_name = str(contract.get("display_name") or "").strip()
        merged = merged_by_display.get(display_name)
        if merged is not None:
            if display_name in emitted_displays:
                continue
            normalized = replace_merged_ids(merged)
            retained_contracts.append(normalized)
            emitted_displays.add(display_name)
            continue
        normalized = replace_merged_ids(contract)
        retained_contracts.append(normalized)
    data["identity_contracts"] = retained_contracts

    rebuilt = type(plan).model_validate(data)
    for field in type(plan).model_fields:
        setattr(plan, field, getattr(rebuilt, field))
    return changes

