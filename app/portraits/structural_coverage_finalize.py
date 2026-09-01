"""结构化覆盖审计的收尾阶段：把 provider 选中的决议目录投影成新增候选列表
（人物名/身份分组/证据摘录），追加到调用方原有的 candidates 后返回。
"""

from __future__ import annotations

from app.evidence import repository as evidence_repository
from app.errors import ContentGenerationError

from .constants import STRUCTURAL_IDENTITY_COVERAGE_VERSION
from .discovery_resample import _bounded_owned_identity_evidence
from .identity_schemas import StructuralIdentityCoverageResponse


def _selected_decisions_from_response(
    response: StructuralIdentityCoverageResponse,
    coverage_group_keys: list[str],
    decision_by_id: dict[str, dict],
) -> list[dict]:
    """按 group_key 顺序把响应的 decision_id 解引用为完整决议记录。"""
    return [
        decision_by_id[str(response.decisions[group_key])]
        for group_key in coverage_group_keys
    ]


def _normalized_new_group_refs(
    selected_decisions: list[dict],
    *,
    structural_by_key: dict[str, list[dict]],
    source_order: dict[str, int],
) -> dict[str, str]:
    """把响应里以 'new:' 开头的临时 group_ref 归一化成确定性的 'structural:' 内容哈希。

    同一响应内选中同一临时 ref 的全部 label 会被合并进同一个最终 group_ref——
    这是把「本轮新识别的多个 label 属于同一人」落成同一 identity_group 的机制。
    """
    new_group_members: dict[str, set[str]] = {}
    for decision in selected_decisions:
        raw_group = str(decision.get("identity_group_ref") or "").strip()
        label = str(decision.get("source_label") or "").strip()
        if raw_group.startswith("new:") and label:
            new_group_members.setdefault(raw_group, set()).add(label)
    return {
        raw_group: (
            "structural:"
            + evidence_repository.content_hash({
                "policy_version": STRUCTURAL_IDENTITY_COVERAGE_VERSION,
                "source_labels": sorted(labels),
                "source_segment_ids": sorted(
                    {
                        str(source_id)
                        for label in labels
                        for typed_item in structural_by_key.get(label, [])
                        for source_id in typed_item.get("source_segment_ids") or []
                        if str(source_id) in source_order
                    },
                    key=lambda source_id: source_order[source_id],
                ),
            })[:24]
        )
        for raw_group, labels in new_group_members.items()
    }


def _typed_evidence_for_decision(
    label: str, structural_by_key: dict[str, list[dict]]
) -> list[dict]:
    """取该 label 的全部 owned 结构化证据；为空即数据损坏，直接拒绝。"""
    typed_evidence = structural_by_key.get(label) or []
    if not label or not typed_evidence:
        raise ContentGenerationError(f"结构人物 coverage 缺少 owned evidence：{label}")
    return typed_evidence


def _projected_kind_for_typed_evidence(typed_evidence: list[dict]) -> str:
    """'mentioned' 当全部 typed usage 都是纯提及，否则 'onscreen'。"""
    usages = {str(value.get("usage") or "").strip() for value in typed_evidence}
    return "mentioned" if usages == {"mentioned"} else "onscreen"


def _raise_if_named_onscreen_not_materializable(
    label: str, identity_kind: str, projected_kind: str, raw: dict,
) -> None:
    """named + onscreen 但决议未声明可物化时直接拒绝——不得静默物化不兼容权威。"""
    if (
        identity_kind == "named"
        and projected_kind == "onscreen"
        and not raw.get("materialization_compatible")
    ):
        raise ContentGenerationError(
            f"structural coverage K authority 不可直接物化人物卡：{label}"
        )


def _owned_source_ids_for_typed_evidence(
    typed_evidence: list[dict],
    *,
    source_by_id: dict[str, str],
    source_order: dict[str, int],
) -> list[str]:
    """typed_evidence 覆盖的全部 owned source_segment_id，按原文出现顺序排序。"""
    return sorted({
        str(source_id)
        for value in typed_evidence
        for source_id in value.get("source_segment_ids") or []
        if str(source_id) in source_by_id
    }, key=lambda source_id: source_order[source_id])


