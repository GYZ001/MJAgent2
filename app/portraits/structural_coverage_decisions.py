"""结构化覆盖审计的决议登记阶段：给每个 coverage group 枚举全部候选决议
（functional 兜底、挂靠既有 group、逐 authority 的 named），产出 decision_by_id /
decision_ids_by_group 供发给 provider 的 schema 与后续校验使用。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.evidence import repository as evidence_repository

from .constants import STRUCTURAL_IDENTITY_COVERAGE_VERSION


@dataclass
class IdentityAuthorityCatalog:
    """已确权的 authority/group 目录：authority_id -> authority、identity_group_ref -> group。"""

    authority_by_id: dict[str, dict]
    groups_by_ref: dict[str, dict]


@dataclass
class StructuralCoverageIndex:
    """本轮结构化覆盖审计的只读派生索引，供决议登记与响应校验共用。"""

    source_by_id: dict[str, str]
    structural_by_key: dict[str, list[dict]]
    coverage_group_by_key: dict[str, dict]
    evidence_by_id: dict[str, dict]
    evidence_ids_by_group: dict[str, list[str]]


def _matching_group_evidence_ids(
    group_key: str,
    identity_group_ref: str,
    *,
    catalog: IdentityAuthorityCatalog,
    index: StructuralCoverageIndex,
) -> list[str]:
    """Return owned spans for an exact backend-registered label binding.

    Mere SRC overlap or co-occurrence is not identity evidence: one source
    sentence routinely contains several people.  An existing group is
    eligible only when this exact synthetic identity key is already one of
    its registered labels and appears verbatim in the owned span.
    """
    group = catalog.groups_by_ref.get(identity_group_ref, {})
    registered_labels = {
        str(value).strip()
        for value in group.get("source_labels") or []
        if str(value).strip()
    }
    source_label = str(index.coverage_group_by_key[group_key]["source_label"])
    if source_label not in registered_labels:
        return []
    return [
        evidence_id
        for evidence_id in index.evidence_ids_by_group.get(group_key, [])
        if source_label in str(index.evidence_by_id[evidence_id]["text"])
    ]


def _coverage_group_kind(
    group_key: str,
    *,
    index: StructuralCoverageIndex,
) -> str:
    """'mentioned' 当该 group 的全部 typed usage 都是纯提及，否则 'onscreen'。"""
    label = str(index.coverage_group_by_key[group_key]["source_label"])
    usages = {
        str(item.get("usage") or "").strip()
        for item in index.structural_by_key.get(label, [])
    }
    return "mentioned" if usages == {"mentioned"} else "onscreen"


def _register_decision(
    group_key: str,
    payload: dict,
    *,
    decision_by_id: dict[str, dict],
    decision_ids_by_group: dict[str, list[str]],
) -> str:
    """登记一条候选决议，decision_id 按内容寻址（K=named，F=functional）。"""
    decision_kind = "K" if payload.get("identity_kind") == "named" else "F"
    decision_id = (
        f"{decision_kind}:{group_key}:"
        + evidence_repository.content_hash({
            "contract_version": STRUCTURAL_IDENTITY_COVERAGE_VERSION,
            **payload,
        })[:16]
    )
    decision_by_id[decision_id] = {
        "decision_id": decision_id,
        "group_key": group_key,
        **payload,
    }
    decision_ids_by_group.setdefault(group_key, []).append(decision_id)
    return decision_id


def _register_functional_seed_decision(
    group: dict,
    evidence_ids: list[str],
    *,
    decision_by_id: dict[str, dict],
    decision_ids_by_group: dict[str, list[str]],
) -> None:
    """为每个 group 登记一条兜底 F 决议（新 seed group，锚定首条 owned evidence）。"""
    group_key = str(group["group_key"])
    _register_decision(group_key, {
        "source_label": str(group["source_label"]),
        "identity_kind": "functional",
        "identity_group_ref": str(group["seed_group_ref"]),
        "authority_id": "",
        "canonical_name": "",
        "evidence_id": evidence_ids[0],
        "owned_source_segment_ids": list(group["source_segment_ids"]),
        "proof_kind": "owned_functional_new",
    }, decision_by_id=decision_by_id, decision_ids_by_group=decision_ids_by_group)


def _register_existing_group_functional_decisions(
    group: dict,
    *,
    catalog: IdentityAuthorityCatalog,
    index: StructuralCoverageIndex,
    decision_by_id: dict[str, dict],
    decision_ids_by_group: dict[str, list[str]],
) -> None:
    """若某既有 group（非本 label 的 seed group）尚无 named 权威，登记一条 F 决议挂靠它。"""
    group_key = str(group["group_key"])
    label = str(group["source_label"])
    seed_group_ref = str(group["seed_group_ref"])
    source_ids = list(group["source_segment_ids"])
    for identity_group_ref, catalog_group in catalog.groups_by_ref.items():
        if identity_group_ref == seed_group_ref:
            continue
        matching_ids = _matching_group_evidence_ids(
            group_key, identity_group_ref, catalog=catalog, index=index
        )
        if not matching_ids:
            continue
        group_authorities = sorted(set(
            str(value)
            for value in catalog_group.get("authority_ids") or []
            if str(value)
        ))
        if not group_authorities:
            _register_decision(group_key, {
                "source_label": label,
                "identity_kind": "functional",
                "identity_group_ref": identity_group_ref,
                "authority_id": "",
                "canonical_name": "",
                "evidence_id": matching_ids[0],
                "owned_source_segment_ids": source_ids,
                "proof_kind": "owned_functional_existing_group",
            }, decision_by_id=decision_by_id, decision_ids_by_group=decision_ids_by_group)


def _register_label_anchor_named_decision(
    group_key: str,
    label: str,
    source_ids: list[str],
    seed_group_ref: str,
    evidence_ids: list[str],
    authority_id: str,
    authority: dict,
    canonical_name: str,
    materialization_compatible: bool,
    *,
    index: StructuralCoverageIndex,
    decision_by_id: dict[str, dict],
    decision_ids_by_group: dict[str, list[str]],
) -> None:
    """label 逐字等于该 authority 的正典名/别名，且命中 owned evidence 原文时登记 K 决议。"""
    authority_anchors = list(dict.fromkeys(
        value
        for value in [
            canonical_name,
            *[str(alias).strip() for alias in authority.get("aliases") or []],
        ]
        if value
    ))
    identity_label_anchor_ids = [
        evidence_id
        for evidence_id in evidence_ids
        if label in authority_anchors
        and label in str(index.evidence_by_id[evidence_id]["text"])
    ]
    if identity_label_anchor_ids:
        _register_decision(group_key, {
            "source_label": label,
            "identity_kind": "named",
            "identity_group_ref": seed_group_ref,
            "authority_id": authority_id,
            "canonical_name": canonical_name,
            "evidence_id": identity_label_anchor_ids[0],
            "owned_source_segment_ids": source_ids,
            "proof_kind": "identity_key_registered_authority",
            "proof_anchors": [label],
            "materialization_compatible": materialization_compatible,
        }, decision_by_id=decision_by_id, decision_ids_by_group=decision_ids_by_group)


def _register_existing_bound_group_named_decisions(
    group_key: str,
    label: str,
    source_ids: list[str],
    authority_id: str,
    canonical_name: str,
    materialization_compatible: bool,
    *,
    catalog: IdentityAuthorityCatalog,
    index: StructuralCoverageIndex,
    decision_by_id: dict[str, dict],
    decision_ids_by_group: dict[str, list[str]],
) -> None:
    """已有 group 的 authority_ids 精确等于 {authority_id} 且命中该 group owned evidence 时登记 K 决议。"""
    for identity_group_ref, catalog_group in catalog.groups_by_ref.items():
        if set(catalog_group.get("authority_ids") or []) != {authority_id}:
            continue
        matching_ids = _matching_group_evidence_ids(
            group_key, identity_group_ref, catalog=catalog, index=index
        )
        if not matching_ids:
            continue
        _register_decision(group_key, {
            "source_label": label,
            "identity_kind": "named",
            "identity_group_ref": identity_group_ref,
            "authority_id": authority_id,
            "canonical_name": canonical_name,
            "evidence_id": matching_ids[0],
            "owned_source_segment_ids": source_ids,
            "proof_kind": "existing_bound_group",
            "proof_anchors": [],
            "materialization_compatible": materialization_compatible,
        }, decision_by_id=decision_by_id, decision_ids_by_group=decision_ids_by_group)


def _register_named_authority_decisions_for_group(
    group: dict,
    evidence_ids: list[str],
    *,
    catalog: IdentityAuthorityCatalog,
    index: StructuralCoverageIndex,
    decision_by_id: dict[str, dict],
    decision_ids_by_group: dict[str, list[str]],
) -> None:
    """遍历全部已知 authority，为符合条件的每一个登记 K 决议（直接锚定 + 既有绑定 group 两路）。"""
    group_key = str(group["group_key"])
    label = str(group["source_label"])
    source_ids = list(group["source_segment_ids"])
    seed_group_ref = str(group["seed_group_ref"])
    for authority_id, authority in catalog.authority_by_id.items():
        materialization_compatible = bool(authority.get("materialization_compatible"))
        if (
            _coverage_group_kind(group_key, index=index) == "onscreen"
            and not materialization_compatible
        ):
            # A non-Bible/manual authority may be cited while mentioned,
            # but cannot be upgraded through coverage into a card-backed
            # onscreen identity without atomically migrating its authority.
            continue
        canonical_name = str(authority.get("canonical_name") or "").strip()
        _register_label_anchor_named_decision(
            group_key, label, source_ids, seed_group_ref,
            evidence_ids, authority_id, authority, canonical_name,
            materialization_compatible,
            index=index,
            decision_by_id=decision_by_id,
            decision_ids_by_group=decision_ids_by_group,
        )
        _register_existing_bound_group_named_decisions(
            group_key, label, source_ids,
            authority_id, canonical_name, materialization_compatible,
            catalog=catalog, index=index,
            decision_by_id=decision_by_id,
            decision_ids_by_group=decision_ids_by_group,
        )


def _register_decisions_for_group(
    group: dict,
    *,
    catalog: IdentityAuthorityCatalog,
    index: StructuralCoverageIndex,
    decision_by_id: dict[str, dict],
    decision_ids_by_group: dict[str, list[str]],
) -> None:
    """为单个 coverage group 登记全部候选决议：兜底 F、既有 group 挂靠 F、逐 authority 的 K。"""
    group_key = str(group["group_key"])
    evidence_ids = index.evidence_ids_by_group[group_key]
    _register_functional_seed_decision(
        group, evidence_ids,
        decision_by_id=decision_by_id, decision_ids_by_group=decision_ids_by_group,
    )
    _register_existing_group_functional_decisions(
        group, catalog=catalog, index=index,
        decision_by_id=decision_by_id, decision_ids_by_group=decision_ids_by_group,
    )
    _register_named_authority_decisions_for_group(
        group, evidence_ids, catalog=catalog, index=index,
        decision_by_id=decision_by_id, decision_ids_by_group=decision_ids_by_group,
    )


def _build_decision_catalog(
    coverage_groups: list[dict],
    *,
    catalog: IdentityAuthorityCatalog,
    index: StructuralCoverageIndex,
) -> tuple[dict[str, dict], dict[str, list[str]]]:
    """遍历全部 coverage group，产出 decision_by_id / decision_ids_by_group。"""
    decision_by_id: dict[str, dict] = {}
    decision_ids_by_group: dict[str, list[str]] = {}
    for group in coverage_groups:
        _register_decisions_for_group(
            group, catalog=catalog, index=index,
            decision_by_id=decision_by_id, decision_ids_by_group=decision_ids_by_group,
        )
    return decision_by_id, decision_ids_by_group
