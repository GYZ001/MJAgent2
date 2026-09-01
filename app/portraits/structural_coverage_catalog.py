"""结构化覆盖审计的目录构建阶段：从 structural_evidence/candidates/existing_
resolutions/bible 里建出 source 索引、authority/group 目录、owned 证据分组与
coverage group 列表。供 `structural_coverage_audit.py` 的编排器调用。
"""

from __future__ import annotations

import json

from app.evidence import repository as evidence_repository
from app.character_policy import resolution_declares_functional_identity
from app.errors import ContentGenerationError
from app.identity_authority import identity_resolution_is_authoritative
from app.schemas import Bible
from app.source_excerpt import index_source_segments

from .constants import STRUCTURAL_IDENTITY_COVERAGE_VERSION
from .discovery_resample import _canonical_named_authority_id
from .structural_coverage import screenplay_identity_resolution_is_current_for_source


def _index_structural_evidence(
    evidence: list[dict], source_text: str
) -> tuple[dict[str, str], dict[str, int], list[dict], list[str]]:
    """从已过滤的结构化证据条目建立 source_segment 索引与去重后的 identity_key 顺序。

    产出 (source_by_id, source_order, minimal, allowed_source_labels)：
    minimal 是每条证据裁剪到已知 source_segment 范围内的副本；
    allowed_source_labels 是按首次出现顺序去重的 identity_key 列表。
    """
    source_by_id = {
        segment.segment_id: segment.text
        for segment in index_source_segments(source_text)
    }
    source_order = {
        source_id: index for index, source_id in enumerate(source_by_id)
    }
    minimal = []
    for item in evidence:
        source_ids = [
            str(value) for value in item.get("source_segment_ids") or []
            if str(value) in source_by_id
        ]
        minimal.append({
            **item,
            "source_segment_ids": source_ids,
            "source_segments": {
                source_id: source_by_id[source_id] for source_id in source_ids
            },
        })
    allowed_source_labels = list(dict.fromkeys(
        str(item.get("identity_key") or "").strip()
        for item in minimal
        if str(item.get("identity_key") or "").strip()
    ))
    return source_by_id, source_order, minimal, allowed_source_labels


def _seed_authority_catalog_from_bible(bible: Bible) -> dict[str, dict]:
    """用 Bible 里的正典人物名预置 authority_by_id——这些名字天然可物化人物卡。"""
    authority_by_id: dict[str, dict] = {}
    for character in bible.characters:
        canonical_name = str(character.name or "").strip()
        if canonical_name:
            authority_by_id[f"bible:{canonical_name}"] = {
                "authority_id": f"bible:{canonical_name}",
                "canonical_name": canonical_name,
                "identity_group": "",
                "aliases": [],
                "materialization_compatible": True,
            }
    return authority_by_id


def _catalog_candidates_from_resolutions(
    candidates: list[dict],
    existing_resolutions: list[dict] | None,
    *,
    episode_no: int,
    source_text: str,
) -> list[dict]:
    """汇总本轮候选 + 仍对当前来源生效的历史权威决议，产出统一的候选目录。

    只保留 identity_resolution_is_authoritative() 判定为权威的条目；历史决议还
    要求 screenplay_identity_resolution_is_current_for_source() 判定仍对齐当前
    source_text/episode_no，否则视为过期决议丢弃。
    """
    catalog_candidates = [
        candidate
        for candidate in candidates
        if identity_resolution_is_authoritative(candidate)
    ]
    for resolution in existing_resolutions or []:
        if not screenplay_identity_resolution_is_current_for_source(
            resolution,
            episode_no=episode_no,
            source_text=source_text,
        ) or not identity_resolution_is_authoritative(resolution):
            continue
        canonical_name = str(resolution.get("canonical_name") or "").strip()
        catalog_candidates.append({
            "source_label": str(resolution.get("source_label") or "").strip(),
            "name": canonical_name,
            "identity_kind": (
                "functional"
                if resolution_declares_functional_identity(resolution)
                else "named"
            ),
            "identity_group": str(resolution.get("identity_group") or "").strip(),
            "authority_id": str(resolution.get("authority_id") or "").strip(),
        })
    return catalog_candidates


