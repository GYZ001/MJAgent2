"""Top-level entry point that orchestrates scene-shard generation for an
episode: builds plans and input contracts, drives the per-shard
generate/validate/semantic-review pipeline, and writes the resulting
artifacts.

Moved verbatim from ``app/screenplay_scene_shards.py`` (see
``app/screenplay_scene_shards/__init__.py`` for the full re-export map).
"""
from __future__ import annotations

import asyncio
import json
from app.evidence import repository as evidence_repository
from app.harness import model_gateway
from app.harness.types import EvidenceArtifact
from app.narrative_blueprint import NarrativeBlueprint
from app.observability.tracing import current_trace
from app.screenplay_ir import IRIdentity
from app.source_excerpt import (
    index_source_segments,
    structural_front_matter_ids,
)
from collections.abc import Callable
from pydantic import ValidationError
from typing import Any

from .artifact_compat import screenplay_scene_shard_artifact_compatibility
from .common import (
    _SceneStructuredOperationGate,
    _gather_fail_fast,
    _hash,
    _scene_structured_with_undelivered_retry,
    _setting_int,
)
from .compile_draft import compile_screenplay_scene_shard_draft
from .constants import (
    SCREENPLAY_SCENE_CREATIVE_VERSION,
    SCREENPLAY_SCENE_INPUT_VERSION,
    SCREENPLAY_SCENE_SEMANTIC_REVIEW_VERSION,
    SCREENPLAY_SCENE_SHARD_VERSION,
)
from .identity_registry import (
    ScreenplaySceneShardError,
    ScreenplaySceneShardOwnershipLost,
    _assert_episode_owner,
)
from .identity_scaffold import (
    build_screenplay_scene_shard_repair_schema,
    screenplay_scene_generation_scaffold_hash,
    screenplay_scene_identity_scaffold_hash,
)
from .input_contracts import _validate_scene_input_contracts
from .models import (
    ScreenplayActionParticipantDeliveryContract,
    ScreenplaySceneInputContract,
    ScreenplaySceneShardCreativeIR,
    ScreenplaySceneShardIR,
    ScreenplaySceneShardPlan,
)
from .review_prompt import _scene_shard_strict_response_format
from .scene_prompt import (
    _scene_shard_prompt,
    _scene_shard_semantic_authority_payload,
)
from .semantic_review import _semantic_review_scene_shard_draft
from .shard_merge import (
    _latest_validated_artifact,
    _raw_parent_artifact,
    _recover_scene_shard_from_provider_calls,
)
from .shard_plan import (
    _screenplay_scene_shard_budget_meta,
    screenplay_scene_shard_token_budget,
)
from .shard_validate import validate_screenplay_scene_shard


