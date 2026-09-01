"""结构化覆盖审计的响应校验阶段：核对 provider 选中的 decision_id 是否闭合、
未越界、owned 证据与 authority 锚点齐全，供 `_identity_structured_with_resample`
的 `validate` 回调使用。
"""

from __future__ import annotations

from .identity_schemas import StructuralIdentityCoverageResponse
from .structural_coverage_decisions import (
    IdentityAuthorityCatalog,
    StructuralCoverageIndex,
    _coverage_group_kind,
    _matching_group_evidence_ids,
)


def _validate_named_decision(
    item: dict,
    group_key: str,
    identity_group: str,
    evidence: dict,
    evidence_id: str,
    source_label: str,
    *,
    catalog: IdentityAuthorityCatalog,
    index: StructuralCoverageIndex,
) -> tuple[list[str], str]:
    """校验 named 决议的 authority 越界、group 权威冲突、owned 锚点、可物化性。

    返回 (errors, authority_id)。
    """
    errors: list[str] = []
    authority_id = str(item.get("authority_id") or "")
    authority = catalog.authority_by_id.get(authority_id)
    if authority is None:
        errors.append(f"authority_id 越界：{group_key}")
        return errors, authority_id
    existing_group_authorities = set(
        catalog.groups_by_ref.get(identity_group, {}).get("authority_ids", [])
    )
    if existing_group_authorities and authority_id not in existing_group_authorities:
        errors.append(f"named authority 与已有 group 权威冲突：{group_key}")
    authority_anchors = set(
        str(anchor).strip()
        for anchor in item.get("proof_anchors") or []
        if str(anchor).strip()
    )
    proof_kind = str(item.get("proof_kind") or "")
    bound_group_proof = bool(
        proof_kind == "existing_bound_group"
        and existing_group_authorities == {authority_id}
        and evidence_id in _matching_group_evidence_ids(
            group_key, identity_group, catalog=catalog, index=index
        )
    )
    label_authority_proof = bool(
        proof_kind == "identity_key_registered_authority"
        and source_label in authority_anchors
        and source_label in str(evidence.get("text") or "")
        and source_label in {
            str(authority.get("canonical_name") or "").strip(),
            *(
                str(alias or "").strip()
                for alias in authority.get("aliases") or []
            ),
        }
    )
    if not (bound_group_proof or label_authority_proof):
        errors.append(f"named group 缺少 owned authority 锚点：{group_key}")
    if (
        _coverage_group_kind(group_key, index=index) == "onscreen"
        and not item.get("materialization_compatible")
    ):
        errors.append(f"structural coverage K authority 不可直接物化人物卡：{group_key}")
    return errors, authority_id


def _validate_group_decision(
    group_key: str,
    value: StructuralIdentityCoverageResponse,
    *,
    catalog: IdentityAuthorityCatalog,
    index: StructuralCoverageIndex,
    decision_by_id: dict[str, dict],
) -> tuple[list[str], tuple[str, bool, str] | None]:
    """校验单个 group_key 的选中决议是否闭合、越界、锚点齐全。

    返回 (errors, classification)。classification 为 None 表示该 group 决议本身
    无效（decision_id 越界或 evidence receipt 无效），调用方不应再统计它属于
    named/functional 聚合；否则是 (identity_group_ref, is_named, authority_id)。
    """
    errors: list[str] = []
    selected_id = str(value.decisions.get(group_key) or "")
    item = decision_by_id.get(selected_id)
    if item is None or item.get("group_key") != group_key:
        errors.append(f"structural coverage decision_id 越界：{group_key}")
        return errors, None
    source_label = str(item.get("source_label") or "")
    expected_label = str(index.coverage_group_by_key[group_key]["source_label"])
    if source_label != expected_label:
        errors.append(f"structural coverage label 不匹配：{group_key}")
    identity_group = str(item.get("identity_group_ref") or "")
    if identity_group not in catalog.groups_by_ref:
        errors.append(f"identity_group_ref 越界：{group_key}")
    expected_source_ids = list(
        index.coverage_group_by_key[group_key]["source_segment_ids"]
    )
    if list(item.get("owned_source_segment_ids") or []) != expected_source_ids:
        errors.append(f"owned source ids 不闭合：{group_key}")
    evidence_id = str(item.get("evidence_id") or "")
    evidence = index.evidence_by_id.get(evidence_id)
    if (
        evidence_id not in index.evidence_ids_by_group.get(group_key, [])
        or evidence is None
        or str(evidence.get("source_segment_id") or "") not in expected_source_ids
        or str(evidence.get("text") or "")
        != index.source_by_id.get(str(evidence.get("source_segment_id") or ""), "")
    ):
        errors.append(f"owned evidence receipt 无效：{group_key}")
        return errors, None
    if item.get("identity_kind") == "named":
        named_errors, authority_id = _validate_named_decision(
            item, group_key, identity_group, evidence, evidence_id, source_label,
            catalog=catalog, index=index,
        )
        errors.extend(named_errors)
        return errors, (identity_group, True, authority_id)
    if item.get("authority_id") or item.get("canonical_name"):
        errors.append(f"functional 携带权威：{group_key}")
    return errors, (identity_group, False, "")


def _validate_cross_group_consistency(
    named_groups: set[str],
    functional_groups: set[str],
    named_authorities_by_group: dict[str, set[str]],
    *,
    catalog: IdentityAuthorityCatalog,
) -> list[str]:
    """跨 group 一致性：单 group 单 named 权威；functional 不得引用已升级/已命名 group。"""
    errors: list[str] = []
    for identity_group, authority_ids in named_authorities_by_group.items():
        if len(authority_ids) > 1:
            errors.append(f"identity_group 对应多个 named authority：{identity_group}")
    for identity_group in named_groups & functional_groups:
        errors.append(f"functional 不得引用本响应已升级 group：{identity_group}")
    for identity_group in functional_groups:
        if catalog.groups_by_ref.get(identity_group, {}).get("authority_ids"):
            errors.append(f"functional 不得引用已命名 group：{identity_group}")
    return errors


def _validate_structural_coverage_response(
    value: StructuralIdentityCoverageResponse,
    *,
    coverage_group_keys: list[str],
    catalog: IdentityAuthorityCatalog,
    index: StructuralCoverageIndex,
    decision_by_id: dict[str, dict],
) -> list[str]:
    """整体校验响应：decisions 键闭合 + 逐 group 校验 + 跨 group 一致性。"""
    errors: list[str] = []
    if set(value.decisions) != set(coverage_group_keys):
        errors.append("structural coverage decisions keys 不闭合")
    named_authorities_by_group: dict[str, set[str]] = {}
    named_groups: set[str] = set()
    functional_groups: set[str] = set()
    for group_key in coverage_group_keys:
        group_errors, classification = _validate_group_decision(
            group_key, value,
            catalog=catalog, index=index, decision_by_id=decision_by_id,
        )
        errors.extend(group_errors)
        if classification is None:
            continue
        identity_group, is_named, authority_id = classification
        if is_named:
            named_groups.add(identity_group)
            named_authorities_by_group.setdefault(identity_group, set()).add(
                authority_id
            )
        else:
            functional_groups.add(identity_group)
    errors.extend(_validate_cross_group_consistency(
        named_groups, functional_groups, named_authorities_by_group, catalog=catalog,
    ))
    return errors
