from __future__ import annotations

import asyncio
from copy import deepcopy
import json
from pathlib import Path
import uuid

import pytest
from pydantic import ValidationError

from app import db
from app.harness import model_gateway
from app.narrative_blueprint import NarrativeBlueprint, derive_blueprint_scene_plans
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
    ScreenplayEnvelopeExperience,
    ScreenplayEnvelopeIR,
    ScreenplayEnvelopeMetadata,
    ScreenplaySceneMergeError,
    ScreenplaySceneShardError,
    ScreenplaySceneShardOwnershipLost,
    ScreenplaySceneShardIR,
    UnresolvedParticipant,
    blueprint_content_hash,
    build_frozen_identity_registry,
    build_screenplay_scene_input_contract_set,
    build_screenplay_scene_input_contracts,
    build_screenplay_scene_shard_plans,
    generate_screenplay_envelope,
    generate_screenplay_scene_shards,
    merge_screenplay_scene_shards,
    normalize_screenplay_scene_shard,
    normalize_screenplay_scene_shard_payload,
    validate_screenplay_scene_shard,
)
from app.source_excerpt import index_source_segments


SOURCE = "甲推门进入。\n\n乙接过钥匙并回答。"
ERR_20260810_REPLAY = (
    Path(__file__).parent
    / "fixtures"
    / "screenplay_scene_shard_err_20260810_2e8f0a.json"
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


def _shard(plan, blueprint: NarrativeBlueprint) -> ScreenplaySceneShardIR:
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
    shard = _shard(plans[0], blueprint)
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
    shard = _shard(plan, blueprint)
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
    registry = [{
        "identity_key": "narrator",
        "canonical_name": "旁白",
        "source_labels": ["旁白"],
    }]
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
    assert normalized.scenes[0].character_keys == ["narrator"]
    assert normalized.scenes[0].units[0].actor_keys == ["narrator"]
    assert normalized.scenes[0].units[0].target_keys == []
    assert normalized.scenes[0].units[0].onscreen_entity_keys == ["narrator"]
    assert normalized.scenes[0].units[0].text == "原文实际口播"
    assert "SRC_TITLE" not in normalized.consumed_source_ids
    assert validate_screenplay_scene_shard(
        normalized,
        plan=plan,
        scene_plans={item.key: item for item in blueprint.scene_plans},
        scene_input_contracts=scene_input_contracts,
        identity_keys={"narrator"},
    ) == []


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
    assert SCREENPLAY_SCENE_SHARD_VERSION == "screenplay-scene-shard.v4"


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
        return _shard(plan, blueprint)

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
        return _shard(plan, blueprint)

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
    scene_input_contracts = _contracts(plans, blueprint)

    async def fake_structured(messages, **kwargs):
        meta = kwargs["call_meta"]
        prompt_key = str(meta["stage_key"] + ":" + meta.get("shard_id", ""))
        prompts[prompt_key] = messages[0]["content"]
        operation_ids[prompt_key] = kwargs["operation_id"]
        if kwargs.get("output_schema"):
            output_schemas[prompt_key] = kwargs["output_schema"]
        if kwargs.get("repair_context"):
            repair_contexts[prompt_key] = json.loads(kwargs["repair_context"])
        if meta["stage_key"] == "screenplay_envelope":
            return _envelope(blueprint)
        plan = next(item for item in plans if item.shard_id == meta["shard_id"])
        return _shard(plan, blueprint)

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
    assert "根对象必须是完整 ScreenplaySceneShardIR" in first_prompt
    assert "绝不能把单个 scene、unit、数组或解释文字作为根输出" in first_prompt
    assert "dialogue.text 与 dialogue.source_text 必须填写同一段逐字原文对白" in first_prompt
    assert "禁止生成 unresolved_* 占位 ID" in first_prompt
    assert "逐场输入合同（来源正文不得跨 scene_plan_key 使用）" in first_prompt
    assert (
        f'"contract_version":"{SCREENPLAY_SCENE_INPUT_VERSION}"'
        in first_prompt
    )
    assert "owned_source" not in repair_contexts["screenplay_scene_shards:SS001"]
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
    ]["$defs"]["ScreenplaySceneShardUnit"]
    assert "participant_deliveries" in unit_schema["required"]
    assert delivery_contract["evidence_schema"] == output_schemas[
        "screenplay_scene_shards:SS001"
    ]["$defs"]["IRActionParticipantDelivery"]
    assert operation_ids["screenplay_scene_shards:SS001"].startswith(
        "screenplay.scene-shard:screenplay-scene-shard.v4:"
        f"{SCREENPLAY_SCENE_INPUT_VERSION}:"
    )
    assert SCREENPLAY_SCENE_INPUT_VERSION == "screenplay-scene-input.v4"
    assert "甲推门进入。" in first_prompt
    assert "乙接过钥匙并回答。" not in first_prompt
    assert "乙接过钥匙并回答。" in second_prompt
    assert "甲推门进入。" not in second_prompt


def test_generation_and_semantic_retry_reject_cross_scene_frozen_identity(
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
    semantic_attempts: list[int] = []

    async def local_model_response(messages, **kwargs):
        prompts.append(messages[0]["content"])
        semantic_attempts.append(int(kwargs["call_meta"]["semantic_attempt"]))
        return shard.model_dump_json()

    monkeypatch.setattr(model_gateway, "chat", local_model_response)
    scene_input_contracts = _contracts(plans, blueprint, registry)

    with pytest.raises(ScreenplaySceneShardError, match="逐场参与者合同"):
        asyncio.run(generate_screenplay_scene_shards(
            episode={"id": "ep-cross-scene-participant", "episode_no": 1},
            source_text=SOURCE,
            blueprint=blueprint,
            identity_registry=registry,
            identities=identities,
            plans=plans,
            scene_input_contracts=scene_input_contracts,
        ))

    assert semantic_attempts == [0, 1]
    assert "逐场参与者合同" in prompts[1]
    assert "character_keys 违反逐场参与者合同" in prompts[1]
    assert "speaker_key 违反逐场参与者合同" in prompts[1]
    assert "onscreen_entity_keys 违反逐场参与者合同" in prompts[1]
    assert "actor/target 违反逐场参与者合同" in prompts[1]
    assert '"action_participant_delivery_contract"' in prompts[0]
    assert '"action_participant_delivery_contract"' in prompts[1]
    assert IR_VERSION in prompts[0]
    assert IR_VERSION in prompts[1]
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
