"""Recovers and merges scene shards into the generation IR: provider-call
recovery for interrupted shards, scene-key namespacing, and
``merge_screenplay_scene_shards`` which assembles the validated shards (plus
the envelope) into one ``ScreenplayGenerationIR``.

Moved verbatim from ``app/screenplay_scene_shards.py`` (see
``app/screenplay_scene_shards/__init__.py`` for the full re-export map).
"""
from __future__ import annotations

import json
from app.db import get_conn
from app.evidence import repository as evidence_repository
from app.harness import model_gateway
from app.narrative_blueprint import (
    BlueprintScenePlan,
    NarrativeBlueprint,
)
from app.screenplay_ir import (
    IRCoverageGroup,
    IRIdentity,
    IRScene,
    IR_VERSION,
    ScreenplayGenerationIR,
    screenplay_ir_source_audit_contract_errors,
)
from app.source_excerpt import index_source_segments
from collections.abc import Callable
from pydantic import ValidationError
from typing import Any

from .artifact_compat import _artifact_parent_ids
from .compile_draft import compile_screenplay_scene_shard_draft
from .identity_registry import (
    ScreenplaySceneMergeError,
    _source_ownership_hash,
    blueprint_content_hash,
)
from .models import (
    ScreenplayEnvelopeIR,
    ScreenplaySceneInputContract,
    ScreenplaySceneShardCreativeIR,
    ScreenplaySceneShardIR,
    ScreenplaySceneShardPlan,
)
from .shard_validate import (
    normalize_screenplay_scene_creative_payload,
    validate_screenplay_scene_shard,
)


def _recover_scene_shard_from_provider_calls(
    *,
    operation_id: str,
    episode_no: int,
    plan: ScreenplaySceneShardPlan,
    scene_plans: dict[str, BlueprintScenePlan],
    scene_input_contracts: list[ScreenplaySceneInputContract],
    blueprint: NarrativeBlueprint,
    identity_keys: set[str],
    front_matter_ids: set[str],
) -> tuple[ScreenplaySceneShardCreativeIR, dict[str, Any]] | None:
    """Revalidate a complete prior response before issuing another paid call."""
    rows = get_conn().execute(
        """SELECT id,response_json
             FROM provider_calls
            WHERE operation_id=? AND status='OK' AND response_json IS NOT NULL
            ORDER BY id DESC LIMIT 10""",
        (operation_id,),
    ).fetchall()
    for row in rows:
        try:
            envelope = json.loads(row["response_json"])
            raw = str(envelope["choices"][0]["message"]["content"] or "")
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError):
            continue
        for payload in model_gateway._json_candidates(raw):
            try:
                normalized_payload = normalize_screenplay_scene_creative_payload(
                    payload,
                    scene_plans=scene_plans,
                    blueprint=blueprint,
                )
                draft = ScreenplaySceneShardCreativeIR.model_validate(
                    normalized_payload
                )
                shard = compile_screenplay_scene_shard_draft(
                    draft,
                    episode_no=episode_no,
                    plan=plan,
                    scene_plans=scene_plans,
                    scene_input_contracts=scene_input_contracts,
                )
            except (TypeError, ValueError, ValidationError):
                continue
            errors = validate_screenplay_scene_shard(
                shard,
                plan=plan,
                scene_plans=scene_plans,
                scene_input_contracts=scene_input_contracts,
                identity_keys=identity_keys,
                front_matter_ids=front_matter_ids,
            )
            if not errors:
                return draft, {
                    "outcome": "validated_provider_recovery",
                    "provider_call_id": int(row["id"]),
                    "local_recovery": True,
                    "validation_errors": [],
                }
    return None


def _namespace_shard_scene_keys(
    shard: ScreenplaySceneShardIR,
) -> list[IRScene]:
    return [
        IRScene.model_validate(scene.model_dump(mode="json"))
        for scene in shard.scenes
    ]


