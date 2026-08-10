from __future__ import annotations

import asyncio
from copy import deepcopy
import json
from pathlib import Path
import uuid

import pytest
from pydantic import ValidationError

from app import db, errors as app_errors
from app import screenplay_scene_shards as scene_shards_module
from app.harness import model_gateway
from app.narrative_blueprint import (
    BlueprintScenePlan,
    NarrativeBlueprint,
    NarrativeNode,
    NarrativeParticipantEvidence,
    derive_blueprint_scene_plans,
)
from app.evidence import repository as evidence_repository
from app.harness.types import EvidenceArtifact
from app.schemas import Bible, World
from app.screenplay_ir import (
    IR_VERSION,
    IRActionParticipantDelivery,
    IRIdentity,
    IRScene,
    IRSceneUnit,
)
from app.screenplay_scene_shards import (
    SCREENPLAY_SCENE_INPUT_VERSION,
    SCREENPLAY_SCENE_SHARD_VERSION,
    ScreenplaySceneShardCreativeIR,
    ScreenplaySceneShardCreativeUnit,
    ScreenplayEnvelopeExperience,
    ScreenplayEnvelopeIR,
    ScreenplayEnvelopeMetadata,
    ScreenplaySceneMergeError,
    ScreenplaySceneInputContract,
    ScreenplaySceneParticipantBinding,
    ScreenplaySceneShardError,
    ScreenplaySceneShardOwnershipLost,
    ScreenplaySceneShardIR,
    ScreenplaySceneShardPlan,
    ScreenplaySceneSourceSegment,
    UnresolvedParticipant,
    blueprint_content_hash,
    build_screenplay_scene_shard_repair_schema,
    build_frozen_identity_registry,
    build_screenplay_scene_input_contract_set,
    build_screenplay_scene_input_contracts,
    build_screenplay_scene_shard_plans,
    generate_screenplay_envelope,
    generate_screenplay_scene_shards,
    merge_screenplay_scene_shards,
    normalize_screenplay_scene_shard,
    normalize_screenplay_scene_shard_payload,
    screenplay_scene_identity_scaffold_hash,
    validate_screenplay_scene_shard,
)
from app.source_excerpt import index_source_segments


SOURCE = "甲推门进入。\n\n乙接过钥匙并回答。"
ERR_20260810_REPLAY = (
    Path(__file__).parent
    / "fixtures"
    / "screenplay_scene_shard_err_20260810_2e8f0a.json"
)
ERR_20260810_B66DDA_REPLAY = (
    Path(__file__).parent
    / "fixtures"
    / "screenplay_scene_shard_err_20260810_b66dda.json"
)
ERR_20260810_48009F_REPLAY = (
    Path(__file__).parent
    / "fixtures"
    / "screenplay_scene_shard_err_20260810_48009f.json"
)
SS004_REPLAY_INPUT = (
    Path(__file__).parent
    / "fixtures"
    / "screenplay_scene_shard_ss004.json"
)
ERR_20260810_533AC9_REPLAY = (
    Path(__file__).parent
    / "fixtures"
    / "screenplay_scene_shard_err_20260810_533ac9.json"
)


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "screenplay-scene-shards.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()


def _blueprint(*, split_domain: bool = True) -> NarrativeBlueprint:
    value = NarrativeBlueprint.model_validate({
        "episode_no": 1,
        "nodes": [
            {
                "key": "n1",
                "source_segment_ids": ["SRC0001"],
                "summary": "甲推门进入",
                "temporal_domain_key": "present",
                "time_label": "日",
                "time_relation": "episode_start",
                "location_key": "door",
                "location_label": "门口",
                "participants": [],
                "action_logic": "甲推门进入",
                "scene_boundary_before": True,
            },
            {
                "key": "n2",
                "source_segment_ids": ["SRC0002"],
                "summary": "乙接过钥匙并回答",
                "temporal_domain_key": "later" if split_domain else "present",
                "time_label": "稍后" if split_domain else "日",
                "time_relation": "elapsed" if split_domain else "continuous",
                "location_key": "room",
                "location_label": "室内",
                "participants": [],
                "action_logic": "乙接过钥匙并回答",
                "scene_boundary_before": True,
            },
        ],
    })
    derive_blueprint_scene_plans(value)
    return value


def _identities() -> list[IRIdentity]:
    return [IRIdentity(
        key="narrator",
        display_name="旁白",
        authority_id="narrator:narrator",
        kind="narrator",
        visual_policy="offscreen_only",
        asset_requirement="forbidden",
        role_type="narrator",
    )]


def _shard(
    plan,
    blueprint: NarrativeBlueprint,
    identity_registry: list[dict] | None = None,
) -> ScreenplaySceneShardIR:
    scene_map = {scene.key: scene for scene in blueprint.scene_plans}
    scenes: list[IRScene] = []
    consumed: list[str] = []
    for scene_key in plan.scene_plan_keys:
        scene_plan = scene_map[scene_key]
        units = []
        for source_id in scene_plan.source_segment_ids:
            consumed.append(source_id)
            units.append(IRSceneUnit(
                kind="action",
                text=f"交付 {source_id}",
                event_key="local-event-1",
                source_segment_ids=[source_id],
                actor_keys=[],
                target_keys=[],
                onscreen_entity_keys=[],
                participant_deliveries=[],
                resulting_state=f"完成 {source_id}",
            ))
        scenes.append(IRScene(
            key=scene_plan.key,
            scene_heading=scene_plan.scene_heading,
            story_function="完整交付本场来源",
            summary="交付来源",
            entry_state=scene_plan.previous_scene_exit_state,
            exit_state=scene_plan.exit_state,
            units=units,
        ))
    contracts = _contracts(
        [plan],
        blueprint,
        identity_registry,
    )[plan.shard_id]
    return ScreenplaySceneShardIR.model_validate({
        "episode_no": 1,
        "shard_id": plan.shard_id,
        "scene_plan_keys": plan.scene_plan_keys,
        "scenes": [scene.model_dump(mode="json") for scene in scenes],
        "consumed_source_ids": consumed,
        "source_hash": plan.source_hash,
        "boundary_hash": plan.boundary_hash,
        "blueprint_hash": plan.blueprint_hash,
        "identity_registry_hash": plan.identity_registry_hash,
        "source_ownership_hash": plan.source_ownership_hash,
        "identity_scaffold_hash": (
            screenplay_scene_identity_scaffold_hash(contracts)
        ),
    })


def _creative_shard(
    plan,
    blueprint: NarrativeBlueprint,
) -> ScreenplaySceneShardCreativeIR:
    scene_map = {scene.key: scene for scene in blueprint.scene_plans}
    return ScreenplaySceneShardCreativeIR.model_validate({
        "scenes": [
            {
                "scene_plan_key": scene_key,
                "story_function": "完整交付本场来源",
                "summary": "交付来源",
                "units": [
                    {
                        "kind": "action",
                        "text": f"交付 {source_id}",
                        "source_segment_ids": [source_id],
                        "resulting_state": f"完成 {source_id}",
                    }
                    for source_id in scene_map[
                        scene_key
                    ].source_segment_ids
                ],
            }
            for scene_key in plan.scene_plan_keys
        ],
    })


def _envelope(blueprint: NarrativeBlueprint) -> ScreenplayEnvelopeIR:
    return ScreenplayEnvelopeIR(
        episode_no=1,
        metadata=ScreenplayEnvelopeMetadata(title="测试"),
        experience=ScreenplayEnvelopeExperience(
            director_objective="清楚交付",
            satisfaction_criteria="来源完整",
        ),
        blueprint_hash=blueprint_content_hash(blueprint),
        identity_registry_hash="identity-hash",
    )


def _contracts(
    plans,
    blueprint: NarrativeBlueprint,
    identity_registry: list[dict] | None = None,
):
    return build_screenplay_scene_input_contract_set(
        plans=plans,
        blueprint=blueprint,
        source_text=SOURCE,
        identity_registry=identity_registry or [],
    )


def _participant_case(
    *,
    shared_identity: bool = False,
) -> tuple[
    NarrativeBlueprint,
    list,
    list[dict],
    list[IRIdentity],
    ScreenplaySceneShardIR,
]:
    blueprint = _blueprint(split_domain=False)
    first_participants = ["甲"]
    second_participants = ["乙"]
    if shared_identity:
        first_participants.append("见证人")
        second_participants.append("见证人")
    blueprint.nodes[0].participants = first_participants
    blueprint.nodes[1].participants = second_participants
    derive_blueprint_scene_plans(blueprint)
    registry = [
        {
            "identity_key": "person_a",
            "authority_id": "bible:甲",
            "canonical_name": "甲",
            "source_labels": ["甲"],
        },
        {
            "identity_key": "person_b",
            "authority_id": "bible:乙",
            "canonical_name": "乙",
            "source_labels": ["乙"],
        },
    ]
    if shared_identity:
        registry.append({
            "identity_key": "person_shared",
            "authority_id": "bible:见证人",
            "canonical_name": "见证人",
            "source_labels": ["见证人"],
        })
    identities = [
        IRIdentity(
            key=item["identity_key"],
            display_name=item["canonical_name"],
            authority_id=item["authority_id"],
            source_names=item["source_labels"],
            kind="named_character",
            visual_policy="canonical",
            asset_requirement="required",
            role_type="named_character",
        )
        for item in registry
    ]
    plans = build_screenplay_scene_shard_plans(
        blueprint,
        source_text=SOURCE,
        identity_registry_hash="identity-hash",
    )
    assert len(plans) == 1
    shard = _shard(plans[0], blueprint, registry)
    for index, scene in enumerate(shard.scenes, start=1):
        scene.units[0].event_key = f"local-event-{index}"
    return blueprint, plans, registry, identities, shard


