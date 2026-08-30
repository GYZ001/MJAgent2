"""Shared narrative-validation primitives.

ID/reference bookkeeping, cycle detection, and audience-state fragment
matching used by both ``.screenplay_validate`` and ``.storyboard_validate``.
Moved verbatim out of the pre-split ``app/narrative.py`` (see
``app/narrative/__init__.py`` for the package-split rationale). Add new
generic relation-matching helpers here only if both validators need them;
concern-specific helpers belong in the file that uses them.
"""
from __future__ import annotations

from typing import Any, Iterable

from app.schemas import ShotContribution


def _norm(value: object) -> str:
    return " ".join(str(value or "").split())


def normalize_source_evidence_text(value: object) -> str:
    """Ignore import-time wrapping while preserving every non-whitespace character."""
    return "".join(str(value or "").split())


def _ids(items: Iterable[Any], field: str, errors: list[str], label: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for index, item in enumerate(items):
        value = _norm(getattr(item, field, ""))
        if not value:
            errors.append(f"[NARRATIVE_ID_MISSING] {label}[{index}].{field} 不能为空")
            continue
        if value in result:
            errors.append(f"[NARRATIVE_ID_DUPLICATE] {label}.{field} 重复：{value}")
            continue
        result[value] = item
    return result


def _require_refs(
    values: Iterable[str],
    target: dict[str, Any] | set[str],
    errors: list[str],
    subject: str,
) -> None:
    known = target if isinstance(target, set) else set(target)
    for value in values:
        ref = _norm(value)
        if not ref:
            errors.append(f"[NARRATIVE_REF_EMPTY] {subject} 含空引用")
        elif ref not in known:
            errors.append(f"[NARRATIVE_REF_MISSING] {subject} 引用了不存在的 {ref}")


def _cycle_nodes(parents: dict[str, list[str]]) -> list[str]:
    visiting: set[str] = set()
    visited: set[str] = set()
    cycle: list[str] = []

    def visit(node: str, trail: list[str]) -> bool:
        if node in visiting:
            start = trail.index(node) if node in trail else 0
            cycle.extend(trail[start:] + [node])
            return True
        if node in visited:
            return False
        visiting.add(node)
        trail.append(node)
        for parent in parents.get(node, []):
            if parent in parents and visit(parent, trail):
                return True
        trail.pop()
        visiting.remove(node)
        visited.add(node)
        return False

    for item in parents:
        if visit(item, []):
            break
    return cycle


def _contribution_nonempty(contribution: ShotContribution | None) -> bool:
    if contribution is None:
        return False
    return any((
        contribution.target_delta_ids,
        contribution.assimilation_task_ids,
        contribution.evidence_ids,
        contribution.story_delta_fact_ids,
        contribution.character_state_delta_ids,
        contribution.audience_state_delta_ids,
        contribution.affective_delta,
        contribution.spatial_temporal_delta,
        abs(contribution.dramatic_pressure_delta) > 1e-9,
    ))


def _anchor_ref_errors(
    anchor: Any,
    *,
    events: dict[str, Any],
    scenes: dict[str, Any],
    errors: list[str],
    subject: str,
) -> None:
    """Validate only anchor kinds whose identity is authoritative at this layer.

    Beat/sequence/shot anchors may be materialized later, so they remain open
    semantic anchors.  Event and scene anchors, however, must resolve inside
    the current episode contract; this also prevents cross-episode ID reuse.
    """
    anchor_type = _norm(getattr(anchor, "type", ""))
    anchor_id = _norm(getattr(anchor, "id", ""))
    if not anchor_type or not anchor_id:
        errors.append(f"[NARRATIVE_ANCHOR_MISSING] {subject} 的锚点类型和 ID 不能为空")
    elif anchor_type == "event" and anchor_id not in events:
        errors.append(f"[NARRATIVE_REF_MISSING] {subject} 引用了不存在的事件锚点 {anchor_id}")
    elif anchor_type == "scene" and anchor_id not in scenes:
        errors.append(f"[NARRATIVE_REF_MISSING] {subject} 引用了不存在的场景锚点 {anchor_id}")


def _curve_errors(
    points: Iterable[dict[str, Any]],
    *,
    events: dict[str, Any],
    scenes: dict[str, Any],
    errors: list[str],
    subject: str,
) -> None:
    for position, point in enumerate(points):
        anchor = point.get("anchor") if isinstance(point, dict) else None
        if not isinstance(anchor, dict):
            errors.append(f"[CURVE_ANCHOR_MISSING] {subject}[{position}] 缺少事件或节拍锚点")
            continue
        _anchor_ref_errors(
            type("Anchor", (), anchor)(),
            events=events,
            scenes=scenes,
            errors=errors,
            subject=f"{subject}[{position}]",
        )
        value = point.get("value")
        if value is not None and (not isinstance(value, (int, float)) or not 0 <= float(value) <= 1):
            errors.append(f"[CURVE_VALUE_RANGE] {subject}[{position}].value 必须在 0..1")


def _state_without_identity(state: Any) -> dict[str, Any]:
    return state.model_dump(mode="json", exclude={"audience_state_id", "anchor"})


def _json_fragment_matches(fragment: Any, actual: Any) -> bool:
    """Return whether ``fragment`` is a non-empty, exact structural fragment.

    Director-authored target deltas may omit unchanged sibling fields, but
    they may not introduce arbitrary keys or values that are absent from the
    authoritative audience snapshot.  This is relation validation, not a
    vocabulary or story-category classifier.
    """
    if isinstance(fragment, dict):
        return bool(fragment) and isinstance(actual, dict) and all(
            key in actual and _json_fragment_matches(value, actual[key])
            for key, value in fragment.items()
        )
    if isinstance(fragment, list):
        return isinstance(actual, list) and fragment == actual
    return fragment == actual


def _belief_fragment_matches(
    fragment: dict[str, Any],
    state: Any,
    proposition_ids: list[str],
) -> bool:
    beliefs = {
        item.proposition_id: item.model_dump(mode="json", exclude={"proposition_id"})
        for item in state.beliefs
    }
    if len(proposition_ids) == 1 and set(fragment).issubset(
        {"stance", "confidence", "evidence_ids"}
    ):
        actual = beliefs.get(proposition_ids[0])
        return actual is not None and _json_fragment_matches(fragment, actual)
    if set(fragment) == {"beliefs"}:
        declared = fragment["beliefs"]
        if isinstance(declared, dict):
            return bool(declared) and all(
                proposition_id in proposition_ids
                and proposition_id in beliefs
                and _json_fragment_matches(value, beliefs[proposition_id])
                for proposition_id, value in declared.items()
            )
        if isinstance(declared, list):
            actual = [
                item.model_dump(mode="json")
                for item in state.beliefs
                if item.proposition_id in proposition_ids
            ]
            return bool(declared) and declared == actual
        return False
    return bool(fragment) and all(
        proposition_id in proposition_ids
        and proposition_id in beliefs
        and isinstance(value, dict)
        and _json_fragment_matches(value, beliefs[proposition_id])
        for proposition_id, value in fragment.items()
    )


def _target_state_fragment_matches(delta: Any, fragment: dict[str, Any], state: Any) -> bool:
    dimension = delta.dimension
    if dimension == "belief":
        return _belief_fragment_matches(fragment, state, delta.proposition_ids)
    if dimension == "character_goal":
        actual = state.character_goal_hypotheses
        if set(fragment) == {"character_goal_hypotheses"}:
            return fragment["character_goal_hypotheses"] == actual
        return _json_fragment_matches(fragment, actual)
    if dimension == "spatial_temporal":
        wrapped = {
            "spatial_model": state.spatial_model,
            "temporal_model": state.temporal_model,
        }
        if fragment and set(fragment).issubset(wrapped):
            return all(fragment[key] == wrapped[key] for key in fragment)
        return _json_fragment_matches(fragment, wrapped)
    if dimension == "affective":
        actual = state.affective_state
        if set(fragment) == {"affective_state"}:
            return fragment["affective_state"] == actual
        return _json_fragment_matches(fragment, actual)
    if dimension == "question":
        return _json_fragment_matches(
            fragment,
            {"active_question_ids": state.active_question_ids},
        )
    if dimension == "attention":
        return _json_fragment_matches(
            fragment,
            {
                "attention_residue_ids": state.attention_residue_ids,
                "working_memory": [
                    (
                        item.model_dump(mode="json")
                        if hasattr(item, "model_dump")
                        else item
                    )
                    for item in state.working_memory
                ],
            },
        )
    # Open semantic dimensions remain expressible, but must point at an actual
    # changed fragment of the snapshot instead of becoming unbound prose.
    return _json_fragment_matches(fragment, _state_without_identity(state))


def _changed_audience_state_fields(state_in: Any, state_out: Any) -> set[str]:
    before = _state_without_identity(state_in)
    after = _state_without_identity(state_out)
    return {
        key for key in set(before) | set(after)
        if before.get(key) != after.get(key)
    }


def _declared_change_matches(declared: Any, before: Any, after: Any) -> bool:
    """Whether an open semantic delta describes values that actually changed.

    Keys and values come from the AI-authored state model.  Deterministic code
    only checks their relation to authoritative before/after snapshots; it does
    not enumerate emotions, locations, action kinds or genres.
    """
    if isinstance(declared, dict):
        if not declared or not isinstance(before, dict) or not isinstance(after, dict):
            return False
        return all(
            key in after
            and (
                _declared_change_matches(value, before.get(key), after[key])
                if isinstance(value, dict)
                else before.get(key) != after[key] and value == after[key]
            )
            for key, value in declared.items()
        )
    return before != after and declared == after