def merge_screenplay_scene_shards(
    *,
    envelope: ScreenplayEnvelopeIR,
    identities: list[IRIdentity],
    plans: list[ScreenplaySceneShardPlan],
    shards: list[ScreenplaySceneShardIR],
    scene_input_contracts: dict[
        str, list[ScreenplaySceneInputContract]
    ],
    blueprint: NarrativeBlueprint,
    source_text: str,
) -> ScreenplayGenerationIR:
    errors: list[str] = []
    expected_ownership_hash = _source_ownership_hash(blueprint)
    by_id = {shard.shard_id: shard for shard in shards}
    if len(by_id) != len(shards):
        errors.append("shard_id 必须全局唯一")
    if set(by_id) != {plan.shard_id for plan in plans}:
        errors.append("validated shards 与 shard plan 集合不一致")
    expected_blueprint_hash = blueprint_content_hash(blueprint)
    if envelope.blueprint_hash != expected_blueprint_hash:
        errors.append("Envelope blueprint_hash 不匹配")
    for plan in plans:
        if plan.blueprint_hash != expected_blueprint_hash:
            errors.append(f"{plan.shard_id} blueprint_hash 不匹配")
        if plan.source_scene_owners != blueprint.source_scene_owners:
            errors.append(f"{plan.shard_id} source owner 合同与 Blueprint 不一致")
        if plan.source_ownership_hash != expected_ownership_hash:
            errors.append(f"{plan.shard_id} source_ownership_hash 不匹配")
    expected_scenes = [plan.key for plan in blueprint.scene_plans]
    merged_scenes: list[IRScene] = []
    consumed: list[str] = []
    scene_plan_map = {plan.key: plan for plan in blueprint.scene_plans}
    identity_keys = {identity.key for identity in identities}
    segments = index_source_segments(source_text)
    for plan_index, plan in enumerate(plans):
        shard = by_id.get(plan.shard_id)
        if shard is None:
            continue
        shard_errors = validate_screenplay_scene_shard(
            shard,
            plan=plan,
            scene_plans=scene_plan_map,
            scene_input_contracts=scene_input_contracts.get(
                plan.shard_id, []
            ),
            identity_keys=identity_keys,
        )
        errors.extend(shard_errors)
        if plan_index and plan.boundary_state_in != plans[plan_index - 1].boundary_state_out:
            errors.append(f"{plan.shard_id} boundary state 与前一 shard 不闭合")
        if shard_errors:
            continue
        merged_scenes.extend(_namespace_shard_scene_keys(shard))
        consumed.extend(shard.consumed_source_ids)
    if [scene.key for scene in merged_scenes] != expected_scenes:
        errors.append("合并后 scene 顺序与 Blueprint 不一致")
    required_ids = [segment.segment_id for segment in segments]
    picture_source_ids = [
        source_id
        for source_id in required_ids
        if (
            blueprint.source_semantics.get(source_id) is not None
            and blueprint.source_semantics[source_id].projection_policy
            == "picture"
        )
    ]
    audit_only_source_ids = [
        source_id
        for source_id in required_ids
        if (
            blueprint.source_semantics.get(source_id) is not None
            and blueprint.source_semantics[source_id].projection_policy
            == "audit_only"
        )
    ]
    missing_semantics = [
        source_id
        for source_id in required_ids
        if source_id not in blueprint.source_semantics
    ]
    if missing_semantics:
        errors.append(
            "Blueprint 来源语义漏掉 SRC：" + ",".join(missing_semantics)
        )
    annotated_audit_source_ids = [
        source_id
        for annotation in blueprint.source_audit_annotations
        for source_id in annotation.source_segment_ids
    ]
    if annotated_audit_source_ids != audit_only_source_ids:
        errors.append(
            "Blueprint source_audit_annotations 未精确覆盖 audit-only SRC"
        )
    missing = [
        source_id
        for source_id in picture_source_ids
        if source_id not in consumed
    ]
    if missing:
        errors.append("合并 IR 未覆盖 picture SRC：" + ",".join(missing))
    leaked_audit_sources = [
        source_id
        for source_id in audit_only_source_ids
        if source_id in consumed
    ]
    if leaked_audit_sources:
        errors.append(
            "audit-only SRC 不得进入创作 unit："
            + ",".join(leaked_audit_sources)
        )
    source_order = {source_id: index for index, source_id in enumerate(required_ids)}
    first_owned = []
    already: set[str] = set()
    actual_source_owners: dict[str, str] = {}
    for scene in merged_scenes:
        for unit in scene.units:
            for source_id in unit.source_segment_ids:
                expected_owner = blueprint.source_scene_owners.get(source_id)
                if expected_owner != scene.key:
                    errors.append(
                        f"{source_id} 唯一归属 "
                        f"{expected_owner or '未定义'}，"
                        f"不得由 {scene.key} 消费"
                    )
                previous_owner = actual_source_owners.get(source_id)
                if previous_owner is None:
                    actual_source_owners[source_id] = scene.key
                elif previous_owner != scene.key:
                    errors.append(
                        f"{source_id} 被 {previous_owner} 与 "
                        f"{scene.key} 跨场重复消费"
                    )
                if source_id in source_order and source_id not in already:
                    already.add(source_id)
                    first_owned.append(source_order[source_id])
    if first_owned != sorted(first_owned):
        errors.append("来源首次所有权顺序不单调")
    if errors:
        raise ScreenplaySceneMergeError(errors)
    merged_ir = ScreenplayGenerationIR(
        format_version=IR_VERSION,
        episode_no=envelope.episode_no,
        metadata=envelope.metadata.to_ir(),
        identities=identities,
        coverage=[
            IRCoverageGroup(
                source_segment_ids=[source_id],
                disposition="audit_only",
                projection_policy="audit_only",
                reason="来源旁文本仅保留完整审计，不参与画面投影",
            )
            for annotation in blueprint.source_audit_annotations
            for source_id in annotation.source_segment_ids
        ],
        scenes=merged_scenes,
        experience=envelope.experience.to_ir(),
        source_scene_owners=dict(blueprint.source_scene_owners),
        source_semantics={
            source_id: semantics.model_dump(mode="json")
            for source_id, semantics in blueprint.source_semantics.items()
        },
        source_audit_annotations=list(
            blueprint.source_audit_annotations
        ),
        scene_derivations=[
            relation.model_dump(mode="json")
            for relation in blueprint.scene_derivations
        ],
        source_ownership_hash=expected_ownership_hash,
    )
    audit_authority_errors = screenplay_ir_source_audit_contract_errors(
        merged_ir.model_dump(mode="json"),
        expected_source_audit_annotations=list(
            blueprint.source_audit_annotations
        ),
    )
    if audit_authority_errors:
        raise ScreenplaySceneMergeError(audit_authority_errors)
    return merged_ir


