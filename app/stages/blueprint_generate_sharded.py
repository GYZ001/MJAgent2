"""叙事蓝图分片——_generate_sharded_narrative_blueprint 分片生成主循环。"""
from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any


from app import config, hiagent
from app.db import get_conn, log_provider_call
from app.harness import model_gateway
from app.narrative_blueprint import (
    BLUEPRINT_SHARD_LOCAL_AUTHORITY_VERSION,
    BLUEPRINT_SHARD_POLICY_VERSION,
    BLUEPRINT_SPLIT_MANIFEST_VERSION,
    BLUEPRINT_VERSION,
    BlueprintStateSubjectOwnershipPatch,
    NarrativeBlueprint,
    NarrativeBlueprintShard,
    apply_blueprint_state_subject_ownership_patch,
    derive_blueprint_scene_plans,
    normalize_blueprint_fact_versions,
    normalize_blueprint_provider_payload,
    normalize_blueprint_requirement_state_keys,
    normalize_blueprint_state_subject_evidence_projection,
    normalize_blueprint_state_subject_perception,
    validate_narrative_blueprint,
    validate_narrative_blueprint_shard,
)
from app.schemas import (extract_json)
from app.source_excerpt import (
    index_source_segments,
    structural_front_matter_ids,
)
from app.source_facts import (
    SOURCE_FACT_VERSION,
)

from .blueprint_budget import _BlueprintGenerationBudget
from .blueprint_budget_trace import (
    _blueprint_generation_budget_for_trace,
    _blueprint_shard_source_entry,
    _cached_leaf_superseded_by_feedback,
)
from .blueprint_freeze import (
    _freeze_unreported_state_subject_ownership,
    _freeze_unreported_voice_pairs,
)
from .blueprint_prompt import (
    _blueprint_provider_operation_id,
    _blueprint_shard_prompt,
    _blueprint_state_subject_repair_issues,
    _blueprint_state_subject_repair_prompt,
    _blueprint_state_subject_repair_target_keys,
)
from .blueprint_repair import _repair_narrative_blueprint
from .blueprint_shard_structure import (
    _blueprint_leaf_plan_from_cache,
    _blueprint_shard_boundary_context,
    _blueprint_shard_token_budget,
    _namespace_blueprint_shard,
    _normalize_blueprint_shard_structure,
    _split_blueprint_segments,
)
from .common import StageError
from .constants import (
    BLUEPRINT_GENERATION_MAX_SPLIT_DEPTH,
    BLUEPRINT_SHARD_MAX_ATTEMPTS,
    BLUEPRINT_SHARD_MAX_STALL_RETRIES,
    SCREENPLAY_BLUEPRINT_PROMPT_VERSION,
    SYSTEM_PREFIX,
)
from .ir_snapshot import _current_blueprint_authority_snapshot