def test_scene_shard_grouping_is_deterministic_and_respects_domains() -> None:
    blueprint = _blueprint(split_domain=True)
    first = build_screenplay_scene_shard_plans(
        blueprint,
        source_text=SOURCE,
        identity_registry_hash="identity-hash",
    )
    second = build_screenplay_scene_shard_plans(
        deepcopy(blueprint),
        source_text=SOURCE,
        identity_registry_hash="identity-hash",
    )
    assert [item.model_dump() for item in first] == [item.model_dump() for item in second]
    assert [item.shard_id for item in first] == ["SS001", "SS002"]
    assert [item.scene_plan_keys for item in first] == [["bp-sc001"], ["bp-sc002"]]


def test_scene_input_contracts_align_source_and_frozen_participants_per_scene() -> None:
    blueprint = _blueprint(split_domain=False)
    blueprint.nodes[0].participants = ["甲"]
    blueprint.nodes[1].participants = ["乙"]
    derive_blueprint_scene_plans(blueprint)
    plan = build_screenplay_scene_shard_plans(
        blueprint,
        source_text=SOURCE,
        identity_registry_hash="identity-hash",
    )[0]
    assert plan.scene_plan_keys == ["bp-sc001", "bp-sc002"]
    registry = [
        {
            "identity_key": "person_a",
            "authority_id": "bible:甲",
            "canonical_name": "甲",
            "source_labels": ["甲"],
        },
        {
            "identity_key": "person_b",
            "authority_id": "bible:乙",
            "canonical_name": "乙",
            "source_labels": ["乙"],
        },
    ]

    contracts = build_screenplay_scene_input_contracts(
        plan=plan,
        scene_plans=blueprint.scene_plans,
        source_by_id={
            segment.segment_id: segment.text
            for segment in index_source_segments(SOURCE)
        },
        identity_registry=registry,
    )

    assert [contract.scene_plan_key for contract in contracts] == [
        "bp-sc001",
        "bp-sc002",
    ]
    assert contracts[0].source_segment_ids == ["SRC0001"]
    assert contracts[0].source_segments[0].model_dump() == {
        "source_segment_id": "SRC0001",
        "text": "甲推门进入。",
    }
    assert contracts[0].participant_bindings[0].model_dump() == {
        "blueprint_key": "甲",
        "identity_key": "person_a",
    }
    delivery_contract = contracts[0].action_participant_delivery_contract
    assert delivery_contract.contract_version == IR_VERSION
    assert delivery_contract.unit_field_required is True
    assert delivery_contract.offscreen_relation_requires_evidence is True
    assert delivery_contract.observable_claim_required is True
    assert delivery_contract.perceivable_channel_required is True
    assert delivery_contract.evidence_schema == (
        IRActionParticipantDelivery.model_json_schema()
    )
    assert contracts[1].source_segment_ids == ["SRC0002"]
    assert contracts[1].participant_bindings[0].identity_key == "person_b"
    assert plan.source_scene_owners == blueprint.source_scene_owners
    assert contracts[0].source_scene_owners == blueprint.source_scene_owners
    assert contracts[1].source_scene_owners == blueprint.source_scene_owners
    assert any(
        relation.relation_type == "scene_transition"
        and relation.source_scene_plan_key == "bp-sc001"
        and relation.target_scene_plan_key == "bp-sc002"
        for relation in contracts[1].derived_relations
    )


def test_scene_input_contract_rejects_unfrozen_blueprint_participant() -> None:
    blueprint = _blueprint(split_domain=False)
    blueprint.nodes[0].participants = ["未冻结来客"]
    derive_blueprint_scene_plans(blueprint)
    plan = build_screenplay_scene_shard_plans(
        blueprint,
        source_text=SOURCE,
        identity_registry_hash="identity-hash",
    )[0]

    with pytest.raises(
        ScreenplaySceneShardError,
        match="bp-sc001 Blueprint participant 未冻结：未冻结来客",
    ):
        build_screenplay_scene_input_contracts(
            plan=plan,
            scene_plans=blueprint.scene_plans,
            source_by_id={
                segment.segment_id: segment.text
                for segment in index_source_segments(SOURCE)
            },
            identity_registry=[],
        )


def test_frozen_functional_identity_has_a_visible_contextual_anchor() -> None:
    identities, _registry, _registry_hash = build_frozen_identity_registry(
        Bible(characters=[], world=World(visual_style_canonical="测试")),
        [{
            "source_label": "邮差",
            "canonical_name": "邮差",
            "resolution": "functional_identity",
            "identity_group": "current-1:F1",
        }],
    )

    functional = next(
        item for item in identities if item.role_type == "functional_character"
    )
    assert functional.visual_policy == "contextual"
    assert functional.asset_requirement == "optional"
    assert functional.visual_canonical
    assert "邮差" in functional.visual_canonical


def test_frozen_identity_registry_rejects_conflicting_canonical_names() -> None:
    resolutions = [
        {
            "source_label": "门口的人",
            "canonical_name": "甲",
            "resolution": "functional_identity",
            "authority_id": "functional:same",
        },
        {
            "source_label": "屋内的人",
            "canonical_name": "乙",
            "resolution": "functional_identity",
            "authority_id": "functional:same",
        },
    ]

    with pytest.raises(ValueError, match="functional:same.*多个 canonical_name"):
        build_frozen_identity_registry(
            Bible(characters=[], world=World(visual_style_canonical="测试")),
            resolutions,
        )


def test_validated_shards_merge_in_blueprint_order_with_global_namespaces() -> None:
    blueprint = _blueprint(split_domain=True)
    plans = build_screenplay_scene_shard_plans(
        blueprint,
        source_text=SOURCE,
        identity_registry_hash="identity-hash",
    )
    shards = [_shard(plan, blueprint) for plan in plans]
    scene_input_contracts = _contracts(plans, blueprint)
    merged = merge_screenplay_scene_shards(
        envelope=_envelope(blueprint),
        identities=_identities(),
        plans=plans,
        shards=shards,
        scene_input_contracts=scene_input_contracts,
        blueprint=blueprint,
        source_text=SOURCE,
    )
    assert [scene.key for scene in merged.scenes] == ["bp-sc001", "bp-sc002"]
    event_keys = [scene.units[0].event_key for scene in merged.scenes]
    assert event_keys == [
        "ss001_bp_sc001_local-event-1",
        "ss002_bp_sc002_local-event-1",
    ]
    assert {source for scene in merged.scenes for unit in scene.units for source in unit.source_segment_ids} == {
        segment.segment_id for segment in index_source_segments(SOURCE)
    }
    assert merged.source_scene_owners == blueprint.source_scene_owners
    assert merged.scene_derivations == [
        relation.model_dump(mode="json")
        for relation in blueprint.scene_derivations
    ]
    assert merged.source_ownership_hash == plans[0].source_ownership_hash


def test_merge_rejects_source_consumed_by_multiple_scenes() -> None:
    blueprint = _blueprint(split_domain=True)
    plans = build_screenplay_scene_shard_plans(
        blueprint,
        source_text=SOURCE,
        identity_registry_hash="identity-hash",
    )
    shards = [_shard(plan, blueprint) for plan in plans]
    scene_input_contracts = _contracts(plans, blueprint)

    duplicated_source_id = "SRC0001"
    second_scene_plan = blueprint.scene_plans[1]
    second_scene_plan.source_segment_ids = [
        duplicated_source_id,
        *second_scene_plan.source_segment_ids,
    ]
    plans[1].source_segment_ids = [
        duplicated_source_id,
        *plans[1].source_segment_ids,
    ]
    second_contract = scene_input_contracts[plans[1].shard_id][0]
    second_contract.source_segment_ids = [
        duplicated_source_id,
        *second_contract.source_segment_ids,
    ]
    second_contract.source_segments = [
        scene_input_contracts[plans[0].shard_id][0].source_segments[0],
        *second_contract.source_segments,
    ]
    shards[1].scenes[0].units[0].source_segment_ids = [
        duplicated_source_id,
        "SRC0002",
    ]
    shards[1].consumed_source_ids = [duplicated_source_id, "SRC0002"]

    with pytest.raises(
        ScreenplaySceneMergeError,
        match=r"SRC0001.*bp-sc001.*bp-sc002",
    ):
        merge_screenplay_scene_shards(
            envelope=_envelope(blueprint),
            identities=_identities(),
            plans=plans,
            shards=shards,
            scene_input_contracts=scene_input_contracts,
            blueprint=blueprint,
            source_text=SOURCE,
        )


def test_merge_does_not_require_front_matter_in_scene_units(
    monkeypatch,
) -> None:
    blueprint = _blueprint(split_domain=True)
    plans = build_screenplay_scene_shard_plans(
        blueprint,
        source_text=SOURCE,
        identity_registry_hash="identity-hash",
    )
    shards = [_shard(plan, blueprint) for plan in plans]
    scene_input_contracts = _contracts(plans, blueprint)
    front_matter_id = plans[0].source_segment_ids[0]
    shards[0].scenes[0].units = []
    shards[0].consumed_source_ids = []
    monkeypatch.setattr(
        "app.screenplay_scene_shards.structural_front_matter_ids",
        lambda _segments: {front_matter_id},
    )

    merged = merge_screenplay_scene_shards(
        envelope=_envelope(blueprint),
        identities=_identities(),
        plans=plans,
        shards=shards,
        scene_input_contracts=scene_input_contracts,
        blueprint=blueprint,
        source_text=SOURCE,
    )

    assert merged.scenes[0].units == []
    assert merged.scenes[1].units