def _evidence_record_for_decision(
    raw: dict,
    label: str,
    source_ids: list[str],
    *,
    evidence_by_id: dict[str, dict],
) -> dict:
    """解引用决议的 evidence_id；source_segment_id 越界 owned 范围即拒绝。"""
    evidence_record = evidence_by_id.get(str(raw.get("evidence_id") or ""), {})
    source_segment_id = str(evidence_record.get("source_segment_id") or "")
    if source_segment_id not in source_ids:
        raise ContentGenerationError(f"结构人物 coverage evidence receipt 越界：{label}")
    return evidence_record


def _bounded_evidence_for_decision(
    evidence_text: str, identity_kind: str, proof_anchors: list[str],
) -> str:
    """named 且有锚点时按锚点截断证据，否则原样截断到 80 字。"""
    if identity_kind == "named" and proof_anchors:
        return _bounded_owned_identity_evidence(
            evidence_text, anchors=proof_anchors, max_chars=80
        )
    return evidence_text.strip()[:80]


def _addition_from_selected_decision(
    raw: dict,
    existing: set[tuple[str, str]],
    *,
    structural_by_key: dict[str, list[dict]],
    authority_by_id: dict[str, dict],
    normalized_new_groups: dict[str, str],
    evidence_by_id: dict[str, dict],
    source_by_id: dict[str, str],
    source_order: dict[str, int],
) -> dict | None:
    """把一条选中决议投影成候选追加项；(label, group) 已存在则返回 None 表示跳过。"""
    label = str(raw.get("source_label") or "").strip()
    typed_evidence = _typed_evidence_for_decision(label, structural_by_key)
    identity_kind = str(raw.get("identity_kind") or "functional")
    authority_id = str(raw.get("authority_id") or "").strip()
    canonical_name = str(
        authority_by_id.get(authority_id, {}).get("canonical_name") or ""
    )
    raw_group = str(raw.get("identity_group_ref") or "").strip()
    group = normalized_new_groups.get(raw_group, raw_group)
    if (label, group) in existing:
        return None
    projected_kind = _projected_kind_for_typed_evidence(typed_evidence)
    _raise_if_named_onscreen_not_materializable(label, identity_kind, projected_kind, raw)
    source_ids = _owned_source_ids_for_typed_evidence(
        typed_evidence, source_by_id=source_by_id, source_order=source_order
    )
    evidence_record = _evidence_record_for_decision(
        raw, label, source_ids, evidence_by_id=evidence_by_id
    )
    source_segment_id = str(evidence_record.get("source_segment_id") or "")
    evidence_text = str(evidence_record.get("text") or "")
    proof_anchors = [str(value) for value in raw.get("proof_anchors") or [] if str(value)]
    bounded_evidence = _bounded_evidence_for_decision(
        evidence_text, identity_kind, proof_anchors
    )
    return {
        "name": canonical_name or label,
        "source_label": label,
        "identity_kind": identity_kind,
        "identity_group": group,
        "authority_id": authority_id if identity_kind == "named" else "",
        "kind": projected_kind,
        "evidence": bounded_evidence,
        "future_evidence": "",
        "source_segment_ids": source_ids,
        "source_segment_id": source_segment_id,
        "source_quote": source_by_id.get(source_segment_id, ""),
        "_typed_source_evidence_owned": bool(source_segment_id),
        "materialization_compatible": bool(raw.get("materialization_compatible")),
    }


def _build_candidate_additions(
    selected_decisions: list[dict],
    candidates: list[dict],
    *,
    structural_by_key: dict[str, list[dict]],
    authority_by_id: dict[str, dict],
    evidence_by_id: dict[str, dict],
    source_by_id: dict[str, str],
    source_order: dict[str, int],
) -> list[dict]:
    """把选中的决议目录投影成新增候选列表，按 (source_label, identity_group) 去重。"""
    existing = {
        (str(item.get("source_label") or ""), str(item.get("identity_group") or ""))
        for item in candidates
    }
    normalized_new_groups = _normalized_new_group_refs(
        selected_decisions, structural_by_key=structural_by_key, source_order=source_order,
    )
    additions: list[dict] = []
    for raw in selected_decisions:
        addition = _addition_from_selected_decision(
            raw, existing,
            structural_by_key=structural_by_key,
            authority_by_id=authority_by_id,
            normalized_new_groups=normalized_new_groups,
            evidence_by_id=evidence_by_id,
            source_by_id=source_by_id,
            source_order=source_order,
        )
        if addition is not None:
            additions.append(addition)
    return additions