def _absorb_single_catalog_candidate(
    candidate: dict,
    *,
    authority_by_id: dict[str, dict],
    groups_by_ref: dict[str, dict],
    source_by_id: dict[str, str],
) -> None:
    """把一条候选（本轮候选或历史决议）吸收进 authority/group 目录（原地修改）。

    判据：identity_group 非空则登记/扩展该 group 的 source_labels 与
    source_segment_ids；identity_kind == "named" 且有真名则登记/校验唯一权威，
    同一 authority_id 对应两个不同真名是数据损坏，直接拒绝。
    """
    source_label = str(candidate.get("source_label") or "").strip()
    canonical_name = str(candidate.get("name") or "").strip()
    identity_group = str(candidate.get("identity_group") or "").strip()
    identity_kind = str(candidate.get("identity_kind") or "").strip()
    if identity_group:
        group = groups_by_ref.setdefault(identity_group, {
            "identity_group_ref": identity_group,
            "source_labels": [],
            "authority_ids": [],
            "source_segment_ids": [],
        })
        if source_label and source_label not in group["source_labels"]:
            group["source_labels"].append(source_label)
        candidate_source_ids = [
            str(value).strip()
            for value in (
                candidate.get("source_segment_ids")
                or [candidate.get("source_segment_id")]
            )
            if str(value or "").strip() in source_by_id
        ]
        for source_id in candidate_source_ids:
            if source_id not in group["source_segment_ids"]:
                group["source_segment_ids"].append(source_id)
    if identity_kind == "named" and canonical_name:
        authority_id = str(candidate.get("authority_id") or "").strip()
        if not authority_id:
            authority_id = _canonical_named_authority_id(canonical_name)
        authority = authority_by_id.setdefault(authority_id, {
            "authority_id": authority_id,
            "canonical_name": canonical_name,
            "identity_group": identity_group,
            "aliases": [],
            "materialization_compatible": (
                authority_id == _canonical_named_authority_id(canonical_name)
                and identity_group in {"", authority_id}
            ),
        })
        if authority["canonical_name"] != canonical_name:
            raise ContentGenerationError(
                f"identity authority={authority_id} 对应多个真名"
            )
        if source_label and source_label not in authority["aliases"]:
            authority["aliases"].append(source_label)
        if identity_group:
            group = groups_by_ref[identity_group]
            if authority_id not in group["authority_ids"]:
                group["authority_ids"].append(authority_id)


def _absorb_catalog_candidates(
    catalog_candidates: list[dict],
    *,
    authority_by_id: dict[str, dict],
    groups_by_ref: dict[str, dict],
    source_by_id: dict[str, str],
) -> None:
    """按候选目录顺序逐条吸收进 authority/group 目录（原地修改，顺序即权威优先级）。"""
    for candidate in catalog_candidates:
        _absorb_single_catalog_candidate(
            candidate,
            authority_by_id=authority_by_id,
            groups_by_ref=groups_by_ref,
            source_by_id=source_by_id,
        )


def _seed_group_refs_for_labels(
    allowed_source_labels: list[str],
    minimal: list[dict],
    groups_by_ref: dict[str, dict],
) -> dict[str, str]:
    """为每个 identity_key 生成确定性 seed group ref 并登记进目录（原地修改）。

    seed_ref 由 policy_version + label + 该 label 下全部结构化证据的内容哈希算出，
    保证同一输入必然产出同一 seed_ref（跨次调用可复现）。
    """
    seed_group_by_label: dict[str, str] = {}
    for label in allowed_source_labels:
        seed_ref = "new:" + evidence_repository.content_hash({
            "policy_version": STRUCTURAL_IDENTITY_COVERAGE_VERSION,
            "source_label": label,
            "structural_evidence": sorted(
                [
                    item for item in minimal
                    if str(item.get("identity_key") or "").strip() == label
                ],
                key=evidence_repository.content_hash,
            ),
        })[:24]
        seed_group_by_label[label] = seed_ref
        groups_by_ref.setdefault(seed_ref, {
            "identity_group_ref": seed_ref,
            "source_labels": [label],
            "authority_ids": [],
            "source_segment_ids": [],
        })
    return seed_group_by_label