def test_scene_shard_rejects_source_boundary_and_unresolved_identity() -> None:
    blueprint = _blueprint(split_domain=True)
    plans = build_screenplay_scene_shard_plans(
        blueprint,
        source_text=SOURCE,
        identity_registry_hash="identity-hash",
    )
    shard = _shard(plans[0], blueprint)
    shard.scenes[0].units[0].source_segment_ids = ["SRC0002"]
    shard.unresolved_participants = [UnresolvedParticipant(
        source_label="陌生人",
        source_segment_ids=["SRC0001"],
        scene_key="bp-sc001",
    )]
    scene_input_contracts = _contracts(plans, blueprint)[plans[0].shard_id]
    normalize_screenplay_scene_shard(
        shard,
        episode_no=1,
        plan=plans[0],
        scene_plans={item.key: item for item in blueprint.scene_plans},
        scene_input_contracts=scene_input_contracts,
    )
    assert shard.scenes[0].units[0].source_segment_ids == ["SRC0002"]
    errors = validate_screenplay_scene_shard(
        shard,
        plan=plans[0],
        scene_plans={item.key: item for item in blueprint.scene_plans},
        scene_input_contracts=scene_input_contracts,
        identity_keys={"narrator"},
    )
    assert any("来源唯一归属冲突" in error for error in errors)
    assert any(
        "owner=bp-sc002" in error and "consumer=bp-sc001" in error
        for error in errors
    )
    assert any("未冻结参与者" in error for error in errors)


def test_scene_shard_rejects_unit_without_source_when_scene_coverage_is_complete() -> None:
    blueprint = _blueprint(split_domain=True)
    plan = build_screenplay_scene_shard_plans(
        blueprint,
        source_text=SOURCE,
        identity_registry_hash="identity-hash",
    )[0]
    shard = _shard(plan, blueprint)
    source_less = shard.scenes[0].units[0].model_copy(deep=True)
    source_less.event_key = "source-less-event"
    source_less.source_segment_ids = []
    shard.scenes[0].units.append(source_less)
    scene_input_contracts = _contracts([plan], blueprint)[plan.shard_id]

    errors = validate_screenplay_scene_shard(
        shard,
        plan=plan,
        scene_plans={item.key: item for item in blueprint.scene_plans},
        scene_input_contracts=scene_input_contracts,
        identity_keys={"narrator"},
    )

    assert any(
        "units[1] 必须声明 source_segment_ids" in error
        for error in errors
    )


def test_scene_shard_normalizes_program_fields_and_identity_relations() -> None:
    blueprint = _blueprint(split_domain=True)
    blueprint.nodes[0].participants = ["旁白"]
    derive_blueprint_scene_plans(blueprint)
    plan = build_screenplay_scene_shard_plans(
        blueprint,
        source_text=SOURCE,
        identity_registry_hash="identity-hash",
    )[0]
    registry = [{
        "identity_key": "narrator",
        "canonical_name": "旁白",
        "source_labels": ["旁白"],
    }]
    shard = _shard(plan, blueprint, registry)
    shard.episode_no = 99
    shard.shard_id = "invented"
    shard.scene_plan_keys = ["invented-scene"]
    shard.source_hash = "invented-source"
    shard.consumed_source_ids = ["SRC_TITLE", *shard.consumed_source_ids]
    shard.scenes[0].character_keys = ["旁白"]
    unit = shard.scenes[0].units[0]
    unit.kind = "dialogue"
    unit.text = "说明性对白摘要"
    unit.source_text = "原文实际口播"
    unit.speaker_key = "旁白"
    unit.actor_keys = ["旁白"]
    unit.target_keys = []
    unit.onscreen_entity_keys = ["旁白"]
    scene_input_contracts = _contracts(
        [plan],
        blueprint,
        registry,
    )[plan.shard_id]

    normalized = normalize_screenplay_scene_shard(
        shard,
        episode_no=1,
        plan=plan,
        scene_plans={item.key: item for item in blueprint.scene_plans},
        scene_input_contracts=scene_input_contracts,
    )

    assert normalized.episode_no == 1
    assert normalized.shard_id == plan.shard_id
    assert normalized.scene_plan_keys == plan.scene_plan_keys
    assert normalized.source_hash == plan.source_hash
    assert normalized.scenes[0].character_keys == ["旁白"]
    assert normalized.scenes[0].units[0].actor_keys == ["旁白"]
    assert normalized.scenes[0].units[0].target_keys == []
    assert normalized.scenes[0].units[0].onscreen_entity_keys == ["旁白"]
    assert normalized.scenes[0].units[0].text == "原文实际口播"
    assert "SRC_TITLE" not in normalized.consumed_source_ids
    errors = validate_screenplay_scene_shard(
        normalized,
        plan=plan,
        scene_plans={item.key: item for item in blueprint.scene_plans},
        scene_input_contracts=scene_input_contracts,
        identity_keys={"narrator"},
    )
    assert any("character_keys 违反逐场参与者合同" in error for error in errors)
    assert any("speaker_key 违反逐场参与者合同" in error for error in errors)


def test_scene_shard_payload_derives_generic_story_function_from_blueprint() -> None:
    blueprint = _blueprint(split_domain=True)
    plan = build_screenplay_scene_shard_plans(
        blueprint,
        source_text=SOURCE,
        identity_registry_hash="identity-hash",
    )[0]
    payload = _shard(plan, blueprint).model_dump(mode="json")
    payload["episode_no"] = 99
    payload["shard_id"] = "invented"
    payload["scenes"][0]["story_function"] = "setup"

    normalized = normalize_screenplay_scene_shard_payload(
        payload,
        episode_no=1,
        plan=plan,
        scene_plans={item.key: item for item in blueprint.scene_plans},
        blueprint=blueprint,
    )

    shard = ScreenplaySceneShardIR.model_validate(normalized)
    assert shard.episode_no == 1
    assert shard.shard_id == plan.shard_id
    assert shard.scenes[0].story_function.startswith("推进本场事件：")
    assert blueprint.nodes[0].summary in shard.scenes[0].story_function


def test_scene_shard_does_not_require_front_matter_in_units() -> None:
    blueprint = _blueprint(split_domain=True)
    plan = build_screenplay_scene_shard_plans(
        blueprint,
        source_text=SOURCE,
        identity_registry_hash="identity-hash",
    )[0]
    shard = _shard(plan, blueprint)
    front_matter_id = plan.source_segment_ids[0]
    shard.scenes[0].units = []
    shard.consumed_source_ids = []
    scene_plans = {
        item.key: item for item in blueprint.scene_plans
    }
    scene_input_contracts = _contracts([plan], blueprint)[plan.shard_id]

    without_exclusion = validate_screenplay_scene_shard(
        shard,
        plan=plan,
        scene_plans=scene_plans,
        scene_input_contracts=scene_input_contracts,
        identity_keys={"narrator"},
    )
    with_exclusion = validate_screenplay_scene_shard(
        shard,
        plan=plan,
        scene_plans=scene_plans,
        scene_input_contracts=scene_input_contracts,
        identity_keys={"narrator"},
        front_matter_ids={front_matter_id},
    )

    assert any(front_matter_id in error for error in without_exclusion)
    assert with_exclusion == []


def test_scene_shard_rejects_short_story_function() -> None:
    blueprint = _blueprint(split_domain=True)
    plan = build_screenplay_scene_shard_plans(
        blueprint,
        source_text=SOURCE,
        identity_registry_hash="identity-hash",
    )[0]
    shard = _shard(plan, blueprint)
    shard.scenes[0].story_function = "turn"
    scene_input_contracts = _contracts([plan], blueprint)[plan.shard_id]

    errors = validate_screenplay_scene_shard(
        shard,
        plan=plan,
        scene_plans={item.key: item for item in blueprint.scene_plans},
        scene_input_contracts=scene_input_contracts,
        identity_keys={"narrator"},
    )

    assert any("story_function 必须完整说明本场戏剧功能" in error for error in errors)


def test_ir_scene_schema_rejects_short_story_function() -> None:
    with pytest.raises(ValidationError, match="story_function"):
        IRScene(
            key="bp-sc001",
            scene_heading="【场1】日 / 门口",
            story_function="setup",
            summary="甲推门进入",
        )


def test_merge_fails_closed_on_boundary_hash_or_missing_source() -> None:
    blueprint = _blueprint(split_domain=True)
    plans = build_screenplay_scene_shard_plans(
        blueprint,
        source_text=SOURCE,
        identity_registry_hash="identity-hash",
    )
    shards = [_shard(plan, blueprint) for plan in plans]
    shards[1].boundary_hash = "stale"
    shards[1].scenes[0].units = []
    shards[1].consumed_source_ids = []
    scene_input_contracts = _contracts(plans, blueprint)
    with pytest.raises(ScreenplaySceneMergeError) as caught:
        merge_screenplay_scene_shards(
            envelope=_envelope(blueprint),
            identities=_identities(),
            plans=plans,
            shards=shards,
            scene_input_contracts=scene_input_contracts,
            blueprint=blueprint,
            source_text=SOURCE,
        )
    assert "boundary_hash" in str(caught.value)
    assert "未覆盖非标题 SRC" in str(caught.value)


def test_merge_rejects_source_less_unit_when_other_units_cover_all_sources() -> None:
    blueprint = _blueprint(split_domain=True)
    plans = build_screenplay_scene_shard_plans(
        blueprint,
        source_text=SOURCE,
        identity_registry_hash="identity-hash",
    )
    shards = [_shard(plan, blueprint) for plan in plans]
    source_less = shards[0].scenes[0].units[0].model_copy(deep=True)
    source_less.event_key = "source-less-event"
    source_less.source_segment_ids = []
    shards[0].scenes[0].units.append(source_less)
    scene_input_contracts = _contracts(plans, blueprint)

    with pytest.raises(
        ScreenplaySceneMergeError,
        match=r"bp-sc001\.units\[1\] 必须声明 source_segment_ids",
    ):
        merge_screenplay_scene_shards(
            envelope=_envelope(blueprint),
            identities=_identities(),
            plans=plans,
            shards=shards,
            scene_input_contracts=scene_input_contracts,
            blueprint=blueprint,
            source_text=SOURCE,
        )


