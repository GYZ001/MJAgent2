"""Deterministic patch-strategy bookkeeping and source-span/spine-beat planning
helpers used before falling back to LLM-driven repair.

Split out of app/production/screenplay_repair.py.
"""
from __future__ import annotations

from app import textmatch
from app.harness.types import Issue
from app.production.patch import PatchOperation
from app.schemas import EpisodeScreenplay

from .dialogue_source_alignment import _source_evidence_span
from .gates import _SOURCE_SPAN_EXACT_MISMATCH_RE


def _strategy_was_tried(entries: list[str], strategy: str) -> bool:
    """Recognize both current keys and legacy keys such as ``rederive:``."""
    return any(
        entry == strategy or entry.startswith(f"{strategy}:")
        for entry in entries
        if not entry.startswith(("fail:", "exhausted"))
    )


def _patch_strategy_key(ops: list[PatchOperation]) -> str:
    op = ops[0]
    kind = str((op.target or {}).get("kind") or "")
    if op.op == "rederive":
        return "rederive"
    if op.op == "split_dialogue_chain_by_scene":
        return f"split_dialogue_chain_{(op.target or {}).get('chain_id') or 'unknown'}"
    if kind == "metadata":
        return f"fill_{op.path}"
    if kind in {"scene", "screenplay_scene"}:
        return f"fill_scene_{op.target.get('id')}_{op.path}"
    if kind == "information" and op.path == "event_id":
        return "fix_ledger_event"
    if kind == "dialogue_chain_turn" and op.path == "source_text":
        return str(
            (op.target or {}).get("strategy")
            or (
                f"fix_dialogue_source_{op.target.get('chain_id')}_"
                f"{op.target.get('turn_index')}"
            )
        )
    if kind == "dialogue_chain_turn" and op.path == "function":
        return (
            f"fix_dialogue_function_{op.target.get('chain_id')}_"
            f"{op.target.get('turn_index')}"
        )
    if (
        kind == "narrative_node"
        and str((op.target or {}).get("collection") or "") == "source_evidence"
        and op.path in {"source_span", "verbatim_excerpt"}
    ):
        return str(
            (op.target or {}).get("strategy")
            or f"fix_source_span_{(op.target or {}).get('id') or 'unknown'}"
        )
    if kind == "narrative_node":
        return (
            f"{op.op}:{(op.target or {}).get('id') or 'unknown'}:"
            f"{op.path or 'node'}"
        )
    if op.op == "create_node" and kind == "dialogue_turn":
        return "insert_trigger"
    if op.op == "create_node" and kind == "action_block":
        return str(
            (op.target or {}).get("strategy")
            or f"create_action_{(op.target or {}).get('id') or 'unknown'}"
        )
    locator = op.path or str((op.target or {}).get("id") or "")
    return f"{op.op}:{locator}" if locator else op.op


def _source_evidence_contexts(script: EpisodeScreenplay) -> dict[str, list[str]]:
    plan = script.narrative_plan
    if plan is None:
        return {}
    contexts: dict[str, list[str]] = {}
    for proposition in plan.propositions or []:
        statement = str(proposition.canonical_statement or "").strip()
        if not statement:
            continue
        for evidence_id in proposition.direct_source_evidence_ids or []:
            contexts.setdefault(str(evidence_id), []).append(statement)
    return contexts


def _source_span_issue_evidence_id(issue: Issue) -> str:
    match = _SOURCE_SPAN_EXACT_MISMATCH_RE.search(issue.message or "")
    return match.group(1).strip() if match else ""


def _plan_source_span_patch(
    issue: Issue,
    script: EpisodeScreenplay,
    *,
    source_text: str,
    tried: list[str],
) -> list[PatchOperation]:
    if script.narrative_plan is None or not source_text:
        return []
    evidence_id = _source_span_issue_evidence_id(issue)
    if not evidence_id:
        return []
    strategy = f"fix_source_span_{evidence_id}"
    if _strategy_was_tried(tried, strategy):
        return []
    evidence = next(
        (
            item for item in script.narrative_plan.source_evidence
            if item.source_evidence_id == evidence_id
        ),
        None,
    )
    if evidence is None:
        return []
    contexts = _source_evidence_contexts(script)
    resolved = _source_evidence_span(
        source_text,
        evidence.verbatim_excerpt,
        context=" ".join(contexts.get(evidence_id, [])),
    )
    if resolved is None:
        return []
    start, end, expanded_excerpt = resolved
    target = {
        "kind": "narrative_node",
        "collection": "source_evidence",
        "id": evidence_id,
        "strategy": strategy,
    }
    operations: list[PatchOperation] = []
    if (
        expanded_excerpt is not None
        and expanded_excerpt != evidence.verbatim_excerpt
    ):
        operations.append(PatchOperation(
            op="replace_field",
            path="verbatim_excerpt",
            value=expanded_excerpt,
            target=target,
        ))
    current_span = evidence.source_span.model_dump(mode="json")
    if current_span.get("start") != start or current_span.get("end") != end:
        operations.append(PatchOperation(
            op="replace_field",
            path="source_span",
            value={**current_span, "start": start, "end": end},
            target=target,
        ))
    return operations


def _best_scene_for_spine_beat(
    script: EpisodeScreenplay,
    *,
    beat_index: int,
    who: str,
    does: str,
) -> str:
    """Place one authoritative spine action in the closest existing scene."""
    scenes = list(script.scene_outline or [])
    if not scenes:
        return ""
    beat_text = f"{who}{does}"
    ranked: list[tuple[float, int, int, str]] = []
    for index, scene in enumerate(scenes):
        scene_text = " ".join([
            scene.story_function or "",
            scene.summary or "",
            scene.conflict or "",
            scene.turn or "",
            scene.source_basis or "",
        ])
        semantic_score = max(
            textmatch.longest_run_ratio(beat_text, scene_text),
            textmatch.bigram_coverage(beat_text, scene_text),
        )
        actor_score = int(
            bool(who)
            and any(
                who == character
                or who in character
                or character in who
                for character in (scene.characters or [])
            )
        )
        ordinal_distance = abs(index - min(beat_index, len(scenes) - 1))
        ranked.append((
            semantic_score,
            actor_score,
            -ordinal_distance,
            f"SC{int(scene.scene_no):02d}",
        ))
    ranked.sort(reverse=True)
    return ranked[0][3]


