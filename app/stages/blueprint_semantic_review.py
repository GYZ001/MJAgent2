"""叙事蓝图——语义双审裁决 _semantic_review_narrative_blueprint（单一巨函数，拆分说明见模块末尾）。"""
from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any


from app import hiagent
from app.db import get_conn, get_setting
from app.errors import ContentGenerationError
from app.harness import model_gateway
from app.narrative_blueprint import (
    BLUEPRINT_VERSION,
    BlueprintSemanticReview,
    NarrativeBlueprint,
    blueprint_authority_validator_fingerprint,
    blueprint_semantic_issue_is_resolved,
    blueprint_semantic_voice_issue_has_dialogue_authority,
    filter_blueprint_semantic_review_voice_issues,
    blueprint_semantic_review_schema,
    normalize_blueprint_semantic_review_payload,
    validate_blueprint_semantic_review,
)
from app.source_excerpt import (
    index_source_segments,
    render_indexed_source,
)
from app.source_facts import (
    source_facts,
)

from .blueprint_budget import _BlueprintGenerationBudget
from .blueprint_ownership_repair import (
    _blueprint_exact_ownership_claims,
    _repair_reviewed_blueprint_state_subject_ownership,
)
from .blueprint_prompt import (
    _blueprint_format_repair_reservation_operation_id,
    _blueprint_structured_operation_id,
)
from .blueprint_repair import _repair_narrative_blueprint
from .common import StageError
from .constants import (
    BLUEPRINT_REVIEW_FORMAT_RETRY_LIMIT,
    BLUEPRINT_REVIEW_MAX_TOKENS,
    BLUEPRINT_REVIEW_PROVIDER_RETRY_LIMIT,
    BLUEPRINT_REVIEW_SUPPLEMENTARY_SAMPLE,
    BLUEPRINT_SEMANTIC_REVIEW_POLICY_VERSION,
    SCREENPLAY_BLUEPRINT_PROMPT_VERSION,
    SYSTEM_PREFIX,
)
from .ir_snapshot import _artifact_json_content_is_sealed, _current_blueprint_authority_snapshot
from .screenplay_source import _render_screenplay_source


