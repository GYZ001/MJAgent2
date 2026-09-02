"""角色候选发现的历史（legacy）主流程：整合证据目录、发起结构化调用、
记录 visual entity 合并，是 discover_character_candidates 的前置实现。
"""

from __future__ import annotations

import json
import sqlite3

from app.evidence import repository as evidence_repository
from app import hiagent
from app.character_policy import resolution_declares_functional_identity
from app.db import get_conn, new_id, now
from app.errors import ContentGenerationError
from app.harness import model_gateway
from app.identity_authority import identity_authority_registry, identity_resolution_is_authoritative
from app.schemas import Bible, extract_json

from .constants import (
    CAST_DISCOVERY_SOURCE_BUDGET,
    CURRENT_IDENTITY_DECISION_VERSION,
    CURRENT_IDENTITY_SYNTHETIC_PROVENANCE,
    IDENTITY_DISCOVERY_CONTRACT_VERSION,
    IDENTITY_REQUEST_MAX_TOKENS,
)
from .discovery_fragments import (
    _aligned_identity_source_label,
    _future_identity_context,
)
from .discovery_resample import (
    _identity_operation_retry_epoch,
    _identity_structured_with_resample,
)
from .evidence_catalog import (
    _current_identity_evidence_batches,
    _current_identity_known_decision_catalog,
    _current_identity_prior_decision_catalog,
)
from .current_identity_prompt import _current_identity_prompt
from .evidence_merge import (
    _current_identity_durable_signature,
    _current_identity_is_schema_violation,
    _merge_current_identity_occurrences,
    _normalize_current_identity_payload,
)
from . import identity_investigation
from .identity_response_projection import _project_current_identity_response
from .identity_schemas import CurrentIdentityCandidateResponse

def _current_identity_projection_errors(candidates: list[dict]) -> list[str]:
    """Reject cross-batch projection conflicts instead of last/first wins.

    独立于单批内 by_label 判定的一道跨批一致性检查（ERR-20260824-407c9b/
    b16bb4）：键同样是 (source_label, scope_qualifier) 复合键，与单批内判定
    用同一把尺子；没有 scope_qualifier 的称谓按裸 label 生效。
    """
    by_label: dict[tuple[str, str], set[tuple[str, str, str]]] = {}
    for item in candidates:
        label = str(item.get("source_label") or "").strip()
        qualifier = str(item.get("scope_qualifier") or "").strip()
        by_label.setdefault((label, qualifier), set()).add((
            str(item.get("identity_group") or "").strip(),
            str(item.get("identity_kind") or "").strip(),
            str(item.get("name") or "").strip(),
        ))
    return [
        f"current 投影后同一 source_label 冲突：{label}"
        for (label, _qualifier), signatures in by_label.items()
        if not label or len(signatures) != 1
    ]


def _record_visual_entity_merge(
    conn,
    project_id: str,
    *,
    from_visual_entity_id: str,
    to_visual_entity_id: str,
    canonical_name: str,
    evidence_episode_no: int,
    merge_rule: str = "same_batch_k_absorption",
) -> bool:
    """落一条视觉实体折叠记账（``visual_entity_merges``，设计文档 §4.2）；表未
    迁移时静默跳过（折叠本身不受影响，只是记账可补齐）。优先复用 ``to`` 侧
    已有的 ready 定妆照。"""
    if not project_id or not from_visual_entity_id or not to_visual_entity_id:
        return False
    if from_visual_entity_id == to_visual_entity_id:
        return False
    selected_portrait_id = None
    if canonical_name:
        try:
            row = conn.execute(
                "SELECT id FROM character_portraits WHERE project_id=? "
                "AND character_name=? AND pack_status='ready' "
                "ORDER BY ep_start DESC LIMIT 1",
                (project_id, canonical_name),
            ).fetchone()
        except sqlite3.OperationalError:
            row = None
        if row is not None:
            selected_portrait_id = row["id"]
    try:
        conn.execute(
            "INSERT INTO visual_entity_merges (id, project_id, "
            "from_visual_entity_id, to_visual_entity_id, canonical_name, "
            "merge_rule, selected_portrait_id, evidence_episode_no, "
            "created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                new_id("visual_merge"),
                project_id,
                from_visual_entity_id,
                to_visual_entity_id,
                canonical_name,
                merge_rule,
                selected_portrait_id,
                evidence_episode_no,
                now(),
            ),
        )
        conn.commit()
    except sqlite3.OperationalError:
        return False
    return True