def test_scene_shard_contract_version_is_not_silently_upgraded() -> None:
    blueprint = _blueprint(split_domain=True)
    plan = build_screenplay_scene_shard_plans(
        blueprint,
        source_text=SOURCE,
        identity_registry_hash="identity-hash",
    )[0]
    payload = _shard(plan, blueprint).model_dump(mode="json")
    payload["contract_version"] = "screenplay-scene-shard.v0"
    with pytest.raises(ValidationError):
        ScreenplaySceneShardIR.model_validate(payload)
    assert SCREENPLAY_SCENE_SHARD_VERSION == "screenplay-scene-shard.v5"


def test_scene_shard_schema_requires_explicit_participant_deliveries() -> None:
    blueprint = _blueprint(split_domain=True)
    plan = build_screenplay_scene_shard_plans(
        blueprint,
        source_text=SOURCE,
        identity_registry_hash="identity-hash",
    )[0]
    payload = _shard(plan, blueprint).model_dump(mode="json")
    payload["scenes"][0]["units"][0].pop("participant_deliveries")

    with pytest.raises(ValidationError, match="participant_deliveries"):
        ScreenplaySceneShardIR.model_validate(payload)

    schema = ScreenplaySceneShardIR.model_json_schema()
    unit_schema = schema["$defs"]["ScreenplaySceneShardUnit"]
    assert "participant_deliveries" in unit_schema["required"]


def test_err_20260810_original_provider_response_fails_at_explicit_unit_schema() -> None:
    replay = json.loads(ERR_20260810_REPLAY.read_text(encoding="utf-8"))

    assert replay["error_id"] == "ERR-20260810-2e8f0a"
    assert replay["blueprint"]["contract_version"] == (
        "screenplay-narrative-blueprint.v3"
    )
    assert replay["envelope"]["contract_version"] == "screenplay-envelope.v1"
    assert replay["initial_response"]["blueprint_hash"] == (
        replay["envelope"]["blueprint_hash"]
    )
    assert replay["initial_response"]["scene_plan_keys"] == [
        item["key"] for item in replay["blueprint"]["scene_plans"]
    ]
    participant_evidence = {
        (
            item["identity_key"],
            tuple(item["source_segment_ids"]),
        ): item["usage"]
        for item in replay["blueprint"]["participant_evidence"]
    }
    assert participant_evidence[
        ("王有材", ("SRC0033",))
    ] == "voice"
    assert participant_evidence[
        ("大青山被困少年甲", ("SRC0031",))
    ] == "voice"
    with pytest.raises(ValidationError, match="participant_deliveries"):
        ScreenplaySceneShardIR.model_validate(replay["initial_response"])

    assert {
        (item["scene_key"], item["unit_index"])
        for item in replay["failed_repair_deliveries"]
    } == {
        ("bp-sc006", 6),
        ("bp-sc007", 1),
        ("bp-sc008", 0),
        ("bp-sc008", 3),
        ("bp-sc008", 7),
    }
    assert all(
        not (
            item["audible"]
            or item["visible_effect"]
            or item["visible_reaction"]
        )
        for item in replay["failed_repair_deliveries"]
    )


def test_err_20260810_provider_recovery_rejects_original_response(
    monkeypatch,
) -> None:
    replay = json.loads(ERR_20260810_REPLAY.read_text(encoding="utf-8"))
    blueprint = _blueprint(split_domain=True)
    plan = build_screenplay_scene_shard_plans(
        blueprint,
        source_text=SOURCE,
        identity_registry_hash="identity-hash",
    )[0]
    episode_id = "ep-err-20260810-provider-replay"
    operation_id = (
        f"screenplay.scene-shard:{SCREENPLAY_SCENE_SHARD_VERSION}:"
        f"{SCREENPLAY_SCENE_INPUT_VERSION}:"
        f"{episode_id}:{plan.shard_id}:{plan.source_hash}:"
        f"{plan.boundary_hash}:{plan.blueprint_hash}:"
        f"{plan.identity_registry_hash}:{plan.source_ownership_hash}"
    )
    db.log_provider_call(
        "chat",
        "fixture-provider",
        "OK",
        200,
        1,
        response_json={
            "choices": [{
                "message": {
                    "content": json.dumps(
                        replay["initial_response"],
                        ensure_ascii=False,
                    ),
                },
            }],
        },
        operation_id=operation_id,
    )
    provider_calls = 0

    async def fake_structured(*_args, **_kwargs):
        nonlocal provider_calls
        provider_calls += 1
        return _creative_shard(plan, blueprint)

    monkeypatch.setattr(
        "app.screenplay_scene_shards.model_gateway.chat_structured",
        fake_structured,
    )
    contracts = _contracts([plan], blueprint)

    shards, _artifact_ids, rows = asyncio.run(
        generate_screenplay_scene_shards(
            episode={"id": episode_id, "episode_no": 1},
            source_text=SOURCE,
            blueprint=blueprint,
            identity_registry=[],
            identities=_identities(),
            plans=[plan],
            scene_input_contracts=contracts,
        )
    )

    assert provider_calls == 1
    assert len(shards) == 1
    assert rows[0]["status"] == "validated"
    raw = evidence_repository.latest_artifact(
        "screenplay_scene_shard_raw",
        "episode",
        episode_id,
    )
    assert raw is not None
    assert all(
        attempt.get("outcome") != "validated_provider_recovery"
        for attempt in raw["content"]["attempts"]
    )


def _unit_delivery_contracts(schema: dict) -> dict[tuple[str, int], dict]:
    return {
        (item["scene_key"], item["unit_index"]): item
        for item in schema["x-unit-delivery-contracts"]
    }


def _normalize_b66dda_payload(response: dict) -> dict:
    payload = deepcopy(response)
    for scene in payload["scenes"]:
        if len(str(scene.get("story_function") or "").strip()) < 6:
            scene["story_function"] = "推进本场事件：" + scene["summary"]
    return payload


def _b66dda_shard(response: dict) -> ScreenplaySceneShardIR:
    return ScreenplaySceneShardIR.model_validate(
        _normalize_b66dda_payload(response)
    )


def _ss004_replay_validation_context() -> tuple[
    ScreenplaySceneShardPlan,
    dict[str, BlueprintScenePlan],
    list[ScreenplaySceneInputContract],
    set[str],
]:
    replay_input = json.loads(SS004_REPLAY_INPUT.read_text(encoding="utf-8"))
    scene_plans = {
        item["key"]: BlueprintScenePlan.model_validate(item)
        for item in replay_input["scene_plans"]
    }
    hashes = replay_input["hashes"]
    plan = ScreenplaySceneShardPlan(
        shard_id="SS004",
        scene_plan_keys=list(scene_plans),
        source_segment_ids=[
            source_id
            for scene_plan in scene_plans.values()
            for source_id in scene_plan.source_segment_ids
        ],
        source_scene_owners=replay_input["source_scene_owners"],
        source_ownership_hash=hashes["source_ownership_hash"],
        estimated_units=replay_input["recorded_request"]["estimated_units"],
        estimated_output_chars=replay_input["recorded_request"][
            "estimated_output_chars"
        ],
        source_hash=hashes["source_hash"],
        boundary_hash=hashes["boundary_hash"],
        blueprint_hash=hashes["blueprint_hash"],
        identity_registry_hash=hashes["identity_registry_hash"],
    )
    contracts = [
        ScreenplaySceneInputContract(
            scene_plan_key=item["scene_plan_key"],
            node_keys=item["node_keys"],
            source_segment_ids=[
                segment["source_segment_id"]
                for segment in item["source_segments"]
            ],
            source_segments=[
                ScreenplaySceneSourceSegment.model_validate(segment)
                for segment in item["source_segments"]
            ],
            participant_bindings=[
                ScreenplaySceneParticipantBinding(
                    blueprint_key=blueprint_key,
                    identity_key=identity_key,
                )
                for blueprint_key, identity_key in item["participant_bindings"]
            ],
            source_scene_owners=replay_input["source_scene_owners"],
            source_ownership_hash=hashes["source_ownership_hash"],
        )
        for item in replay_input["scene_inputs"]
    ]
    identity_keys = {
        item["identity_key"] for item in replay_input["identity_registry"]
    }
    return plan, scene_plans, contracts, identity_keys


def _unit_delivery_array_schema(
    schema: dict,
    scene_index: int,
    unit_index: int,
) -> dict:
    scene_constraint = schema["properties"]["scenes"]["prefixItems"][
        scene_index
    ]["allOf"][1]
    unit_constraint = scene_constraint["properties"]["units"]["prefixItems"][
        unit_index
    ]["allOf"][1]
    return unit_constraint["properties"]["participant_deliveries"]


def _scene_contract_schema(schema: dict, scene_key: str) -> dict:
    for item in schema["properties"]["scenes"]["prefixItems"]:
        constraint = item["allOf"][1]
        if constraint["properties"]["key"]["const"] == scene_key:
            return constraint
    raise AssertionError(f"missing schema for {scene_key}")


def _unit_contract_schema(
    schema: dict,
    scene_key: str,
    unit_index: int | None = None,
) -> dict:
    units_schema = _scene_contract_schema(schema, scene_key)["properties"][
        "units"
    ]
    if unit_index is None:
        return units_schema["items"]["allOf"][1]
    return units_schema["prefixItems"][unit_index]["allOf"][1]


def _err_48009f_candidate(
    provider_response: dict,
) -> ScreenplaySceneShardIR:
    replay_units = {
        item["unit_index"]: deepcopy(item["unit"])
        for item in provider_response["units"]
        if item["scene_key"] == "bp-sc014"
    }
    units = []
    for unit_index in range(max(replay_units) + 1):
        unit = deepcopy(replay_units.get(unit_index, replay_units[0]))
        if unit_index not in replay_units:
            unit["event_key"] = f"replay-placeholder-{unit_index}"
            unit["source_segment_ids"] = ["SRC0055"]
        units.append(unit)
    return ScreenplaySceneShardIR.model_validate({
        "episode_no": 1,
        "shard_id": "SS004",
        "scene_plan_keys": ["bp-sc014"],
        "scenes": [{
            "key": "bp-sc014",
            "scene_heading": "日·外·靠山宗半山腰青石空地",
            "story_function": "推进靠山宗杂役分配事件",
            "summary": "绿袍执事分配新入门弟子",
            "units": units,
        }],
    })