def _blueprint_semantic_issue_exact_scope(
    issue: Any,
) -> tuple[str, tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """Return the exact scope used to bind a review to local authority."""
    return (
        str(issue.code),
        tuple(sorted(str(key) for key in issue.node_keys)),
        tuple(sorted(str(key) for key in issue.source_segment_ids)),
        tuple(sorted(str(key) for key in issue.source_unit_keys)),
    )


def _blueprint_semantic_issue_has_deterministic_authority(
    issue: Any,
    blueprint: NarrativeBlueprint,
    source_text: str,
) -> bool:
    """Whether a typed one-sided finding has deterministic local authority.

    The shared validator accepts a reviewer sub-scope when every referenced
    node/source is covered by a server-derived delivery or state-subject issue.
    Its default ``True`` for ordinary craft findings is deliberately fenced
    out here. Environment misclassification is also excluded: that check only
    proves exact-unit scope, not the semantic identity of the state subject.
    """
    code = str(issue.code)
    if (
        code == "state_subject_environment_misclassified"
        or not code.startswith((
            "voice_identity_",
            "source_delivery_",
            "state_subject_",
        ))
    ):
        return False
    return blueprint_semantic_voice_issue_has_dialogue_authority(
        issue,
        blueprint,
        source_text,
    )


async def _semantic_review_narrative_blueprint(
    blueprint: NarrativeBlueprint,
    *,
    episode: dict[str, Any],
    source_text: str,
    generation_budget: _BlueprintGenerationBudget | None = None,
) -> NarrativeBlueprint:
    from app.evidence import repository as evidence_repository
    from app.harness.types import EvidenceArtifact
    from app.observability.tracing import current_trace

    def persist_reviewed_authority(
        *,
        parent_artifact_ids: list[str] | None = None,
    ) -> None:
        """Persist reviewed authority, then terminalize old unknown retries.

        The artifact commit deliberately happens first.  A crash between the
        two writes leaves the historical provider outcome unresolved (safe);
        the inverse state -- resolving without durable reviewed authority --
        is impossible.
        """
        episode_id = str(episode.get("id") or "")
        trace = current_trace()
        run_id = str(trace.run_id or "")
        if not episode_id or not run_id:
            return
        content = blueprint.model_dump(mode="json")
        content_digest = evidence_repository.content_hash(content)
        source_digest = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
        existing = get_conn().execute(
            """SELECT id FROM artifacts
                 WHERE type='screenplay_narrative_blueprint'
                   AND scope_type='episode' AND scope_id=?
                   AND status='validated' AND content_hash=?
                   AND contract_version=? AND prompt_version=?
                   AND json_extract(
                       model_snapshot_json,'$.generation_mode'
                   )='semantic_reviewed'
                   AND json_extract(
                       model_snapshot_json,'$.source_corpus_hash'
                   )=?
                   AND json_extract(
                       model_snapshot_json,'$.review_policy_version'
                   )=?
                 ORDER BY created_at DESC LIMIT 1""",
            (
                episode_id,
                content_digest,
                BLUEPRINT_VERSION,
                SCREENPLAY_BLUEPRINT_PROMPT_VERSION,
                source_digest,
                BLUEPRINT_SEMANTIC_REVIEW_POLICY_VERSION,
            ),
        ).fetchone()
        if existing is None:
            evidence_repository.create_artifact(
                EvidenceArtifact(
                    type="screenplay_narrative_blueprint",
                    scope_type="episode",
                    scope_id=episode_id,
                    status="validated",
                    trust_level="T1",
                    content=content,
                    parent_artifact_ids=list(parent_artifact_ids or []),
                    contract_version=BLUEPRINT_VERSION,
                    prompt_version=SCREENPLAY_BLUEPRINT_PROMPT_VERSION,
                    model_snapshot=_current_blueprint_authority_snapshot(
                        source_text,
                        generation_mode="semantic_reviewed",
                        generation_budget=generation_budget,
                    ),
                ),
                step_run_id=trace.step_run_id,
            )

        # Historical unknown provider outcomes are resolved only after this
        # reviewed artifact has been selected as current authority and written
        # into the active revision checkpoint by the downstream boundary.

    initial_blueprint_hash = hashlib.sha256(
        json.dumps(
            blueprint.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    review_source_corpus_hash = hashlib.sha256(
        source_text.encode("utf-8")
    ).hexdigest()
    review_input_fingerprint = hashlib.sha256(
        json.dumps(
            {
                "episode_id": str(episode.get("id") or ""),
                "blueprint_hash": initial_blueprint_hash,
                "source_corpus_hash": review_source_corpus_hash,
                "review_policy_version": BLUEPRINT_SEMANTIC_REVIEW_POLICY_VERSION,
                "authority_fingerprint": blueprint_authority_validator_fingerprint(),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    cached_rows = get_conn().execute(
        """SELECT id,content_json,content_hash,model_snapshot_json
             FROM artifacts
            WHERE scope_type='episode' AND scope_id=?
              AND type='screenplay_narrative_blueprint_review_consensus'
              AND status='validated'
              AND contract_version=? AND prompt_version=?
            ORDER BY created_at DESC LIMIT 20""",
        (
            str(episode.get("id") or ""),
            BLUEPRINT_VERSION,
            SCREENPLAY_BLUEPRINT_PROMPT_VERSION,
        ),
    ).fetchall()
    for row in cached_rows:
        try:
            cached = json.loads(row["content_json"] or "{}")
            if not _artifact_json_content_is_sealed(row, cached):
                continue
            cached_snapshot = json.loads(
                row["model_snapshot_json"] or "{}"
            )
            cached_authoritative_issue_count = int(
                cached.get("authoritative_issue_count")
            )
            cached_residual_issue_count = int(
                cached.get("non_authoritative_residual_issue_count")
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        cached_outcome = str(cached.get("review_outcome") or "")
        reusable_no_authority_outcome = bool(
            (
                cached_outcome == "clean"
                and cached_residual_issue_count == 0
            )
            or (
                cached_outcome
                == "non_authoritative_one_sided_residual"
                and cached.get("review_mode") == "full"
                and cached_residual_issue_count > 0
            )
        )
        if (
            cached.get("blueprint_hash") == initial_blueprint_hash
            and not cached.get("consensus_issue_keys")
            and not cached.get("deterministic_authority_issue_keys")
            and cached_authoritative_issue_count == 0
            and reusable_no_authority_outcome
            and cached_snapshot.get("review_policy_version")
            == BLUEPRINT_SEMANTIC_REVIEW_POLICY_VERSION
            and cached_snapshot.get("authority_fingerprint")
            == blueprint_authority_validator_fingerprint()
            and cached_snapshot.get("source_corpus_hash")
            == review_source_corpus_hash
            and cached_snapshot.get("review_input_fingerprint")
            == review_input_fingerprint
        ):
            persist_reviewed_authority(parent_artifact_ids=[str(row["id"])])
            return blueprint

    targeted_review = str(
        get_setting("screenplay_targeted_blueprint_review_enabled") or "true"
    ).strip().lower() not in {"0", "false", "off", "no"}

    def review_projection() -> tuple[dict[str, Any], str, list[str]]:
        nodes = blueprint.nodes
        risky: set[int] = set()
        for index, node in enumerate(nodes):
            previous = nodes[index - 1] if index else None
            if (
                node.time_relation not in {"episode_start", "continuous"}
                or (previous is not None and (
                    node.temporal_domain_key != previous.temporal_domain_key
                    or node.location_key != previous.location_key
                ))
                or (node.decision is not None and node.decision.impact == "major")
                or bool(node.released_constraints_for)
                or bool(node.state_requirements)
                or bool(node.environment_source_unit_keys)
                or node.dramatic_load >= 3
            ):
                risky.add(index)
        if not risky and nodes:
            risky.update({0, len(nodes) - 1})
        selected = {
            neighbor
            for index in risky
            for neighbor in range(max(0, index - 1), min(len(nodes), index + 2))
        }
        selected_nodes = [nodes[index] for index in sorted(selected)]
        selected_keys = {node.key for node in selected_nodes}
        source_ids = list(dict.fromkeys(
            source_id
            for node in selected_nodes
            for source_id in node.source_segment_ids
        ))
        indexed = {
            segment.segment_id: segment.text
            for segment in index_source_segments(source_text)
        }
        projected = {
            "format_version": blueprint.format_version,
            "episode_no": blueprint.episode_no,
            "nodes": [node.model_dump(mode="json") for node in selected_nodes],
            "scene_plans": [
                plan.model_dump(mode="json")
                for plan in blueprint.scene_plans
                if selected_keys.intersection(plan.node_keys)
            ],
            "review_scope": {
                "risk_node_keys": [nodes[index].key for index in sorted(risky)],
                "included_neighbor_node_keys": [node.key for node in selected_nodes],
                "total_blueprint_nodes": len(nodes),
            },
        }
        source_projection = "\n".join(
            f"[{source_id}] {indexed[source_id]}"
            for source_id in source_ids
            if source_id in indexed
        )
        return projected, source_projection, [node.key for node in selected_nodes]

    for review_round in range(1, 5):
        current_blueprint_hash = hashlib.sha256(
            json.dumps(
                blueprint.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        projected_blueprint, projected_source, projected_node_keys = (
            review_projection()
            if targeted_review
            else (
                blueprint.model_dump(mode="json"),
                _render_screenplay_source(render_indexed_source(source_text)),
                [node.key for node in blueprint.nodes],
            )
        )
        node_reference_contract = {
            "contract_version": "blueprint-semantic-node-reference.v1",
            "canonical_nodes": [
                {
                    "ordinal": ordinal,
                    "identity": node_key,
                }
                for ordinal, node_key in enumerate(
                    projected_node_keys,
                    start=1,
                )
            ],
        }
        projected_source_ids = list(dict.fromkeys(
            source_id
            for node in blueprint.nodes
            if node.key in set(projected_node_keys)
            for source_id in node.source_segment_ids
        ))
        projected_source_facts = [
            fact
            for fact in source_facts(source_text)
            if fact.source_segment_id in set(projected_source_ids)
        ]
        projected_source_unit_keys = [
            fact.source_unit_key for fact in projected_source_facts
        ]
        source_reference_contract = {
            "contract_version": "blueprint-semantic-source-reference.v1",
            "canonical_source_segment_ids": projected_source_ids,
            "canonical_source_unit_keys": projected_source_unit_keys,
            "structured_source_units": [
                fact.model_dump(mode="json")
                for fact in projected_source_facts
            ],
        }
        review_schema = blueprint_semantic_review_schema(
            projected_node_keys,
            projected_source_ids,
            projected_source_unit_keys,
        )
        prompt = (
            "你是漫剧叙事蓝图的独立语义审稿人。只找会导致观众理解错误、"
            "人物瞬移、状态矛盾、因果跳跃或动机突变的可证实问题；不改稿，"
            "不评价题材或人物道德，不因为个人偏好要求美化原文。\n"
            "逐项检查：\n"
            "1. 回忆进入/退出、次日/当晚/数日后是否可识别，时间标签是否互相冲突；\n"
            "2. 人物、车辆、司机、行李、房间和关键物品的位置与行动是否闭环；\n"
            "3. 已建立的住宿、关系、知情状态等是否被后文无理由推翻，是否为了推进"
            "剧情临时发明满房、同谋、开放关系等便利条件；\n"
            "4. 重大决定是否有此前可见的压力、欲望和认知依据；\n"
            "5. 威胁、武器、醉酒或失去行动能力是否被错误改写为自主选择，约束解除"
            "是否真实发生；\n"
            "6. 后文引用的视觉事实是否此前真正给观众看见。\n"
            "7. 每个节点的三元叙事语义是否与来源职责一致：story 必须是可表演、"
            "可形成画面状态变化的故事语义；paratext 必须只做来源审计并使用"
            " connective+exclude_from_spine。不得按 SRC 编号、章节位置、人物是否"
            "为空或文本关键词判断，只能依据该段在叙事中的语义职责。\n"
            "8. 每个 projection=picture 的 quoted source unit 是否恰有一个"
            " source_unit_delivery；只有 spoken_dialogue/offscreen_voice 才能有"
            " usage=voice participant evidence，并通过 source_unit_keys 精确绑定"
            "且与 performer_key 一致；"
            "missing、多个 identity 或重复/冲突 claim 必须分别输出"
            " voice_identity_missing、voice_identity_ambiguous、"
            "voice_identity_conflict，不得拖到 SceneInput。quoted source unit 只以"
            "本轮来源合同 structured_source_units 中 projection=quoted 的机器事实"
            "为准；书页、信件、回忆引语、声音效果等非口播内容必须使用对应非声音"
            "delivery mode，不能为其伪造 speaker。story/picture中 projection=action "
            "的正文及 Blueprint 的 summary/"
            "action_logic 即使出现‘旁白’‘介绍’等自然语言，也不需要 voice，禁止"
            "将其提升为 dialogue 或要求伪造旁白 identity。\n"
            "9. 每个story/picture节点中 projection=action 的 prose source unit 必须拥有唯一"
            " exact-unit usage=state_subject evidence，或在 "
            "environment_source_unit_keys 中显式标记为纯环境。visible、"
            "scene roster、content_owner 不是主体证据；缺失、多主体或"
            "人物主体与环境标记冲突必须作为 must_fix 报告。"
            "若且仅若当前 environment_source_unit_keys 中的 action unit 在本轮"
            "完整语义中实际是人物的思考、反应、发问或动作，必须只输出"
            " code=state_subject_environment_misclassified；每条 issue 恰好引用"
            "一个 owning node，并在 source_unit_keys 中精确列出该 issue 涉及的"
            "全部 canonical exact units，在 source_segment_ids 中列出这些 units"
            "精确对应的 SRC。不得为真正的环境变化输出该 code，不得用文本关键词、"
            "姓名或内容列表判断。"
            "paratext/audit_only的quoted/action unit不适用delivery或state-subject要求，"
            "其所有剧情合同字段必须为空。\n"
            "连续剧可继承前序集已经建立的人物和关系；原文在当前节点明确揭示的"
            "既有关系，只要该节点先以可见/可听内容建立再引用，也不属于"
            " setup_missing。不得要求删除原文明确写出的关系来修复 setup。\n"
            "required_resolution 不得把无来源的便利设定伪装为原文事实；若只能通过"
            "改编补桥修复，必须明确要求 adaptation_kind=logic_bridge 及审计理由。"
            "每个问题必须引用本轮节点引用合同中的 canonical identity；node_keys"
            " 每项可直接使用 identity，或使用结构化 {\"ordinal\":正整数} /"
            " {\"identity\":\"canonical identity\"}。ordinal 从 1 开始，严格对应"
            " canonical_nodes 顺序。禁止根据文本相似度推断、拼接或改写 identity。"
            "发现确定问题后必须保留完整 issue；修正引用时不得删除该 issue。"
            "有直接原文依据时附 source_segment_ids。只输出 must_fix=true 的确定"
            "问题，禁止泛泛建议。"
            "\n\n本轮节点引用合同：\n"
            + json.dumps(
                node_reference_contract,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n本轮来源引用合同：\n"
            + json.dumps(
                source_reference_contract,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n\n蓝图：\n"
            + json.dumps(
                projected_blueprint,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n\n带稳定 ID 的原文：\n"
            + projected_source
            + "\n\n输出 Schema：\n"
            + json.dumps(
                review_schema,
                ensure_ascii=False,
            )
        )
        trace = current_trace()
        reviews: list[BlueprintSemanticReview] = []
        review_artifact_ids: list[str] = []
        dropped_voice_issue_counts: dict[int, int] = {}
        async def run_reviewer(sample_no: int) -> BlueprintSemanticReview:
            last_validated_review: BlueprintSemanticReview | None = None
            validated_drop_count = 0

            def validate_review(candidate_review: BlueprintSemanticReview) -> list[str]:
                nonlocal last_validated_review, validated_drop_count
                dropped = filter_blueprint_semantic_review_voice_issues(
                    candidate_review,
                    blueprint,
                    source_text,
                )
                if candidate_review is last_validated_review:
                    validated_drop_count += dropped
                else:
                    last_validated_review = candidate_review
                    validated_drop_count = dropped
                errors = validate_blueprint_semantic_review(
                    candidate_review,
                    blueprint,
                    source_text,
                )
                if targeted_review:
                    allowed = set(projected_node_keys)
                    errors.extend(
                        f"风险审稿引用了范围外节点：{node_key}"
                        for issue in candidate_review.issues
                        for node_key in issue.node_keys
                        if node_key not in allowed
                    )
                return errors

            review_messages = [
                {"role": "system", "content": SYSTEM_PREFIX},
                {
                    "role": "user",
                    "content": f"{prompt}\n独立审稿样本编号：{sample_no}",
                },
            ]
            operation_id, effective_max_tokens = (
                _blueprint_structured_operation_id(
                    operation_kind="review",
                    episode_id=str(episode.get("id") or ""),
                    semantic_input_hash=current_blueprint_hash,
                    ordinal=(
                        f"{review_round}:{sample_no}:"
                        f"{'targeted' if targeted_review else 'full'}"
                    ),
                    messages=review_messages,
                    output_schema=review_schema,
                    requested_max_tokens=BLUEPRINT_REVIEW_MAX_TOKENS,
                    temperature=0.1,
                )
            )
            format_retry_limit = BLUEPRINT_REVIEW_FORMAT_RETRY_LIMIT
            durable_base_replay = bool(
                generation_budget is not None
                and operation_id
                in generation_budget._durable_successful_operations
            )
            reservation_operation_id = operation_id
            if durable_base_replay and format_retry_limit > 0:
                reservation_operation_id = (
                    _blueprint_format_repair_reservation_operation_id(
                        operation_id
                    )
                )
            reservation_id: int | None = None
            remaining_seconds: float | None = None
            legacy_retry_call_id: int | None = None
            if generation_budget is not None:
                legacy_retry_call_id = (
                    generation_budget.explicit_retry_call_id(
                        "screenplay_blueprint_review"
                    )
                )
                reservation_id = generation_budget.claim(
                    max_tokens=effective_max_tokens,
                    requested_max_tokens=BLUEPRINT_REVIEW_MAX_TOKENS,
                    operation_id=reservation_operation_id,
                )
                remaining_seconds = generation_budget.remaining_seconds()
            review_call = model_gateway.chat_structured(
                review_messages,
                model_type=BlueprintSemanticReview,
                validate=validate_review,
                operation_id=operation_id,
                temperature=0.1,
                max_tokens=BLUEPRINT_REVIEW_MAX_TOKENS,
                format_retry_limit=format_retry_limit,
                semantic_retry_limit=0,
                call_meta={
                    "stage": "剧本蓝图语义审稿",
                    "stage_key": "screenplay_blueprint_review",
                    "call_role": "stage_critic",
                    "call_role_label": "蓝图独立语义审稿",
                    "review_round": review_round,
                    "review_sample": sample_no,
                    "supersedes_provider_call_id": legacy_retry_call_id,
                    "episode_id": str(episode.get("id") or ""),
                    "production_grant_id": (
                        generation_budget.retry_grant_id
                        if generation_budget is not None else ""
                    ),
                    "contract_version": BLUEPRINT_VERSION,
                    "substage": "risk_nodes" if targeted_review else "full",
                    "source_count": len(projected_source.splitlines()),
                    "reuse_successful_operation": True,
                    "require_cached_successful_operation": (
                        durable_base_replay and format_retry_limit <= 0
                    ),
                    "disable_reasoning_fallback": True,
                    "disable_provider_retries": True,
                    "disable_provider_candidate_fallback": True,
                },
                repair_context=json.dumps(
                    {
                        "node_reference_contract": node_reference_contract,
                        "source_reference_contract": (
                            source_reference_contract
                        ),
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                output_schema=review_schema,
                normalize_payload=lambda payload: (
                    normalize_blueprint_semantic_review_payload(
                        payload,
                        projected_node_keys,
                    )
                ),
                usage_callback=(
                    None
                    if reservation_id is None
                    else lambda usage_event: generation_budget.record_usage(
                        reservation_id,
                        usage_event,
                    )
                ),
            )
            try:
                review = (
                    await review_call
                    if remaining_seconds is None
                    else await asyncio.wait_for(
                        review_call,
                        timeout=max(0.001, remaining_seconds),
                    )
                )
            except hiagent.ProviderError as exc:
                if reservation_id is not None:
                    generation_budget.settle(
                        reservation_id,
                        unreported_outcome=(
                            "not_sent"
                            if exc.delivery_state == "not_sent"
                            and exc.replay_safe
                            else "unknown"
                        ),
                    )
                raise
            except BaseException:
                if reservation_id is not None:
                    generation_budget.settle(reservation_id)
                raise
            else:
                if reservation_id is not None:
                    generation_budget.settle(reservation_id)
            # The real gateway invokes validate_review, but test/replay
            # adapters are allowed to return a typed cached value directly.
            # Reapply the deterministic authority filter at the boundary so an
            # unsupported delivery/state guess can never reach consensus.  If
            # the same object was already filtered by the callback, retain its
            # prior count instead of counting the boundary no-op twice.
            boundary_dropped = filter_blueprint_semantic_review_voice_issues(
                review,
                blueprint,
                source_text,
            )
            dropped_voice_issue_counts[sample_no] = (
                validated_drop_count + boundary_dropped
                if review is last_validated_review
                else boundary_dropped
            )
            review.issues = [
                issue
                for issue in review.issues
                if not blueprint_semantic_issue_is_resolved(
                    issue,
                    blueprint,
                )
            ]
            return review

        async def run_reviewer_resilient(
            sample_no: int,
        ) -> BlueprintSemanticReview:
            # Retry a single reviewer ONLY when the provider never received the
            # request (not_sent + replay_safe): that cannot double-charge or
            # leave unknown liability, and re-uses the same deterministic
            # operation_id. Timeouts / mid-stream cuts (unknown outcome) are not
            # ProviderError-not_sent, so they still propagate and fail closed.
            attempts = BLUEPRINT_REVIEW_PROVIDER_RETRY_LIMIT + 1
            for attempt in range(1, attempts + 1):
                try:
                    return await run_reviewer(sample_no)
                except hiagent.ProviderError as exc:
                    replay_safe = bool(
                        getattr(exc, "delivery_state", None) == "not_sent"
                        and getattr(exc, "replay_safe", False)
                    )
                    if not replay_safe or attempt >= attempts:
                        raise
                    if trace.run_id:
                        evidence_repository.append_event(
                            trace.run_id,
                            "BLUEPRINT_REVIEWER_RETRY",
                            "info",
                            "独立审稿样本未送达，按 replay-safe 重试同一确定性 operation",
                            step_run_id=trace.step_run_id,
                            trace_id=trace.trace_id,
                            payload={
                                "review_round": review_round,
                                "review_sample": sample_no,
                                "attempt": attempt,
                            },
                        )
            raise AssertionError("unreachable reviewer retry exhaustion")

        def record_review(sample_no: int, result: Any) -> bool:
            if isinstance(result, BaseException):
                evidence_repository.append_event(
                    trace.run_id,
                    "BLUEPRINT_REVIEWER_UNAVAILABLE",
                    "warning",
                    "蓝图独立审稿样本不可用，已按 operational fail-closed 处理",
                    step_run_id=trace.step_run_id,
                    trace_id=trace.trace_id,
                    payload={
                        "review_round": review_round,
                        "review_sample": sample_no,
                        "error_type": type(result).__name__,
                    },
                ) if trace.run_id else None
                return False
            review = result
            reviews.append(review)
            artifact = evidence_repository.create_artifact(
                EvidenceArtifact(
                    type="screenplay_narrative_blueprint_review",
                    scope_type="episode",
                    scope_id=str(episode.get("id") or ""),
                    status="candidate",
                    trust_level="T1",
                    content=review.model_dump(mode="json"),
                    contract_version=BLUEPRINT_VERSION,
                    prompt_version=SCREENPLAY_BLUEPRINT_PROMPT_VERSION,
                    model_snapshot={
                        "review_round": review_round,
                        "review_sample": sample_no,
                        "dropped_unsupported_voice_issue_count": (
                            dropped_voice_issue_counts.get(sample_no, 0)
                        ),
                    },
                ),
                step_run_id=trace.step_run_id,
            )
            review_artifact_ids.append(artifact["id"])
            return True

        results = await asyncio.gather(
            run_reviewer_resilient(1),
            run_reviewer_resilient(2),
            return_exceptions=True,
        )
        for failure in results:
            # A generation breaker (call/token/wall budget) is not a reviewer
            # being unavailable.  Letting gather() swallow it would resurface it
            # as "审稿人不足两份" and send the operator after the wrong thing.
            if isinstance(failure, StageError):
                raise failure
        outcomes = list(enumerate(results, start=1))
        for sample_no, result in outcomes:
            record_review(sample_no, result)

        undelivered = [
            result
            for _sample_no, result in outcomes
            if isinstance(result, BaseException)
            and _blueprint_review_sample_is_undelivered(result)
        ]
        if len(reviews) == 1 and len(undelivered) == 1:
            # Exactly one reviewer never delivered an opinion, so consensus is
            # one clean sample short rather than compromised.  Draw that one
            # sample again as a NEW deterministic operation (sample no 3), which
            # is not a replay of the unresolved call and cannot double-charge
            # it.  Bounded to a single supplementary sample per round, and the
            # call still goes through generation_budget.claim() plus the
            # activation's remaining wall clock, so it cannot outrun any
            # breaker.  Discarding a whole validated blueprint costs ~30
            # minutes; one more review sample costs ~45s.
            if trace.run_id:
                evidence_repository.append_event(
                    trace.run_id,
                    "BLUEPRINT_REVIEWER_SUPPLEMENTED",
                    "info",
                    "一名独立审稿样本未送达，补采一个新样本而非作废整份蓝图",
                    step_run_id=trace.step_run_id,
                    trace_id=trace.trace_id,
                    payload={
                        "review_round": review_round,
                        "review_sample": BLUEPRINT_REVIEW_SUPPLEMENTARY_SAMPLE,
                        "undelivered_error_type": type(
                            undelivered[0]
                        ).__name__,
                    },
                )
            try:
                supplementary = await run_reviewer_resilient(
                    BLUEPRINT_REVIEW_SUPPLEMENTARY_SAMPLE,
                )
            except StageError:
                raise
            except BaseException as exc:  # noqa: BLE001 - fail closed below
                record_review(BLUEPRINT_REVIEW_SUPPLEMENTARY_SAMPLE, exc)
            else:
                record_review(
                    BLUEPRINT_REVIEW_SUPPLEMENTARY_SAMPLE,
                    supplementary,
                )

        if len(reviews) < 2:
            evidence_repository.create_artifact(
                EvidenceArtifact(
                    type="screenplay_narrative_blueprint_review_consensus",
                    scope_type="episode",
                    scope_id=str(episode.get("id") or ""),
                    status="needs_revision",
                    trust_level="T1",
                    content={
                        "review_round": review_round,
                        "blueprint_hash": current_blueprint_hash,
                        "consensus_issue_keys": [],
                        "non_consensus_issue_count": sum(
                            len(review.issues) for review in reviews
                        ),
                        "valid_review_sample_count": len(reviews),
                        "unavailable_review_sample_count": 2 - len(reviews),
                        "dropped_unsupported_voice_issue_count": sum(
                            dropped_voice_issue_counts.values()
                        ),
                    },
                    parent_artifact_ids=review_artifact_ids,
                    contract_version=BLUEPRINT_VERSION,
                    prompt_version=SCREENPLAY_BLUEPRINT_PROMPT_VERSION,
                    model_snapshot={
                        "review_policy_version": (
                            BLUEPRINT_SEMANTIC_REVIEW_POLICY_VERSION
                        ),
                        "authority_fingerprint": (
                            blueprint_authority_validator_fingerprint()
                        ),
                        "source_corpus_hash": review_source_corpus_hash,
                        "review_input_fingerprint": review_input_fingerprint,
                    },
                ),
                step_run_id=trace.step_run_id,
            )
            raise ContentGenerationError(
                "蓝图语义审稿人不足两份，已停止而非静默视为无问题"
            )

        issue_maps = [
            {
                (
                    issue.code,
                    tuple(sorted(issue.node_keys)),
                    tuple(sorted(issue.source_unit_keys)),
                ): issue
                for issue in review.issues
                if issue.must_fix
            }
            for review in reviews
        ]
        consensus_keys = set(issue_maps[0]).intersection(issue_maps[1])
        consensus_issues = [
            issue_maps[0][key] for key in sorted(consensus_keys)
        ]
        non_consensus_issue_count = (
            sum(len(issue_map) for issue_map in issue_maps)
            - 2 * len(consensus_keys)
        )
        deterministic_authority_issues = sorted(
            (
                issue
                for issue_map in issue_maps
                for issue_key, issue in issue_map.items()
                if (
                    issue_key not in consensus_keys
                    and _blueprint_semantic_issue_has_deterministic_authority(
                        issue,
                        blueprint,
                        source_text,
                    )
                )
            ),
            key=_blueprint_semantic_issue_exact_scope,
        )
        authoritative_issues = (
            consensus_issues + deterministic_authority_issues
        )
        non_authoritative_residual_issue_count = (
            non_consensus_issue_count
            - len(deterministic_authority_issues)
        )
        reviews_are_clean = not issue_maps[0] and not issue_maps[1]
        needs_full_fallback = bool(
            targeted_review
            and not authoritative_issues
            and non_authoritative_residual_issue_count
        )
        full_review_has_non_authoritative_residual = bool(
            not targeted_review
            and not authoritative_issues
            and non_authoritative_residual_issue_count
        )
        consensus_artifact = evidence_repository.create_artifact(
            EvidenceArtifact(
                type="screenplay_narrative_blueprint_review_consensus",
                scope_type="episode",
                scope_id=str(episode.get("id") or ""),
                status=(
                    "needs_revision"
                    if needs_full_fallback or authoritative_issues
                    else "validated"
                ),
                trust_level="T1",
                content={
                    "review_round": review_round,
                    "blueprint_hash": current_blueprint_hash,
                    "consensus_issue_keys": [
                        {
                            "code": code,
                            "node_keys": list(node_keys),
                            "source_unit_keys": list(source_unit_keys),
                        }
                        for code, node_keys, source_unit_keys
                        in sorted(consensus_keys)
                    ],
                    "deterministic_authority_issue_keys": [
                        {
                            "code": issue.code,
                            "node_keys": sorted(issue.node_keys),
                            "source_segment_ids": sorted(
                                issue.source_segment_ids
                            ),
                            "source_unit_keys": sorted(
                                issue.source_unit_keys
                            ),
                        }
                        for issue in deterministic_authority_issues
                    ],
                    "authoritative_issue_count": len(
                        authoritative_issues
                    ),
                    "non_consensus_issue_count": non_consensus_issue_count,
                    "non_authoritative_residual_issue_count": (
                        non_authoritative_residual_issue_count
                    ),
                    "dropped_unsupported_voice_issue_count": sum(
                        dropped_voice_issue_counts.values()
                    ),
                    "review_mode": "targeted" if targeted_review else "full",
                    "review_outcome": (
                        "full_fallback_required"
                        if needs_full_fallback else
                        "consensus_issues"
                        if consensus_keys else
                        "deterministic_authority_issues"
                        if deterministic_authority_issues else
                        "non_authoritative_one_sided_residual"
                        if full_review_has_non_authoritative_residual else
                        "clean"
                    ),
                },
                parent_artifact_ids=review_artifact_ids,
                contract_version=BLUEPRINT_VERSION,
                prompt_version=SCREENPLAY_BLUEPRINT_PROMPT_VERSION,
                model_snapshot={
                    "review_policy_version": (
                        BLUEPRINT_SEMANTIC_REVIEW_POLICY_VERSION
                    ),
                    "authority_fingerprint": (
                        blueprint_authority_validator_fingerprint()
                    ),
                    "source_corpus_hash": review_source_corpus_hash,
                    "review_input_fingerprint": review_input_fingerprint,
                },
            ),
            step_run_id=trace.step_run_id,
        )
        if needs_full_fallback:
            # A targeted one-sided result cannot establish clean authority.
            # The next bounded round switches to the complete Blueprint; no
            # patch is attempted from non-consensus findings.
            if review_round >= 4:
                raise ContentGenerationError(
                    "蓝图定向语义复审仍有单侧必须修复问题，已按非 clean 停止"
                )
            targeted_review = False
            continue
        if reviews_are_clean:
            persist_reviewed_authority(
                parent_artifact_ids=[str(consensus_artifact["id"])],
            )
            return blueprint
        if full_review_has_non_authoritative_residual:
            persist_reviewed_authority(
                parent_artifact_ids=[str(consensus_artifact["id"])],
            )
            return blueprint
        if not authoritative_issues:
            raise ContentGenerationError(
                "蓝图双审存在未解决问题，但没有可安全修复的权威问题"
            )
        if review_round >= 4:
            gate_label = (
                "语义共识"
                if consensus_issues
                else "确定性权威"
            )
            raise ContentGenerationError(
                f"蓝图{gate_label}复审仍有必须修复问题："
                + "；".join(
                    issue.message for issue in authoritative_issues[:10]
                )
            )
        semantic_errors = [
            (
                f"[BLUEPRINT_SEMANTIC_{issue.code.upper()}] "
                f"{'、'.join(issue.node_keys)} "
                f"{'、'.join(issue.source_segment_ids)} "
                f"{'、'.join(issue.source_unit_keys)}："
                f"{issue.message}；必须：{issue.required_resolution}"
            )
            for issue in authoritative_issues
        ]
        ownership_issues = [
            issue
            for issue in authoritative_issues
            if issue.code == "state_subject_environment_misclassified"
        ]
        mixed_issues = [
            issue
            for issue in authoritative_issues
            if issue.code != "state_subject_environment_misclassified"
        ]
        ownership_artifact_ids: list[str] = []
        if mixed_issues:
            protected_unit_keys = list(dict.fromkeys(
                unit_key
                for issue in ownership_issues
                for unit_key in issue.source_unit_keys
            ))
            protected_claims = _blueprint_exact_ownership_claims(
                blueprint,
                protected_unit_keys,
            )
            blueprint = await _repair_narrative_blueprint(
                blueprint,
                episode=episode,
                source_text=source_text,
                additional_errors=[
                    error
                    for error, issue in zip(
                        semantic_errors,
                        authoritative_issues,
                    )
                    if issue.code
                    != "state_subject_environment_misclassified"
                ],
                generation_budget=generation_budget,
            )
            if protected_claims != _blueprint_exact_ownership_claims(
                blueprint,
                protected_unit_keys,
            ):
                raise ContentGenerationError(
                    "蓝图普通节点修复越权改写 exact-unit ownership"
                )
        if ownership_issues:
            blueprint, ownership_artifact_id = (
                await _repair_reviewed_blueprint_state_subject_ownership(
                    blueprint,
                    issues=ownership_issues,
                    episode=episode,
                    source_text=source_text,
                    generation_budget=generation_budget,
                )
            )
            ownership_artifact_ids.append(ownership_artifact_id)
            targeted_review = False
        elif non_consensus_issue_count:
            targeted_review = False
        evidence_repository.create_artifact(
            EvidenceArtifact(
                type="screenplay_narrative_blueprint_review_repair_link",
                scope_type="episode",
                scope_id=str(episode.get("id") or ""),
                status="validated",
                trust_level="T1",
                content={
                    "review_artifact_ids": review_artifact_ids,
                    "repaired_issue_count": len(authoritative_issues),
                    "consensus_repaired_issue_count": len(
                        consensus_issues
                    ),
                    "deterministic_authority_repaired_issue_count": len(
                        deterministic_authority_issues
                    ),
                    "ownership_repaired_issue_count": len(ownership_issues),
                    "mixed_repaired_issue_count": len(mixed_issues),
                    "ownership_source_unit_keys": list(dict.fromkeys(
                        unit_key
                        for issue in ownership_issues
                        for unit_key in issue.source_unit_keys
                    )),
                },
                parent_artifact_ids=(
                    review_artifact_ids + ownership_artifact_ids
                ),
                contract_version=BLUEPRINT_VERSION,
                prompt_version=SCREENPLAY_BLUEPRINT_PROMPT_VERSION,
            ),
            step_run_id=trace.step_run_id,
        )
    return blueprint


def _blueprint_review_sample_is_undelivered(exc: BaseException) -> bool:
    """Whether a reviewer failed without ever authoring a review opinion.

    Only these are worth drawing again.  A transport failure (timeout, cut
    stream) and a body that never decoded into JSON both mean the reviewer
    never said anything, so a fresh sample restores the missing opinion without
    overruling one.

    Deliberately excluded:

    * ``StructuredSemanticError`` -- the reviewer *did* author an opinion and it
      failed the review contract.  Re-drawing until some sample passes is
      exactly the coached-compliance failure the strict contracts forbid.
    * ``StructuredFormatError`` with ``unparseable=False`` -- a decoded but
      off-schema answer is likewise authored, and the gateway already spent its
      one bounded format repair on it.
    * ``StructuredProviderRejection`` -- an explicit refusal envelope is
      normally persistent; another sample just burns wall clock.
    * ``StageError`` -- generation breakers must surface, not be re-drawn.
    """
    if isinstance(exc, StageError):
        return False
    if isinstance(exc, hiagent.ProviderError):
        return True
    if isinstance(exc, model_gateway.StructuredProviderRejection):
        return False
    if isinstance(exc, model_gateway.StructuredFormatError):
        return bool(getattr(exc, "unparseable", False))
    return False
