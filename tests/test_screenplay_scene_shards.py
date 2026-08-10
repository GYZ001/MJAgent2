from __future__ import annotations

import asyncio
from copy import deepcopy
import uuid

import pytest
from pydantic import ValidationError

from app.narrative_blueprint import NarrativeBlueprint, derive_blueprint_scene_plans
from app.evidence import repository as evidence_repository
from app.harness.types import EvidenceArtifact
from app.schemas import Bible, World
from app.screenplay_ir import IRIdentity, IRScene, IRSceneUnit
from app.screenplay_scene_shards import (
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


def test_scene_shard_normalizes_program_fields_and_identity_relations() -> None:
    blueprint = _blueprint(split_domain=True)
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
    unit.target_keys = ["门板"]
    unit.onscreen_entity_keys = ["旁白"]

    normalized = normalize_screenplay_scene_shard(
        shard,
        episode_no=1,
        plan=plan,
        scene_plans={item.key: item for item in blueprint.scene_plans},
        identity_registry=[{
            "identity_key": "narrator",
            "canonical_name": "旁白",
            "source_labels": ["旁白"],
        }],
        identity_keys={"narrator"},
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
    scene_plan = blueprint.scene_plans[0].model_copy(deep=True)
    scene_plan.source_segment_ids = [
        "SRC_TITLE",
        *scene_plan.source_segment_ids,
    ]
    scene_plans = {
        **{item.key: item for item in blueprint.scene_plans},
        scene_plan.key: scene_plan,
    }

    without_exclusion = validate_screenplay_scene_shard(
        shard,
        plan=plan,
        scene_plans=scene_plans,
        identity_keys={"narrator"},
    )
    with_exclusion = validate_screenplay_scene_shard(
        shard,
        plan=plan,
        scene_plans=scene_plans,
        identity_keys={"narrator"},
        front_matter_ids={"SRC_TITLE"},
    )

    assert any("SRC_TITLE" in error for error in without_exclusion)
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

    errors = validate_screenplay_scene_shard(
        shard,
        plan=plan,
        scene_plans={item.key: item for item in blueprint.scene_plans},
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
    assert SCREENPLAY_SCENE_SHARD_VERSION == "screenplay-scene-shard.v2"


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
    shards, artifact_ids, rows = asyncio.run(generate_screenplay_scene_shards(
        episode={"id": episode_id, "episode_no": 1},
        source_text=SOURCE,
        blueprint=blueprint,
        identity_registry=[],
        identities=_identities(),
        plans=plans,
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
    with pytest.raises(ScreenplaySceneShardError, match="owner changed"):
        asyncio.run(generate_screenplay_scene_shards(
            episode={"id": episode_id, "episode_no": 1},
            source_text=SOURCE,
            blueprint=blueprint,
            identity_registry=[],
            identities=_identities(),
            plans=[plan],
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

    async def fake_structured(messages, **kwargs):
        meta = kwargs["call_meta"]
        prompts[str(meta["stage_key"] + ":" + meta.get("shard_id", ""))] = messages[0]["content"]
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
    assert "甲推门进入。" in first_prompt
    assert "乙接过钥匙并回答。" not in first_prompt
    assert "乙接过钥匙并回答。" in second_prompt
    assert "甲推门进入。" not in second_prompt