def _latest_validated_artifact(
    *,
    episode_id: str,
    artifact_type: str,
    predicate: Callable[[dict[str, Any]], bool],
) -> dict[str, Any] | None:
    rows = get_conn().execute(
        """SELECT id,type,scope_type,scope_id,status,content_json,content_hash,
                  parent_artifact_ids_json,contract_version,prompt_version,
                  model_snapshot_json
             FROM artifacts
            WHERE scope_type='episode' AND scope_id=? AND type=?
              AND status='validated'
            ORDER BY created_at DESC LIMIT 100""",
        (episode_id, artifact_type),
    ).fetchall()
    for row in rows:
        try:
            content = json.loads(row["content_json"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if (
            not str(row["content_hash"] or "")
            or str(row["content_hash"])
            != evidence_repository.content_hash(content)
        ):
            continue
        if predicate(content):
            return {**dict(row), "content": content}
    return None


def _raw_parent_artifact(
    artifact: dict[str, Any],
) -> dict[str, Any] | None:
    parent_ids = _artifact_parent_ids(artifact)
    if parent_ids is None or len(parent_ids) != 1:
        return None
    row = get_conn().execute(
        "SELECT * FROM artifacts WHERE id=?",
        (next(iter(parent_ids)),),
    ).fetchone()
    return dict(row) if row else None