def test_err_20260810_48009f_replay_uses_contract_canonical_keys_without_mutation() -> None:
    replay = json.loads(ERR_20260810_48009F_REPLAY.read_text(encoding="utf-8"))
    _plan, _scene_plans, contracts, _identity_keys = (
        _ss004_replay_validation_context()
    )

    assert replay["error_id"] == "ERR-20260810-48009f"
    assert replay["run_id"] == "run_82ac46b576af"
    assert replay["replay_scope"] == (
        "unaltered contract-relevant units projected from each complete "
        "provider response"
    )
    assert [
        item["provider_call_id"] for item in replay["provider_responses"]
    ] == [60900, 60901]
    assert [
        item["semantic_attempt"] for item in replay["provider_responses"]
    ] == [0, 1]
    assert [
        item["response_sha256"] for item in replay["provider_responses"]
    ] == [
        "04aee9ca0916cdf52452e630ed58357cca80ad33d7772c684851eb09d975928f",
        "589c0dfb4f7df3bc8ab5165958bc0b87c8e3963d428dc1d54723d9e601ffab29",
    ]

    schema = build_screenplay_scene_shard_repair_schema(
        scene_input_contracts=contracts,
    )
    unit_properties = schema["$defs"][
        "ScreenplaySceneShardCreativeUnit"
    ]["properties"]
    assert not {
        "actor_keys",
        "target_keys",
        "speaker_key",
        "onscreen_entity_keys",
        "participant_deliveries",
    }.intersection(unit_properties)
    for provider_response in replay["provider_responses"]:
        unit = next(
            item["unit"]
            for item in provider_response["units"]
            if item["scene_key"] == "bp-sc014"
        )
        with pytest.raises(ValidationError, match="extra_forbidden"):
            ScreenplaySceneShardCreativeUnit.model_validate(unit)


def test_scene_contract_schema_is_shared_by_initial_and_semantic_attempts() -> None:
    replay = json.loads(ERR_20260810_48009F_REPLAY.read_text(encoding="utf-8"))
    _plan, _scene_plans, contracts, _identity_keys = (
        _ss004_replay_validation_context()
    )
    initial_schema = build_screenplay_scene_shard_repair_schema(
        scene_input_contracts=contracts,
    )
    candidate = _err_48009f_candidate(replay["provider_responses"][0])
    repair_schema = build_screenplay_scene_shard_repair_schema(
        candidate,
        scene_input_contracts=contracts,
    )

    assert initial_schema == repair_schema
    assert initial_schema["x-schema-purpose"] == (
        "creative-content-with-deterministic-identity-scaffold"
    )


def test_scene_contract_schema_preserves_relation_cardinality_boundaries() -> None:
    _replay, _plan, _scene_plans, contracts = (
        _ss004_533ac9_compile_context()
    )
    schema = build_screenplay_scene_shard_repair_schema(
        scene_input_contracts=contracts,
    )
    scene_14 = schema["properties"]["scenes"]["prefixItems"][0][
        "allOf"
    ][1]
    source_schema = scene_14["properties"]["units"]["items"]["allOf"][1][
        "properties"
    ]["source_segment_ids"]
    assert source_schema["minItems"] == 1
    assert source_schema["uniqueItems"] is True
    assert source_schema["items"]["enum"] == [
        "SRC0054",
        "SRC0055",
        "SRC0056",
        "SRC0057",
    ]


def test_scene_contract_schema_does_not_interchange_similar_bound_identities() -> None:
    replay = json.loads(ERR_20260810_B66DDA_REPLAY.read_text(encoding="utf-8"))
    for provider_response in replay["provider_responses"]:
        with pytest.raises(ValidationError, match="extra_forbidden"):
            ScreenplaySceneShardCreativeIR.model_validate(
                provider_response["response"]
            )


def test_repair_schema_derives_relations_visibility_and_evidence_per_unit() -> None:
    replay, _plan, _scene_plans, contracts = (
        _ss004_533ac9_compile_context()
    )
    draft = ScreenplaySceneShardCreativeIR.model_validate(
        replay["creative_response"]
    )
    initial = build_screenplay_scene_shard_repair_schema(
        scene_input_contracts=contracts,
    )
    repair = build_screenplay_scene_shard_repair_schema(
        draft,
        scene_input_contracts=contracts,
    )
    assert initial == repair
    assert "IRActionParticipantDelivery" not in initial["$defs"]


def test_repair_schema_allows_only_a_genuine_empty_delivery_set() -> None:
    _replay, _plan, _scene_plans, contracts = (
        _ss004_533ac9_compile_context()
    )
    schema = build_screenplay_scene_shard_repair_schema(
        scene_input_contracts=contracts,
    )
    unit_properties = schema["$defs"][
        "ScreenplaySceneShardCreativeUnit"
    ]["properties"]
    assert "participant_deliveries" not in unit_properties


def test_repair_schema_accepts_audible_offscreen_speaker_evidence() -> None:
    replay, plan, scene_plans, contracts = (
        _ss004_533ac9_compile_context()
    )
    shard = scene_shards_module.compile_screenplay_scene_shard_draft(
        ScreenplaySceneShardCreativeIR.model_validate(
            replay["creative_response"]
        ),
        episode_no=1,
        plan=plan,
        scene_plans=scene_plans,
        scene_input_contracts=contracts,
    )
    answer = shard.scenes[1].units[2]
    assert answer.speaker_key == "person_46e7e8b742ed"
    assert answer.participant_deliveries[0].audible is True


def test_err_20260810_b66dda_replays_60895_and_60897_without_runtime_access() -> None:
    replay = json.loads(ERR_20260810_B66DDA_REPLAY.read_text(encoding="utf-8"))

    assert replay["error_id"] == "ERR-20260810-b66dda"
    assert [
        item["provider_call_id"] for item in replay["provider_responses"]
    ] == [60895, 60897]
    assert [
        item["semantic_attempt"] for item in replay["provider_responses"]
    ] == [0, 1]

    for provider_response in replay["provider_responses"]:
        with pytest.raises(ValidationError, match="extra_forbidden"):
            ScreenplaySceneShardCreativeIR.model_validate(
                provider_response["response"]
            )


def test_err_20260810_b66dda_semantic_retry_uses_60895_bound_schema(
    monkeypatch,
) -> None:
    replay = json.loads(ERR_20260810_B66DDA_REPLAY.read_text(encoding="utf-8"))
    responses = [
        item["response"] for item in replay["provider_responses"]
    ]
    prompts: list[str] = []
    attempts: list[dict] = []
    _plan, _scene_plans, contracts, _identity_keys = (
        _ss004_replay_validation_context()
    )

    async def fake_chat(messages, **_kwargs):
        prompts.append(messages[0]["content"])
        return json.dumps(
            responses[len(prompts) - 1],
            ensure_ascii=False,
        )

    monkeypatch.setattr(model_gateway, "chat", fake_chat)

    with pytest.raises(
        model_gateway.StructuredFormatError,
        match="结构化输出失败",
    ):
        asyncio.run(model_gateway.chat_structured(
            [{"role": "user", "content": "SS004 replay"}],
            model_type=ScreenplaySceneShardCreativeIR,
            validate=None,
            operation_id="test.ss004-b66dda-replay:v1",
            max_tokens=1024,
            format_retry_limit=1,
            semantic_retry_limit=1,
            output_schema=build_screenplay_scene_shard_repair_schema(
                scene_input_contracts=contracts,
            ),
            on_attempt=attempts.append,
        ))

    assert len(prompts) == 2
    assert [item["outcome"] for item in attempts] == [
        "format_error",
        "format_error",
    ]
    assert all(item["semantic_attempt"] == 0 for item in attempts)
    assert "actor_keys" in prompts[1]
    assert "Extra inputs are not permitted" in prompts[1]


@pytest.mark.parametrize(
    "channel",
    ["audible", "visible_effect", "visible_reaction"],
)
def test_validator_and_merge_accept_source_authored_offscreen_evidence(
    channel: str,
) -> None:
    blueprint, plans, registry, identities, shard = _participant_case()
    unit = shard.scenes[0].units[0]
    unit.actor_keys = ["person_a"]
    unit.onscreen_entity_keys = []
    unit.participant_deliveries = [
        IRActionParticipantDelivery(
            participant_key="person_a",
            observable_claim="画外动作通过本场可感知证据抵达观众",
            **{channel: True},
        )
    ]
    scene_input_contracts = _contracts(plans, blueprint, registry)

    errors = validate_screenplay_scene_shard(
        shard,
        plan=plans[0],
        scene_plans={item.key: item for item in blueprint.scene_plans},
        scene_input_contracts=scene_input_contracts[plans[0].shard_id],
        identity_keys={item.key for item in identities},
    )
    assert not any("参与者交付" in error for error in errors)

    merged = merge_screenplay_scene_shards(
        envelope=_envelope(blueprint),
        identities=identities,
        plans=plans,
        shards=[shard],
        scene_input_contracts=scene_input_contracts,
        blueprint=blueprint,
        source_text=SOURCE,
    )
    delivery = merged.scenes[0].units[0].participant_deliveries[0]
    assert delivery.participant_key == "person_a"
    assert getattr(delivery, channel) is True