def _record_current_identity_absorbed_visual_merges(
    project_id: str | None,
    episode_no: int,
    candidates: list[dict],
) -> None:
    """遍历一批已通过校验的 current-identity 候选，落地同批折叠的视觉记账。
    核验已在 ``_project_current_identity_response`` 内完成，这里只做落库；
    只在整批通过后调用。``project_id`` 缺失时静默跳过，命名侧结果不受影响。
    """
    if not project_id:
        return
    conn = get_conn()
    for item in candidates:
        merges = item.get("_current_identity_absorbed_visual_merges") or []
        for merge in merges:
            _record_visual_entity_merge(
                conn,
                project_id,
                from_visual_entity_id=str(
                    merge.get("from_visual_entity_id") or ""
                ),
                to_visual_entity_id=str(
                    merge.get("to_visual_entity_id") or ""
                ),
                canonical_name=str(merge.get("canonical_name") or ""),
                evidence_episode_no=episode_no,
                merge_rule=str(
                    merge.get("merge_rule") or "same_batch_k_absorption"
                ),
            )


def _current_identity_operation_hash(
    *, episode_no: int, current_batch: int, gate_round: int, resample_attempt: int, prompt_text: str,
    evidence_catalog_hash: str, prior_decision_catalog_hash: str, provider: str, model: str,
    effective_max_tokens: int, semantic_settings: dict, schema: dict, response_format: dict,
) -> str:
    """operation_id 哈希输入；从 run_phase_b 抽出腾 function_lines 棘轮空间（该文件顶部 baseline 零余量）。"""
    payload = {
        "contract_version": IDENTITY_DISCOVERY_CONTRACT_VERSION, "current_identity_version": CURRENT_IDENTITY_DECISION_VERSION,
        "current_evidence_catalog_hash": evidence_catalog_hash, "prior_decision_catalog_hash": prior_decision_catalog_hash,
        "provider": provider, "model": model, "requested_max_tokens": 8192, "effective_max_tokens": effective_max_tokens,
        "temperature": 0.1, "provider_semantic_settings": semantic_settings, "retry_epoch": _identity_operation_retry_epoch(),
        "resample_attempt": resample_attempt, "gate_round": gate_round, "prompt": prompt_text,
        "schema": schema, "response_format": response_format,
    }
    return f"screenplay.identity.current.v6:{episode_no}:{current_batch}:{gate_round}:" + evidence_repository.content_hash(payload)


def _current_identity_call_meta(
    *, episode_no: int, current_batch: int, source_batches: int, gate_round: int, evidence_catalog_hash: str,
    known_decision_projection: list[dict], prior_decision_catalog_hash: str, schema: dict,
    provider: str, model: str, effective_max_tokens: int, semantic_settings: dict,
) -> dict:
    """call_meta：同上，从 run_phase_b 抽出腾行，字段集与原来内联版本一致。"""
    return {
        "stage": "discover_character_candidates", "stage_key": "screenplay_character_discovery",
        "substage": "current_identity", "episode_no": episode_no, "discovery_phase": "current",
        "source_batch": current_batch, "source_batches": source_batches, "gate_round": gate_round,
        "reuse_successful_operation": False, "disable_provider_retries": True,
        "disable_provider_candidate_fallback": True, "disable_reasoning_fallback": True,
        "contract_version": IDENTITY_DISCOVERY_CONTRACT_VERSION, "current_identity_version": CURRENT_IDENTITY_DECISION_VERSION,
        "current_evidence_catalog_hash": evidence_catalog_hash,
        "current_decision_catalog_hash": evidence_repository.content_hash(known_decision_projection),
        "prior_decision_catalog_hash": prior_decision_catalog_hash,
        "schema_hash": evidence_repository.content_hash(schema), "provider": provider, "model": model,
        "effective_max_tokens": effective_max_tokens, "provider_semantic_settings": semantic_settings,
        "retry_epoch": _identity_operation_retry_epoch(),
    }


