"""从结构化证据审计身份覆盖：audit_identity_coverage_from_structural_evidence。

本文件只保留编排器与「向 provider 发起结构化覆盖审计请求」这一小簇 helper；
目录构建、决议登记、响应校验、结果收尾四个阶段分别拆到同目录下的
structural_coverage_catalog / structural_coverage_decisions /
structural_coverage_validate / structural_coverage_finalize 四个模块，避免本
文件的行数基线（FILE_CONVENTIONS.toml）随重构继续上涨。
"""

from __future__ import annotations

import json

from app.evidence import repository as evidence_repository
from app import hiagent
from app.schemas import Bible

from .constants import (
    IDENTITY_REQUEST_MAX_TOKENS,
    STRUCTURAL_IDENTITY_COVERAGE_VERSION,
)
from .discovery_resample import (
    _identity_operation_retry_epoch,
    _identity_structured_with_resample,
)
from .evidence_receipt import _attach_candidate_source_evidence
from .identity_schemas import (
    StructuralIdentityCoverageResponse,
    _structural_identity_coverage_response_format,
    _structural_identity_coverage_schema,
)
from .structural_coverage import _STRUCTURAL_IDENTITY_CATALOG_RECEIPT_VERSION
from .structural_coverage_catalog import (
    _absorb_catalog_candidates,
    _build_coverage_groups,
    _build_evidence_catalog,
    _catalog_candidates_from_resolutions,
    _index_structural_evidence,
    _owned_structural_by_label,
    _raise_if_groups_have_conflicting_authorities,
    _raise_if_onscreen_label_lacks_materializable_authority,
    _seed_authority_catalog_from_bible,
    _seed_group_refs_for_labels,
)
from .structural_coverage_decisions import (
    IdentityAuthorityCatalog,
    StructuralCoverageIndex,
    _build_decision_catalog,
)
from .structural_coverage_finalize import (
    _build_candidate_additions,
    _selected_decisions_from_response,
)
from .structural_coverage_validate import _validate_structural_coverage_response


def _structural_coverage_projections(
    coverage_groups: list[dict],
    evidence_by_id: dict[str, dict],
    decision_by_id: dict[str, dict],
) -> tuple[list[dict], list[dict], list[dict]]:
    """产出发给 provider 的三份精简目录投影：group/evidence/decision。"""
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
    return (
        coverage_group_projection,
        coverage_evidence_projection,
        coverage_decision_projection,
    )


def _structural_coverage_receipt_hashes(
    authority_by_id: dict[str, dict],
    groups_by_ref: dict[str, dict],
    coverage_decision_projection: list[dict],
    coverage_evidence_projection: list[dict],
) -> dict[str, str]:
    """给 authority/group/decision/evidence 四份目录各算一个内容哈希，供覆盖回执核验。"""
    return {
        "authority_catalog_hash": evidence_repository.content_hash(
            sorted(
                authority_by_id.values(),
                key=lambda item: str(item.get("authority_id") or ""),
            )
        ),
        "group_catalog_hash": evidence_repository.content_hash(
            sorted(
                groups_by_ref.values(),
                key=lambda item: str(item.get("identity_group_ref") or ""),
            )
        ),
        "decision_catalog_hash": evidence_repository.content_hash(
            coverage_decision_projection
        ),
        "evidence_catalog_hash": evidence_repository.content_hash(
            coverage_evidence_projection
        ),
    }


