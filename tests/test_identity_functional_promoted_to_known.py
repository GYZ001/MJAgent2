"""f 冒用已登记身份称谓时确定性改选同 (label, ref) 的 K 决议（2026-09-05 第 8 集「中年男子」）。"""

from __future__ import annotations

from app import portraits
from app.portraits.identity_schemas import (
    CurrentIdentityCandidateResponse, promote_functional_matching_known,
)


def _evidence():
    records = portraits._current_identity_evidence_records("孟浩站在广场。\n\n放丹换了一个中年男子主持。\n\n门卫守在殿前。")
    return {f"E{index:03d}": record for index, record in enumerate(records, start=1)}


def test_functional_matching_known_decision_is_promoted_and_passes_projection():
    evidence_by_ref = _evidence()
    known = {"K:E002:abc": {"evidence_ref": "E002", "source_label": "中年男子", "canonical_name": "中年男子", "allowed_kinds": ["onscreen", "mentioned"]}}
    response = CurrentIdentityCandidateResponse.model_validate({
        "k": [], "n": [],
        "f": [{"evidence_ref": "E002", "source_label": "中年男子", "functional_identity_key": "中年男子", "kind": "onscreen"}],
    })
    promoted = promote_functional_matching_known(response, known)
    assert [item.decision_id for item in promoted.k] == ["K:E002:abc"] and promoted.f == []
    projected, errors = portraits._project_current_identity_response(
        response, evidence_by_ref=evidence_by_ref, known_decisions=known,
        reserved_authority_labels={"中年男子"}, group_scope="current-1", existing_functional_routes=set(),
    )
    assert not any("冒用" in e for e in errors), errors


def test_functional_without_matching_known_decision_is_left_for_validation():
    known = {"K:E001:abc": {"evidence_ref": "E001", "source_label": "孟浩", "canonical_name": "孟浩"}}
    response = CurrentIdentityCandidateResponse.model_validate({
        "k": [], "n": [],
        "f": [{"evidence_ref": "E003", "source_label": "门卫", "functional_identity_key": "门卫", "kind": "onscreen"}],
    })
    assert promote_functional_matching_known(response, known) is response