async def _generate_sharded_narrative_blueprint(
    episode: dict[str, Any],
    source_text: str,
    bible_context: dict[str, Any],
    *,
    generation_budget: _BlueprintGenerationBudget | None = None,
    semantic_feedback: dict[str, list[str]] | None = None,
) -> NarrativeBlueprint:
    from app.evidence import repository as evidence_repository
    from app.harness.types import EvidenceArtifact
    from app.observability.tracing import current_trace

    segments = index_source_segments(source_text)
    source_corpus_hash = hashlib.sha256(
        source_text.encode("utf-8")
    ).hexdigest()
    cached_rows = get_conn().execute(
        """SELECT id,content_json,content_hash,model_snapshot_json
             FROM artifacts
            WHERE scope_type='episode' AND scope_id=?
              AND type='screenplay_narrative_blueprint_shard'
              AND contract_version=?
              AND prompt_version=? AND status='validated'
            ORDER BY created_at DESC LIMIT 200""",
        (
            str(episode.get("id") or ""),
            BLUEPRINT_VERSION,
            SCREENPLAY_BLUEPRINT_PROMPT_VERSION,
        ),
    ).fetchall()
    (
        segment_shards,
        shard_split_depths,
        cached_by_plan_index,
    ) = _blueprint_leaf_plan_from_cache(
        segments,
        list(cached_rows),
        source_corpus_hash=source_corpus_hash,
    )
    if generation_budget is None:
        generation_budget = _blueprint_generation_budget_for_trace(
            current_trace(),
        )
    generation_budget.adopt_shard_plan(len(segment_shards))
    optional_ids = structural_front_matter_ids(segments)
    merged_nodes: list[Any] = []
    shard_index = 1
    while shard_index <= len(segment_shards):
        generation_budget.adopt_shard_plan(len(segment_shards))
        shard_segments = segment_shards[shard_index - 1]
        split_depth = shard_split_depths[shard_index - 1]
        source_ids = [segment.segment_id for segment in shard_segments]
        source_payload = [
            _blueprint_shard_source_entry(segment, semantic_feedback)
            for segment in shard_segments
        ]
        boundary = _blueprint_shard_boundary_context(merged_nodes)
        source_hash = hashlib.sha256(
            json.dumps(
                source_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        boundary_hash = hashlib.sha256(
            json.dumps(
                boundary,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        shard: NarrativeBlueprintShard | None = None
        planned_cached = cached_by_plan_index.get(shard_index)
        if planned_cached is not None and _cached_leaf_superseded_by_feedback(
            cached_source_hash=planned_cached[1].source_hash,
            source_hash=source_hash,
            source_payload=source_payload,
        ):
            planned_cached = None
        if planned_cached is not None:
            cached_row, cached = planned_cached
            try:
                cached_snapshot = json.loads(
                    cached_row["model_snapshot_json"] or "{}"
                )
            except (TypeError, ValueError, json.JSONDecodeError):
                raise StageError(
                    "剧本时空因果蓝图分片",
                    ["[BLUEPRINT_SPLIT_MANIFEST_SNAPSHOT] leaf snapshot 损坏"],
                )
            cached_errors = validate_narrative_blueprint_shard(
                cached,
                expected_episode_no=int(episode["episode_no"]),
                expected_shard_index=shard_index,
                expected_source_segment_ids=source_ids,
                optional_source_segment_ids=optional_ids,
                boundary_state_facts=boundary["active_state_facts"],
                source_text=source_text,
            )
            if not (
                cached_snapshot.get("source_fact_version")
                == SOURCE_FACT_VERSION
                and cached_snapshot.get("shard_policy_version")
                == BLUEPRINT_SHARD_POLICY_VERSION
                and cached_snapshot.get("local_authority_validator_version")
                == BLUEPRINT_SHARD_LOCAL_AUTHORITY_VERSION
                and cached.source_hash == source_hash
                and cached.boundary_hash == boundary_hash
            ):
                raise StageError(
                    "剧本时空因果蓝图分片",
                    ["[BLUEPRINT_SPLIT_MANIFEST_AUTHORITY] leaf source/boundary/policy 漂移"],
                )
            if cached_errors:
                raise StageError(
                    "剧本时空因果蓝图分片",
                    ["[BLUEPRINT_SPLIT_MANIFEST_VALIDATION] " + cached_errors[0]],
                )
            shard = cached
            log_provider_call(
                "screenplay_blueprint_shard_local_recompile",
                config.MODEL_TEXT,
                "REUSED",
                None,
                0,
                meta={
                    "episode_id": str(episode.get("id") or ""),
                    "shard_index": shard_index,
                    "artifact_id": str(cached_row["id"]),
                    "split_manifest_version": BLUEPRINT_SPLIT_MANIFEST_VERSION,
                },
            )
        if shard is None:
            errors: list[str] = []
            previous_candidate: dict[str, Any] | None = None
            ownership_repair_issues: list[Any] | None = None
            split_for_truncation = False
            token_budget = _blueprint_shard_token_budget(shard_segments)
            stall_epoch = 0
            attempt = 0
            previous_attempt_repaired_ownership = False
            while attempt < BLUEPRINT_SHARD_MAX_ATTEMPTS:
                attempt += 1
                # The bounded ownership-map mode belongs to the attempt after
                # a repairable candidate actually appeared -- not to attempt 2
                # whatever happened before it.  In production shard 21 of
                # ep_3b07c59c0856 lost attempt 1 to malformed JSON, so the one
                # repair slot was spent before any candidate existed; attempt 2
                # then produced exactly the 15 ownership issues this mode
                # repairs and had to be re-rolled blind instead.  A repair that
                # did not resolve its own issues falls back to a full shard so
                # the remaining budget is never spent re-sending the same map.
                repair_only = bool(
                    previous_candidate is not None
                    and ownership_repair_issues is not None
                    and not previous_attempt_repaired_ownership
                )
                if repair_only:
                    assert previous_candidate is not None
                    assert ownership_repair_issues is not None
                    prompt = _blueprint_state_subject_repair_prompt(
                        previous_candidate=previous_candidate,
                        issues=ownership_repair_issues,
                        source_payload=source_payload,
                        source_text=source_text,
                    )
                else:
                    prompt = _blueprint_shard_prompt(
                        episode_no=int(episode["episode_no"]),
                        shard_index=shard_index,
                        shard_count=len(segment_shards),
                        errors=errors,
                        bible_context=bible_context,
                        boundary=boundary,
                        source_payload=source_payload,
                        previous_candidate=previous_candidate,
                    )
                provider, model, effective_max_tokens = (
                    hiagent.text_request_token_limits(
                        requested_max_tokens=token_budget,
                    )
                )
                operation_id = _blueprint_provider_operation_id(
                    episode_id=str(episode.get("id") or ""),
                    shard_index=shard_index,
                    attempt=attempt,
                    stall_epoch=stall_epoch,
                    split_depth=split_depth,
                    source_hash=source_hash,
                    boundary_hash=boundary_hash,
                    prompt=prompt,
                    provider=provider,
                    model=model,
                    max_tokens=token_budget,
                    effective_max_tokens=effective_max_tokens,
                    temperature=0.15,
                    provider_semantic_settings=(
                        hiagent.text_request_semantic_settings(provider)
                    ),
                )
                reservation_id = generation_budget.claim(
                    max_tokens=effective_max_tokens,
                    requested_max_tokens=token_budget,
                    operation_id=operation_id,
                )
                durable_replay = bool(
                    operation_id
                    in generation_budget._durable_successful_operations
                )
                remaining_seconds = (
                    generation_budget.max_wall_seconds
                    if durable_replay
                    else generation_budget.remaining_seconds()
                )
                settlement: dict[str, Any] | None = None
                try:
                    raw = await asyncio.wait_for(
                        model_gateway.chat(
                            [
                                {"role": "system", "content": SYSTEM_PREFIX},
                                {"role": "user", "content": prompt},
                            ],
                            temperature=0.15,
                            max_tokens=token_budget,
                            call_meta={
                                "stage": "剧本时空因果蓝图分片",
                                "stage_key": "screenplay_blueprint_shard",
                                "episode_id": str(episode.get("id") or ""),
                                "shard_index": shard_index,
                                "shard_count": len(segment_shards),
                                "attempt": attempt,
                                "response_mode": (
                                    "ownership_repair"
                                    if repair_only
                                    else "full_shard"
                                ),
                                "split_depth": split_depth,
                                "source_count": len(source_ids),
                                "source_fact_count": sum(
                                    len(item["source_facts"])
                                    for item in source_payload
                                ),
                                "expected_json": True,
                                "operation_id": operation_id,
                                "production_grant_id": (
                                    generation_budget.retry_grant_id
                                ),
                                "reuse_successful_operation": True,
                                "require_cached_successful_operation": (
                                    durable_replay
                                ),
                                "contract_version": BLUEPRINT_VERSION,
                                "disable_reasoning_fallback": True,
                                "disable_provider_retries": True,
                                "disable_provider_candidate_fallback": True,
                            },
                            usage_callback=lambda usage_event: (
                                generation_budget.record_usage(
                                    reservation_id,
                                    usage_event,
                                )
                            ),
                        ),
                        timeout=max(0.001, remaining_seconds),
                    )
                except hiagent.ProviderError as exc:
                    settlement = generation_budget.settle(
                        reservation_id,
                        unreported_outcome=(
                            "not_sent"
                            if exc.delivery_state == "not_sent"
                            and exc.replay_safe
                            else "unknown"
                        ),
                    )
                    if (
                        exc.failure_kind
                        == hiagent.ProviderFailureKind.OUTPUT_TRUNCATED.value
                        and len(shard_segments) > 1
                        and split_depth < BLUEPRINT_GENERATION_MAX_SPLIT_DEPTH
                    ):
                        split_for_truncation = True
                        break
                    if (
                        stall_epoch < BLUEPRINT_SHARD_MAX_STALL_RETRIES
                        and hiagent.provider_answer_undelivered(exc)
                    ):
                        # An answer the provider never delivered -- a stall
                        # before the first character, or a stream cut before its
                        # own ``[DONE]`` -- is not a completed generation:
                        # nothing was authored, so there is no candidate to
                        # preserve and nothing to re-roll.  It replays the same
                        # semantic attempt out of its own bounded budget instead
                        # of consuming one -- a stall landing on the last attempt
                        # used to kill the episode having delivered zero
                        # characters.  The fresh stall epoch gives the retry a
                        # distinct operation id, so the budget does not treat it
                        # as replaying the same unknown outcome.
                        stall_epoch += 1
                        attempt -= 1
                        continue
                    raise
                except asyncio.TimeoutError as exc:
                    settlement = generation_budget.settle(reservation_id)
                    raise StageError(
                        "剧本时空因果蓝图分片",
                        ["[BLUEPRINT_GENERATION_TIME_BUDGET] provider调用超过全局时间上限"],
                    ) from exc
                except BaseException:
                    generation_budget.settle(reservation_id)
                    raise
                else:
                    settlement = generation_budget.settle(reservation_id)
                assert settlement is not None
                trace = current_trace()
                evidence_repository.create_artifact(
                    EvidenceArtifact(
                        type="screenplay_narrative_blueprint_shard_raw",
                        scope_type="episode",
                        scope_id=str(episode.get("id") or ""),
                        status="candidate",
                        trust_level="T0",
                        content={
                            "raw_output": raw,
                            "shard_index": shard_index,
                            "attempt": attempt,
                            "response_mode": (
                                "ownership_repair"
                                if repair_only
                                else "full_shard"
                            ),
                            "source_hash": source_hash,
                            "boundary_hash": boundary_hash,
                            "source_fact_version": SOURCE_FACT_VERSION,
                            "shard_policy_version": (
                                BLUEPRINT_SHARD_POLICY_VERSION
                            ),
                            "split_manifest_version": (
                                BLUEPRINT_SPLIT_MANIFEST_VERSION
                            ),
                            "split_depth": split_depth,
                            "requested_max_tokens": token_budget,
                            "token_settlement": settlement,
                        },
                        contract_version=BLUEPRINT_VERSION,
                        prompt_version=SCREENPLAY_BLUEPRINT_PROMPT_VERSION,
                    ),
                    step_run_id=trace.step_run_id,
                )
                try:
                    provider_payload = extract_json(
                        raw,
                        repair_unescaped_inner_quotes=True,
                    )
                    if repair_only:
                        assert previous_candidate is not None
                        assert ownership_repair_issues is not None
                        patch = (
                            BlueprintStateSubjectOwnershipPatch.model_validate(
                                provider_payload
                            )
                        )
                        candidate = (
                            apply_blueprint_state_subject_ownership_patch(
                                previous_candidate,
                                patch,
                                target_unit_keys=(
                                    _blueprint_state_subject_repair_target_keys(
                                        ownership_repair_issues
                                    )
                                ),
                                source_text=source_text,
                            )
                        )
                    else:
                        normalized_payload = (
                            normalize_blueprint_provider_payload(
                                provider_payload,
                            )
                        )
                        if previous_candidate and isinstance(
                            normalized_payload,
                            dict,
                        ):
                            normalized_payload = _freeze_unreported_voice_pairs(
                                normalized_payload,
                                previous_candidate=previous_candidate,
                                validation_errors=errors,
                            )
                            normalized_payload = (
                                normalize_blueprint_provider_payload(
                                    normalized_payload,
                                )
                            )
                        candidate = NarrativeBlueprintShard.model_validate(
                            normalized_payload,
                        )
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    errors = [f"[BLUEPRINT_SHARD_JSON] {exc}"]
                    continue
                if not repair_only:
                    candidate.source_hash = source_hash
                    candidate.boundary_hash = boundary_hash
                    candidate.source_segment_ids = source_ids
                    _normalize_blueprint_shard_structure(
                        candidate,
                        boundary_context=boundary,
                        attempt=attempt,
                        previous_candidate=previous_candidate,
                        previous_validation_errors=errors,
                    )
                    _namespace_blueprint_shard(candidate)
                    if previous_candidate is not None:
                        _freeze_unreported_state_subject_ownership(
                            candidate,
                            previous_candidate=previous_candidate,
                            validation_errors=errors,
                        )
                # Returns a count of removed keys; the in-place normalization
                # on ``candidate`` is the intended effect.
                _removed_state_subject_keys = (
                    normalize_blueprint_state_subject_evidence_projection(
                        candidate,
                        source_text,
                    )
                )
                # A unit that already resolves to exactly one state subject
                # implies its own perception evidence; the merged-blueprint
                # repair loop has always settled that locally.  Running the
                # same normalization per shard keeps the contract identical and
                # stops every shard from spending a whole ownership-repair
                # round trip restating an owner it had already chosen.
                normalize_blueprint_state_subject_perception(candidate)
                previous_candidate = candidate.model_dump(mode="json")
                errors = validate_narrative_blueprint_shard(
                    candidate,
                    expected_episode_no=int(episode["episode_no"]),
                    expected_shard_index=shard_index,
                    expected_source_segment_ids=source_ids,
                    optional_source_segment_ids=optional_ids,
                    boundary_state_facts=boundary[
                        "active_state_facts"
                    ],
                    source_text=source_text,
                )
                if not errors:
                    shard = candidate
                    evidence_repository.create_artifact(
                        EvidenceArtifact(
                            type="screenplay_narrative_blueprint_shard",
                            scope_type="episode",
                            scope_id=str(episode.get("id") or ""),
                            status="validated",
                            trust_level="T1",
                            content=shard.model_dump(mode="json"),
                            contract_version=BLUEPRINT_VERSION,
                            prompt_version=SCREENPLAY_BLUEPRINT_PROMPT_VERSION,
                            model_snapshot={
                                "shard_index": shard_index,
                                "source_fact_version": SOURCE_FACT_VERSION,
                                "source_count": len(source_ids),
                                "source_fact_count": sum(
                                    len(item["source_facts"])
                                    for item in source_payload
                                ),
                                "shard_policy_version": (
                                    BLUEPRINT_SHARD_POLICY_VERSION
                                ),
                                "local_authority_validator_version": (
                                    BLUEPRINT_SHARD_LOCAL_AUTHORITY_VERSION
                                ),
                                "split_manifest_version": (
                                    BLUEPRINT_SPLIT_MANIFEST_VERSION
                                ),
                                "source_corpus_hash": source_corpus_hash,
                                "split_depth": split_depth,
                                "requested_max_tokens": token_budget,
                                "actual_completion_tokens": settlement[
                                    "actual_completion_tokens"
                                ],
                                "usage_reported": settlement[
                                    "usage_reported"
                                ],
                                "charged_output_tokens": settlement[
                                    "charged_output_tokens"
                                ],
                                "global_charged_output_tokens": settlement[
                                    "global_charged_output_tokens"
                                ],
                            },
                        ),
                        step_run_id=trace.step_run_id,
                    )
                    break
                previous_attempt_repaired_ownership = repair_only
                ownership_repair_issues = (
                    _blueprint_state_subject_repair_issues(
                        candidate,
                        validation_errors=errors,
                        source_text=source_text,
                    )
                )
            if split_for_truncation:
                split = _split_blueprint_segments(shard_segments)
                segment_shards[shard_index - 1:shard_index] = split
                shard_split_depths[shard_index - 1:shard_index] = [
                    split_depth + 1,
                    split_depth + 1,
                ]
                cached_by_plan_index = {
                    (index + 1 if index > shard_index else index): value
                    for index, value in cached_by_plan_index.items()
                    if index != shard_index
                }
                continue
            if shard is None:
                if (
                    len(shard_segments) > 1
                    and split_depth < BLUEPRINT_GENERATION_MAX_SPLIT_DEPTH
                ):
                    split = _split_blueprint_segments(shard_segments)
                    segment_shards[shard_index - 1:shard_index] = split
                    shard_split_depths[shard_index - 1:shard_index] = [
                        split_depth + 1,
                        split_depth + 1,
                    ]
                    cached_by_plan_index = {
                        (index + 1 if index > shard_index else index): value
                        for index, value in cached_by_plan_index.items()
                        if index != shard_index
                    }
                    continue
                raise StageError(
                    f"剧本时空因果蓝图分片 {shard_index}",
                    errors[:10],
                )
        merged_nodes.extend(shard.nodes)
        shard_index += 1
    blueprint = NarrativeBlueprint(
        episode_no=int(episode["episode_no"]),
        nodes=merged_nodes,
    )
    normalize_blueprint_fact_versions(blueprint)
    normalize_blueprint_requirement_state_keys(blueprint)
    errors = validate_narrative_blueprint(blueprint, source_text)
    if errors:
        blueprint = await _repair_narrative_blueprint(
            blueprint,
            episode=episode,
            source_text=source_text,
            generation_budget=generation_budget,
        )
    derive_blueprint_scene_plans(blueprint)
    trace = current_trace()
    evidence_repository.create_artifact(
        EvidenceArtifact(
            type="screenplay_narrative_blueprint",
            scope_type="episode",
            scope_id=str(episode.get("id") or ""),
            status="validated",
            trust_level="T1",
            content=blueprint.model_dump(mode="json"),
            contract_version=BLUEPRINT_VERSION,
            prompt_version=SCREENPLAY_BLUEPRINT_PROMPT_VERSION,
            model_snapshot=_current_blueprint_authority_snapshot(
                source_text,
                generation_mode="source_shards",
                generation_budget=generation_budget,
                shard_count=len(segment_shards),
            ),
        ),
        step_run_id=trace.step_run_id,
    )
    return blueprint