async def _discover_character_candidates_legacy(
    source_text: str,
    bible: Bible,
    episode_no: int,
    *,
    draft_text: str = "",
    future_text: str = "",
    future_label: str = "",
    existing_resolutions: list[dict] | None = None,
    project_id: str | None = None,
) -> list[dict]:
    """Resolve the current episode's cast before/after screenplay generation.

    后续章节只能用来把当前章节的“大汉/老者/黑衣人”解析成稳定真名，
    不得把尚未出场的人物或剧情带回本集。身份模型确认的稳定真名必须完成
    最小人物卡；未确认真名的一次性人物保留来源称谓并签发 typed functional identity。
    """
    known_names = [c.name for c in bible.characters if c.name]
    known = "、".join(known_names) or "（无）"
    existing_functional_routes = {
        str(item.get("canonical_name") or "").strip()
        for item in (existing_resolutions or [])
        if (
            isinstance(item, dict)
            and identity_resolution_is_authoritative(item)
            and resolution_declares_functional_identity(item)
            and str(item.get("canonical_name") or "").strip()
        )
    }
    existing_resolution_projection = [
        {
            "source_label": str(item.get("source_label") or "").strip(),
            "canonical_name": str(item.get("canonical_name") or "").strip(),
        }
        for item in (existing_resolutions or [])
        if (
            isinstance(item, dict)
            and identity_resolution_is_authoritative(item)
            and resolution_declares_functional_identity(item)
            and str(item.get("source_label") or "").strip()
            and str(item.get("canonical_name") or "").strip()
        )
    ]
    # 同批折叠通道（absorbed_functional_keys）第三类可吸收来源——"本集已有
    # 功能身份决议"——的 canonical_name -> source_label 反查表：
    # existing_functional_routes（下方）只是扁平 canonical_name 集合，供
    # 成员关系核验；这里额外保留 source_label，供折叠命中后计算被吸收组的
    # visual_entity_id（见 _project_current_identity_response 的 k 循环）。
    existing_functional_route_labels = {
        item["canonical_name"]: item["source_label"]
        for item in existing_resolution_projection
        if item["canonical_name"] and item["source_label"]
    }
    current_authorities = identity_authority_registry(
        bible,
        existing_resolutions or [],
    )
    reserved_authority_labels = {
        str(label).strip()
        for authority in current_authorities
        if str(authority.get("identity_kind") or "").strip() != "functional"
        for label in (
            authority.get("canonical_name"),
            *(authority.get("source_labels") or []),
        )
        if str(label or "").strip()
    }
    current_haystack = f"{source_text or ''}\n{draft_text or ''}"
    current_evidence_batches = _current_identity_evidence_batches(
        source_text,
        draft_text=draft_text,
    )
    seen: set[tuple[str, str, str]] = set()
    candidates: list[dict] = []
    current_provider, current_model, current_effective_max = (
        hiagent.text_request_token_limits(
            requested_max_tokens=IDENTITY_REQUEST_MAX_TOKENS,
        )
    )
    current_semantic_settings = hiagent.text_request_semantic_settings(
        current_provider
    )

    def collect(
        raw: str,
        *,
        identity_haystack: str,
        group_scope: str,
    ) -> None:
        try:
            obj = extract_json(raw, repair_unescaped_inner_quotes=True)
        except ValueError as exc:
            raise ContentGenerationError(
                "人物身份模型返回了不可验证的非结构化结果，当前阶段已停止"
            ) from exc
        for item in obj.get("characters") or []:
            if not isinstance(item, dict):
                continue
            # 兼容旧模型形状 {name, kind, evidence}。新协议的身份判断完全以模型输出为准。
            legacy_name = str(item.get("name") or "").strip()
            model_source_label = str(
                item.get("source_label") or legacy_name
            ).strip()
            source_label = _aligned_identity_source_label(
                model_source_label,
                current_haystack,
            )
            identity_kind = str(item.get("identity_kind") or "named").strip().lower()
            canonical_name = str(item.get("canonical_name") or legacy_name).strip()
            if identity_kind not in {"named", "functional"}:
                continue
            future_evidence = str(
                item.get("future_evidence") or ""
            ).strip()
            if (
                group_scope in {"future", "coverage"}
                and canonical_name
                and future_evidence
                and canonical_name in known_names
            ):
                # Only an existing canonical Bible identity can repair provider
                # enum drift. Repeating a relation/description in source text
                # proves label presence, not a stable named identity.
                identity_kind = "named"
            elif identity_kind == "functional":
                canonical_name = ""
            dedupe_key = (source_label, canonical_name, identity_kind)
            if (
                not source_label
                or len(source_label) > 16
                or dedupe_key in seen
                or (
                    identity_kind == "named"
                    and (
                        not canonical_name
                        or len(canonical_name) > 16
                        or (
                            canonical_name not in identity_haystack
                            and canonical_name not in known_names
                        )
                    )
                )
            ):
                continue
            seen.add(dedupe_key)
            functional_identity_key = str(
                item.get("functional_identity_key") or ""
            ).strip()[:64]
            existing_route_name = (
                functional_identity_key
                if functional_identity_key in existing_functional_routes
                else ""
            )
            prior_groups = {
                str(candidate.get("identity_group") or "").strip()
                for candidate in candidates
                if (
                    str(candidate.get("source_label") or "").strip()
                    == source_label
                    and str(candidate.get("identity_group") or "").strip()
                )
            }
            declared_group = str(
                item.get("identity_group")
                or functional_identity_key
                or ""
            ).strip()
            existing_groups = {
                str(candidate.get("identity_group") or "").strip()
                for candidate in candidates
                if str(candidate.get("identity_group") or "").strip()
            }
            declared_matches = {
                group
                for group in existing_groups
                if (
                    declared_group
                    and (
                        group == declared_group
                        or group.endswith(f":{declared_group}")
                    )
                )
            }
            identity_group = (
                next(iter(prior_groups))
                if group_scope in {"future", "coverage"} and len(prior_groups) == 1
                else (
                    next(iter(declared_matches))
                    if (
                        group_scope in {"future", "coverage"}
                        and len(declared_matches) == 1
                    )
                    else (
                        f"existing:{existing_route_name}"
                        if existing_route_name
                        else f"{group_scope}:{declared_group or source_label}"
                    )
                )
            )
            candidates.append({
                "name": canonical_name or source_label,
                "source_label": source_label,
                "identity_kind": identity_kind,
                "identity_group": identity_group,
                "existing_route_name": existing_route_name,
                "kind": "mentioned" if item.get("kind") == "mentioned" else "onscreen",
                "evidence": str(item.get("evidence") or "").strip()[:80],
                "future_evidence": future_evidence[:120],
                "source_segment_id": str(
                    item.get("source_segment_id") or ""
                ).strip(),
                "source_quote": str(item.get("source_quote") or "").strip()[:240],
                "model_source_label": (
                    model_source_label
                    if model_source_label != source_label else ""
                ),
            })

    for current_batch, evidence_records in enumerate(
        current_evidence_batches, start=1
    ):
        evidence_by_ref = {
            f"E{index:03d}": record
            for index, record in enumerate(evidence_records, start=1)
        }
        evidence_catalog = [
            {
                "evidence_ref": evidence_ref,
                "origin": str(record.get("origin") or ""),
                "source_segment_id": str(
                    record.get("source_segment_id") or ""
                ),
                "path": str(record.get("path") or ""),
                "text": str(record.get("text") or ""),
            }
            for evidence_ref, record in evidence_by_ref.items()
        ]
        registered_decisions = _current_identity_known_decision_catalog(
            evidence_by_ref,
            authorities=current_authorities,
        )
        prior_named_decisions, prior_functional_groups = (
            _current_identity_prior_decision_catalog(
                evidence_by_ref,
                prior_candidates=candidates,
            )
        )
        known_decisions = {
            **registered_decisions,
            **prior_named_decisions,
        }
        known_decision_projection = [
            {
                "decision_id": decision_id,
                "decision_type": str(item.get("decision_type") or ""),
                "evidence_ref": str(item.get("evidence_ref") or ""),
                "source_label": str(item.get("source_label") or ""),
                "canonical_name": str(item.get("canonical_name") or ""),
                "allowed_kinds": (
                    ["onscreen", "mentioned"]
                    if item.get("materialization_compatible")
                    else ["mentioned"]
                ),
            }
            for decision_id, item in sorted(known_decisions.items())
        ]
        prior_functional_projection = [
            {
                "decision_id": decision_id,
                "source_labels": list(item.get("source_labels") or []),
                "existing_route_name": str(
                    item.get("existing_route_name") or ""
                ),
            }
            for decision_id, item in sorted(prior_functional_groups.items())
        ]
        prior_decision_catalog_hash = evidence_repository.content_hash({
            "named": [
                item for item in known_decision_projection
                if str(item.get("decision_type") or "").startswith("prior_")
            ],
            "functional": prior_functional_projection,
        })
        evidence_catalog_hash = evidence_repository.content_hash({
            "contract_version": CURRENT_IDENTITY_DECISION_VERSION,
            "evidence": evidence_records,
            "known_decisions": known_decision_projection,
            "prior_functional_groups": prior_functional_projection,
        })
        current_schema, current_response_format, prompt = _current_identity_prompt(
            episode_no=episode_no,
            known=known,
            prior_functional_projection=prior_functional_projection,
            evidence_catalog=evidence_catalog,
            known_decision_projection=known_decision_projection,
            existing_resolution_projection=existing_resolution_projection,
            evidence_refs=list(evidence_by_ref),
            known_decision_ids=list(known_decisions),
        )

        # Phase B：严格作答 + 业务校验。真正的语义违规（如 source_label 重复）
        # 由 validate_current_response 直接反馈给 chat_structured，后者在
        # strict_identity_substage 下对非空 validate() 返回值立即
        # raise StructuredSemanticError，不给这里任何拦截机会——这是刻意的
        # fail-once 设计（discovery_resample.py 顶部原文：不得把模型自己的
        # 错误答案回显给它换取"改对"，那是在教它伪造合规），本次调查回喂循环
        # 不改这一条，异常原样向上抛出、不重试。下面这条 captured_errors 只用
        # 于 validate() 判定通过之后的复核：命中的只可能是 wire-schema 已声明
        # 的越界（人为的第二次分类判据完全一致，避免重算一遍
        # _project_current_identity_response）。
        async def run_phase_b(
            prompt_text: str, gate_round: int,
        ) -> tuple[list[dict], list[str]]:
            captured_candidates: list[dict] = []
            captured_errors: list[str] = []

            def validate_current_response(
                value: CurrentIdentityCandidateResponse,
            ) -> list[str]:
                projected, errors = _project_current_identity_response(
                    value,
                    evidence_by_ref=evidence_by_ref,
                    known_decisions=known_decisions,
                    prior_functional_groups=prior_functional_groups,
                    reserved_authority_labels=reserved_authority_labels,
                    group_scope=f"current-{current_batch}",
                    existing_functional_routes=existing_functional_routes,
                    existing_functional_route_labels=existing_functional_route_labels,
                )
                captured_candidates[:] = projected
                captured_errors[:] = errors
                return [
                    error for error in errors
                    if not _current_identity_is_schema_violation(error)
                ]

            await _identity_structured_with_resample(
                [{"role": "user", "content": prompt_text}],
                model_type=CurrentIdentityCandidateResponse,
                validate=validate_current_response,
                normalize_payload=_normalize_current_identity_payload,
                operation_id_for_attempt=lambda resample_attempt: (
                    _current_identity_operation_hash(
                        episode_no=episode_no,
                        current_batch=current_batch,
                        gate_round=gate_round,
                        resample_attempt=resample_attempt,
                        prompt_text=prompt_text,
                        evidence_catalog_hash=evidence_catalog_hash,
                        prior_decision_catalog_hash=prior_decision_catalog_hash,
                        provider=current_provider,
                        model=current_model,
                        effective_max_tokens=current_effective_max,
                        semantic_settings=current_semantic_settings,
                        schema=current_schema,
                        response_format=current_response_format,
                    )
                ),
                temperature=0.1,
                max_tokens=IDENTITY_REQUEST_MAX_TOKENS,
                format_retry_limit=0,
                semantic_retry_limit=0,
                call_meta=_current_identity_call_meta(
                    episode_no=episode_no,
                    current_batch=current_batch,
                    source_batches=len(current_evidence_batches),
                    gate_round=gate_round,
                    evidence_catalog_hash=evidence_catalog_hash,
                    known_decision_projection=known_decision_projection,
                    prior_decision_catalog_hash=prior_decision_catalog_hash,
                    schema=current_schema,
                    provider=current_provider,
                    model=current_model,
                    effective_max_tokens=current_effective_max,
                    semantic_settings=current_semantic_settings,
                ),
                output_schema=current_schema,
                response_format=current_response_format,
                require_response_format=True,
            )
            if not captured_errors:
                return captured_candidates, []
            # validate_current_response 判定通过（否则上面已经在
            # StructuredSemanticError 里向上抛出，见本函数前的注释），走到这里
            # 说明 captured_errors 只剩 wire-schema 已声明的越界。供应商对深层
            # 数组 enum 的严格模式偶发失效（RCA ERR-20260824-e3628f，约 0.5%
            # 采样缺陷），改判为格式失败：不在本次调用内重采样，也不进入调查
            # 回喂循环，交给 scripts/yyft_serial10.py 的瞬时族分诊，本集 60s
            # 后重发。
            raise model_gateway.StructuredFormatError("；".join(captured_errors))

        candidates.extend(
            await identity_investigation.resolve_with_investigation_gate(
                prompt,
                context=identity_investigation.InvestigationContext(
                    episode_no=episode_no,
                    current_batch=current_batch,
                    current_haystack=current_haystack,
                    future_text=future_text,
                    future_label=future_label,
                    bible=bible,
                    current_authorities=current_authorities,
                    known_decision_projection=known_decision_projection,
                    prior_functional_projection=prior_functional_projection,
                    existing_resolution_projection=existing_resolution_projection,
                ),
                phase_b=run_phase_b,
            )
        )

    if projection_errors := _current_identity_projection_errors(candidates):
        raise ContentGenerationError("；".join(projection_errors))

    _record_current_identity_absorbed_visual_merges(
        project_id, episode_no, candidates
    )

    unresolved_onscreen_groups = {
        str(item.get("identity_group") or "").strip()
        for item in candidates
        if (
            item.get("identity_kind") == "functional"
            and item.get("source_label_provenance")
            != CURRENT_IDENTITY_SYNTHETIC_PROVENANCE
            and item.get("kind") == "onscreen"
            and str(item.get("identity_group") or "").strip()
        )
    }
    future_candidates = [
        item
        for item in candidates
        if (
            item.get("identity_kind") == "functional"
            and item.get("source_label_provenance")
            != CURRENT_IDENTITY_SYNTHETIC_PROVENANCE
            and (
                item.get("kind") == "onscreen"
                or str(item.get("identity_group") or "").strip()
                in unresolved_onscreen_groups
                or str(item.get("source_label") or "").strip()
                in future_text
            )
        )
    ]
    future_context = _future_identity_context(
        future_text,
        [item["source_label"] for item in future_candidates],
        known_names=known_names,
        current_text=source_text,
    )
    if future_context:
        current_identity_audit_source = str(source_text or "").strip()
        if len(current_identity_audit_source) > CAST_DISCOVERY_SOURCE_BUDGET:
            half = CAST_DISCOVERY_SOURCE_BUDGET // 2
            current_identity_audit_source = (
                current_identity_audit_source[:half]
                + "\n……（身份覆盖复核中段省略）……\n"
                + current_identity_audit_source[-half:]
            )
        future_prompt = f"""任务：只为当前集已经发现的人物称谓做后续姓名消歧。

当前人物谱已有角色：
{known}

当前集尚未确认真名的候选：
{json.dumps(future_candidates, ensure_ascii=False, separators=(',', ':'))}

当前集原文（同时复核第一遍是否漏掉独立出场/开口的实体）：
{current_identity_audit_source}

后续章节中命中这些称谓的局部窗口（{future_label or '后续章节'}）：
{future_context}

规则：
1. 优先输出当前集候选中 source_label 完全相同的项目；若第一遍遗漏了当前原文中可区分、
   独立出场或开口的实体，也必须补充输出，source_label 使用当前原文逐字称谓。
   称谓可以在章节边界发生变化；
   若当前集离场状态、后续开场承接和人物谱真名窗口共同形成唯一同一性证据，可据此确认，
   不要求旧称谓与真名必须出现在同一句。
2. canonical_name 必须出现在后续窗口或当前人物谱中；稳定唯一的法号、尊号、专属称号
   也属于 named identity，不要求必须是户籍式真名；有歧义就不输出。
3. 不得新增只在后续章节出场的人，不得复述与身份无关的剧情。

只输出 JSON：
{{"characters": [{{"source_label": "当前称谓", "canonical_name": "稳定真名", "identity_kind": "named", "kind": "onscreen|mentioned", "evidence": "本集身份依据", "future_evidence": "同一性依据"}}]}}"""
        future_raw = await model_gateway.chat(
            [{"role": "user", "content": future_prompt}],
            temperature=0.1,
            max_tokens=IDENTITY_REQUEST_MAX_TOKENS,
            call_meta={
                "stage": "discover_character_candidates",
                "episode_no": episode_no,
                "discovery_phase": "future_identity",
                "reuse_successful_operation": True,
            },
        )
        collect(
            future_raw,
            identity_haystack=f"{current_haystack}\n{future_context}",
            group_scope="future",
        )
        if len(current_identity_audit_source) >= 1000:
            coverage_prompt = f"""任务：独立审计当前集人物身份覆盖，只找第一遍遗漏或错误降级的实体。

当前人物谱已有角色：
{known}

当前集原文：
{current_identity_audit_source}

前两遍候选：
{json.dumps(candidates, ensure_ascii=False, separators=(',', ':'))}

后续姓名证据：
{future_context}

规则：
1. 逐段核对每个独立行动、开口或具有可区分外观的实体；集合称谓不能替代其中的独立人物。
2. 只输出遗漏实体，或已有候选中能由后续证据唯一升级为 named identity 的实体。
3. source_label 必须逐字来自当前集原文。canonical_name 必须有当前原文、人物谱或后续窗口证据。
4. 不得按职业、年龄、服饰、称号词表判断人物是否重要；无法唯一确认就不输出。

只输出 JSON：
{{"characters": [{{"source_label": "当前原文逐字称谓", "canonical_name": "稳定真名或专属称号", "identity_kind": "named|functional", "identity_group": "若与已有候选同一实体则精确复用其 identity_group，否则空串", "functional_identity_key": "同一实体分组或空串", "kind": "onscreen|mentioned", "evidence": "当前依据", "future_evidence": "后续同一性依据"}}]}}"""
            coverage_raw = await model_gateway.chat(
                [{"role": "user", "content": coverage_prompt}],
                temperature=0.05,
                max_tokens=IDENTITY_REQUEST_MAX_TOKENS,
                call_meta={
                    "stage": "discover_character_candidates",
                    "episode_no": episode_no,
                    "discovery_phase": "coverage_audit",
                    "reuse_successful_operation": True,
                },
            )
            collect(
                coverage_raw,
                identity_haystack=f"{current_haystack}\n{future_context}",
                group_scope="coverage",
            )

    # 同一称谓在不同后文批次中可能先被保守判为 functional，后被真名证据命中。
    # 具名证据唯一时优先；出现两个不同真名时不猜，降级为一次性角色。
    def strongest_occurrence(options: list[dict]) -> dict:
        """Preserve an onscreen owned receipt independent of batch order."""
        signatures = {
            _current_identity_durable_signature(item) for item in options
        }
        if len(signatures) == 1 and not any(
            item.get("source_label_provenance")
            == CURRENT_IDENTITY_SYNTHETIC_PROVENANCE
            for item in options
        ) and all(
            isinstance(item.get("source_evidence_receipt"), dict)
            and isinstance(item.get("source_evidence_receipts"), list)
            and bool(item.get("source_evidence_receipts"))
            for item in options
        ):
            return _merge_current_identity_occurrences(options)
        return next(
            (item for item in options if item.get("kind") == "onscreen"),
            options[0],
        )

    resolved: list[dict] = []
    # 跨 collect() 批次（current/future/coverage）的最终折叠，键同样是
    # (source_label, scope_qualifier) 复合键，与前两道判定用同一把尺子
    # （ERR-20260824-614276：裸 source_label 会把两个合法的不同人拍扁成一条）。
    for group_key in dict.fromkeys(
        (item["source_label"], str(item.get("scope_qualifier") or "").strip())
        for item in candidates
    ):
        source_label, qualifier = group_key
        options = [
            item for item in candidates
            if item["source_label"] == source_label
            and str(item.get("scope_qualifier") or "").strip() == qualifier
        ]
        named_options_by_name: dict[str, list[dict]] = {}
        for item in options:
            if item["identity_kind"] == "named":
                named_options_by_name.setdefault(item["name"], []).append(item)
        named_by_name = {
            name: strongest_occurrence(named_options)
            for name, named_options in named_options_by_name.items()
        }
        if len(named_by_name) == 1:
            resolved.append(next(iter(named_by_name.values())))
        elif len(named_by_name) > 1:
            functional_options = [
                item for item in options
                if item["identity_kind"] == "functional"
            ]
            functional = (
                strongest_occurrence(functional_options)
                if functional_options else None
            )
            resolved.append(functional or {
                "name": source_label,
                "source_label": source_label,
                "identity_kind": "functional",
                "identity_group": f"conflict:{source_label}",
                "existing_route_name": "",
                "kind": "onscreen",
                "evidence": "多批次身份线索冲突，不猜真名",
                "future_evidence": "",
            })
        else:
            resolved.append(strongest_occurrence(options))

    named_by_group: dict[str, set[str]] = {}
    named_evidence: dict[tuple[str, str], dict] = {}
    for item in resolved:
        if item.get("identity_kind") != "named":
            continue
        group = str(item.get("identity_group") or "").strip()
        name = str(item.get("name") or "").strip()
        if group and name:
            named_by_group.setdefault(group, set()).add(name)
            named_evidence[(group, name)] = item
    upgraded: list[dict] = []
    for item in resolved:
        group = str(item.get("identity_group") or "").strip()
        names = named_by_group.get(group, set())
        if (
            item.get("identity_kind") == "functional"
            and item.get("source_label_provenance")
            != CURRENT_IDENTITY_SYNTHETIC_PROVENANCE
            and len(names) == 1
        ):
            canonical_name = next(iter(names))
            evidence = named_evidence[(group, canonical_name)]
            upgraded.append({
                **item,
                "name": canonical_name,
                "identity_kind": "named",
                "future_evidence": str(
                    evidence.get("future_evidence") or ""
                ),
            })
        else:
            upgraded.append(item)
    resolved = upgraded

    # 按本集第一次出现排序，保证后续“路人甲/乙/丙/丁”分配不受模型输出顺序影响。
    return sorted(
        resolved,
        key=lambda item: current_haystack.find(item["source_label"]),
    )

