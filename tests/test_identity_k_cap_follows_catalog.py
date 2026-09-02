"""K 决议计数帽不得低于本批目录实际提供的 K 数（ERR-20260902-b227f9，《三国演义》第一回）。

第二轮时人物谱已有 15 人，7 条证据里这些名字逐字出现 87 次，目录因此提供 87 条 K 决议；
契约要求已登记称谓在每条证据里都必须选 K，模型如实选了 87 条，却被 max(64, 7×3)=64 的
帽子当成失控拒绝——目录允许的，校验就不许拒绝。
"""
from __future__ import annotations

from app import portraits


def _catalog(label_count: int, ref_count: int) -> tuple[dict[str, dict], dict[str, dict], list[dict]]:
    labels = [f"将{index}" for index in range(1, label_count + 1)]
    evidence_by_ref = {
        f"E{index:03d}": {"text": "、".join(labels) + "齐聚帐中议事。"} for index in range(1, ref_count + 1)
    }
    known_decisions: dict[str, dict] = {}
    k_items: list[dict] = []
    for ref in evidence_by_ref:
        for label in labels:
            decision_id = f"K:{ref}:{label}"
            known_decisions[decision_id] = {
                "evidence_ref": ref,
                "source_label": label,
                "canonical_name": label,
                "authority_id": f"bible:{label}",
                "decision_type": "registered_authority",
            }
            k_items.append({"decision_id": decision_id, "kind": "mentioned"})
    return evidence_by_ref, known_decisions, k_items


def test_selecting_every_offered_k_decision_is_not_overflow() -> None:
    evidence_by_ref, known_decisions, k_items = _catalog(label_count=15, ref_count=7)
    assert len(k_items) == 105 > 64  # 与 64 的历史帽子直接对撞

    response = portraits.CurrentIdentityCandidateResponse.model_validate({"k": k_items, "n": [], "f": []})
    projected, errors = portraits._project_current_identity_response(
        response,
        evidence_by_ref=evidence_by_ref,
        known_decisions=known_decisions,
        reserved_authority_labels=set(),
        group_scope="current-1",
        existing_functional_routes=set(),
    )

    assert not any("过多" in message for message in errors), errors
    # 投影按称谓归并（同一已登记角色在 7 条证据里的 K 决议合成一个候选）。
    assert {item["source_label"] for item in projected} == {f"将{index}" for index in range(1, 16)}


def test_n_and_f_branches_keep_the_batch_scaled_cap() -> None:
    """帽子只对 k 放开到目录规模；n/f 没有目录约束，仍按批规模封顶。"""
    evidence_by_ref, known_decisions, _k_items = _catalog(label_count=15, ref_count=7)
    n_items = [
        {"evidence_ref": "E001", "identity_label": f"路人{index}", "kind": "mentioned", "name_kind": "personal_name"}
        for index in range(1, 70)
    ]
    response = portraits.CurrentIdentityCandidateResponse.model_validate({"k": [], "n": n_items, "f": []})
    _projected, errors = portraits._project_current_identity_response(
        response,
        evidence_by_ref=evidence_by_ref,
        known_decisions=known_decisions,
        reserved_authority_labels=set(),
        group_scope="current-1",
        existing_functional_routes=set(),
    )
    assert any(message.startswith("current identity n decisions 过多") for message in errors)
