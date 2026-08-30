"""未来章节身份候选的解析：resolve_future_identity_candidates 编排入口。

按集判定待揭示身份是否已在未来文本中出现。原来是一条 876 代码行的单函数
（待决候选筛选 -> 权威目录 -> 分组 -> 证据检索 -> 决议铸造 -> Prompt 组装 ->
结构化调用 -> 结果合并，全部内联在一个函数体里）。按真实阶段边界拆成兄弟
模块，本文件只做编排：

* ``future_identity_groups.py`` —— 待决候选筛选、按 (source_label,
  scope_qualifier) 复合键分组。
* ``future_identity_authorities.py`` —— 已有人物权威目录、本集已具名权威
  集合。
* ``future_identity_evidence.py`` —— 按分组切分未来原文证据窗口。
* ``future_identity_decisions.py`` —— 铸造每个分组的 F:/K:/N: 决议目录。
* ``future_identity_prompt.py`` —— Schema 与消歧 Prompt 构造。
* ``future_identity_response_contract.py`` —— 结构化响应的归一改写
  （``normalize_payload``）与硬校验（``validate``）回调，外加两者共享的只读
  上下文 ``_FutureIdentityResolutionContext``。
* ``future_identity_response_decisions.py`` —— 把模型选择的 decision_id 投影
  回按 identity_group 的结果。
* ``future_identity_merge.py`` —— 把按组结果合并回原始候选列表。

拆分时的闭包/绑定处理：原函数的三个回调闭包（``normalize_identity_payload``/
``validate_response``/``response_decisions``）隐式捕获了 ``group_keys``/
``decision_by_id``/``authority_by_id``/``evidence_by_id``/
``evidence_ids_by_group``/``named_authorities_by_identity_group``/
``group_specs``/``future_text`` 共八个只读局部量。提升为顶层函数后统一打包进
``_FutureIdentityResolutionContext`` 按值传递。其中 ``decision_by_id`` 是唯一
会被写入的一个——``normalize_identity_payload`` 会原地新增归一决议
（``decision_by_id[reissue_id] = {...}``，字典项赋值而非重新绑定整个字典），
调用方持有的是同一个对象，后续 ``validate_response``/``response_decisions``
读到的是同一份已更新的目录，与原来的闭包捕获语义完全一致。
"""
from __future__ import annotations

import json

from app import hiagent
from app.evidence import repository as evidence_repository
from app.schemas import Bible

from .constants import FUTURE_IDENTITY_DECISION_VERSION, IDENTITY_REQUEST_MAX_TOKENS
from .discovery_resample import _identity_operation_retry_epoch, _identity_structured_with_resample
from .future_identity_authorities import (
    _future_identity_authority_by_id,
    _future_identity_known_names,
    _future_identity_named_authority_context,
)
from .future_identity_decisions import _future_identity_decision_catalog
from .future_identity_evidence import _future_identity_evidence_windows
from .future_identity_groups import (
    _future_identity_group_specs,
    _future_identity_unresolved_candidates,
)
from .future_identity_merge import _merge_future_identity_resolution
from .future_identity_prompt import _future_identity_prompt
from .future_identity_response_contract import (
    _FutureIdentityResolutionContext,
    _normalize_future_identity_payload,
    _validate_future_identity_response,
)
from .future_identity_response_decisions import _future_identity_response_decisions
from .identity_schemas import FutureIdentityCandidateResponse