async def generate_screenplay_scene_shards(
    *,
    episode: dict[str, Any],
    source_text: str,
    blueprint: NarrativeBlueprint,
    identity_registry: list[dict[str, Any]],
    identities: list[IRIdentity],
    plans: list[ScreenplaySceneShardPlan],
    scene_input_contracts: dict[
        str, list[ScreenplaySceneInputContract]
    ],
    blueprint_artifact_id: str | None = None,
    identity_artifact_id: str | None = None,
    progress: Callable[[list[dict[str, Any]]], Any] | None = None,
) -> tuple[list[ScreenplaySceneShardIR], list[str], list[dict[str, Any]]]:
    """Generate/reuse independent shards with a per-episode concurrency cap."""
    episode_id = str(episode.get("id") or f"episode-{episode['episode_no']}")
    scene_plan_map = {plan.key: plan for plan in blueprint.scene_plans}
    source_segments = index_source_segments(source_text)
    front_matter_ids = structural_front_matter_ids(source_segments)
    identity_keys = {identity.key for identity in identities}
    parallelism = _setting_int(
        "screenplay_scene_shard_parallelism", 2, minimum=1, maximum=8
    )
    semaphore = asyncio.Semaphore(parallelism)
    # batch_abort/structured_operation_gate are per-shard, not per-episode:
    # each generate_one() call below makes its own, so one shard's failure
    # can only short-circuit that shard's own later sub-operations (its
    # reviewers, its repair rounds).  The one failure that must still cancel
    # every other shard -- upstream authority invalidated, i.e. the episode's
    # active_screenplay_run_id changed underneath this run -- is raised by
    # _assert_episode_owner() as ScreenplaySceneShardOwnershipLost and is
    # cascaded explicitly by the top-level _gather_fail_fast(cascades=...)
    # call at the bottom of this function, independent of batch_abort.
    checkpoint_rows: dict[str, dict[str, Any]] = {
        plan.shard_id: {
            "shard_id": plan.shard_id,
            "status": "pending",
            "attempt": 0,
            "source_hash": plan.source_hash,
            "boundary_hash": plan.boundary_hash,
            "source_ownership_hash": plan.source_ownership_hash,
            "generation_scaffold_hash": (
                screenplay_scene_generation_scaffold_hash(
                    plan,
                    scene_input_contracts.get(plan.shard_id, []),
                )
            ),
        }
        for plan in plans
    }

    def emit_progress() -> None:
        if progress is not None:
            progress([checkpoint_rows[plan.shard_id] for plan in plans])

    async def generate_one(
        plan: ScreenplaySceneShardPlan,
    ) -> tuple[ScreenplaySceneShardIR, str]:
        # Shard-local abort signal/gate: setting it only ever short-circuits
        # this one shard's remaining sub-operations, never a sibling shard's.
        batch_abort = asyncio.Event()
        structured_operation_gate = _SceneStructuredOperationGate(batch_abort)

        def abort_outer_batch() -> None:
            batch_abort.set()

        _assert_episode_owner(episode_id)
        plan_scene_input_contracts = scene_input_contracts.get(
            plan.shard_id, []
        )
        _, preflight_errors = _validate_scene_input_contracts(
            plan=plan,
            scene_plans=scene_plan_map,
            scene_input_contracts=plan_scene_input_contracts,
            identity_keys=identity_keys,
        )
        if preflight_errors:
            raise ScreenplaySceneShardError(
                plan.shard_id,
                preflight_errors,
            )
        identity_scaffold_hash = (
            screenplay_scene_identity_scaffold_hash(
                plan_scene_input_contracts
            )
        )
        generation_scaffold_hash = (
            screenplay_scene_generation_scaffold_hash(
                plan,
                plan_scene_input_contracts,
            )
        )
        cached = _latest_validated_artifact(
            episode_id=episode_id,
            artifact_type="screenplay_scene_shard",
            predicate=lambda content: all(
                content.get(field) == getattr(plan, field)
                for field in (
                    "shard_id", "source_hash", "boundary_hash",
                    "blueprint_hash", "identity_registry_hash",
                    "source_ownership_hash",
                )
            )
            and content.get("identity_scaffold_hash")
            == identity_scaffold_hash
            and content.get("generation_scaffold_hash")
            == generation_scaffold_hash
            and content.get("contract_version")
            == SCREENPLAY_SCENE_SHARD_VERSION,
        )
        if cached:
            compatible, _reason = screenplay_scene_shard_artifact_compatibility(
                cached,
                expected_blueprint_hash=plan.blueprint_hash,
                expected_identity_registry_hash=plan.identity_registry_hash,
                expected_generation_scaffold_hash=generation_scaffold_hash,
                raw_artifact=_raw_parent_artifact(cached),
                expected_authority_artifact_ids={
                    str(blueprint_artifact_id or ""),
                    str(identity_artifact_id or ""),
                },
            )
            try:
                shard = (
                    ScreenplaySceneShardIR.model_validate(cached["content"])
                    if compatible
                    else None
                )
            except ValidationError:
                shard = None
            if shard is not None:
                errors = validate_screenplay_scene_shard(
                    shard,
                    plan=plan,
                    scene_plans=scene_plan_map,
                    scene_input_contracts=plan_scene_input_contracts,
                    identity_keys=identity_keys,
                    front_matter_ids=front_matter_ids,
                )
                if not errors:
                    checkpoint_rows[plan.shard_id].update({
                        "status": "validated",
                        "attempt": 0,
                        "normalized_artifact_id": str(cached["id"]),
                        "identity_scaffold_hash": identity_scaffold_hash,
                        "generation_scaffold_hash": (
                            generation_scaffold_hash
                        ),
                        "reused": True,
                    })
                    emit_progress()
                    return shard, str(cached["id"])
        selected_scene_plans = [scene_plan_map[key] for key in plan.scene_plan_keys]
        selected_node_keys = {
            node_key for scene_plan in selected_scene_plans
            for node_key in scene_plan.node_keys
        }
        repair_contracts: list[dict[str, Any]] = []
        for contract in plan_scene_input_contracts:
            payload = contract.model_dump(mode="json")
            payload["source_scene_owners"] = {
                source_id: contract.source_scene_owners[source_id]
                for source_id in contract.source_segment_ids
            }
            repair_contracts.append(payload)
        exact_slot_authority, identity_labels = (
            _scene_shard_semantic_authority_payload(
                scene_input_contracts=plan_scene_input_contracts,
                identity_registry=identity_registry,
            )
        )
        creative_repair_context = json.dumps({
            "root_contract": {
                "contract_version": SCREENPLAY_SCENE_CREATIVE_VERSION,
                "required_root_fields": [
                    "contract_version", "slots",
                ],
                "structural_fields_owned_by": (
                    "deterministic_generation_scaffold"
                ),
                "generation_scaffold_hash": generation_scaffold_hash,
            },
            "scene_input_contracts": repair_contracts,
            "exact_slot_authority": exact_slot_authority,
            "identity_authority": identity_labels,
            "action_participant_delivery_contract": (
                ScreenplayActionParticipantDeliveryContract()
                .model_dump(mode="json")
            ),
            "final_gate_contract": [
                "slot keys must exactly equal the declared unit_key set",
                "missing or extra slots are generation_contract failures",
                "structural and identity fields are forbidden in slot content",
                "dialogue text must equal scaffold source_text",
                "action_agency, agency_kind, text_provenance and identity_keys are compiler-owned additional properties",
                "required_text, prop_text and on_screen_text are content fields and never create identity relations",
                "compiler derives agency and provenance from scaffold relations and source IDs",
                "empty action source_text does not authorize free rewriting; use only that slot's source_fact.text",
                "each slot may rewrite only its own source_fact and may not borrow adjacent units",
                "cross-slot content is attributed to the earliest overreaching slot",
            ],
        }, ensure_ascii=False, separators=(",", ":"))
        output_schema = build_screenplay_scene_shard_repair_schema(
            plan=plan,
            scene_input_contracts=plan_scene_input_contracts,
        )
        prompt = _scene_shard_prompt(
            episode_no=int(episode["episode_no"]),
            plan=plan,
            blueprint_scene_plans=selected_scene_plans,
            blueprint_nodes=[
                node.model_dump(mode="json")
                for node in blueprint.nodes if node.key in selected_node_keys
            ],
            scene_input_contracts=plan_scene_input_contracts,
            identity_registry=identity_registry,
            output_schema=output_schema,
        )
        operation_id = (
            f"screenplay.scene-shard:{SCREENPLAY_SCENE_SHARD_VERSION}:"
            f"{SCREENPLAY_SCENE_INPUT_VERSION}:"
            f"{episode_id}:{plan.shard_id}:{plan.source_hash}:"
            f"{plan.boundary_hash}:{plan.blueprint_hash}:"
            f"{plan.identity_registry_hash}:{plan.source_ownership_hash}:"
            f"{generation_scaffold_hash}"
        )

        def validate_draft(
            value: ScreenplaySceneShardCreativeIR,
        ) -> list[str]:
            try:
                compiled = compile_screenplay_scene_shard_draft(
                    value,
                    episode_no=int(episode["episode_no"]),
                    plan=plan,
                    scene_plans=scene_plan_map,
                    scene_input_contracts=plan_scene_input_contracts,
                )
            except ScreenplaySceneShardError as exc:
                return list(exc.errors)
            return validate_screenplay_scene_shard(
                compiled,
                plan=plan,
                scene_plans=scene_plan_map,
                scene_input_contracts=plan_scene_input_contracts,
                identity_keys=identity_keys,
                front_matter_ids=front_matter_ids,
            )

        def repair_schema(
            value: ScreenplaySceneShardCreativeIR,
        ) -> dict[str, Any]:
            del value
            return output_schema

        attempts: list[dict[str, Any]] = []
        recovered = _recover_scene_shard_from_provider_calls(
            operation_id=operation_id,
            episode_no=int(episode["episode_no"]),
            plan=plan,
            scene_plans=scene_plan_map,
            scene_input_contracts=plan_scene_input_contracts,
            blueprint=blueprint,
            identity_keys=identity_keys,
            front_matter_ids=front_matter_ids,
        )
        if recovered is not None:
            draft, recovery_attempt = recovered
            attempts.append(recovery_attempt)
        else:
            async with semaphore:
                if batch_abort.is_set():
                    raise asyncio.CancelledError
                try:
                    checkpoint_rows[plan.shard_id].update({
                        "status": "running", "attempt": 1,
                    })
                    emit_progress()
                    budget_meta = _screenplay_scene_shard_budget_meta(plan)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    batch_abort.set()
                    raise
                try:
                    async def write_draft() -> (
                        ScreenplaySceneShardCreativeIR
                    ):
                        async def issue_draft(
                            attempt_operation_id: str,
                        ) -> ScreenplaySceneShardCreativeIR:
                            return await model_gateway.chat_structured(
                                [{"role": "user", "content": prompt}],
                                model_type=ScreenplaySceneShardCreativeIR,
                                validate=validate_draft,
                                operation_id=attempt_operation_id,
                                max_tokens=(
                                    screenplay_scene_shard_token_budget(plan)
                                ),
                                temperature=0.4,
                                format_retry_limit=_setting_int(
                                    "screenplay_format_retry_limit",
                                    1,
                                    minimum=0,
                                    maximum=3,
                                ),
                                semantic_retry_limit=_setting_int(
                                    "screenplay_semantic_retry_limit",
                                    1,
                                    minimum=0,
                                    maximum=3,
                                ),
                                call_meta={
                                    "stage": "剧本场次分片",
                                    "stage_key": "screenplay_scene_shards",
                                    "substage": "scene_writing",
                                    "shard_id": plan.shard_id,
                                    "shard_count": len(plans),
                                    "episode_id": episode_id,
                                    "source_count": len(plan.source_segment_ids),
                                    "scene_count": len(plan.scene_plan_keys),
                                    "identity_scaffold_hash": (
                                        identity_scaffold_hash
                                    ),
                                    "generation_scaffold_hash": (
                                        generation_scaffold_hash
                                    ),
                                    "input_chars": len(prompt),
                                    **budget_meta,
                                },
                                repair_context=creative_repair_context,
                                format_repair_context=creative_repair_context,
                                output_schema=output_schema,
                                response_format=(
                                    _scene_shard_strict_response_format(
                                        name="screenplay_scene_shard_creative",
                                        local_schema=output_schema,
                                    )
                                ),
                                require_response_format=True,
                                repair_schema=repair_schema,
                                on_attempt=attempts.append,
                            )

                        return await _scene_structured_with_undelivered_retry(
                            issue_draft,
                            operation_id=operation_id,
                        )

                    draft = await structured_operation_gate.run(
                        write_draft,
                        on_failure=abort_outer_batch,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # Belt-and-suspenders: the gate's on_failure callback
                    # (abort_outer_batch) already set this shard's own
                    # batch_abort before this exception reached us.  This
                    # only ever affects this shard's later checkpoints below
                    # -- it cannot reach sibling shards, which is the point.
                    batch_abort.set()
                    raise
        if batch_abort.is_set():
            raise asyncio.CancelledError
        initial_creative_hash = _hash(draft.model_dump(mode="json"))
        draft, semantic_reviews = await _semantic_review_scene_shard_draft(
            draft=draft,
            scene_input_contracts=plan_scene_input_contracts,
            identity_registry=identity_registry,
            operation_id=operation_id,
            shard_id=plan.shard_id,
            validate_draft=validate_draft,
            batch_abort=batch_abort,
            abort_batch=abort_outer_batch,
            structured_operation_gate=structured_operation_gate,
            full_creative_schema=output_schema,
        )
        if batch_abort.is_set():
            raise asyncio.CancelledError
        reviewed_creative_hash = _hash(draft.model_dump(mode="json"))
        if (
            not semantic_reviews
            or semantic_reviews[-1].get("consensus") != []
            or semantic_reviews[0].get("creative_hash")
            != initial_creative_hash
            or semantic_reviews[-1].get("creative_hash")
            != reviewed_creative_hash
        ):
            raise ScreenplaySceneShardError(
                plan.shard_id,
                ["语义审查证据未绑定精确 creative candidate"],
            )
        shard = compile_screenplay_scene_shard_draft(
            draft,
            episode_no=int(episode["episode_no"]),
            plan=plan,
            scene_plans=scene_plan_map,
            scene_input_contracts=plan_scene_input_contracts,
        )
        shard_payload = shard.model_dump(mode="json")
        reviewed_shard_content_hash = evidence_repository.content_hash(
            shard_payload
        )
        _assert_episode_owner(episode_id)
        trace = current_trace()
        parents = [
            value for value in (blueprint_artifact_id, identity_artifact_id) if value
        ]
        raw_artifact = evidence_repository.create_artifact(
            EvidenceArtifact(
                type="screenplay_scene_shard_raw",
                scope_type="episode",
                scope_id=episode_id,
                status="candidate",
                trust_level="T0",
                content={
                    "shard_id": plan.shard_id,
                    "operation_id": operation_id,
                    "identity_scaffold_hash": identity_scaffold_hash,
                    "generation_scaffold_hash": (
                        generation_scaffold_hash
                    ),
                    "creative_contract_version": (
                        SCREENPLAY_SCENE_CREATIVE_VERSION
                    ),
                    "attempts": attempts,
                    "semantic_review_evidence": {
                        "contract_version": (
                            SCREENPLAY_SCENE_SEMANTIC_REVIEW_VERSION
                        ),
                        "initial_creative_hash": initial_creative_hash,
                        "reviewed_creative_hash": reviewed_creative_hash,
                        "reviewed_shard_content_hash": (
                            reviewed_shard_content_hash
                        ),
                        "phases": semantic_reviews,
                    },
                },
                parent_artifact_ids=parents,
                contract_version=SCREENPLAY_SCENE_SHARD_VERSION,
            ),
            step_run_id=trace.step_run_id,
        )
        artifact = evidence_repository.create_artifact(
            EvidenceArtifact(
                type="screenplay_scene_shard",
                scope_type="episode",
                scope_id=episode_id,
                status="validated",
                trust_level="T1",
                content=shard_payload,
                parent_artifact_ids=[raw_artifact["id"]],
                contract_version=SCREENPLAY_SCENE_SHARD_VERSION,
                model_snapshot={
                    "shard_id": plan.shard_id,
                    "scene_count": len(shard.scenes),
                    "unit_count": sum(len(scene.units) for scene in shard.scenes),
                    "identity_scaffold_hash": identity_scaffold_hash,
                    "generation_scaffold_hash": (
                        generation_scaffold_hash
                    ),
                    "creative_contract_version": (
                        SCREENPLAY_SCENE_CREATIVE_VERSION
                    ),
                    "semantic_review_version": (
                        SCREENPLAY_SCENE_SEMANTIC_REVIEW_VERSION
                    ),
                    "reviewed_creative_hash": reviewed_creative_hash,
                    "reviewed_shard_content_hash": (
                        reviewed_shard_content_hash
                    ),
                },
            ),
            step_run_id=trace.step_run_id,
        )
        checkpoint_rows[plan.shard_id].update({
            "status": "validated",
            "raw_artifact_id": raw_artifact["id"],
            "normalized_artifact_id": artifact["id"],
            "identity_scaffold_hash": identity_scaffold_hash,
            "generation_scaffold_hash": generation_scaffold_hash,
            "reused": False,
        })
        emit_progress()
        return shard, str(artifact["id"])

    async def generate_one_with_checkpoint(
        plan: ScreenplaySceneShardPlan,
    ) -> tuple[ScreenplaySceneShardIR, str]:
        try:
            return await generate_one(plan)
        except asyncio.CancelledError:
            # Cancellation (whether user-requested or an ownership-loss
            # cascade -- see the cascades= predicate below) is not a shard
            # validation failure.
            raise
        except Exception as exc:
            checkpoint_rows[plan.shard_id]["status"] = "failed"
            checkpoint_rows[plan.shard_id]["error_type"] = type(exc).__name__
            emit_progress()
            raise

    results = await _gather_fail_fast(
        *(
            lambda plan=plan: generate_one_with_checkpoint(plan)
            for plan in plans
        ),
        # Narrow cascade: a shard's own content/provider failure (deterministic
        # rejection, validation gate, exhausted retries, ...) must not cancel
        # its still-running siblings -- that cross-shard cascade is what
        # turned one canned upstream refusal into an episode-wide pileup
        # (measured: 97.2% of cancellations landed on scene shards, 99.4% of
        # those with zero received chars).  The one failure that genuinely
        # invalidates every shard's in-flight work is the episode's owner
        # changing underneath this run -- a newer run superseded it, so
        # continuing would burn provider calls whose results could never be
        # persisted (_assert_episode_owner enforces that at every checkpoint
        # regardless).  That is the only exception type allowed to cascade.
        cascades=lambda exc: isinstance(exc, ScreenplaySceneShardOwnershipLost),
    )
    shards = [shard for shard, _artifact_id in results]
    artifact_ids = [artifact_id for _shard, artifact_id in results]
    emit_progress()
    return shards, artifact_ids, [checkpoint_rows[plan.shard_id] for plan in plans]
