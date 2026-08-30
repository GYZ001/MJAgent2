"""_preflight_document_candidate: validates a candidate document-patch
operation set against the full screenplay before it is committed.

Split out of app/production/screenplay_repair.py.
"""
from __future__ import annotations

import re
from app.harness.types import Issue
from app.production.patch import PatchOperation
from app.production.structured_issues import issues_from_validator_messages
from app.schemas import Bible
from typing import Any

from .dialogue_chain_repair import (
    _dialogue_chain_replacement_is_local,
    _normalize_dialogue_source_references,
    _source_references_are_grounded,
)
from .issue_selection import (
    _introduced_issue_messages,
    _target_issue_signature_still_open,
)
from .narrative_patch_ops import _normalize_patch_operation_payload


def _preflight_document_candidate(
    candidate: dict[str, Any],
    *,
    document: Any,
    source_text: str,
    issue: Issue,
    episode: dict[str, Any] | None = None,
) -> list[PatchOperation]:
    """Find the smallest executable operation subset that passes full local QA."""
    from itertools import combinations

    from app.narrative import validate_screenplay_narrative
    from app.production.patch import apply_patch_operation_to_document
    from app.production.screenplay_document import (
        document_to_screenplay,
        resolve_field_patch_target,
    )
    from app.validators import validate_screenplay

    raw_operations = candidate.get("operations")
    if not isinstance(raw_operations, list) or not 1 <= len(raw_operations) <= 3:
        return []
    try:
        parsed = [
            PatchOperation.model_validate(
                _normalize_patch_operation_payload(raw),
            )
            for raw in raw_operations
            if isinstance(raw, dict)
        ]
    except Exception:  # noqa: BLE001 - untrusted model output
        return []
    if len(parsed) != len(raw_operations):
        return []

    def errors_for(candidate_document: Any) -> list[str]:
        screenplay = document_to_screenplay(candidate_document)
        errors = validate_screenplay_narrative(screenplay, require=True)
        errors.extend(validate_screenplay(
            screenplay,
            Bible(
                characters=[],
                world={"visual_style_canonical": ""},
            ),
            expected_beats=max(1, len(screenplay.scene_outline or [])),
            episode_no=screenplay.episode_no,
            source_text=source_text,
            require_dialogue_chains=True,
            validate_narrative=False,
            episode=episode,
        ))
        return errors

    baseline_errors = errors_for(document)
    baseline_issues = issues_from_validator_messages(
        baseline_errors,
        subject="screenplay",
        stage="screenplay",
    )
    for subset_size in range(1, len(parsed) + 1):
        for subset_indices in combinations(range(len(parsed)), subset_size):
            working = document
            accepted: list[PatchOperation] = []
            valid = True
            for index in subset_indices:
                operation = parsed[index].model_copy(deep=True)
                operation.value = _normalize_dialogue_source_references(
                    operation.value,
                    source_text,
                )
                target = operation.target or {}
                collection = re.split(
                    r"[.\[]+",
                    str(target.get("collection") or "").strip(),
                    maxsplit=1,
                )[0]
                script_plan = getattr(document, "narrative_plan", None)
                plan_data = (
                    script_plan.model_dump(mode="json")
                    if script_plan is not None
                    else {}
                )
                if collection and isinstance(plan_data.get(collection), list):
                    valid = False
                    break
                if operation.op == "replace_field":
                    target = resolve_field_patch_target(
                        working,
                        path=operation.path,
                        target=target,
                    )
                    operation.target = target
                    patch_field = re.split(
                        r"[./]+",
                        operation.path.strip("/"),
                    )[-1]
                    chain_id = str(
                        target.get("chain_id") or target.get("id") or "",
                    ).strip()
                    chain = next(
                        (
                            item for item in working.dialogue_chains
                            if (item.chain_id or "").strip() == chain_id
                        ),
                        None,
                    )
                    if (
                        chain is not None
                        and patch_field == "turns"
                        and not _dialogue_chain_replacement_is_local(
                            working,
                            chain_id=chain_id,
                            turns=operation.value,
                            source_text=source_text,
                        )
                    ):
                        valid = False
                        break
                if not _source_references_are_grounded(
                    operation.value,
                    source_text,
                ):
                    valid = False
                    break
                before = working.model_dump(mode="json")
                try:
                    working, _ = apply_patch_operation_to_document(
                        working,
                        operation,
                    )
                except Exception:  # noqa: BLE001 - isolated probe
                    valid = False
                    break
                if working.model_dump(mode="json") == before:
                    valid = False
                    break
                accepted.append(operation)
            if not valid:
                continue
            candidate_errors = errors_for(working)
            if issue.message in candidate_errors:
                continue
            candidate_issues = issues_from_validator_messages(
                candidate_errors,
                subject="screenplay",
                stage="screenplay",
            )
            if _target_issue_signature_still_open(issue, candidate_issues):
                continue
            if _introduced_issue_messages(baseline_issues, candidate_issues):
                continue
            selection = {
                "candidate_ids": [candidate.get("candidate_id")],
                "selected_candidate_id": candidate.get("candidate_id"),
                "selection_reason": (
                    "生产执行器对候选操作子集逐一隔离复验后选择的最小充分子集"
                ),
                "expected_narrative_gain": candidate.get("expected_narrative_gain"),
                "destructive_cost": candidate.get("destructive_cost"),
            }
            for operation in accepted:
                operation.target = {
                    **(operation.target or {}),
                    "semantic_selection": selection,
                }
            return accepted
    return []