async def resolve_future_identity_candidates(
    candidates: list[dict],
    *,
    source_text: str,
    future_text: str,
    bible: Bible,
    episode_no: int,
    future_label: str = "",
) -> list[dict]:
    """Resolve current unresolved identity groups from bounded future evidence.

    The provider never copies a label, authority, group or evidence quote on
    this wire.  It selects one backend-owned decision token per exact group
    key.  Only a genuinely new name remains open text, and that name must be
    anchored verbatim in one backend-owned raw-future evidence span.
    """
    unresolved = _future_identity_unresolved_candidates(
        candidates, future_text=future_text,
    )
    if not unresolved or not str(future_text or "").strip():
        return candidates

    known_names = _future_identity_known_names(bible)
    authority_by_id, authority_projection = _future_identity_authority_by_id(
        candidates, known_names,
    )
    named_authorities_by_identity_group, episode_named_authorities = (
        _future_identity_named_authority_context(candidates)
    )
    group_specs, group_keys = _future_identity_group_specs(unresolved)
    evidence_by_id, evidence_ids_by_group, fallback_evidence_group_keys = (
        _future_identity_evidence_windows(
            group_specs, future_text=future_text, known_names=known_names,
        )
    )
    decision_by_id, decision_ids_by_group = _future_identity_decision_catalog(
        group_specs,
        fallback_evidence_group_keys=fallback_evidence_group_keys,
        authority_by_id=authority_by_id,
        named_authorities_by_identity_group=named_authorities_by_identity_group,
        episode_named_authorities=episode_named_authorities,
        evidence_ids_by_group=evidence_ids_by_group,
        evidence_by_id=evidence_by_id,
    )

    (
        identity_schema,
        identity_response_format,
        group_projection,
        decision_projection,
        evidence_projection,
        prompt,
    ) = _future_identity_prompt(
        group_specs,
        group_keys=group_keys,
        authority_projection=authority_projection,
        decision_by_id=decision_by_id,
        decision_ids_by_group=decision_ids_by_group,
        evidence_by_id=evidence_by_id,
        evidence_ids_by_group=evidence_ids_by_group,
        source_text=source_text,
        future_label=future_label,
    )

    context = _FutureIdentityResolutionContext(
        group_specs=group_specs,
        group_keys=group_keys,
        decision_by_id=decision_by_id,
        evidence_by_id=evidence_by_id,
        evidence_ids_by_group=evidence_ids_by_group,
        authority_by_id=authority_by_id,
        named_authorities_by_identity_group=named_authorities_by_identity_group,
        future_text=future_text,
    )

    identity_provider, identity_model, identity_effective_max = (
        hiagent.text_request_token_limits(
            requested_max_tokens=IDENTITY_REQUEST_MAX_TOKENS,
        )
    )
    identity_semantic_settings = hiagent.text_request_semantic_settings(
        identity_provider
    )
    operation_id = (
        "screenplay.identity.future.v11:"
        + evidence_repository.content_hash({
            "episode_no": episode_no,
            "provider": identity_provider,
            "model": identity_model,
            "requested_max_tokens": 4096,
            "effective_max_tokens": identity_effective_max,
            "temperature": 0.1,
            "provider_semantic_settings": identity_semantic_settings,
            "retry_epoch": _identity_operation_retry_epoch(),
            "messages": [{"role": "user", "content": prompt}],
            "output_schema": identity_schema,
            "response_format": identity_response_format,
            "contract_version": FUTURE_IDENTITY_DECISION_VERSION,
        })
    )

    response = await _identity_structured_with_resample(
        [{"role": "user", "content": prompt}],
        model_type=FutureIdentityCandidateResponse,
        validate=lambda value: _validate_future_identity_response(value, context),
        operation_id_for_attempt=lambda resample_attempt: (
            operation_id
            if not resample_attempt
            else f"{operation_id}:resample:{resample_attempt}"
        ),
        max_tokens=IDENTITY_REQUEST_MAX_TOKENS,
        temperature=0.1,
        format_retry_limit=0,
        semantic_retry_limit=0,
        call_meta={
            "contract_version": FUTURE_IDENTITY_DECISION_VERSION,
            "provider": identity_provider,
            "model": identity_model,
            "effective_max_tokens": identity_effective_max,
            "provider_semantic_settings": identity_semantic_settings,
            "retry_epoch": _identity_operation_retry_epoch(),
            "stage": "discover_character_candidates",
            "stage_key": "screenplay_character_discovery",
            "substage": "future_identity",
            "discovery_phase": "future_identity",
            "episode_no": episode_no,
            "reuse_successful_operation": False,
            "disable_provider_retries": True,
            "disable_provider_candidate_fallback": True,
            "disable_reasoning_fallback": True,
            "schema_hash": evidence_repository.content_hash(identity_schema),
            "decision_catalog_hash": evidence_repository.content_hash(
                decision_projection
            ),
            "evidence_catalog_hash": evidence_repository.content_hash(
                evidence_projection
            ),
        },
        repair_context=json.dumps(
            {
                "groups": group_projection,
                "decisions": decision_projection,
                "evidence": evidence_projection,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        output_schema=identity_schema,
        response_format=identity_response_format,
        require_response_format=True,
        normalize_payload=lambda payload: _normalize_future_identity_payload(payload, context),
    )

    # 真实第20轮 EP4 回归 ERR-20260824-407c9b 结构性排查命中：resolved_by_group
    # 直接按 identity_group（真正唯一键，见 response_decisions 上方注释）
    # 取解析结果，不再经过裸 source_label 的两跳字典推导式（原设计里两个
    # 不同的人共享同一个裸标签时，Python 字典推导式会静默用后者覆盖前者，
    # 两个人的解析结果被悄悄合并成一个——"外宗弟子"甲乙正是这个形状）。
    _decisions, resolved_by_group = _future_identity_response_decisions(response, context)
    return _merge_future_identity_resolution(candidates, resolved_by_group)