def test_validator_and_merge_reject_offscreen_claim_without_perceivable_channel() -> None:
    blueprint, plans, registry, identities, shard = _participant_case()
    unit = shard.scenes[0].units[0]
    unit.actor_keys = ["person_a"]
    unit.onscreen_entity_keys = []
    unit.participant_deliveries = [
        IRActionParticipantDelivery(
            participant_key="person_a",
            observable_claim="只有文字声称，没有任何可听或可见证据",
        )
    ]
    scene_input_contracts = _contracts(plans, blueprint, registry)

    errors = validate_screenplay_scene_shard(
        shard,
        plan=plans[0],
        scene_plans={item.key: item for item in blueprint.scene_plans},
        scene_input_contracts=scene_input_contracts[plans[0].shard_id],
        identity_keys={item.key for item in identities},
    )
    assert any(
        "person_a 缺少结构化可感知证据" in error
        for error in errors
    )
    with pytest.raises(
        ScreenplaySceneMergeError,
        match="person_a 缺少结构化可感知证据",
    ):
        merge_screenplay_scene_shards(
            envelope=_envelope(blueprint),
            identities=identities,
            plans=plans,
            shards=[shard],
            scene_input_contracts=scene_input_contracts,
            blueprint=blueprint,
            source_text=SOURCE,
        )


def test_validator_and_merge_reject_participant_delivery_contract_drift() -> None:
    blueprint, plans, registry, identities, shard = _participant_case()
    scene_input_contracts = _contracts(plans, blueprint, registry)
    contract = scene_input_contracts[plans[0].shard_id][0]
    contract.action_participant_delivery_contract.evidence_schema = {
        "type": "object",
    }

    errors = validate_screenplay_scene_shard(
        shard,
        plan=plans[0],
        scene_plans={item.key: item for item in blueprint.scene_plans},
        scene_input_contracts=scene_input_contracts[plans[0].shard_id],
        identity_keys={item.key for item in identities},
    )
    assert any(
        f"action participant delivery 合同与 {IR_VERSION} 不一致"
        in error
        for error in errors
    )
    with pytest.raises(
        ScreenplaySceneMergeError,
        match="action participant delivery 合同",
    ):
        merge_screenplay_scene_shards(
            envelope=_envelope(blueprint),
            identities=identities,
            plans=plans,
            shards=[shard],
            scene_input_contracts=scene_input_contracts,
            blueprint=blueprint,
            source_text=SOURCE,
        )


def test_normalization_does_not_invent_offscreen_participant_evidence() -> None:
    blueprint, plans, registry, identities, shard = _participant_case()
    unit = shard.scenes[0].units[0]
    unit.target_keys = ["person_a"]
    unit.onscreen_entity_keys = []
    unit.participant_deliveries = []
    scene_input_contracts = _contracts(plans, blueprint, registry)

    normalize_screenplay_scene_shard(
        shard,
        episode_no=1,
        plan=plans[0],
        scene_plans={item.key: item for item in blueprint.scene_plans},
        scene_input_contracts=scene_input_contracts[plans[0].shard_id],
    )

    assert unit.participant_deliveries == []
    errors = validate_screenplay_scene_shard(
        shard,
        plan=plans[0],
        scene_plans={item.key: item for item in blueprint.scene_plans},
        scene_input_contracts=scene_input_contracts[plans[0].shard_id],
        identity_keys={item.key for item in identities},
    )
    assert any(
        "缺少 participant_deliveries" in error
        and "person_a" in error
        for error in errors
    )


def test_validated_scene_shard_is_reused_without_provider_call(monkeypatch) -> None:
    blueprint = _blueprint(split_domain=True)
    plans = build_screenplay_scene_shard_plans(
        blueprint,
        source_text=SOURCE,
        identity_registry_hash="identity-hash",
    )
    episode_id = "ep-scene-shard-cache-test"
    cached_ids: list[str] = []
    for plan in plans:
        shard = _shard(plan, blueprint)
        artifact = evidence_repository.create_artifact(EvidenceArtifact(
            type="screenplay_scene_shard",
            scope_type="episode",
            scope_id=episode_id,
            status="validated",
            trust_level="T1",
            content=shard.model_dump(mode="json"),
            contract_version=SCREENPLAY_SCENE_SHARD_VERSION,
        ))
        cached_ids.append(str(artifact["id"]))

    async def forbidden_chat(*_args, **_kwargs):
        raise AssertionError("validated shard must not be billed twice")

    monkeypatch.setattr(
        "app.screenplay_scene_shards.model_gateway.chat_structured",
        forbidden_chat,
    )
    scene_input_contracts = _contracts(plans, blueprint)
    shards, artifact_ids, rows = asyncio.run(generate_screenplay_scene_shards(
        episode={"id": episode_id, "episode_no": 1},
        source_text=SOURCE,
        blueprint=blueprint,
        identity_registry=[],
        identities=_identities(),
        plans=plans,
        scene_input_contracts=scene_input_contracts,
    ))
    assert len(shards) == len(plans)
    assert artifact_ids == cached_ids
    assert all(row["status"] == "validated" for row in rows)
    assert all(row["attempt"] == 0 and row["reused"] for row in rows)


def test_owner_change_after_provider_response_prevents_artifact_persist(monkeypatch) -> None:
    blueprint = _blueprint(split_domain=True)
    plan = build_screenplay_scene_shard_plans(
        blueprint,
        source_text=SOURCE,
        identity_registry_hash="identity-hash",
    )[0]
    episode_id = "ep-scene-shard-owner-fence-test"
    checks = 0

    def fake_owner_check(_episode_id: str) -> None:
        nonlocal checks
        checks += 1
        if checks >= 2:
            raise ScreenplaySceneShardOwnershipLost("owner changed")

    async def fake_structured(*_args, **_kwargs):
        return _creative_shard(plan, blueprint)

    monkeypatch.setattr(
        "app.screenplay_scene_shards._assert_episode_owner",
        fake_owner_check,
    )
    monkeypatch.setattr(
        "app.screenplay_scene_shards.model_gateway.chat_structured",
        fake_structured,
    )
    scene_input_contracts = _contracts([plan], blueprint)
    with pytest.raises(ScreenplaySceneShardError, match="owner changed"):
        asyncio.run(generate_screenplay_scene_shards(
            episode={"id": episode_id, "episode_no": 1},
            source_text=SOURCE,
            blueprint=blueprint,
            identity_registry=[],
            identities=_identities(),
            plans=[plan],
            scene_input_contracts=scene_input_contracts,
        ))
    assert evidence_repository.latest_artifact(
        "screenplay_scene_shard", "episode", episode_id,
    ) is None


def test_envelope_never_receives_full_source_and_shards_receive_only_owned_src(
    monkeypatch,
) -> None:
    blueprint = _blueprint(split_domain=True)
    plans = build_screenplay_scene_shard_plans(
        blueprint,
        source_text=SOURCE,
        identity_registry_hash="identity-hash",
    )
    prompts: dict[str, str] = {}
    repair_contexts: dict[str, dict] = {}
    operation_ids: dict[str, str] = {}
    output_schemas: dict[str, dict] = {}
    repair_schema_builders: dict[str, object] = {}
    scene_input_contracts = _contracts(plans, blueprint)

    async def fake_structured(messages, **kwargs):
        meta = kwargs["call_meta"]
        prompt_key = str(meta["stage_key"] + ":" + meta.get("shard_id", ""))
        prompts[prompt_key] = messages[0]["content"]
        operation_ids[prompt_key] = kwargs["operation_id"]
        if kwargs.get("output_schema"):
            output_schemas[prompt_key] = kwargs["output_schema"]
        if kwargs.get("repair_schema"):
            repair_schema_builders[prompt_key] = kwargs["repair_schema"]
        if kwargs.get("repair_context"):
            repair_contexts[prompt_key] = json.loads(kwargs["repair_context"])
        if meta["stage_key"] == "screenplay_envelope":
            return _envelope(blueprint)
        plan = next(item for item in plans if item.shard_id == meta["shard_id"])
        return _creative_shard(plan, blueprint)

    monkeypatch.setattr(
        "app.screenplay_scene_shards.model_gateway.chat_structured",
        fake_structured,
    )
    episode_id = f"ep-shard-prompt-scope-test-{uuid.uuid4()}"
    asyncio.run(generate_screenplay_envelope(
        episode={"id": episode_id, "episode_no": 1, "title": "测试"},
        blueprint=blueprint,
        identity_registry=[],
        identity_registry_hash="identity-hash",
    ))
    asyncio.run(generate_screenplay_scene_shards(
        episode={"id": episode_id, "episode_no": 1},
        source_text=SOURCE,
        blueprint=blueprint,
        identity_registry=[],
        identities=_identities(),
        plans=plans,
        scene_input_contracts=scene_input_contracts,
    ))
    envelope_prompt = prompts["screenplay_envelope:"]
    assert "甲推门进入。" not in envelope_prompt
    assert "乙接过钥匙并回答。" not in envelope_prompt
    first_prompt = prompts["screenplay_scene_shards:SS001"]
    second_prompt = prompts["screenplay_scene_shards:SS002"]
    assert "根对象必须是完整 ScreenplaySceneShardCreativeIR" in first_prompt
    assert "模型无权输出或修改这些身份字段" in first_prompt
    assert "dialogue.source_text 必须逐字一致" in first_prompt
    assert "participant_deliveries" in first_prompt
    assert "逐场输入合同（来源正文不得跨 scene_plan_key 使用）" in first_prompt
    assert (
        f'"contract_version":"{SCREENPLAY_SCENE_INPUT_VERSION}"'
        in first_prompt
    )
    assert "owned_source" not in repair_contexts["screenplay_scene_shards:SS001"]
    assert (
        "identity_registry"
        not in repair_contexts["screenplay_scene_shards:SS001"]
    )
    first_contracts = repair_contexts[
        "screenplay_scene_shards:SS001"
    ]["scene_input_contracts"]
    assert first_contracts[0]["scene_plan_key"] == "bp-sc001"
    assert first_contracts[0]["source_segment_ids"] == ["SRC0001"]
    assert first_contracts[0]["source_segments"] == [{
        "source_segment_id": "SRC0001",
        "text": "甲推门进入。",
    }]
    delivery_contract = first_contracts[0][
        "action_participant_delivery_contract"
    ]
    assert delivery_contract["contract_version"] == IR_VERSION
    assert delivery_contract["unit_field_required"] is True
    assert '"action_participant_delivery_contract"' in first_prompt
    unit_schema = output_schemas[
        "screenplay_scene_shards:SS001"
    ]["$defs"]["ScreenplaySceneShardCreativeUnit"]
    assert "participant_deliveries" not in unit_schema["properties"]
    assert "actor_keys" not in unit_schema["properties"]
    repair_schema_builder = repair_schema_builders[
        "screenplay_scene_shards:SS001"
    ]
    assert callable(repair_schema_builder)
    repair_schema = repair_schema_builder(
        _creative_shard(plans[0], blueprint)
    )
    assert repair_schema == output_schemas[
        "screenplay_scene_shards:SS001"
    ]
    assert repair_schema["x-schema-purpose"] == (
        "creative-content-with-deterministic-identity-scaffold"
    )
    assert operation_ids["screenplay_scene_shards:SS001"].startswith(
        "screenplay.scene-shard:screenplay-scene-shard.v5:"
        f"{SCREENPLAY_SCENE_INPUT_VERSION}:"
    )
    assert SCREENPLAY_SCENE_INPUT_VERSION == "screenplay-scene-input.v5"
    assert "甲推门进入。" in first_prompt
    assert "乙接过钥匙并回答。" not in first_prompt
    assert "乙接过钥匙并回答。" in second_prompt
    assert "甲推门进入。" not in second_prompt