def _structural_coverage_prompt(
    coverage_group_projection: list[dict],
    coverage_evidence_projection: list[dict],
    coverage_decision_projection: list[dict],
    coverage_schema: dict,
) -> str:
    """拼装发给 provider 的结构化覆盖审计 prompt：目录 + 规则 + Schema。"""
    return f"""任务：审计结构化蓝图/IR 中未绑定的人物引用。
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


def _coverage_completion_operation_id(
    resample_attempt: int,
    *,
    episode_no: int,
    prompt: str,
    coverage_schema: dict,
    coverage_response_format: dict,
    coverage_provider: str,
    coverage_model: str,
    coverage_effective_max: int,
    coverage_semantic_settings: dict,
) -> str:
    """按本次尝试的完整请求内容算幂等 operation_id，resample_attempt 变化则必然改变。"""
    return (
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
    )


def _coverage_completion_call_meta(
    episode_no: int,
    coverage_schema: dict,
    coverage_decision_projection: list[dict],
    coverage_evidence_projection: list[dict],
    coverage_provider: str,
    coverage_model: str,
    coverage_effective_max: int,
    coverage_semantic_settings: dict,
) -> dict:
    """本次结构化覆盖审计请求的可观测性元数据（供 model_gateway 记录与限流判定）。"""
    return {
        "stage": "discover_character_candidates",
        "stage_key": "screenplay_character_discovery",
        "substage": "structural_coverage",
        "discovery_phase": "coverage",
        "episode_no": episode_no,
        "contract_version": STRUCTURAL_IDENTITY_COVERAGE_VERSION,
        "schema_hash": evidence_repository.content_hash(coverage_schema),
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
    }


async def _request_structural_coverage_decisions(
    prompt: str,
    *,
    episode_no: int,
    coverage_schema: dict,
    coverage_response_format: dict,
    coverage_group_keys: list[str],
    catalog: IdentityAuthorityCatalog,
    index: StructuralCoverageIndex,
    decision_by_id: dict[str, dict],
    coverage_decision_projection: list[dict],
    coverage_evidence_projection: list[dict],
) -> StructuralIdentityCoverageResponse:
    """向 provider 发出结构化覆盖审计请求，语义校验用 validate_structural_coverage_response。"""
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
        return _validate_structural_coverage_response(
            value,
            coverage_group_keys=coverage_group_keys,
            catalog=catalog,
            index=index,
            decision_by_id=decision_by_id,
        )

    call_meta = _coverage_completion_call_meta(
        episode_no, coverage_schema, coverage_decision_projection,
        coverage_evidence_projection, coverage_provider, coverage_model,
        coverage_effective_max, coverage_semantic_settings,
    )
    return await _identity_structured_with_resample(
        [{"role": "user", "content": prompt}],
        model_type=StructuralIdentityCoverageResponse,
        validate=validate_response,
        operation_id_for_attempt=lambda resample_attempt: (
            _coverage_completion_operation_id(
                resample_attempt,
                episode_no=episode_no,
                prompt=prompt,
                coverage_schema=coverage_schema,
                coverage_response_format=coverage_response_format,
                coverage_provider=coverage_provider,
                coverage_model=coverage_model,
                coverage_effective_max=coverage_effective_max,
                coverage_semantic_settings=coverage_semantic_settings,
            )
        ),
        max_tokens=IDENTITY_REQUEST_MAX_TOKENS,
        temperature=0.05,
        format_retry_limit=0,
        semantic_retry_limit=0,
        call_meta=call_meta,
        output_schema=coverage_schema,
        response_format=coverage_response_format,
        require_response_format=True,
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

    source_by_id, source_order, minimal, allowed_source_labels = (
        _index_structural_evidence(evidence, source_text)
    )

    authority_by_id = _seed_authority_catalog_from_bible(bible)
    groups_by_ref: dict[str, dict] = {}
    catalog_candidates = _catalog_candidates_from_resolutions(
        candidates, existing_resolutions,
        episode_no=episode_no, source_text=source_text,
    )
    _absorb_catalog_candidates(
        catalog_candidates,
        authority_by_id=authority_by_id,
        groups_by_ref=groups_by_ref,
        source_by_id=source_by_id,
    )
    seed_group_by_label = _seed_group_refs_for_labels(
        allowed_source_labels, minimal, groups_by_ref
    )
    _raise_if_groups_have_conflicting_authorities(groups_by_ref)

    structural_by_key = _owned_structural_by_label(minimal, allowed_source_labels)
    _raise_if_onscreen_label_lacks_materializable_authority(
        structural_by_key, authority_by_id
    )

    coverage_groups = _build_coverage_groups(
        allowed_source_labels, structural_by_key, source_order, seed_group_by_label
    )
    coverage_group_by_key = {
        str(group["group_key"]): group for group in coverage_groups
    }
    evidence_by_id, evidence_ids_by_group = _build_evidence_catalog(
        coverage_groups, source_by_id
    )

    catalog = IdentityAuthorityCatalog(
        authority_by_id=authority_by_id, groups_by_ref=groups_by_ref
    )
    index = StructuralCoverageIndex(
        source_by_id=source_by_id,
        structural_by_key=structural_by_key,
        coverage_group_by_key=coverage_group_by_key,
        evidence_by_id=evidence_by_id,
        evidence_ids_by_group=evidence_ids_by_group,
    )
    decision_by_id, decision_ids_by_group = _build_decision_catalog(
        coverage_groups, catalog=catalog, index=index
    )

    coverage_group_keys = [str(group["group_key"]) for group in coverage_groups]
    coverage_schema = _structural_identity_coverage_schema(
        coverage_group_keys,
        decision_ids_by_group=decision_ids_by_group,
    )
    coverage_response_format = _structural_identity_coverage_response_format(
        coverage_schema
    )
    (
        coverage_group_projection,
        coverage_evidence_projection,
        coverage_decision_projection,
    ) = _structural_coverage_projections(coverage_groups, evidence_by_id, decision_by_id)

    receipt_hashes = _structural_coverage_receipt_hashes(
        authority_by_id, groups_by_ref,
        coverage_decision_projection, coverage_evidence_projection,
    )
    if catalog_receipt is not None:
        catalog_receipt.clear()
        catalog_receipt.update({
            "version": _STRUCTURAL_IDENTITY_CATALOG_RECEIPT_VERSION,
            **receipt_hashes,
            "hash": evidence_repository.content_hash(receipt_hashes),
        })

    prompt = _structural_coverage_prompt(
        coverage_group_projection, coverage_evidence_projection,
        coverage_decision_projection, coverage_schema,
    )
    response = await _request_structural_coverage_decisions(
        prompt,
        episode_no=episode_no,
        coverage_schema=coverage_schema,
        coverage_response_format=coverage_response_format,
        coverage_group_keys=coverage_group_keys,
        catalog=catalog,
        index=index,
        decision_by_id=decision_by_id,
        coverage_decision_projection=coverage_decision_projection,
        coverage_evidence_projection=coverage_evidence_projection,
    )

    selected_decisions = _selected_decisions_from_response(
        response, coverage_group_keys, decision_by_id
    )
    additions = _build_candidate_additions(
        selected_decisions, candidates,
        structural_by_key=structural_by_key,
        authority_by_id=authority_by_id,
        evidence_by_id=evidence_by_id,
        source_by_id=source_by_id,
        source_order=source_order,
    )
    return _attach_candidate_source_evidence([*candidates, *additions], source_text)
