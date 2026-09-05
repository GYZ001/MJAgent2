"""人物谱为空时 K 目录只能放占位枚举 K:NONE；模型选了它等于「没有已登记身份」，不是越界（2026-09-05 第 23 集）。"""

from __future__ import annotations

from app import portraits


def _evidence():
    records = portraits._current_identity_evidence_records("孟浩站在山顶。\n\n门卫守在殿前。")
    return {f"E{index:03d}": record for index, record in enumerate(records, start=1)}


def test_k_none_with_empty_catalog_is_not_a_violation():
    evidence_by_ref = _evidence()
    response = portraits.CurrentIdentityCandidateResponse.model_validate(
        {"k": [{"decision_id": "K:NONE", "kind": "mentioned", "absorbed_functional_keys": []}], "n": [], "f": []}
    )
    projected, errors = portraits._project_current_identity_response(
        response,
        evidence_by_ref=evidence_by_ref,
        known_decisions={},
        reserved_authority_labels=set(),
        group_scope="current-1",
        existing_functional_routes=set(),
    )
    assert errors == [], errors
    assert projected == []


def test_k_none_with_real_catalog_is_still_a_violation():
    evidence_by_ref = _evidence()
    known = {"K:1": {"evidence_ref": "E001", "source_label": "孟浩", "canonical_name": "孟浩"}}
    response = portraits.CurrentIdentityCandidateResponse.model_validate(
        {"k": [{"decision_id": "K:NONE", "kind": "mentioned", "absorbed_functional_keys": []}], "n": [], "f": []}
    )
    _, errors = portraits._project_current_identity_response(
        response,
        evidence_by_ref=evidence_by_ref,
        known_decisions=known,
        reserved_authority_labels={"孟浩"},
        group_scope="current-1",
        existing_functional_routes=set(),
    )
    assert any("K decision 越界" in e for e in errors)
