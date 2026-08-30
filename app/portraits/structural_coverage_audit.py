"""从结构化证据审计身份覆盖：audit_identity_coverage_from_structural_evidence
单一巨函数，产出结构化覆盖回执。
"""

from __future__ import annotations

import json

from app.evidence import repository as evidence_repository
from app import hiagent
from app.character_policy import resolution_declares_functional_identity
from app.errors import ContentGenerationError
from app.identity_authority import identity_resolution_is_authoritative
from app.schemas import Bible
from app.source_excerpt import index_source_segments

from .constants import (
    IDENTITY_REQUEST_MAX_TOKENS,
    STRUCTURAL_IDENTITY_COVERAGE_VERSION,
)
from .discovery_resample import (
    _bounded_owned_identity_evidence,
    _canonical_named_authority_id,
    _identity_operation_retry_epoch,
    _identity_structured_with_resample,
)
from .evidence_receipt import _attach_candidate_source_evidence
from .identity_schemas import (
    StructuralIdentityCoverageResponse,
    _structural_identity_coverage_response_format,
    _structural_identity_coverage_schema,
)
from .structural_coverage import (
    _STRUCTURAL_IDENTITY_CATALOG_RECEIPT_VERSION,
    screenplay_identity_resolution_is_current_for_source,
)