def _raise_if_groups_have_conflicting_authorities(
    groups_by_ref: dict[str, dict],
) -> None:
    """一个 identity_group 绑定 ≥2 个不同 authority_id 是数据损坏，直接拒绝生成。"""
    conflicting_groups = {
        group_ref: sorted(set(group.get("authority_ids") or []))
        for group_ref, group in groups_by_ref.items()
        if len(set(group.get("authority_ids") or [])) > 1
    }
    if conflicting_groups:
        raise ContentGenerationError(
            "structural identity group 缺少唯一权威："
            + json.dumps(
                conflicting_groups,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )


def _owned_structural_by_label(
    minimal: list[dict],
    allowed_source_labels: list[str],
) -> dict[str, list[dict]]:
    """按 identity_key 分组 owned 结构化证据；任一 allowed label 缺 owned SRC 即拒绝。"""
    structural_by_key: dict[str, list[dict]] = {}
    for item in minimal:
        label = str(item.get("identity_key") or "").strip()
        if label:
            structural_by_key.setdefault(label, []).append(item)
    owned_source_by_key = {
        label: "\n".join(
            str(text)
            for item in items
            for text in (item.get("source_segments") or {}).values()
            if str(text)
        )
        for label, items in structural_by_key.items()
    }
    missing_owned_source = [
        label for label in allowed_source_labels
        if not owned_source_by_key.get(label, "").strip()
    ]
    if missing_owned_source:
        raise ContentGenerationError(
            "structural identity coverage 缺少 owned SRC："
            + ",".join(sorted(missing_owned_source))
        )
    return structural_by_key


def _raise_if_onscreen_label_lacks_materializable_authority(
    structural_by_key: dict[str, list[dict]],
    authority_by_id: dict[str, dict],
) -> None:
    """出镜（非纯提及）label 若命中的 authority 全部不可物化，直接拒绝生成。"""
    for label, typed_items in structural_by_key.items():
        if {
            str(item.get("usage") or "").strip() for item in typed_items
        } == {"mentioned"}:
            continue
        matching_authorities = [
            authority
            for authority in authority_by_id.values()
            if label in {
                str(authority.get("canonical_name") or "").strip(),
                *(
                    str(alias or "").strip()
                    for alias in authority.get("aliases") or []
                ),
            }
        ]
        if (
            matching_authorities
            and not any(
                authority.get("materialization_compatible")
                for authority in matching_authorities
            )
        ):
            raise ContentGenerationError(
                "structural coverage 可见人物只有不可物化的引用身份："
                f"{label}"
            )


def _build_coverage_groups(
    allowed_source_labels: list[str],
    structural_by_key: dict[str, list[dict]],
    source_order: dict[str, int],
    seed_group_by_label: dict[str, str],
) -> list[dict]:
    """按首次出现顺序给每个 label 分配 group_key，并汇总其 owned source_segment_ids。"""
    return [
        {
            "group_key": f"I{index:03d}",
            "source_label": label,
            "source_segment_ids": sorted({
                str(source_id)
                for item in structural_by_key[label]
                for source_id in item.get("source_segment_ids") or []
                if str(source_id) in source_order
            }, key=lambda source_id: source_order[source_id]),
            "seed_group_ref": seed_group_by_label[label],
        }
        for index, label in enumerate(allowed_source_labels, start=1)
    ]


def _build_evidence_catalog(
    coverage_groups: list[dict],
    source_by_id: dict[str, str],
) -> tuple[dict[str, dict], dict[str, list[str]]]:
    """给每个 group 的 owned source_segment 生成确定性 evidence_id（内容寻址）。"""
    evidence_by_id: dict[str, dict] = {}
    evidence_ids_by_group: dict[str, list[str]] = {}
    for group in coverage_groups:
        group_key = str(group["group_key"])
        evidence_ids: list[str] = []
        for source_id in group["source_segment_ids"]:
            text = str(source_by_id[source_id])
            evidence_id = "E:" + evidence_repository.content_hash({
                "contract_version": STRUCTURAL_IDENTITY_COVERAGE_VERSION,
                "group_key": group_key,
                "source_segment_id": source_id,
                "text": text,
            })[:20]
            evidence_by_id[evidence_id] = {
                "evidence_id": evidence_id,
                "group_key": group_key,
                "source_segment_id": source_id,
                "text": text,
            }
            evidence_ids.append(evidence_id)
        evidence_ids_by_group[group_key] = evidence_ids
    return evidence_by_id, evidence_ids_by_group