def test_generation_rejects_identity_fields_as_explicit_format_error(
    monkeypatch,
) -> None:
    blueprint, plans, registry, identities, shard = _participant_case()
    first_scene = shard.scenes[0]
    first_scene.character_keys = ["person_b"]
    first_unit = first_scene.units[0]
    first_unit.actor_keys = ["person_b"]
    first_unit.target_keys = ["person_b"]
    first_unit.onscreen_entity_keys = ["person_b"]
    first_unit.speaker_key = "person_b"
    prompts: list[str] = []
    format_attempts: list[int] = []

    async def local_model_response(messages, **kwargs):
        prompts.append(messages[0]["content"])
        format_attempts.append(int(kwargs["call_meta"]["format_attempt"]))
        return shard.model_dump_json()

    monkeypatch.setattr(model_gateway, "chat", local_model_response)
    scene_input_contracts = _contracts(plans, blueprint, registry)

    with pytest.raises(ScreenplaySceneShardError, match="extra_forbidden"):
        asyncio.run(generate_screenplay_scene_shards(
            episode={"id": "ep-cross-scene-participant", "episode_no": 1},
            source_text=SOURCE,
            blueprint=blueprint,
            identity_registry=registry,
            identities=identities,
            plans=plans,
            scene_input_contracts=scene_input_contracts,
        ))

    assert format_attempts == [0, 1]
    assert "Extra inputs are not permitted" in prompts[1]
    assert "actor_keys" in prompts[1]
    assert "speaker_key" in prompts[1]
    assert "onscreen_entity_keys" in prompts[1]
    assert "participant_deliveries" in prompts[1]
    assert '"action_participant_delivery_contract"' in prompts[0]
    assert IR_VERSION in prompts[0]
    assert evidence_repository.latest_artifact(
        "screenplay_scene_shard",
        "episode",
        "ep-cross-scene-participant",
    ) is None


def test_merge_rejects_frozen_identity_owned_only_by_another_scene() -> None:
    blueprint, plans, registry, identities, shard = _participant_case()
    shard.scenes[0].units[0].actor_keys = ["person_b"]
    shard.scenes[0].units[0].onscreen_entity_keys = ["person_b"]
    scene_input_contracts = _contracts(plans, blueprint, registry)

    with pytest.raises(ScreenplaySceneMergeError, match="逐场参与者合同"):
        merge_screenplay_scene_shards(
            envelope=_envelope(blueprint),
            identities=identities,
            plans=plans,
            shards=[shard],
            scene_input_contracts=scene_input_contracts,
            blueprint=blueprint,
            source_text=SOURCE,
        )


def test_scene_contract_allows_identity_explicitly_shared_by_both_scenes() -> None:
    blueprint, plans, registry, identities, shard = _participant_case(
        shared_identity=True,
    )
    for scene in shard.scenes:
        scene.character_keys = ["person_shared"]
        scene.units[0].actor_keys = ["person_shared"]
        scene.units[0].target_keys = ["person_shared"]
        scene.units[0].onscreen_entity_keys = ["person_shared"]
    scene_input_contracts = _contracts(plans, blueprint, registry)

    merged = merge_screenplay_scene_shards(
        envelope=_envelope(blueprint),
        identities=identities,
        plans=plans,
        shards=[shard],
        scene_input_contracts=scene_input_contracts,
        blueprint=blueprint,
        source_text=SOURCE,
    )

    assert [scene.character_keys for scene in merged.scenes] == [
        ["person_shared"],
        ["person_shared"],
    ]
    assert [
        scene.units[0].actor_keys for scene in merged.scenes
    ] == [["person_shared"], ["person_shared"]]


def test_normalization_preserves_unbound_target_for_hard_gate() -> None:
    blueprint = _blueprint(split_domain=True)
    plan = build_screenplay_scene_shard_plans(
        blueprint,
        source_text=SOURCE,
        identity_registry_hash="identity-hash",
    )[0]
    shard = _shard(plan, blueprint)
    shard.scenes[0].units[0].target_keys = ["unbound-person"]
    scene_input_contracts = _contracts([plan], blueprint)[plan.shard_id]

    normalized = normalize_screenplay_scene_shard(
        shard,
        episode_no=1,
        plan=plan,
        scene_plans={item.key: item for item in blueprint.scene_plans},
        scene_input_contracts=scene_input_contracts,
    )

    assert normalized.scenes[0].units[0].target_keys == ["unbound-person"]
    errors = validate_screenplay_scene_shard(
        normalized,
        plan=plan,
        scene_plans={item.key: item for item in blueprint.scene_plans},
        scene_input_contracts=scene_input_contracts,
        identity_keys={"narrator"},
    )
    assert any(
        "actor/target 违反逐场参与者合同" in error
        and "unbound-person" in error
        for error in errors
    )


def _ss004_533ac9_compile_context():
    replay = json.loads(
        ERR_20260810_533AC9_REPLAY.read_text(encoding="utf-8")
    )
    replay_input = json.loads(SS004_REPLAY_INPUT.read_text(encoding="utf-8"))
    plan, all_scene_plans, _contracts, _identity_keys = (
        _ss004_replay_validation_context()
    )
    selected_keys = ["bp-sc014", "bp-sc015"]
    scene_plans = {
        key: all_scene_plans[key]
        for key in selected_keys
    }
    source_owners = {
        source_id: owner
        for source_id, owner in plan.source_scene_owners.items()
        if owner in selected_keys
    }
    selected_plan = plan.model_copy(update={
        "scene_plan_keys": selected_keys,
        "source_segment_ids": [
            source_id
            for key in selected_keys
            for source_id in scene_plans[key].source_segment_ids
        ],
        "source_scene_owners": source_owners,
    })
    source_by_id = {
        segment["source_segment_id"]: segment["text"]
        for item in replay_input["scene_inputs"]
        if item["scene_plan_key"] in selected_keys
        for segment in item["source_segments"]
    }
    nodes = []
    for index, evidence in enumerate(replay["action_evidence"]):
        participants = list(dict.fromkeys(
            item["identity_key"]
            for item in evidence["participant_evidence"]
        ))
        node = {
            "key": evidence["node_key"],
            "source_segment_ids": evidence["source_segment_ids"],
            "summary": f"回放动作证据 {index + 1}",
            "temporal_domain_key": "T002",
            "time_label": "入夜",
            "time_relation": "continuous",
            "location_key": "L004",
            "location_label": "宗门青石空地",
            "participants": participants,
            "participant_evidence": evidence["participant_evidence"],
            "action_logic": f"回放动作逻辑 {index + 1}",
        }
        if evidence["decision_actor_key"]:
            node["decision"] = {
                "actor_key": evidence["decision_actor_key"],
                "choice": f"回放决定 {index + 1}",
            }
        nodes.append(NarrativeNode.model_validate(node))
    contracts = build_screenplay_scene_input_contracts(
        plan=selected_plan,
        scene_plans=list(scene_plans.values()),
        source_by_id=source_by_id,
        identity_registry=replay_input["identity_registry"],
        blueprint_nodes=nodes,
    )
    return replay, selected_plan, scene_plans, contracts


def test_scene_shard_creative_schema_is_closed_and_rejects_identity_authority() -> None:
    replay, _plan, _scene_plans, contracts = (
        _ss004_533ac9_compile_context()
    )
    draft_type = getattr(
        scene_shards_module,
        "ScreenplaySceneShardCreativeIR",
        None,
    )
    assert draft_type is not None

    schema = build_screenplay_scene_shard_repair_schema(
        scene_input_contracts=contracts,
    )
    assert schema["additionalProperties"] is False
    for definition in schema["$defs"].values():
        if definition.get("type") == "object":
            assert definition["additionalProperties"] is False
    unit_schema = schema["$defs"]["ScreenplaySceneShardCreativeUnit"]
    assert not {
        "actor_keys",
        "target_keys",
        "speaker_key",
        "onscreen_entity_keys",
        "participant_deliveries",
    }.intersection(unit_schema["properties"])

    for provider_response in replay["provider_responses"]:
        unit = provider_response["identity_units"][0]
        with pytest.raises(ValidationError) as caught:
            scene_shards_module.ScreenplaySceneShardCreativeUnit.model_validate({
                "kind": "action",
                "text": "回放越权身份字段",
                **unit,
            })
        forbidden_locations = {
            error["loc"][0] for error in caught.value.errors()
            if error["type"] == "extra_forbidden"
        }
        assert {
            "actor_keys",
            "target_keys",
            "speaker_key",
            "onscreen_entity_keys",
            "participant_deliveries",
        } <= forbidden_locations