async def audit_identity_coverage_from_structural_evidence(
    candidates: list[dict],
    *,
    structural_evidence: list[dict] | None,
    source_text: str,
    bible: Bible,
    episode_no: int,
    existing_resolutions: list[dict] | None = None,
    catalog_receipt: dict[str, object] | None = None,
) -> list[dict]:
    """Audit only typed Blueprint/IR references that lack identity ownership."""
    evidence = [item for item in (structural_evidence or []) if isinstance(item, dict)]
    if not evidence:
        return candidates
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
    groups_by_ref: dict[str, dict] = {}
    # Current RF9 may preserve a non-literal provider label as an explicitly
    # synthetic observation.  It is useful as a low-confidence audit input,
    # but it is not an alias or identity authority and must never suppress the
    # Blueprint-owned coverage gate.
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
        canonical_name = str(
            resolution.get("canonical_name") or ""
        ).strip()
        catalog_candidates.append({
            "source_label": str(
                resolution.get("source_label") or ""
            ).strip(),
            "name": canonical_name,
            "identity_kind": (
                "functional"
                if resolution_declares_functional_identity(resolution)
                else "named"
            ),
            "identity_group": str(
                resolution.get("identity_group") or ""
            ).strip(),
            "authority_id": str(
                resolution.get("authority_id") or ""
            ).strip(),
        })
    for candidate in catalog_candidates:
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

    coverage_groups = [
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
    coverage_group_by_key = {
        str(group["group_key"]): group for group in coverage_groups
    }
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

    def matching_group_evidence_ids(
        group_key: str,
        identity_group_ref: str,
    ) -> list[str]:
        """Return owned spans for an exact backend-registered label binding.

        Mere SRC overlap or co-occurrence is not identity evidence: one source
        sentence routinely contains several people.  An existing group is
        eligible only when this exact synthetic identity key is already one of
        its registered labels and appears verbatim in the owned span.
        """
        group = groups_by_ref.get(identity_group_ref, {})
        registered_labels = {
            str(value).strip()
            for value in group.get("source_labels") or []
            if str(value).strip()
        }
        source_label = str(
            coverage_group_by_key[group_key]["source_label"]
        )
        if source_label not in registered_labels:
            return []
        return [
            evidence_id
            for evidence_id in evidence_ids_by_group.get(group_key, [])
            if source_label in str(evidence_by_id[evidence_id]["text"])
        ]

    decision_by_id: dict[str, dict] = {}
    decision_ids_by_group: dict[str, list[str]] = {}

    def register_decision(group_key: str, payload: dict) -> str:
        decision_kind = (
            "K" if payload.get("identity_kind") == "named" else "F"
        )
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

    def coverage_group_kind(group_key: str) -> str:
        label = str(coverage_group_by_key[group_key]["source_label"])
        usages = {
            str(item.get("usage") or "").strip()
            for item in structural_by_key.get(label, [])
        }
        return "mentioned" if usages == {"mentioned"} else "onscreen"

    for group in coverage_groups:
        group_key = str(group["group_key"])
        label = str(group["source_label"])
        evidence_ids = evidence_ids_by_group[group_key]
        primary_evidence_id = evidence_ids[0]
        source_ids = list(group["source_segment_ids"])
        seed_group_ref = str(group["seed_group_ref"])
        register_decision(group_key, {
            "source_label": label,
            "identity_kind": "functional",
            "identity_group_ref": seed_group_ref,
            "authority_id": "",
            "canonical_name": "",
            "evidence_id": primary_evidence_id,
            "owned_source_segment_ids": source_ids,
            "proof_kind": "owned_functional_new",
        })
        for identity_group_ref, catalog_group in groups_by_ref.items():
            if identity_group_ref == seed_group_ref:
                continue
            matching_ids = matching_group_evidence_ids(
                group_key, identity_group_ref
            )
            if not matching_ids:
                continue
            group_authorities = sorted(set(
                str(value)
                for value in catalog_group.get("authority_ids") or []
                if str(value)
            ))
            if not group_authorities:
                register_decision(group_key, {
                    "source_label": label,
                    "identity_kind": "functional",
                    "identity_group_ref": identity_group_ref,
                    "authority_id": "",
                    "canonical_name": "",
                    "evidence_id": matching_ids[0],
                    "owned_source_segment_ids": source_ids,
                    "proof_kind": "owned_functional_existing_group",
                })

        for authority_id, authority in authority_by_id.items():
            canonical_name = str(
                authority.get("canonical_name") or ""
            ).strip()
            materialization_compatible = bool(
                authority.get("materialization_compatible")
            )
            if (
                coverage_group_kind(group_key) == "onscreen"
                and not materialization_compatible
            ):
                # A non-Bible/manual authority may be cited while mentioned,
                # but cannot be upgraded through coverage into a card-backed
                # onscreen identity without atomically migrating its authority.
                continue
            authority_anchors = list(dict.fromkeys(
                value
                for value in [
                    canonical_name,
                    *[
                        str(alias).strip()
                        for alias in authority.get("aliases") or []
                    ],
                ]
                if value
            ))
            identity_label_anchor_ids = [
                evidence_id
                for evidence_id in evidence_ids
                if label in authority_anchors
                and label in str(evidence_by_id[evidence_id]["text"])
            ]
            if identity_label_anchor_ids:
                register_decision(group_key, {
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
                })
            for identity_group_ref, catalog_group in groups_by_ref.items():
                if set(catalog_group.get("authority_ids") or []) != {
                    authority_id
                }:
                    continue
                matching_ids = matching_group_evidence_ids(
                    group_key, identity_group_ref
                )
                if not matching_ids:
                    continue
                register_decision(group_key, {
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
                })

    coverage_group_keys = [
        str(group["group_key"]) for group in coverage_groups
    ]
    coverage_schema = _structural_identity_coverage_schema(
        coverage_group_keys,
        decision_ids_by_group=decision_ids_by_group,
    )
    coverage_response_format = _structural_identity_coverage_response_format(
        coverage_schema
    )
    coverage_group_projection = [
        {
            "group_key": group["group_key"],
            "source_label": group["source_label"],
            "owned_source_segment_ids": group["source_segment_ids"],
        }
        for group in coverage_groups
    ]
    coverage_evidence_projection = list(evidence_by_id.values())
    coverage_decision_projection = list(decision_by_id.values())
    receipt_hashes = {
        "authority_catalog_hash": evidence_repository.content_hash(
            sorted(
                authority_by_id.values(),
                key=lambda item: str(item.get("authority_id") or ""),
            )
        ),
        "group_catalog_hash": evidence_repository.content_hash(
            sorted(
                groups_by_ref.values(),
                key=lambda item: str(
                    item.get("identity_group_ref") or ""
                ),
            )
        ),
        "decision_catalog_hash": evidence_repository.content_hash(
            coverage_decision_projection
        ),
        "evidence_catalog_hash": evidence_repository.content_hash(
            coverage_evidence_projection
        ),
    }
    if catalog_receipt is not None:
        catalog_receipt.clear()
        catalog_receipt.update({
            "version": _STRUCTURAL_IDENTITY_CATALOG_RECEIPT_VERSION,
            **receipt_hashes,
            "hash": evidence_repository.content_hash(receipt_hashes),
        })
    prompt = f"""任务：审计结构化蓝图/IR 中未绑定的人物引用。
未决引用目录（group_key 是唯一可输出的键；同键所有 owned SRC 行共用一个决议）：
{json.dumps(coverage_group_projection, ensure_ascii=False, separators=(',', ':'))}
owned SRC 证据目录（后端逐字锁定，不得回抄或改写）：
{json.dumps(coverage_evidence_projection, ensure_ascii=False, separators=(',', ':'))}
可选决议目录（每个不透明 decision_id 已绑定 label/kind/group/authority/evidence/source_ids）：
{json.dumps(coverage_decision_projection, ensure_ascii=False, separators=(',', ':'))}
规则：
1. decisions 必须精确输出全部 group_key，不得增删键。
2. 证据不足时选 F 决议，这是合法终态；不得猜测人物权威。
3. 只有目录已提供 K 决议时才能绑定已有人物；不得自行组合姓名、组或证据。
4. 只输出符合下列 Schema 的 JSON：
{json.dumps(coverage_schema, ensure_ascii=False, separators=(',', ':'))}"""
    coverage_provider, coverage_model, coverage_effective_max = (
        hiagent.text_request_token_limits(
            requested_max_tokens=IDENTITY_REQUEST_MAX_TOKENS,
        )
    )
    coverage_semantic_settings = hiagent.text_request_semantic_settings(
        coverage_provider
    )

    def validate_response(
        value: StructuralIdentityCoverageResponse,
    ) -> list[str]:
        errors: list[str] = []
        expected_keys = set(coverage_group_keys)
        if set(value.decisions) != expected_keys:
            errors.append("structural coverage decisions keys 不闭合")
        named_authorities_by_group: dict[str, set[str]] = {}
        named_groups: set[str] = set()
        functional_groups: set[str] = set()
        for group_key in coverage_group_keys:
            selected_id = str(value.decisions.get(group_key) or "")
            item = decision_by_id.get(selected_id)
            if item is None or item.get("group_key") != group_key:
                errors.append(f"structural coverage decision_id 越界：{group_key}")
                continue
            source_label = str(item.get("source_label") or "")
            expected_label = str(
                coverage_group_by_key[group_key]["source_label"]
            )
            if source_label != expected_label:
                errors.append(f"structural coverage label 不匹配：{group_key}")
            identity_group = str(item.get("identity_group_ref") or "")
            if identity_group not in groups_by_ref:
                errors.append(f"identity_group_ref 越界：{group_key}")
            expected_source_ids = list(
                coverage_group_by_key[group_key]["source_segment_ids"]
            )
            if list(item.get("owned_source_segment_ids") or []) != (
                expected_source_ids
            ):
                errors.append(f"owned source ids 不闭合：{group_key}")
            evidence_id = str(item.get("evidence_id") or "")
            evidence = evidence_by_id.get(evidence_id)
            if (
                evidence_id not in evidence_ids_by_group.get(group_key, [])
                or evidence is None
                or str(evidence.get("source_segment_id") or "")
                not in expected_source_ids
                or str(evidence.get("text") or "")
                != source_by_id.get(
                    str(evidence.get("source_segment_id") or ""), ""
                )
            ):
                errors.append(f"owned evidence receipt 无效：{group_key}")
                continue
            if item.get("identity_kind") == "named":
                authority_id = str(item.get("authority_id") or "")
                authority = authority_by_id.get(authority_id)
                if authority is None:
                    errors.append(f"authority_id 越界：{group_key}")
                else:
                    existing_group_authorities = set(
                        groups_by_ref.get(identity_group, {}).get(
                            "authority_ids", []
                        )
                    )
                    if (
                        existing_group_authorities
                        and authority_id not in existing_group_authorities
                    ):
                        errors.append(
                            "named authority 与已有 group 权威冲突："
                            f"{group_key}"
                        )
                    authority_anchors = set(
                        str(value).strip()
                        for value in item.get("proof_anchors") or []
                        if str(value).strip()
                    )
                    proof_kind = str(item.get("proof_kind") or "")
                    bound_group_proof = bool(
                        proof_kind == "existing_bound_group"
                        and existing_group_authorities == {authority_id}
                        and evidence_id in matching_group_evidence_ids(
                            group_key, identity_group
                        )
                    )
                    label_authority_proof = bool(
                        proof_kind == "identity_key_registered_authority"
                        and source_label in authority_anchors
                        and source_label
                        in str(evidence.get("text") or "")
                        and source_label
                        in {
                            str(authority.get("canonical_name") or "").strip(),
                            *(
                                str(alias or "").strip()
                                for alias in authority.get("aliases") or []
                            ),
                        }
                    )
                    if not (bound_group_proof or label_authority_proof):
                        errors.append(
                            "named group 缺少 owned authority 锚点："
                            f"{group_key}"
                        )
                    if (
                        coverage_group_kind(group_key) == "onscreen"
                        and not item.get("materialization_compatible")
                    ):
                        errors.append(
                            "structural coverage K authority 不可直接物化人物卡："
                            f"{group_key}"
                        )
                named_groups.add(identity_group)
                named_authorities_by_group.setdefault(
                    identity_group, set()
                ).add(authority_id)
            else:
                if item.get("authority_id") or item.get("canonical_name"):
                    errors.append(f"functional 携带权威：{group_key}")
                functional_groups.add(identity_group)
        for identity_group, authority_ids in named_authorities_by_group.items():
            if len(authority_ids) > 1:
                errors.append(
                    "identity_group 对应多个 named authority："
                    f"{identity_group}"
                )
        for identity_group in named_groups & functional_groups:
            errors.append(
                "functional 不得引用本响应已升级 group："
                f"{identity_group}"
            )
        for identity_group in functional_groups:
            if groups_by_ref.get(identity_group, {}).get("authority_ids"):
                errors.append(
                    "functional 不得引用已命名 group："
                    f"{identity_group}"
                )
        return errors

    response = await _identity_structured_with_resample(
        [{"role": "user", "content": prompt}],
        model_type=StructuralIdentityCoverageResponse,
        validate=validate_response,
        operation_id_for_attempt=lambda resample_attempt: (
            f"screenplay.identity.coverage.v6:{episode_no}:"
            + evidence_repository.content_hash({
                "resample_attempt": resample_attempt,
                "contract_version": STRUCTURAL_IDENTITY_COVERAGE_VERSION,
                "provider": coverage_provider,
                "model": coverage_model,
                "requested_max_tokens": 4096,
                "effective_max_tokens": coverage_effective_max,
            "temperature": 0.05,
            "provider_semantic_settings": coverage_semantic_settings,
            "retry_epoch": _identity_operation_retry_epoch(),
                "prompt": prompt,
                "schema": coverage_schema,
                "response_format": coverage_response_format,
            })
        ),
        max_tokens=IDENTITY_REQUEST_MAX_TOKENS,
        temperature=0.05,
        format_retry_limit=0,
        semantic_retry_limit=0,
        call_meta={
            "stage": "discover_character_candidates",
            "stage_key": "screenplay_character_discovery",
            "substage": "structural_coverage",
            "discovery_phase": "coverage",
            "episode_no": episode_no,
            "contract_version": STRUCTURAL_IDENTITY_COVERAGE_VERSION,
            "schema_hash": evidence_repository.content_hash(
                coverage_schema
            ),
            "decision_catalog_hash": evidence_repository.content_hash(
                coverage_decision_projection
            ),
            "evidence_catalog_hash": evidence_repository.content_hash(
                coverage_evidence_projection
            ),
            "disable_provider_retries": True,
            "disable_provider_candidate_fallback": True,
            "disable_reasoning_fallback": True,
            "reuse_successful_operation": False,
            "provider": coverage_provider,
            "model": coverage_model,
            "effective_max_tokens": coverage_effective_max,
            "provider_semantic_settings": coverage_semantic_settings,
            "retry_epoch": _identity_operation_retry_epoch(),
        },
        output_schema=coverage_schema,
        response_format=coverage_response_format,
        require_response_format=True,
    )
    selected_decisions = [
        decision_by_id[str(response.decisions[group_key])]
        for group_key in coverage_group_keys
    ]
    existing = {
        (str(item.get("source_label") or ""), str(item.get("identity_group") or ""))
        for item in candidates
    }
    additions: list[dict] = []
    new_group_members: dict[str, set[str]] = {}
    for decision in selected_decisions:
        raw_group = str(decision.get("identity_group_ref") or "").strip()
        label = str(decision.get("source_label") or "").strip()
        if raw_group.startswith("new:") and label:
            new_group_members.setdefault(raw_group, set()).add(label)
    normalized_new_groups = {
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
    for raw in selected_decisions:
        label = str(raw.get("source_label") or "").strip()
        typed_evidence = structural_by_key.get(label) or []
        if not label or not typed_evidence:
            raise ContentGenerationError(
                f"结构人物 coverage 缺少 owned evidence：{label}"
            )
        identity_kind = str(raw.get("identity_kind") or "functional")
        authority_id = str(raw.get("authority_id") or "").strip()
        canonical_name = str(
            authority_by_id.get(authority_id, {}).get("canonical_name") or ""
        )
        raw_group = str(raw.get("identity_group_ref") or "").strip()
        group = normalized_new_groups.get(raw_group, raw_group)
        if (label, group) in existing:
            continue
        usages = {
            str(value.get("usage") or "").strip()
            for value in typed_evidence
        }
        projected_kind = "mentioned" if usages == {"mentioned"} else "onscreen"
        if (
            identity_kind == "named"
            and projected_kind == "onscreen"
            and not raw.get("materialization_compatible")
        ):
            raise ContentGenerationError(
                "structural coverage K authority 不可直接物化人物卡："
                f"{label}"
            )
        source_ids = sorted({
            str(source_id)
            for value in typed_evidence
            for source_id in value.get("source_segment_ids") or []
            if str(source_id) in source_by_id
        }, key=lambda source_id: source_order[source_id])
        evidence_record = evidence_by_id.get(
            str(raw.get("evidence_id") or ""), {}
        )
        source_segment_id = str(
            evidence_record.get("source_segment_id") or ""
        )
        if source_segment_id not in source_ids:
            raise ContentGenerationError(
                f"结构人物 coverage evidence receipt 越界：{label}"
            )
        evidence_text = str(evidence_record.get("text") or "")
        proof_anchors = [
            str(value)
            for value in raw.get("proof_anchors") or []
            if str(value)
        ]
        bounded_evidence = (
            _bounded_owned_identity_evidence(
                evidence_text,
                anchors=proof_anchors,
                max_chars=80,
            )
            if identity_kind == "named" and proof_anchors
            else evidence_text.strip()[:80]
        )
        additions.append({
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
            "materialization_compatible": bool(
                raw.get("materialization_compatible")
            ),
        })
    return _attach_candidate_source_evidence([*candidates, *additions], source_text)

