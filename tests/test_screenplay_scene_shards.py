from __future__ import annotations

from copy import deepcopy

import pytest
from pydantic import ValidationError

from app.narrative_blueprint import NarrativeBlueprint, derive_blueprint_scene_plans
from app.screenplay_ir import IRIdentity, IRScene, IRSceneUnit
from app.screenplay_scene_shards import (
    SCREENPLAY_SCENE_SHARD_VERSION,
    ScreenplayEnvelopeExperience,
    ScreenplayEnvelopeIR,
    ScreenplayEnvelopeMetadata,
    ScreenplaySceneMergeError,
    ScreenplaySceneShardIR,
    UnresolvedParticipant,
    blueprint_content_hash,
    build_screenplay_scene_shard_plans,
    merge_screenplay_scene_shards,
    validate_screenplay_scene_shard,
)
from app.source_excerpt import index_source_segments


SOURCE = "甲推门进入。\n\n乙接过钥匙并回答。"


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
                resulting_state=f"完成 {source_id}",
            ))
        scenes.append(IRScene(
            key=scene_plan.key,
            scene_heading=scene_plan.scene_heading,
            story_function="交付来源",
            summary="交付来源",
            entry_state=scene_plan.previous_scene_exit_state,
            exit_state=scene_plan.exit_state,
            units=units,
        ))
    return ScreenplaySceneShardIR(
        episode_no=1,
        shard_id=plan.shard_id,
        scene_plan_keys=plan.scene_plan_keys,
        scenes=scenes,
        consumed_source_ids=consumed,
        source_hash=plan.source_hash,
        boundary_hash=plan.boundary_hash,
        blueprint_hash=plan.blueprint_hash,
        identity_registry_hash=plan.identity_registry_hash,
    )


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


def test_validated_shards_merge_in_blueprint_order_with_global_namespaces() -> None:
    blueprint = _blueprint(split_domain=True)
    plans = build_screenplay_scene_shard_plans(
        blueprint,
        source_text=SOURCE,
        identity_registry_hash="identity-hash",
    )
    shards = [_shard(plan, blueprint) for plan in plans]
    merged = merge_screenplay_scene_shards(
        envelope=_envelope(blueprint),
        identities=_identities(),
        plans=plans,
        shards=shards,
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


def test_scene_shard_rejects_source_boundary_and_unresolved_identity() -> None:
    blueprint = _blueprint(split_domain=True)
    plans = build_screenplay_scene_shard_plans(
        blueprint,
        source_text=SOURCE,
        identity_registry_hash="identity-hash",
    )
    shard = _shard(plans[0], blueprint)
    shard.scenes[0].units[0].source_segment_ids = ["SRC9999"]
    shard.unresolved_participants = [UnresolvedParticipant(
        source_label="陌生人",
        source_segment_ids=["SRC0001"],
        scene_key="bp-sc001",
    )]
    errors = validate_screenplay_scene_shard(
        shard,
        plan=plans[0],
        scene_plans={item.key: item for item in blueprint.scene_plans},
        identity_keys={"narrator"},
    )
    assert any("来源越界" in error for error in errors)
    assert any("未冻结参与者" in error for error in errors)


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
    with pytest.raises(ScreenplaySceneMergeError) as caught:
        merge_screenplay_scene_shards(
            envelope=_envelope(blueprint),
            identities=_identities(),
            plans=plans,
            shards=shards,
            blueprint=blueprint,
            source_text=SOURCE,
        )
    assert "boundary_hash" in str(caught.value)
    assert "未覆盖非标题 SRC" in str(caught.value)


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
    assert SCREENPLAY_SCENE_SHARD_VERSION == "screenplay-scene-shard.v1"