def test_err_533ac9_replay_compiles_identity_scaffold_without_unit_injection() -> None:
    replay, plan, scene_plans, contracts = (
        _ss004_533ac9_compile_context()
    )
    assert replay["error_id"] == "ERR-20260810-533ac9"
    assert [
        item["provider_call_id"]
        for item in replay["provider_responses"]
    ] == [60904, 60905]
    assert [
        item["semantic_attempt"]
        for item in replay["provider_responses"]
    ] == [0, 1]
    assert [
        item["response_sha256"]
        for item in replay["provider_responses"]
    ] == [
        "2a16517190ecd99ca74cc7af2376e202a2396e117728bce95c4256f2da3cffc7",
        "383a1c60c74a636a7ca10447f9fba46d70ddc94554b0a4aa42fc77e7a1b429a8",
    ]

    draft_type = getattr(
        scene_shards_module,
        "ScreenplaySceneShardCreativeIR",
        None,
    )
    compile_draft = getattr(
        scene_shards_module,
        "compile_screenplay_scene_shard_draft",
        None,
    )
    assert draft_type is not None
    assert callable(compile_draft)
    draft = draft_type.model_validate(replay["creative_response"])
    shard = compile_draft(
        draft,
        episode_no=1,
        plan=plan,
        scene_plans=scene_plans,
        scene_input_contracts=contracts,
    )

    round_trip = ScreenplaySceneShardIR.model_validate(
        shard.model_dump(mode="json")
    )
    assert round_trip == shard
    scene_14 = next(scene for scene in shard.scenes if scene.key == "bp-sc014")
    assignment_line = scene_14.units[4]
    assert assignment_line.target_keys == [
        "person_e79ecc6793f5",
        "person_32ce878a56e2",
    ]
    assert assignment_line.speaker_key == "person_46e7e8b742ed"
    assert assignment_line.onscreen_entity_keys == [
        "person_e79ecc6793f5",
        "person_32ce878a56e2",
    ]
    assert assignment_line.participant_deliveries[0].participant_key == (
        "person_46e7e8b742ed"
    )
    assert assignment_line.participant_deliveries[0].audible is True

    wrong_youth = "person_b9cd0397a07f"
    meng_hao = "person_b67de643afe6"
    for unit in scene_14.units[2:]:
        assert wrong_youth not in {
            *unit.actor_keys,
            *unit.target_keys,
            *unit.onscreen_entity_keys,
        }
        assert all(
            delivery.participant_key != meng_hao
            for delivery in unit.participant_deliveries
        )

    scene_15 = next(scene for scene in shard.scenes if scene.key == "bp-sc015")
    location_answer = scene_15.units[2]
    assert location_answer.speaker_key == "person_46e7e8b742ed"
    assert location_answer.onscreen_entity_keys == [meng_hao]
    assert [
        delivery.participant_key
        for delivery in location_answer.participant_deliveries
    ] == ["person_46e7e8b742ed"]
    assert location_answer.participant_deliveries[0].audible is True


def test_scene_shard_contract_fingerprint_is_upgraded() -> None:
    assert SCREENPLAY_SCENE_SHARD_VERSION == "screenplay-scene-shard.v5"
    assert SCREENPLAY_SCENE_INPUT_VERSION == "screenplay-scene-input.v5"


def test_scene_shard_error_has_generation_contract_classification() -> None:
    error = ScreenplaySceneShardError(
        "SS004",
        ["unit identity scaffold contract failed"],
    )
    assert app_errors.classify(error) == (
        "generation_contract",
        "GEN-CONTRACT",
    )
    record = app_errors.log_error(error)
    assert record.category == "generation_contract"
    assert record.code == "GEN-CONTRACT"
    row = db.get_conn().execute(
        "SELECT category,code FROM error_logs WHERE id=?",
        (record.error_id,),
    ).fetchone()
    assert row is not None
    assert (row["category"], row["code"]) == (
        "generation_contract",
        "GEN-CONTRACT",
    )


def test_scene_shard_unit_rejects_unknown_identity_authority_field() -> None:
    blueprint = _blueprint(split_domain=True)
    plan = build_screenplay_scene_shard_plans(
        blueprint,
        source_text=SOURCE,
        identity_registry_hash="identity-hash",
    )[0]
    unit = _shard(plan, blueprint).scenes[0].units[0]
    payload = unit.model_dump(mode="json")
    payload["actor"] = "person_outside_typed_contract"

    with pytest.raises(ValidationError, match="actor"):
        type(unit).model_validate(payload)


def test_compiled_identity_scaffold_round_trip_and_merge_rejects_drift() -> None:
    blueprint = _blueprint(split_domain=True)
    blueprint.nodes[0].participants = ["甲"]
    blueprint.nodes[0].participant_evidence = [NarrativeParticipantEvidence(
        identity_key="甲",
        source_segment_ids=["SRC0001"],
        usage="visible",
    )]
    blueprint.nodes[1].participants = ["乙"]
    blueprint.nodes[1].participant_evidence = [NarrativeParticipantEvidence(
        identity_key="乙",
        source_segment_ids=["SRC0002"],
        usage="visible",
    )]
    derive_blueprint_scene_plans(blueprint)
    registry = [
        {
            "identity_key": "person_a",
            "authority_id": "bible:甲",
            "canonical_name": "甲",
            "source_labels": ["甲"],
        },
        {
            "identity_key": "person_b",
            "authority_id": "bible:乙",
            "canonical_name": "乙",
            "source_labels": ["乙"],
        },
    ]
    identities = [
        IRIdentity(
            key=item["identity_key"],
            display_name=item["canonical_name"],
            authority_id=item["authority_id"],
            source_names=item["source_labels"],
            kind="named_character",
            visual_policy="canonical",
            asset_requirement="required",
            role_type="named_character",
        )
        for item in registry
    ]
    plans = build_screenplay_scene_shard_plans(
        blueprint,
        source_text=SOURCE,
        identity_registry_hash="identity-hash",
    )
    contracts = _contracts(plans, blueprint, registry)
    scene_plans = {
        scene.key: scene for scene in blueprint.scene_plans
    }
    shards = [
        scene_shards_module.compile_screenplay_scene_shard_draft(
            _creative_shard(plan, blueprint),
            episode_no=1,
            plan=plan,
            scene_plans=scene_plans,
            scene_input_contracts=contracts[plan.shard_id],
        )
        for plan in plans
    ]

    merged = merge_screenplay_scene_shards(
        envelope=_envelope(blueprint),
        identities=identities,
        plans=plans,
        shards=shards,
        scene_input_contracts=contracts,
        blueprint=blueprint,
        source_text=SOURCE,
    )
    assert [scene.units[0].actor_keys for scene in merged.scenes] == [
        ["person_a"],
        ["person_b"],
    ]

    shards[0].scenes[0].units[0].actor_keys = []
    with pytest.raises(
        ScreenplaySceneMergeError,
        match="identity scaffold drift",
    ):
        merge_screenplay_scene_shards(
            envelope=_envelope(blueprint),
            identities=identities,
            plans=plans,
            shards=shards,
            scene_input_contracts=contracts,
            blueprint=blueprint,
            source_text=SOURCE,
        )


def test_creative_provider_response_recovers_with_scaffold_fingerprint(
    monkeypatch,
) -> None:
    blueprint = _blueprint(split_domain=True)
    plan = build_screenplay_scene_shard_plans(
        blueprint,
        source_text=SOURCE,
        identity_registry_hash="identity-hash",
    )[0]
    contracts_by_shard = _contracts([plan], blueprint)
    contracts = contracts_by_shard[plan.shard_id]
    scaffold_hash = screenplay_scene_identity_scaffold_hash(contracts)
    episode_id = "ep-creative-provider-recovery"
    operation_id = (
        f"screenplay.scene-shard:{SCREENPLAY_SCENE_SHARD_VERSION}:"
        f"{SCREENPLAY_SCENE_INPUT_VERSION}:"
        f"{episode_id}:{plan.shard_id}:{plan.source_hash}:"
        f"{plan.boundary_hash}:{plan.blueprint_hash}:"
        f"{plan.identity_registry_hash}:{plan.source_ownership_hash}:"
        f"{scaffold_hash}"
    )
    db.log_provider_call(
        "chat",
        "fixture-provider",
        "OK",
        200,
        1,
        response_json={
            "choices": [{
                "message": {
                    "content": _creative_shard(
                        plan,
                        blueprint,
                    ).model_dump_json(),
                },
            }],
        },
        operation_id=operation_id,
    )

    async def forbidden_provider(*_args, **_kwargs):
        raise AssertionError("complete creative response must recover locally")

    monkeypatch.setattr(
        "app.screenplay_scene_shards.model_gateway.chat_structured",
        forbidden_provider,
    )
    shards, _artifact_ids, rows = asyncio.run(
        generate_screenplay_scene_shards(
            episode={"id": episode_id, "episode_no": 1},
            source_text=SOURCE,
            blueprint=blueprint,
            identity_registry=[],
            identities=_identities(),
            plans=[plan],
            scene_input_contracts=contracts_by_shard,
        )
    )

    assert shards[0].identity_scaffold_hash == scaffold_hash
    assert rows[0]["status"] == "validated"
    raw = evidence_repository.latest_artifact(
        "screenplay_scene_shard_raw",
        "episode",
        episode_id,
    )
    assert raw is not None
    assert raw["content"]["attempts"] == [{
        "outcome": "validated_provider_recovery",
        "provider_call_id": 1,
        "local_recovery": True,
        "validation_errors": [],
    }]
