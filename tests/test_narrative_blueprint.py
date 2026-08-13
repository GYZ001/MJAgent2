import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest

from app import hiagent, stages
from app.errors import ContentGenerationError
from app.harness import model_gateway
from app.narrative_blueprint import (
    BlueprintDecision,
    BlueprintSourceOccurrenceError,
    BlueprintSemanticReview,
    BlueprintSemanticIssue,
    BlueprintStateChange,
    BlueprintStateRequirement,
    NarrativeBlueprint,
    NarrativeBlueprintPatch,
    NarrativeBlueprintShard,
    NarrativeNode,
    apply_narrative_blueprint_patch,
    blueprint_authority_validator_fingerprint,
    blueprint_patch_schema,
    blueprint_shard_provider_schema,
    blueprint_semantic_issue_is_resolved,
    blueprint_semantic_review_schema,
    derive_blueprint_scene_plans,
    filter_blueprint_semantic_review_voice_issues,
    normalize_blueprint_agency_continuity,
    normalize_blueprint_provider_payload,
    normalize_blueprint_semantic_review_payload,
    recover_complete_blueprint_prefix,
    validate_and_apply_blueprint_scene_contract,
    validate_blueprint_semantic_review,
    validate_blueprint_scene_partition,
    validate_narrative_blueprint,
    validate_narrative_blueprint_patch_projection,
    validate_narrative_blueprint_shard,
)
from app.source_facts import SOURCE_FACT_VERSION, SourceFact


SOURCE = "\n\n".join([
    "白洁和王申回到家，白洁洗澡后躺到床上。",
    "白洁回忆冷小玉在咖啡店炫耀优渥生活。",
    "咖啡杯倒影转为卧室台灯，白洁回到现实。",
    "次日小张驾驶王局长的车来到学校。",
])


def _reviewer_drift_fixture() -> dict:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "blueprint_reviewer_node_key_drift.json"
    )
    return json.loads(fixture_path.read_text(encoding="utf-8"))


def _partition_replay_fixture() -> dict:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "run_64a2e395d6df_blueprint_partition.json"
    )
    return json.loads(fixture_path.read_text(encoding="utf-8"))


def _latest_blueprint_failure_fixtures() -> list[dict]:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "blueprint_latest_three_failures.json"
    )
    return json.loads(
        fixture_path.read_text(encoding="utf-8")
    )["cases"]


def _blueprint_cross_field_run_fixtures() -> list[dict]:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "blueprint_cross_field_runs_20260814.json"
    )
    return json.loads(
        fixture_path.read_text(encoding="utf-8")
    )["cases"]


def _replay_source_fact(value: dict) -> SourceFact:
    return SourceFact.model_validate({
        **value,
        "contract_version": SOURCE_FACT_VERSION,
    })


def _blueprint() -> NarrativeBlueprint:
    return NarrativeBlueprint.model_validate({
        "episode_no": 8,
        "nodes": [
            {
                "key": "n1",
                "source_segment_ids": ["SRC0001"],
                "summary": "两人回家，白洁洗澡后躺下",
                "narrative_layer": "story",
                "event_priority": "causal",
                "render_policy": "standalone",
                "temporal_domain_key": "present-night",
                "time_label": "夜",
                "time_relation": "episode_start",
                "location_key": "home-bedroom",
                "location_label": "白洁家卧室",
                "participants": ["白洁", "王申"],
                "participant_evidence": [{
                    "identity_key": "白洁",
                    "source_segment_ids": ["SRC0001"],
                    "source_unit_keys": ["SRC0001:unit:001"],
                    "usage": "state_subject",
                }, {
                    "identity_key": "白洁",
                    "source_segment_ids": ["SRC0001"],
                    "source_unit_keys": ["SRC0001:unit:002"],
                    "usage": "state_subject",
                }, {
                    "identity_key": "王申",
                    "source_segment_ids": ["SRC0001"],
                    "source_unit_keys": ["SRC0001:unit:001"],
                    "usage": "visible",
                }],
                "action_logic": "回家、洗澡、躺下按顺序发生",
                "state_changes": [{
                    "fact_key": "F001",
                    "state_key": "vehicle:wang:driver",
                    "value": "小张",
                    "reason": "建立司机关系",
                }],
            },
            {
                "key": "n2",
                "source_segment_ids": ["SRC0002"],
                "summary": "进入此前与冷小玉见面的回忆",
                "narrative_layer": "story",
                "event_priority": "causal",
                "render_policy": "standalone",
                "temporal_domain_key": "memory-cafe",
                "time_label": "日前",
                "time_relation": "flashback_enter",
                "location_key": "cafe",
                "location_label": "咖啡店",
                "participants": ["白洁", "冷小玉"],
                "participant_evidence": [{
                    "identity_key": "白洁",
                    "source_segment_ids": ["SRC0002"],
                    "source_unit_keys": ["SRC0002:unit:001"],
                    "usage": "state_subject",
                }, {
                    "identity_key": "冷小玉",
                    "source_segment_ids": ["SRC0002"],
                    "source_unit_keys": ["SRC0002:unit:001"],
                    "usage": "visible",
                }],
                "scene_boundary_before": True,
                "transition_cue": "卧室环境声淡出，咖啡杯声进入",
                "action_logic": "冷小玉展示生活差距，刺激白洁",
                "decision": {
                    "actor_key": "白洁",
                    "choice": "开始重新衡量自己的生活",
                    "impact": "major",
                    "setup_node_keys": ["n1"],
                    "pressure": "长期生活压抑",
                    "desire": "改变生活",
                    "agency_mode": "voluntary",
                },
            },
            {
                "key": "n3",
                "source_segment_ids": ["SRC0003"],
                "summary": "回忆结束，返回当晚卧室",
                "narrative_layer": "story",
                "event_priority": "causal",
                "render_policy": "standalone",
                "temporal_domain_key": "present-night",
                "time_label": "夜",
                "time_relation": "flashback_exit",
                "location_key": "home-bedroom",
                "location_label": "白洁家卧室",
                "participants": ["白洁"],
                "participant_evidence": [{
                    "identity_key": "白洁",
                    "source_segment_ids": ["SRC0003"],
                    "source_unit_keys": ["SRC0003:unit:001"],
                    "usage": "state_subject",
                }, {
                    "identity_key": "白洁",
                    "source_segment_ids": ["SRC0003"],
                    "source_unit_keys": ["SRC0003:unit:002"],
                    "usage": "state_subject",
                }],
                "scene_boundary_before": True,
                "transition_cue": "咖啡杯倒影匹配剪辑为卧室台灯",
                "action_logic": "明确回到现在",
            },
            {
                "key": "n4",
                "source_segment_ids": ["SRC0004"],
                "summary": "次日小张开车到学校",
                "narrative_layer": "story",
                "event_priority": "causal",
                "render_policy": "standalone",
                "temporal_domain_key": "next-day",
                "time_label": "次日",
                "time_relation": "jump",
                "location_key": "school-gate",
                "location_label": "学校门口",
                "participants": ["白洁", "小张"],
                "participant_evidence": [{
                    "identity_key": "小张",
                    "source_segment_ids": ["SRC0004"],
                    "source_unit_keys": ["SRC0004:unit:001"],
                    "usage": "state_subject",
                }, {
                    "identity_key": "白洁",
                    "source_segment_ids": ["SRC0004"],
                    "source_unit_keys": ["SRC0004:unit:001"],
                    "usage": "visible",
                }],
                "scene_boundary_before": True,
                "transition_cue": "字幕【次日】并建立学校外景",
                "action_logic": "小张驾驶车辆到达",
                "state_requirements": [{
                    "required_fact_key": "F001",
                    "state_key": "vehicle:wang:driver",
                    "expected_value": "小张",
                    "reason": "驾驶关系必须延续",
                }],
            },
        ],
    })


def test_valid_blueprint_derives_scenes_without_model_scene_grouping() -> None:
    blueprint = _blueprint()

    assert validate_narrative_blueprint(blueprint, SOURCE) == []
    plans = derive_blueprint_scene_plans(blueprint)
    assert [plan.key for plan in plans] == [
        "bp-sc001", "bp-sc002", "bp-sc003", "bp-sc004",
    ]
    assert plans[2].scene_heading == "【场3】夜 / 白洁家卧室"
    assert blueprint.source_scene_owners == {
        "SRC0001": "bp-sc001",
        "SRC0002": "bp-sc002",
        "SRC0003": "bp-sc003",
        "SRC0004": "bp-sc004",
    }
    assert any(
        relation.relation_type == "state_requirement"
        and relation.source_scene_plan_key == "bp-sc001"
        and relation.target_scene_plan_key == "bp-sc004"
        and relation.reference_key == "F001"
        for relation in blueprint.scene_derivations
    )


def test_picture_partition_preserves_mixed_node_order_and_audit_coverage() -> None:
    source = "\n\n".join(["剧情一", "来源审计", "剧情二"])
    nodes = []
    for index, (key, story) in enumerate([
        ("story-before", True),
        ("audit-middle", False),
        ("story-after", True),
    ], start=1):
        nodes.append({
            "key": key,
            "source_segment_ids": [f"SRC{index:04d}"],
            "summary": key,
            "narrative_layer": "story" if story else "paratext",
            "event_priority": "causal" if story else "connective",
            "render_policy": "standalone" if story else "exclude_from_spine",
            "temporal_domain_key": "present",
            "time_label": "当下",
            "time_relation": "episode_start" if index == 1 else "continuous",
                "location_key": "room",
                "location_label": "房间",
                "environment_source_unit_keys": (
                    [f"SRC{index:04d}:unit:001"] if story else []
                ),
                "action_logic": key,
        })
    blueprint = NarrativeBlueprint.model_validate({
        "episode_no": 1,
        "nodes": nodes,
    })

    assert validate_narrative_blueprint(blueprint, source) == []
    plans = derive_blueprint_scene_plans(blueprint)

    assert [
        node_key for plan in plans for node_key in plan.node_keys
    ] == ["story-before", "story-after"]
    assert [
        annotation.model_dump(mode="json")
        for annotation in blueprint.source_audit_annotations
    ] == [{
        "node_key": "audit-middle",
        "source_segment_ids": ["SRC0002"],
        "narrative_layer": "paratext",
        "render_policy": "exclude_from_spine",
        "disposition": "audit_only",
        "projection_policy": "audit_only",
    }]


@pytest.mark.parametrize("surface_text", [
    "第一章 书生孟浩",
    "【特别推广】后续内容敬请期待",
    "“卷末附记”",
])
def test_paratext_provider_projection_preserves_audit_ownership_but_clears_story_contracts(
    surface_text: str,
) -> None:
    dirty = {
        "format_version": stages.BLUEPRINT_VERSION,
        "episode_no": 1,
        "shard_index": 1,
        "source_segment_ids": ["SRC0001"],
        "nodes": [{
            "key": "audit-1",
            "source_segment_ids": ["SRC0001"],
            "summary": surface_text,
            "narrative_layer": "paratext",
            "event_priority": "connective",
            "render_policy": "exclude_from_spine",
            "temporal_domain_key": "paratext",
            "time_label": "章节外",
            "time_relation": "episode_start",
            "location_key": "paratext-card",
            "location_label": "字幕卡",
            "participants": ["person-wrong"],
            "participant_evidence": [],
            "environment_source_unit_keys": ["SRC0001:unit:001"],
            "source_unit_deliveries": [{
                "source_unit_key": "SRC0001:unit:001",
                "mode": "written_text",
            }],
            "exit_state": "标题展示完成",
            "state_requirements": [],
            "state_changes": [],
            "released_constraints_for": ["person-wrong"],
            "decision": None,
            "action_logic": surface_text,
        }],
    }

    with pytest.raises(Exception, match="paratext 节点不得承载"):
        NarrativeBlueprintShard.model_validate(dirty)
    normalized = normalize_blueprint_provider_payload(dirty)
    shard = NarrativeBlueprintShard.model_validate(normalized)
    node = shard.nodes[0]

    assert node.source_segment_ids == ["SRC0001"]
    assert node.summary == surface_text
    assert node.action_logic == surface_text
    assert node.participants == []
    assert node.source_unit_deliveries == []
    assert node.environment_source_unit_keys == []
    assert node.exit_state == ""
    blueprint = NarrativeBlueprint(episode_no=1, nodes=[node])
    assert derive_blueprint_scene_plans(blueprint) == []
    assert blueprint.source_audit_annotations[0].node_key == "audit-1"


def test_paratext_provider_schema_and_patch_schema_encode_empty_contract() -> None:
    schema = blueprint_shard_provider_schema()
    conditional = schema["$defs"]["NarrativeNode"]["allOf"][-1]
    assert conditional["if"]["properties"]["narrative_layer"] == {
        "const": "paratext"
    }
    assert conditional["then"]["properties"]["source_unit_deliveries"] == {
        "const": []
    }
    assert conditional["then"]["properties"]["exit_state"] == {
        "const": ""
    }

    node = NarrativeNode.model_validate({
        "key": "audit-1",
        "source_segment_ids": ["SRC0001"],
        "summary": "第一章",
        "narrative_layer": "paratext",
        "event_priority": "connective",
        "render_policy": "exclude_from_spine",
        "temporal_domain_key": "paratext",
        "time_label": "章节外",
        "time_relation": "episode_start",
        "location_key": "paratext-card",
        "location_label": "字幕卡",
        "action_logic": "展示标题",
    })
    patch = blueprint_patch_schema(
        NarrativeBlueprint(episode_no=1, nodes=[node]),
        ["audit-1"],
    )
    props = patch["properties"]["replacements"]["items"]["oneOf"][0][
        "properties"
    ]["node"]["allOf"][1]["properties"]
    assert props["source_unit_deliveries"] == {"const": []}
    assert props["exit_state"] == {"const": ""}


def test_semantic_consensus_drops_paratext_contract_guesses_but_keeps_real_story_gate() -> None:
    paratext_source = "“卷末附记”"
    paratext = NarrativeBlueprint.model_validate({
        "episode_no": 1,
        "nodes": [{
            "key": "audit-quoted",
            "source_segment_ids": ["SRC0001"],
            "summary": "卷末附记",
            "narrative_layer": "paratext",
            "event_priority": "connective",
            "render_policy": "exclude_from_spine",
            "temporal_domain_key": "paratext",
            "time_label": "章节外",
            "time_relation": "episode_start",
            "location_key": "paratext-card",
            "location_label": "字幕卡",
            "action_logic": "展示附记",
        }],
    })
    guessed = BlueprintSemanticReview(issues=[
        BlueprintSemanticIssue(
            code="source_delivery_missing",
            node_keys=["audit-quoted"],
            source_segment_ids=["SRC0001"],
            message="误报delivery",
            required_resolution="误补delivery",
        ),
        BlueprintSemanticIssue(
            code="state_subject_missing",
            node_keys=["audit-quoted"],
            source_segment_ids=["SRC0001"],
            message="误报subject",
            required_resolution="误补subject",
        ),
    ])

    assert filter_blueprint_semantic_review_voice_issues(
        guessed,
        paratext,
        paratext_source,
    ) == 2
    assert guessed.issues == []

    story_source = "孟浩推开木门。"
    story = NarrativeBlueprint.model_validate({
        "episode_no": 1,
        "nodes": [{
            "key": "story-1",
            "source_segment_ids": ["SRC0001"],
            "summary": "孟浩推门",
            "narrative_layer": "story",
            "event_priority": "causal",
            "render_policy": "standalone",
            "temporal_domain_key": "present",
            "time_label": "当下",
            "time_relation": "episode_start",
            "location_key": "door",
            "location_label": "木门前",
            "action_logic": "孟浩推门",
        }],
    })
    real = BlueprintSemanticReview(issues=[BlueprintSemanticIssue(
        code="state_subject_missing",
        node_keys=["story-1"],
        source_segment_ids=["SRC0001"],
        message="缺少subject",
        required_resolution="补exact subject",
    )])
    assert filter_blueprint_semantic_review_voice_issues(
        real,
        story,
        story_source,
    ) == 0
    assert [issue.code for issue in real.issues] == ["state_subject_missing"]


def test_partition_gate_rejects_picture_omission_duplicate_and_audit_leak() -> None:
    blueprint = _blueprint()
    audit = blueprint.nodes[1].model_copy(deep=True)
    audit.key = "audit-middle"
    audit.source_segment_ids = ["SRC0099"]
    audit.narrative_layer = "paratext"
    audit.event_priority = "connective"
    audit.render_policy = "exclude_from_spine"
    audit.participants = []
    audit.participant_evidence = []
    audit.state_requirements = []
    audit.state_changes = []
    audit.decision = None
    blueprint.nodes.insert(1, audit)
    plans = derive_blueprint_scene_plans(blueprint)

    omitted = [plan.model_copy(deep=True) for plan in plans]
    omitted[0].node_keys = []
    duplicated = [plan.model_copy(deep=True) for plan in plans]
    duplicated[0].node_keys.append(duplicated[0].node_keys[0])
    leaked = [plan.model_copy(deep=True) for plan in plans]
    leaked[0].node_keys.append("audit-middle")

    assert any(
        "SCENE_PARTITION_INVALID" in error
        for error in validate_blueprint_scene_partition(blueprint, omitted)
    )
    assert any(
        "SCENE_PARTITION_INVALID" in error
        for error in validate_blueprint_scene_partition(blueprint, duplicated)
    )
    assert any(
        "AUDIT_NODE_IN_SCENE" in error
        for error in validate_blueprint_scene_partition(blueprint, leaked)
    )


def test_run_64a2e395d6df_candidate_replays_picture_partition() -> None:
    fixture = _partition_replay_fixture()
    projection = fixture["candidate_projection"]
    scene_starts = {
        group[0] for group in projection["scene_node_keys"]
    }
    audit_keys = set(projection["audit_node_keys"])
    nodes = []
    for index, (key, source_ids) in enumerate(
        projection["node_sources"].items(),
    ):
        story = key not in audit_keys
        nodes.append({
            "key": key,
            "source_segment_ids": source_ids,
            "summary": f"{key} timeline node",
            "narrative_layer": "story" if story else "paratext",
            "event_priority": "causal" if story else "connective",
            "render_policy": "standalone" if story else "exclude_from_spine",
            "temporal_domain_key": "episode-present",
            "time_label": "当下",
            "time_relation": "episode_start" if index == 0 else "continuous",
                "location_key": "episode-location",
                "location_label": "当前地点",
                "environment_source_unit_keys": (
                    [f"{source_id}:unit:001" for source_id in source_ids]
                    if story else []
                ),
            "scene_boundary_before": key in scene_starts and index > 0,
            "dramatic_load": 1,
            "action_logic": f"{key} source-backed action",
        })
    blueprint = NarrativeBlueprint.model_validate({
        "episode_no": 1,
        "nodes": nodes,
    })
    source = "\n\n".join(
        f"授权来源段 {index}"
        for index in range(1, fixture["source_segment_count"] + 1)
    )

    assert validate_narrative_blueprint(blueprint, source) == []
    assert len(blueprint.nodes) == 21
    assert len(blueprint.scene_plans) == len(
        projection["scene_node_keys"]
    )
    assert [
        plan.node_keys for plan in blueprint.scene_plans
    ] == projection["scene_node_keys"]
    assert {
        annotation.node_key
        for annotation in blueprint.source_audit_annotations
    } == audit_keys
    audit_source_ids = {
        source_id
        for node in blueprint.nodes
        if node.key in audit_keys
        for source_id in node.source_segment_ids
    }
    assert audit_source_ids == {"SRC0060", "SRC0061", "SRC0062"}
    assert audit_source_ids.isdisjoint({
        source_id
        for plan in blueprint.scene_plans
        for source_id in plan.source_segment_ids
    })
    assert set(fixture["raw_patch_projection"]["replacement_node_keys"]) <= {
        node.key
        for node in blueprint.nodes
        if node.source_semantics().projection_policy == "picture"
    }


def test_reviewer_and_repair_schemas_bind_projection_authority() -> None:
    blueprint = _blueprint()
    review_schema = blueprint_semantic_review_schema(
        ["n1", "n2"],
        ["SRC0001", "SRC0002"],
    )
    issue_properties = review_schema["$defs"][
        "BlueprintSemanticIssue"
    ]["properties"]
    assert review_schema["x-canonical-timeline-node-keys"] == ["n1", "n2"]
    assert issue_properties["source_segment_ids"]["items"]["enum"] == [
        "SRC0001", "SRC0002",
    ]

    schema = blueprint_patch_schema(blueprint, ["n1", "n2"])
    alternatives = schema["properties"]["replacements"]["items"]["oneOf"]
    assert schema["x-canonical-timeline-node-keys"] == [
        node.key for node in blueprint.nodes
    ]
    assert schema["properties"]["delete_node_keys"]["maxItems"] == 0
    assert all(
        "nodes" not in alternative["properties"]
        for alternative in alternatives
    )
    by_key = {
        alternative["properties"]["node_key"]["const"]: alternative
        for alternative in alternatives
    }
    assert by_key["n1"]["properties"]["node"]["allOf"][1][
        "properties"
    ]["narrative_layer"]["const"] == "story"

    audit_node = blueprint.nodes[1].model_copy(deep=True)
    audit_node.narrative_layer = "paratext"
    audit_node.event_priority = "connective"
    audit_node.render_policy = "exclude_from_spine"
    audit_node.participants = []
    audit_node.participant_evidence = []
    audit_node.state_requirements = []
    audit_node.state_changes = []
    audit_node.decision = None
    blueprint.nodes[1] = audit_node
    invalid = audit_node.model_copy(deep=True)
    invalid.narrative_layer = "story"
    invalid.event_priority = "causal"
    invalid.render_policy = "standalone"
    patch = NarrativeBlueprintPatch.model_validate({
        "replacements": [{"node_key": "n2", "node": invalid}],
    })

    assert any(
        "SOURCE_SEMANTICS_CHANGE" in error
        for error in validate_narrative_blueprint_patch_projection(
            patch,
            blueprint,
        )
    )


def test_blueprint_rejects_source_assigned_to_multiple_scene_owners() -> None:
    blueprint = _blueprint()
    blueprint.nodes[1].source_segment_ids = ["SRC0001", "SRC0002"]

    errors = validate_narrative_blueprint(blueprint, SOURCE)

    assert any(
        "[BLUEPRINT_SOURCE_OWNER_CONFLICT]" in error
        and "SRC0001" in error
        and "bp-sc001" in error
        and "bp-sc002" in error
        for error in errors
    )


def test_blueprint_rejects_duplicate_picture_source_within_same_scene() -> None:
    blueprint = _blueprint()
    duplicate = blueprint.nodes[0].model_copy(deep=True)
    duplicate.key = "n1-duplicate"
    duplicate.time_relation = "continuous"
    duplicate.scene_boundary_before = False
    duplicate.decision = None
    duplicate.state_changes = []
    blueprint.nodes.insert(1, duplicate)

    errors = validate_narrative_blueprint(blueprint, SOURCE)

    assert any(
        "[BLUEPRINT_PICTURE_SOURCE_DUPLICATE]" in error
        and "SRC0001" in error
        and "n1" in error
        and "n1-duplicate" in error
        for error in errors
    )
    with pytest.raises(
        BlueprintSourceOccurrenceError,
        match="SRC0001.*n1.*n1-duplicate",
    ):
        derive_blueprint_scene_plans(blueprint)


def test_blueprint_shard_rejects_same_scene_and_same_node_duplicates() -> None:
    first = _blueprint().nodes[0].model_copy(deep=True)
    duplicate = first.model_copy(deep=True)
    duplicate.key = "n1-duplicate"
    duplicate.time_relation = "continuous"
    duplicate.decision = None
    duplicate.state_changes = []
    shard = NarrativeBlueprintShard(
        episode_no=8,
        shard_index=1,
        source_segment_ids=["SRC0001"],
        nodes=[first, duplicate],
    )

    errors = validate_narrative_blueprint_shard(
        shard,
        expected_episode_no=8,
        expected_shard_index=1,
        expected_source_segment_ids=["SRC0001"],
    )

    assert any(
        "[BLUEPRINT_SHARD_PICTURE_SOURCE_DUPLICATE]" in error
        and "SRC0001" in error
        for error in errors
    )

    first.source_segment_ids.append("SRC0001")
    within_node_errors = validate_narrative_blueprint_shard(
        NarrativeBlueprintShard(
            episode_no=8,
            shard_index=1,
            source_segment_ids=["SRC0001"],
            nodes=[first],
        ),
        expected_episode_no=8,
        expected_shard_index=1,
        expected_source_segment_ids=["SRC0001"],
    )
    assert any(
        "[BLUEPRINT_SHARD_PICTURE_SOURCE_DUPLICATE]" in error
        for error in within_node_errors
    )


def test_blueprint_valid_picture_ownership_remains_exactly_once() -> None:
    blueprint = _blueprint()

    assert not any(
        "SOURCE_DUPLICATE" in error
        or "SOURCE_PARTITION_CONFLICT" in error
        for error in validate_narrative_blueprint(blueprint, SOURCE)
    )


def test_blueprint_rejects_picture_audit_source_partition_conflict() -> None:
    blueprint = _blueprint()
    audit = blueprint.nodes[0].model_copy(deep=True)
    audit.key = "audit-duplicate"
    audit.narrative_layer = "paratext"
    audit.event_priority = "connective"
    audit.render_policy = "exclude_from_spine"
    audit.participants = []
    audit.participant_evidence = []
    audit.environment_source_unit_keys = []
    audit.source_unit_deliveries = []
    audit.state_requirements = []
    audit.state_changes = []
    audit.decision = None
    blueprint.nodes.append(audit)

    errors = validate_narrative_blueprint(blueprint, SOURCE)

    assert any(
        "[BLUEPRINT_SOURCE_PARTITION_CONFLICT]" in error
        and "SRC0001" in error
        for error in errors
    )
    with pytest.raises(
        BlueprintSourceOccurrenceError,
        match="SOURCE_PARTITION_CONFLICT.*SRC0001",
    ):
        derive_blueprint_scene_plans(blueprint)


def test_blueprint_rejects_duplicate_audit_source_in_full_and_shard() -> None:
    audit = _blueprint().nodes[1].model_copy(deep=True)
    audit.narrative_layer = "paratext"
    audit.event_priority = "connective"
    audit.render_policy = "exclude_from_spine"
    audit.participants = []
    audit.participant_evidence = []
    audit.environment_source_unit_keys = []
    audit.source_unit_deliveries = []
    audit.state_requirements = []
    audit.state_changes = []
    audit.decision = None
    duplicate = audit.model_copy(deep=True)
    duplicate.key = "audit-duplicate"
    blueprint = _blueprint()
    blueprint.nodes[1] = audit
    blueprint.nodes.append(duplicate)

    full_errors = validate_narrative_blueprint(blueprint, SOURCE)
    shard_errors = validate_narrative_blueprint_shard(
        NarrativeBlueprintShard(
            episode_no=8,
            shard_index=1,
            source_segment_ids=["SRC0002"],
            nodes=[audit, duplicate],
        ),
        expected_episode_no=8,
        expected_shard_index=1,
        expected_source_segment_ids=["SRC0002"],
    )

    assert any(
        "[BLUEPRINT_AUDIT_SOURCE_DUPLICATE]" in error
        and "SRC0002" in error
        for error in full_errors
    )
    assert any(
        "[BLUEPRINT_SHARD_AUDIT_SOURCE_DUPLICATE]" in error
        and "SRC0002" in error
        for error in shard_errors
    )


def test_blueprint_normalizes_unpadded_source_ids() -> None:
    node = _blueprint().nodes[0].model_dump(mode="json")
    node["source_segment_ids"] = ["SRC1"]
    shard = NarrativeBlueprintShard.model_validate({
        "episode_no": 8,
        "shard_index": 1,
        "source_segment_ids": ["SRC001"],
        "nodes": [node],
    })

    assert shard.source_segment_ids == ["SRC0001"]
    assert shard.nodes[0].source_segment_ids == ["SRC0001"]


def test_shard_normalization_binds_intra_node_major_decision_setup() -> None:
    node = _blueprint().nodes[1].model_copy(deep=True)
    node.decision.setup_node_keys = []
    shard = NarrativeBlueprintShard(
        episode_no=8,
        shard_index=1,
        source_segment_ids=["SRC0002"],
        nodes=[node],
    )

    stages._normalize_blueprint_shard_structure(
        shard,
        boundary_context={"active_state_facts": []},
    )

    assert shard.nodes[0].decision.setup_node_keys == ["n2"]


def test_blueprint_rejects_unmarked_spatiotemporal_jump() -> None:
    blueprint = _blueprint()
    blueprint.nodes[3].transition_cue = ""

    errors = validate_narrative_blueprint(blueprint, SOURCE)

    assert any("TRANSITION_CUE_MISSING" in error for error in errors)
    assert any("CHARACTER_TELEPORT" in error for error in errors)


def test_blueprint_rejects_unknown_durable_state_fact() -> None:
    blueprint = _blueprint()
    blueprint.nodes[3].state_requirements[0].required_fact_key = "F999"

    errors = validate_narrative_blueprint(blueprint, SOURCE)

    assert any("STATE_UNESTABLISHED" in error for error in errors)


def test_blueprint_rejects_major_decision_without_prior_motivation() -> None:
    blueprint = _blueprint()
    blueprint.nodes[1].decision.setup_node_keys = []

    errors = validate_narrative_blueprint(blueprint, SOURCE)

    assert any("MOTIVATION_MISSING" in error for error in errors)


def test_blueprint_accepts_intra_node_motivation_before_exit_decision() -> None:
    blueprint = _blueprint()
    blueprint.nodes[1].decision.setup_node_keys = ["n2"]

    errors = validate_narrative_blueprint(blueprint, SOURCE)

    assert not any("MOTIVATION_FUTURE" in error for error in errors)


def test_blueprint_rejects_future_node_as_decision_motivation() -> None:
    blueprint = _blueprint()
    blueprint.nodes[1].decision.setup_node_keys = ["n3"]

    errors = validate_narrative_blueprint(blueprint, SOURCE)

    assert any("MOTIVATION_FUTURE" in error for error in errors)


def test_blueprint_rejects_source_omission() -> None:
    blueprint = _blueprint()
    blueprint.nodes.pop()

    errors = validate_narrative_blueprint(blueprint, SOURCE)

    assert any("SOURCE_MISSING" in error for error in errors)


def test_blueprint_owns_ir_scene_keys_headings_and_source_allocation() -> None:
    blueprint = _blueprint()
    plans = derive_blueprint_scene_plans(blueprint)
    scenes = [
        SimpleNamespace(
            key=f"model-sc{index}",
            scene_heading="模型自由标题",
            units=[
                SimpleNamespace(
                    event_key=f"e{index}",
                    source_segment_ids=list(plan.source_segment_ids),
                ),
            ],
        )
        for index, plan in enumerate(plans, start=1)
    ]
    candidate = SimpleNamespace(scenes=scenes)

    errors = validate_and_apply_blueprint_scene_contract(
        candidate,
        blueprint,
    )

    assert errors == []
    assert [scene.key for scene in candidate.scenes] == [
        plan.key for plan in plans
    ]
    assert candidate.scenes[0].scene_heading == plans[0].scene_heading


def test_blueprint_does_not_infer_identity_from_relationship_description() -> None:
    blueprint = _blueprint()
    plans = derive_blueprint_scene_plans(blueprint)
    identity = SimpleNamespace(
        key="person_gaoyi",
        display_name="高义",
        rationale="原文中白洁的情人，本集仅作为伏笔出现",
        visual_canonical="仅以模糊剪影出现",
    )
    candidate = SimpleNamespace(
        identities=[identity],
        scenes=[
            SimpleNamespace(
                key=f"model-sc{index}",
                scene_heading="模型自由标题",
                units=[
                    SimpleNamespace(
                        event_key=f"e{index}",
                        source_segment_ids=list(plan.source_segment_ids),
                    ),
                ],
            )
            for index, plan in enumerate(plans, start=1)
        ],
    )

    errors = validate_and_apply_blueprint_scene_contract(
        candidate,
        blueprint,
    )

    assert errors == []
    assert identity.display_name == "高义"


def test_setup_issue_is_resolved_by_auditable_logic_bridge() -> None:
    blueprint = _blueprint()
    node = blueprint.nodes[1]
    node.adaptation_kind = "logic_bridge"
    node.bridge_rationale = "补充原文已授权关系的可见建立过程"
    node.transition_cue = "先展示人物身份和关系，再进入当前行动"
    issue = BlueprintSemanticIssue(
        code="setup_missing",
        node_keys=[node.key],
        source_segment_ids=list(node.source_segment_ids),
        message="关系缺少铺垫",
        required_resolution="增加可见铺垫",
    )

    assert blueprint_semantic_issue_is_resolved(issue, blueprint) is True

    node.adaptation_kind = "source_direct"
    assert blueprint_semantic_issue_is_resolved(issue, blueprint) is False


def test_blueprint_does_not_remap_compiler_context_actor() -> None:
    blueprint = _blueprint()
    plans = derive_blueprint_scene_plans(blueprint)
    identity = SimpleNamespace(
        key="context_actor_scene-home",
        display_name="白洁家中客厅中的未具名参与者",
        role_type="source_backed_scene_context_actor",
    )
    candidate = SimpleNamespace(
        identities=[identity],
        scenes=[
            SimpleNamespace(
                key=f"model-sc{index}",
                scene_heading="模型自由标题",
                units=[
                    SimpleNamespace(
                        event_key=f"e{index}",
                        source_segment_ids=list(plan.source_segment_ids),
                    ),
                ],
            )
            for index, plan in enumerate(plans, start=1)
        ],
    )

    errors = validate_and_apply_blueprint_scene_contract(
        candidate,
        blueprint,
    )

    assert errors == []
    assert identity.display_name == "白洁家中客厅中的未具名参与者"


def test_blueprint_patch_drops_supersedes_for_removed_or_other_state() -> None:
    blueprint = _blueprint()
    blueprint.nodes[1].state_changes = [
        BlueprintStateChange(
            fact_key="F002",
            state_key="character:emotion",
            value="焦虑",
            reason="建立情绪",
        )
    ]
    patch = NarrativeBlueprintPatch.model_validate({
        "replacements": [{
            "node_key": "n2",
            "node": {
                **blueprint.nodes[1].model_dump(mode="json"),
                "state_changes": [{
                    "fact_key": "F003",
                    "state_key": "character:emotion",
                    "value": "平静",
                    "reason": "更新情绪",
                    "supersedes_fact_keys": ["F001", "F002"],
                }],
            },
        }],
    })

    apply_narrative_blueprint_patch(blueprint, patch)

    assert blueprint.nodes[1].state_changes[0].supersedes_fact_keys == []
    assert not any(
        "STATE_SUPERSEDE_INVALID" in error
        for error in validate_narrative_blueprint(blueprint, SOURCE)
    )


def test_state_facts_accumulate_unless_explicitly_superseded() -> None:
    blueprint = _blueprint()
    blueprint.nodes[1].state_changes.append(BlueprintStateChange(
        fact_key="F002",
        state_key="vehicle:wang:driver",
        value="小张暂时离车",
        reason="新增并存事实",
    ))
    assert not any(
        "STATE_SUPERSEDED" in error
        for error in validate_narrative_blueprint(blueprint, SOURCE)
    )

    blueprint.nodes[1].state_changes[0].supersedes_fact_keys = ["F001"]
    assert any(
        "STATE_SUPERSEDED" in error
        for error in validate_narrative_blueprint(blueprint, SOURCE)
    )


def test_coerced_to_voluntary_requires_intervening_release_node() -> None:
    blueprint = _blueprint()
    blueprint.nodes[1].state_changes.append(BlueprintStateChange(
        fact_key="F003",
        state_key="constraint:白洁",
        value="受到明确威胁",
        reason="建立约束",
    ))
    blueprint.nodes[1].decision = BlueprintDecision(
        actor_key="白洁",
        choice="因威胁停止反抗",
        agency_mode="coerced",
        constraint_fact_key="F003",
    )
    blueprint.nodes[3].decision = BlueprintDecision(
        actor_key="白洁",
        choice="主动继续",
        agency_mode="voluntary",
        agency_change_reason="情绪变化",
    )

    errors = validate_narrative_blueprint(blueprint, SOURCE)

    assert any("AGENCY_RELEASE_MISSING" in error for error in errors)

    blueprint.nodes[2].released_constraints_for = ["白洁"]
    blueprint.nodes[2].state_changes.append(BlueprintStateChange(
        fact_key="F004",
        state_key="constraint:白洁",
        value="威胁解除",
        reason="威胁者离开",
        supersedes_fact_keys=["F003"],
    ))
    blueprint.nodes[3].decision.constraint_release_node_keys = ["n3"]
    errors = validate_narrative_blueprint(blueprint, SOURCE)
    assert not any("AGENCY_RELEASE" in error for error in errors)


def test_agency_normalization_inherits_unreleased_constraint() -> None:
    blueprint = _blueprint()
    blueprint.nodes[1].state_changes.append(BlueprintStateChange(
        fact_key="F003",
        state_key="constraint:白洁",
        value="受到明确威胁",
        reason="建立约束",
    ))
    blueprint.nodes[1].decision = BlueprintDecision(
        actor_key="白洁",
        choice="因威胁停止反抗",
        agency_mode="coerced",
        constraint_fact_key="F003",
    )
    blueprint.nodes[3].decision = BlueprintDecision(
        actor_key="白洁",
        choice="继续行动",
        agency_mode="voluntary",
        constraint_release_node_keys=["n4"],
    )

    changed = normalize_blueprint_agency_continuity(blueprint)

    assert changed == 2
    decision = blueprint.nodes[3].decision
    assert decision.agency_mode == "coerced"
    assert decision.constraint_fact_key == "F003"
    assert decision.constraint_release_node_keys == []
    assert not any(
        "AGENCY_RELEASE" in error
        for error in validate_narrative_blueprint(blueprint, SOURCE)
    )


def test_coerced_decision_cannot_release_its_own_constraint() -> None:
    blueprint = _blueprint()
    blueprint.nodes[1].state_changes.append(BlueprintStateChange(
        fact_key="F003",
        state_key="constraint:白洁",
        value="受到明确威胁",
        reason="建立约束",
    ))
    blueprint.nodes[1].decision = BlueprintDecision(
        actor_key="白洁",
        choice="因威胁停止反抗",
        agency_mode="coerced",
        constraint_fact_key="F003",
    )
    blueprint.nodes[3].state_changes.append(BlueprintStateChange(
        fact_key="F004",
        state_key="constraint:白洁",
        value="仍在威胁下行动",
        reason="约束仍然有效",
        supersedes_fact_keys=["F003"],
    ))
    blueprint.nodes[3].released_constraints_for = ["F003"]
    blueprint.nodes[3].decision = BlueprintDecision(
        actor_key="白洁",
        choice="继续服从",
        agency_mode="coerced",
        constraint_fact_key="F003",
        constraint_release_node_keys=["n4"],
    )

    normalize_blueprint_agency_continuity(blueprint)

    assert (
        blueprint.nodes[3].state_changes[-1].supersedes_fact_keys
        == []
    )
    assert blueprint.nodes[3].released_constraints_for == []
    assert blueprint.nodes[3].decision.constraint_release_node_keys == []
    assert not any(
        "AGENCY_CONSTRAINT_FACT_MISSING" in error
        for error in validate_narrative_blueprint(blueprint, SOURCE)
    )


def test_blueprint_patch_replaces_node_without_changing_source_ownership() -> None:
    blueprint = _blueprint()
    replacement = blueprint.nodes[2].model_copy(deep=True)
    replacement.transition_cue = "咖啡杯匹配剪辑到台灯"
    patch = NarrativeBlueprintPatch.model_validate({
        "replacements": [{
            "node_key": "n3",
            "node": replacement.model_dump(mode="json"),
        }],
    })

    assert apply_narrative_blueprint_patch(blueprint, patch) == 1
    assert blueprint.nodes[2].transition_cue == "咖啡杯匹配剪辑到台灯"


def test_blueprint_patch_rejects_source_ownership_exchange() -> None:
    blueprint = _blueprint()
    memory_node = blueprint.nodes[1].model_copy(deep=True)
    return_node = blueprint.nodes[2].model_copy(deep=True)
    memory_node.key = "n3"
    return_node.key = "n2"
    patch = NarrativeBlueprintPatch.model_validate({
        "replacements": [
            {
                "node_key": "n2",
                "node": return_node.model_dump(mode="json"),
            },
            {
                "node_key": "n3",
                "node": memory_node.model_dump(mode="json"),
            },
        ],
    })

    with pytest.raises(ValueError, match="SOURCE_OWNERSHIP_CHANGE"):
        apply_narrative_blueprint_patch(
            blueprint,
            patch,
            allow_source_expansion=True,
            source_text=SOURCE,
        )
    assert [node.key for node in blueprint.nodes] == ["n1", "n2", "n3", "n4"]


def test_blueprint_patch_repairs_malformed_provider_json(
    monkeypatch,
) -> None:
    blueprint = _blueprint()
    replacement = blueprint.nodes[3].model_copy(deep=True)
    replacement.transition_cue = "次日字幕后切到学校门口"
    valid_patch = json.dumps(
        {
            "replacements": [{
                "node_key": "n4",
                "node": replacement.model_dump(mode="json"),
            }],
        },
        ensure_ascii=False,
    )
    responses = iter([
        '{"replacements":[{"node_key":"n4" "node":{}}]}',
        valid_patch,
    ])
    calls: list[list[dict[str, str]]] = []
    artifacts = []

    async def fake_chat(messages, **_kwargs):
        calls.append(messages)
        return next(responses)

    def fake_create_artifact(artifact, **_kwargs):
        artifacts.append(artifact)
        return {"id": f"art-{len(artifacts)}"}

    monkeypatch.setattr(stages.model_gateway, "chat", fake_chat)
    monkeypatch.setattr(
        "app.evidence.repository.create_artifact",
        fake_create_artifact,
    )
    monkeypatch.setattr(
        stages,
        "get_setting",
        lambda key: 1 if key == "screenplay_format_retry_limit" else None,
    )

    repaired = asyncio.run(stages._repair_narrative_blueprint(
        blueprint,
        episode={"id": "episode-format-repair"},
        source_text=SOURCE,
        additional_errors=["[BLUEPRINT_TEST] n4 需要局部修复"],
    ))

    assert len(calls) == 2
    assert "只修复下面响应的 JSON 格式和 Schema" in calls[1][0]["content"]
    assert repaired.nodes[3].transition_cue == "次日字幕后切到学校门口"
    raw_attempts = [
        artifact
        for artifact in artifacts
        if artifact.type == "screenplay_narrative_blueprint_patch_raw"
    ]
    assert [artifact.content["outcome"] for artifact in raw_attempts] == [
        "format_error",
        "validated",
    ]


def test_blueprint_state_subject_repair_preserves_timeline_and_source_authority(
    monkeypatch,
) -> None:
    blueprint = _blueprint()
    original_order = [node.key for node in blueprint.nodes]
    original_sources = {
        node.key: list(node.source_segment_ids) for node in blueprint.nodes
    }
    original_projection = {
        node.key: node.source_semantics().projection_policy
        for node in blueprint.nodes
    }
    node = blueprint.nodes[0]
    subject = next(
        evidence for evidence in node.participant_evidence
        if evidence.usage == "state_subject"
    ).model_copy(deep=True)
    node.participant_evidence = [
        evidence for evidence in node.participant_evidence
        if evidence.usage != "state_subject"
    ]
    replacement = node.model_copy(deep=True)
    replacement.participant_evidence.append(subject)
    patch = NarrativeBlueprintPatch.model_validate({
        "replacements": [{
            "node_key": node.key,
            "node": replacement.model_dump(mode="json"),
        }],
    })
    prompts: list[str] = []

    async def fake_structured(messages, **_kwargs):
        prompts.append(messages[-1]["content"])
        return patch

    monkeypatch.setattr(stages.model_gateway, "chat_structured", fake_structured)
    monkeypatch.setattr(
        "app.evidence.repository.create_artifact",
        lambda _artifact, **_kwargs: {"id": str(uuid.uuid4())},
    )
    repaired = asyncio.run(stages._repair_narrative_blueprint(
        blueprint,
        episode={"id": "episode-state-subject-repair"},
        source_text=SOURCE,
    ))

    assert "usage=state_subject" in prompts[0]
    assert "SRC0001:unit:001" in prompts[0]
    assert [item.key for item in repaired.nodes] == original_order
    assert {
        item.key: item.source_segment_ids for item in repaired.nodes
    } == original_sources
    assert {
        item.key: item.source_semantics().projection_policy
        for item in repaired.nodes
    } == original_projection
    assert repaired.nodes[0].environment_source_unit_keys == []


def test_blueprint_review_exhaustion_is_quality_gate(
    monkeypatch,
) -> None:
    review = BlueprintSemanticReview.model_validate({
        "issues": [{
            "code": "spatial_action_gap",
            "node_keys": ["n4"],
            "source_segment_ids": ["SRC0004"],
            "message": "开门后的空间位置过渡不清",
            "required_resolution": "补充连续的位置转换",
            "must_fix": True,
        }],
    })
    reviewer_calls = 0
    repair_calls = 0

    class EmptyRows:
        @staticmethod
        def fetchall():
            return []

    class EmptyConnection:
        @staticmethod
        def execute(*_args, **_kwargs):
            return EmptyRows()

    async def fake_chat_structured(*_args, **_kwargs):
        nonlocal reviewer_calls
        reviewer_calls += 1
        return review.model_copy(deep=True)

    async def fake_repair(blueprint, **_kwargs):
        nonlocal repair_calls
        repair_calls += 1
        return blueprint

    monkeypatch.setattr(stages, "get_conn", lambda: EmptyConnection())
    monkeypatch.setattr(
        stages,
        "get_setting",
        lambda key: "true"
        if key == "screenplay_targeted_blueprint_review_enabled"
        else 1,
    )
    monkeypatch.setattr(
        stages.model_gateway,
        "chat_structured",
        fake_chat_structured,
    )
    monkeypatch.setattr(stages, "_repair_narrative_blueprint", fake_repair)
    monkeypatch.setattr(
        "app.evidence.repository.create_artifact",
        lambda *_args, **_kwargs: {"id": str(uuid.uuid4())},
    )

    with pytest.raises(
        ContentGenerationError,
        match="蓝图语义共识复审仍有必须修复问题",
    ):
        asyncio.run(stages._semantic_review_narrative_blueprint(
            _blueprint(),
            episode={"id": "episode-quality-gate"},
            source_text=SOURCE,
        ))

    assert reviewer_calls == 8
    assert repair_calls == 3


def test_blueprint_patch_rejects_node_split() -> None:
    blueprint = _blueprint()
    original = blueprint.nodes[0]
    first = original.model_copy(deep=True)
    first.key = "n1a"
    first.source_segment_ids = ["SRC0001"]
    second = original.model_copy(deep=True)
    second.key = "n1b"
    second.source_segment_ids = ["SRC0001"]
    second.summary = "同一场内继续交付该来源段的后续动作"
    second.action_logic = "同一场内拆分复合动作但不创建第二个来源 owner"
    patch = NarrativeBlueprintPatch.model_validate({
        "replacements": [{
            "node_key": "n1",
            "nodes": [
                first.model_dump(mode="json"),
                second.model_dump(mode="json"),
            ],
        }],
    })

    with pytest.raises(ValueError, match="TIMELINE_CARDINALITY"):
        apply_narrative_blueprint_patch(blueprint, patch)
    assert [node.key for node in blueprint.nodes] == ["n1", "n2", "n3", "n4"]


def test_blueprint_patch_rejects_unknown_and_renamed_nodes() -> None:
    blueprint = _blueprint()
    original = blueprint.nodes[0]
    first = original.model_copy(deep=True)
    first.key = "n1a"
    second = original.model_copy(deep=True)
    second.key = "model-new-node"
    second.transition_cue = "同一来源段内继续行动"
    patch = NarrativeBlueprintPatch.model_validate({
        "replacements": [
            {
                "node_key": original.key,
                "node": first.model_dump(mode="json"),
            },
            {
                "node_key": "model-new-node",
                "node": second.model_dump(mode="json"),
            },
        ],
    })

    with pytest.raises(ValueError, match="NODE_IDENTITY_CHANGE"):
        apply_narrative_blueprint_patch(blueprint, patch)
    assert [node.key for node in blueprint.nodes] == ["n1", "n2", "n3", "n4"]


def test_semantic_review_must_reference_existing_nodes_and_sources() -> None:
    review = BlueprintSemanticReview.model_validate({
        "issues": [{
            "code": "spatial_action_gap",
            "node_keys": ["missing-node"],
            "source_segment_ids": ["SRC9999"],
            "message": "人物位置不闭环",
            "required_resolution": "补充移动节点",
        }],
    })

    errors = validate_blueprint_semantic_review(
        review,
        _blueprint(),
        SOURCE,
    )

    assert any("NODE_UNKNOWN" in error for error in errors)
    assert any("SOURCE_UNKNOWN" in error for error in errors)


def test_semantic_review_schema_binds_canonical_node_references() -> None:
    schema = blueprint_semantic_review_schema(["n1", "n2", "n3"])
    references = schema["$defs"]["BlueprintSemanticIssue"][
        "properties"
    ]["node_keys"]
    alternatives = references["items"]["oneOf"]

    assert references["minItems"] == 1
    assert alternatives[0]["enum"] == ["n1", "n2", "n3"]
    assert alternatives[1]["properties"]["ordinal"] == {
        "type": "integer",
        "minimum": 1,
        "maximum": 3,
    }
    assert alternatives[2]["properties"]["identity"]["enum"] == [
        "n1",
        "n2",
        "n3",
    ]
    assert "bp-sc001" not in json.dumps(schema)


def test_semantic_review_normalizer_maps_only_exact_structured_references() -> None:
    payload = {
        "issues": [{
            "code": "spatial_action_gap",
            "node_keys": [
                "n1",
                {"ordinal": 2},
                {"identity": "n3"},
                {"ordinal": 0},
                {"identity": "missing"},
                {"ordinal": 2, "identity": "n2"},
                "人物名称",
            ],
            "message": "保留同一个问题及全部引用",
            "required_resolution": "仅修正引用合同",
        }],
    }

    normalized = normalize_blueprint_semantic_review_payload(
        payload,
        ["n1", "n2", "n3", "n4"],
    )
    references = normalized["issues"][0]["node_keys"]

    assert references[:3] == ["n1", "n2", "n3"]
    assert len(references) == 7
    assert all(
        value.startswith("[INVALID_BLUEPRINT_NODE_REFERENCE]")
        for value in references[3:6]
    )
    assert references[6] == "人物名称"
    review = BlueprintSemanticReview.model_validate(normalized)
    errors = validate_blueprint_semantic_review(
        review,
        _blueprint(),
        SOURCE,
    )
    assert any("NODE_UNKNOWN" in error for error in errors)
    assert len(review.issues) == 1
    assert len(review.issues[0].node_keys) == 7


def test_replays_original_reviewer_node_key_drift_without_dropping_issue(
    monkeypatch,
) -> None:
    fixture = _reviewer_drift_fixture()
    canonical = fixture["canonical_node_keys"]
    raw_responses = [
        json.dumps(item["response"], ensure_ascii=False)
        for item in fixture["responses"]
    ]
    prompts: list[str] = []
    observed_issue_counts: list[int] = []
    attempts: list[dict] = []

    async def fake_chat(messages, **_kwargs):
        prompts.append(messages[0]["content"])
        return raw_responses[len(prompts) - 1]

    def validate(review: BlueprintSemanticReview) -> list[str]:
        observed_issue_counts.append(len(review.issues))
        unknown = [
            node_key
            for issue in review.issues
            for node_key in issue.node_keys
            if node_key not in set(canonical)
        ]
        return [f"unknown canonical node identity: {value}" for value in unknown]

    monkeypatch.setattr(model_gateway, "chat", fake_chat)
    schema = blueprint_semantic_review_schema(canonical)

    with pytest.raises(
        model_gateway.StructuredSemanticError,
        match="S002-S001-N007",
    ):
        asyncio.run(model_gateway.chat_structured(
            [{"role": "user", "content": "review current blueprint"}],
            model_type=BlueprintSemanticReview,
            validate=validate,
            operation_id="replay.run_b864b0c8915d.review.2.1",
            max_tokens=8192,
            format_retry_limit=0,
            semantic_retry_limit=1,
            repair_context=json.dumps({
                "canonical_nodes": [
                    {"ordinal": index, "identity": node_key}
                    for index, node_key in enumerate(canonical, start=1)
                ],
            }, ensure_ascii=False),
            output_schema=schema,
            normalize_payload=lambda payload: (
                normalize_blueprint_semantic_review_payload(
                    payload,
                    canonical,
                )
            ),
            on_attempt=attempts.append,
        ))

    assert observed_issue_counts == [3, 3]
    assert [attempt["outcome"] for attempt in attempts] == [
        "semantic_error",
        "semantic_error",
    ]
    assert "bp-sc007" in prompts[1]
    assert "S001-N007" in prompts[1]
    assert "Canonical Node References" in prompts[1]


def test_structured_ordinal_and_identity_recover_reviewer_issue(
    monkeypatch,
) -> None:
    fixture = _reviewer_drift_fixture()
    canonical = fixture["canonical_node_keys"]
    response = json.loads(json.dumps(fixture["responses"][0]["response"]))
    response["issues"][0]["node_keys"] = [
        {"ordinal": 8},
        {"identity": "S001-N007"},
    ]
    calls = 0

    async def fake_chat(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return json.dumps(response, ensure_ascii=False)

    def validate(review: BlueprintSemanticReview) -> list[str]:
        return [
            node_key
            for issue in review.issues
            for node_key in issue.node_keys
            if node_key not in set(canonical)
        ]

    monkeypatch.setattr(model_gateway, "chat", fake_chat)
    result = asyncio.run(model_gateway.chat_structured(
        [{"role": "user", "content": "review current blueprint"}],
        model_type=BlueprintSemanticReview,
        validate=validate,
        operation_id="replay.run_b864b0c8915d.structured-reference",
        max_tokens=8192,
        format_retry_limit=0,
        semantic_retry_limit=0,
        output_schema=blueprint_semantic_review_schema(canonical),
        normalize_payload=lambda payload: (
            normalize_blueprint_semantic_review_payload(payload, canonical)
        ),
    ))

    assert calls == 1
    assert len(result.issues) == 3
    assert result.issues[0].node_keys == [
        "S002-S001-N008",
        "S001-N007",
    ]


def test_recovers_only_complete_blueprint_nodes_from_truncated_json() -> None:
    raw = _blueprint().model_dump_json()
    truncated = raw[:raw.index('"key":"n2"') + len('"key":"n2"')]

    recovered = recover_complete_blueprint_prefix(truncated)

    assert recovered is not None
    assert recovered["episode_no"] == 8
    assert [node["key"] for node in recovered["nodes"]] == ["n1"]
    assert recovered["scene_plans"] == []


def test_shard_gate_rejects_composite_location_and_unknown_fact() -> None:
    node = _blueprint().nodes[0].model_copy(deep=True)
    node.location_label = "学校/车站"
    node.state_requirements = [BlueprintStateRequirement(
        state_key="测试状态",
        required_fact_key="missing-fact",
        reason="验证未知事实门禁",
    )]
    shard = NarrativeBlueprintShard(
        episode_no=8,
        shard_index=1,
        source_segment_ids=["SRC0001"],
        nodes=[node],
    )

    errors = validate_narrative_blueprint_shard(
        shard,
        expected_episode_no=8,
        expected_shard_index=1,
        expected_source_segment_ids=["SRC0001"],
        boundary_state_facts=[],
    )

    assert any("LOCATION_COMPOSITE" in error for error in errors)
    assert any("FACT_UNKNOWN" in error for error in errors)


def test_shard_gate_rejects_local_state_subject_and_participant_authority() -> None:
    node = _blueprint().nodes[0].model_copy(deep=True)
    node.participant_evidence = [
        evidence
        for evidence in node.participant_evidence
        if evidence.usage != "state_subject"
    ]
    shard = NarrativeBlueprintShard(
        episode_no=8,
        shard_index=1,
        source_segment_ids=["SRC0001"],
        nodes=[node],
    )

    errors = validate_narrative_blueprint_shard(
        shard,
        expected_episode_no=8,
        expected_shard_index=1,
        expected_source_segment_ids=["SRC0001"],
        source_text=SOURCE,
    )

    assert any("SHARD_STATE_SUBJECT_MISSING" in error for error in errors)
    assert any(
        "SHARD_PARTICIPANT_EVIDENCE_MISSING" in error
        and "白洁" in error
        for error in errors
    )


@pytest.mark.parametrize(
    "case",
    _latest_blueprint_failure_fixtures(),
    ids=lambda case: case["error_id"],
)
def test_latest_real_blueprint_failures_replay_typed_authority(
    case: dict,
    monkeypatch,
) -> None:
    facts = [_replay_source_fact(value) for value in case["source_facts"]]
    monkeypatch.setattr(
        "app.narrative_blueprint.source_facts",
        lambda _source_text: facts,
    )
    shard = NarrativeBlueprintShard.model_validate(case["shard"])

    errors = validate_narrative_blueprint_shard(
        shard,
        expected_episode_no=shard.episode_no,
        expected_shard_index=shard.shard_index,
        expected_source_segment_ids=shard.source_segment_ids,
        source_text="fixture source facts are injected above",
    )

    for error_code in case["expected_error_codes"]:
        assert any(error_code in error for error in errors), (
            case["provider_call_id"],
            case["raw_artifact_id"],
            errors,
        )


def test_real_src0003_unit016_accepts_explicit_spoken_voice_contract(
    monkeypatch,
) -> None:
    case = _latest_blueprint_failure_fixtures()[0]
    facts = [_replay_source_fact(value) for value in case["source_facts"]]
    monkeypatch.setattr(
        "app.narrative_blueprint.source_facts",
        lambda _source_text: facts,
    )
    payload = json.loads(json.dumps(case["shard"], ensure_ascii=False))
    node = payload["nodes"][0]
    node["participant_evidence"] = [
        node["participant_evidence"][0],
        {
            "identity_key": "char_孟浩",
            "source_segment_ids": ["SRC0003"],
            "source_unit_keys": ["SRC0003:unit:016"],
            "usage": "voice",
        },
    ]
    node["source_unit_deliveries"] = [{
        "source_unit_key": "SRC0003:unit:016",
        "mode": "spoken_dialogue",
        "performer_key": "char_孟浩",
    }]
    shard = NarrativeBlueprintShard.model_validate(payload)

    errors = validate_narrative_blueprint_shard(
        shard,
        expected_episode_no=shard.episode_no,
        expected_shard_index=shard.shard_index,
        expected_source_segment_ids=shard.source_segment_ids,
        source_text="fixture source facts are injected above",
    )

    assert not any("SOURCE_DELIVERY" in error for error in errors)
    assert not any("VOICE_IDENTITY" in error for error in errors)


def test_blueprint_gate_rejects_empty_participant_evidence() -> None:
    blueprint = _blueprint()
    blueprint.nodes[0].participant_evidence = []

    errors = validate_narrative_blueprint(blueprint, SOURCE)

    assert any(
        "BLUEPRINT_PARTICIPANT_EVIDENCE_MISSING" in error
        and "白洁" in error
        and "王申" in error
        for error in errors
    )


def test_real_blueprint_participant_failure_returns_exact_retry_contract() -> None:
    case = _blueprint_cross_field_run_fixtures()[0]
    shard = NarrativeBlueprintShard.model_validate(case["payload"])

    errors = validate_narrative_blueprint_shard(
        shard,
        expected_episode_no=case["episode_no"],
        expected_shard_index=shard.shard_index,
        expected_source_segment_ids=shard.source_segment_ids,
    )

    matching = [
        error
        for error in errors
        if case["expected_error_code"] in error
    ]
    assert len(matching) == 1
    assert case["expected_identity"] in matching[0]
    assert "保留有来源角色" in matching[0]
    assert "不得删除角色或改用默认身份" in matching[0]


def test_real_blueprint_non_audible_voice_drift_normalizes_exact_unit() -> None:
    case = _blueprint_cross_field_run_fixtures()[1]
    with pytest.raises(ValueError, match="非声音 delivery"):
        NarrativeBlueprintShard.model_validate(case["payload"])

    normalized = normalize_blueprint_provider_payload(case["payload"])
    shard = NarrativeBlueprintShard.model_validate(normalized)
    node = shard.nodes[0]

    assert set(node.participants) == {
        evidence.identity_key for evidence in node.participant_evidence
    }
    assert all(
        case["normalized_voice_unit"] not in evidence.source_unit_keys
        for evidence in node.participant_evidence
        if evidence.usage == "voice"
    )
    assert node.source_unit_deliveries[0].content_owner_key == "杂役木牌"


def test_provider_normalization_adds_evidence_identity_without_deleting_roster() -> None:
    payload = _blueprint_cross_field_run_fixtures()[0]["payload"]
    payload = json.loads(json.dumps(payload, ensure_ascii=False))
    node = payload["nodes"][0]
    node["participant_evidence"].append({
        "identity_key": "CHAR_SOURCE_BACKED_EXTRA",
        "source_segment_ids": ["SRC0004"],
        "source_unit_keys": [],
        "usage": "visible",
    })
    original_participants = list(node["participants"])

    normalized = normalize_blueprint_provider_payload(payload)

    assert normalized["nodes"][0]["participants"] == [
        *original_participants,
        "CHAR_SOURCE_BACKED_EXTRA",
    ]


def test_blueprint_patch_unknown_keeps_stable_operation_and_retry_lineage(
    monkeypatch,
) -> None:
    operation_ids: list[str] = []
    traces = iter([
        SimpleNamespace(run_id="run-first", step_run_id="step-first"),
        SimpleNamespace(run_id="run-explicit-retry", step_run_id="step-second"),
    ])

    async def unknown_patch(*_args, **kwargs):
        operation_ids.append(kwargs["operation_id"])
        raise hiagent.ProviderError(
            "read outcome unknown",
            delivery_state="unknown",
            replay_safe=False,
        )

    monkeypatch.setattr(stages.model_gateway, "chat_structured", unknown_patch)
    monkeypatch.setattr(
        stages.hiagent,
        "text_request_token_limits",
        lambda **_kwargs: ("hiagent", "model", 16384),
    )
    monkeypatch.setattr(
        "app.observability.tracing.current_trace",
        lambda: next(traces),
    )
    blueprint = _blueprint()
    blueprint.nodes[0].participant_evidence = [
        evidence
        for evidence in blueprint.nodes[0].participant_evidence
        if evidence.usage != "state_subject"
    ]
    first_budget = stages._BlueprintGenerationBudget()
    first_budget.retry_grant_id = "grant-first"
    with pytest.raises(hiagent.ProviderError, match="outcome unknown"):
        asyncio.run(stages._repair_narrative_blueprint(
            blueprint.model_copy(deep=True),
            episode={"id": "ep-patch-retry"},
            source_text=SOURCE,
            generation_budget=first_budget,
        ))

    retry_budget = stages._BlueprintGenerationBudget()
    retry_budget.provider_calls = first_budget.provider_calls
    retry_budget.unknown_output_tokens = first_budget.unknown_output_tokens
    retry_budget._durable_unknown_operations[operation_ids[0]] = "grant-first"
    retry_budget._durable_unknown_stage_calls[
        "screenplay_blueprint_patch"
    ] = (1, "grant-first")
    retry_budget.retry_grant_id = "grant-explicit-retry"
    retry_budget.authorize_unknown_retry("grant-explicit-retry")
    with pytest.raises(hiagent.ProviderError, match="outcome unknown"):
        asyncio.run(stages._repair_narrative_blueprint(
            blueprint.model_copy(deep=True),
            episode={"id": "ep-patch-retry"},
            source_text=SOURCE,
            generation_budget=retry_budget,
        ))

    assert operation_ids[0] == operation_ids[1]
    assert "run-first" not in operation_ids[0]
    assert "run-explicit-retry" not in operation_ids[1]
    assert first_budget.provider_calls == 1
    assert first_budget.unknown_output_tokens == 16384
    assert retry_budget.provider_calls == 2
    assert retry_budget.unknown_output_tokens == 32768


def test_blueprint_unknown_retry_without_fresh_grant_sends_nothing() -> None:
    budget = stages._BlueprintGenerationBudget()
    budget._durable_unknown_operations["op-unknown"] = "grant-old"
    budget.retry_grant_id = "grant-old"

    with pytest.raises(stages.StageError, match="RETRY_GRANT_REQUIRED"):
        budget.claim(max_tokens=100, operation_id="op-unknown")

    assert budget.provider_calls == 0


def test_blueprint_patch_durable_success_requires_exact_cached_replay(
    monkeypatch,
) -> None:
    observed_meta: dict = {}

    async def cache_miss(*_args, **kwargs):
        observed_meta.update(kwargs["call_meta"])
        raise hiagent.ProviderError(
            "durable replay missing",
            failure_kind="durable_replay_missing",
            delivery_state="not_sent",
            replay_safe=True,
        )

    monkeypatch.setattr(stages.model_gateway, "chat_structured", cache_miss)
    monkeypatch.setattr(
        stages,
        "_blueprint_structured_operation_id",
        lambda **_kwargs: ("stable-patch-success", 4096),
    )
    monkeypatch.setattr(
        "app.observability.tracing.current_trace",
        lambda: SimpleNamespace(run_id="run-replay", step_run_id="step-replay"),
    )
    blueprint = _blueprint()
    blueprint.nodes[0].participant_evidence = [
        evidence
        for evidence in blueprint.nodes[0].participant_evidence
        if evidence.usage != "state_subject"
    ]
    budget = stages._BlueprintGenerationBudget()
    budget._durable_successful_operations.add("stable-patch-success")

    with pytest.raises(hiagent.ProviderError, match="durable replay missing"):
        asyncio.run(stages._repair_narrative_blueprint(
            blueprint,
            episode={"id": "ep-replay"},
            source_text=SOURCE,
            generation_budget=budget,
        ))

    assert observed_meta["reuse_successful_operation"] is True
    assert observed_meta["require_cached_successful_operation"] is True
    assert budget.provider_calls == 0


def test_current_blueprint_selector_prefers_current_same_hash_wrapper() -> None:
    blueprint = _blueprint()
    current_snapshot = stages._current_blueprint_authority_snapshot(
        SOURCE,
        generation_mode="test",
    )
    rows = [{
        "id": "art-old-same-hash",
        "content_json": blueprint.model_dump_json(),
        "contract_version": stages.BLUEPRINT_VERSION,
        "prompt_version": stages.SCREENPLAY_BLUEPRINT_PROMPT_VERSION,
        "model_snapshot_json": "{}",
    }, {
        "id": "art-current-wrapper",
        "content_json": blueprint.model_dump_json(),
        "contract_version": stages.BLUEPRINT_VERSION,
        "prompt_version": stages.SCREENPLAY_BLUEPRINT_PROMPT_VERSION,
        "model_snapshot_json": json.dumps(current_snapshot),
    }]

    selected, legacy = stages._select_current_blueprint_artifact(
        rows,
        blueprint,
        SOURCE,
    )

    assert selected == "art-current-wrapper"
    assert legacy == "art-old-same-hash"


def test_only_old_same_hash_blueprint_requires_current_wrapper() -> None:
    blueprint = _blueprint()
    selected, legacy = stages._select_current_blueprint_artifact(
        [{
            "id": "art-old-same-hash",
            "content_json": blueprint.model_dump_json(),
            "contract_version": stages.BLUEPRINT_VERSION,
            "prompt_version": stages.SCREENPLAY_BLUEPRINT_PROMPT_VERSION,
            "model_snapshot_json": json.dumps({
                "shard_policy_version": "blueprint-shard-policy.v2",
            }),
        }],
        blueprint,
        SOURCE,
    )

    assert selected is None
    assert legacy == "art-old-same-hash"


def test_semantic_repair_validated_artifact_writes_current_authority_snapshot(
    monkeypatch,
) -> None:
    created: list = []
    monkeypatch.setattr(stages, "validate_narrative_blueprint", lambda *_a: [])
    monkeypatch.setattr(
        "app.evidence.repository.create_artifact",
        lambda artifact, **_kwargs: created.append(artifact) or {"id": "art-new"},
    )
    monkeypatch.setattr(
        "app.observability.tracing.current_trace",
        lambda: SimpleNamespace(step_run_id="step-repair-snapshot"),
    )

    result = asyncio.run(stages._repair_narrative_blueprint(
        _blueprint(),
        episode={"id": "ep-repair-snapshot"},
        source_text=SOURCE,
    ))

    assert result == _blueprint()
    snapshot = created[-1].model_snapshot
    assert stages._blueprint_authority_snapshot_is_current(snapshot, SOURCE)
def test_targeted_reviewer_conflict_triggers_full_review(monkeypatch) -> None:
    blueprint = _blueprint()
    derive_blueprint_scene_plans(blueprint)
    modes: list[str] = []

    async def fake_structured(*_args, **kwargs):
        meta = kwargs["call_meta"]
        modes.append(meta["substage"])
        if meta["substage"] == "full":
            return BlueprintSemanticReview(issues=[])
        code = "timeline_conflict" if meta["review_sample"] == 1 else "spatial_action_gap"
        return BlueprintSemanticReview(issues=[BlueprintSemanticIssue(
            code=code,
            node_keys=["n2"],
            source_segment_ids=["SRC0002"],
            message="两位审稿人的定向判断不同",
            required_resolution="仅在全量上下文确认后处理",
        )])

    monkeypatch.setattr(stages.model_gateway, "chat_structured", fake_structured)
    monkeypatch.setattr(
        stages,
        "get_setting",
        lambda key: "true" if key == "screenplay_targeted_blueprint_review_enabled" else "1",
    )
    result = asyncio.run(stages._semantic_review_narrative_blueprint(
        blueprint,
        episode={"id": f"ep-blueprint-full-fallback-{uuid.uuid4()}", "episode_no": 8},
        source_text=SOURCE,
    ))
    assert result is blueprint
    assert modes == ["risk_nodes", "risk_nodes", "full", "full"]


def test_clean_semantic_review_cache_is_bound_to_source_corpus(
    monkeypatch,
) -> None:
    blueprint = _blueprint()
    derive_blueprint_scene_plans(blueprint)
    source_now = SOURCE + "\n新的来源版本"
    blueprint_hash = hashlib.sha256(
        json.dumps(
            blueprint.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    stale_snapshot = {
        "review_policy_version": stages.BLUEPRINT_SEMANTIC_REVIEW_POLICY_VERSION,
        "authority_fingerprint": blueprint_authority_validator_fingerprint(),
        "source_corpus_hash": hashlib.sha256(SOURCE.encode("utf-8")).hexdigest(),
        "review_input_fingerprint": "stale-review-input",
    }

    class CachedRows:
        @staticmethod
        def fetchall():
            return [{
                "id": "stale-clean-consensus",
                "content_json": json.dumps({
                    "blueprint_hash": blueprint_hash,
                    "consensus_issue_keys": [],
                    "review_outcome": "clean",
                }),
                "model_snapshot_json": json.dumps(stale_snapshot),
            }]

    class CachedConnection:
        @staticmethod
        def execute(*_args, **_kwargs):
            return CachedRows()

    calls = 0

    async def fake_structured(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return BlueprintSemanticReview(issues=[])

    monkeypatch.setattr(stages, "get_conn", lambda: CachedConnection())
    monkeypatch.setattr(stages.model_gateway, "chat_structured", fake_structured)
    monkeypatch.setattr(
        stages,
        "get_setting",
        lambda key: "false"
        if key == "screenplay_targeted_blueprint_review_enabled"
        else "1",
    )
    monkeypatch.setattr(
        "app.evidence.repository.create_artifact",
        lambda *_args, **_kwargs: {"id": str(uuid.uuid4())},
    )

    result = asyncio.run(stages._semantic_review_narrative_blueprint(
        blueprint,
        episode={"id": "episode-source-drift-review"},
        source_text=source_now,
    ))

    assert result is blueprint
    assert calls == 2


def test_reviewer_input_and_retry_share_canonical_contract(
    monkeypatch,
) -> None:
    blueprint = _blueprint()
    derive_blueprint_scene_plans(blueprint)
    calls: list[tuple[list[dict], dict]] = []

    class EmptyRows:
        @staticmethod
        def fetchall():
            return []

    class EmptyConnection:
        @staticmethod
        def execute(*_args, **_kwargs):
            return EmptyRows()

    async def fake_structured(messages, **kwargs):
        calls.append((messages, kwargs))
        normalized = kwargs["normalize_payload"]({
            "issues": [{
                "code": "timeline_conflict",
                "node_keys": [{"ordinal": 2}],
                "message": "验证本轮 ordinal 合同",
                "required_resolution": "保持引用精确",
            }],
        })
        assert normalized["issues"][0]["node_keys"] == ["n2"]
        return BlueprintSemanticReview(issues=[])

    monkeypatch.setattr(stages, "get_conn", lambda: EmptyConnection())
    monkeypatch.setattr(
        stages,
        "get_setting",
        lambda key: "true"
        if key == "screenplay_targeted_blueprint_review_enabled"
        else "1",
    )
    monkeypatch.setattr(
        stages.model_gateway,
        "chat_structured",
        fake_structured,
    )
    monkeypatch.setattr(
        "app.evidence.repository.create_artifact",
        lambda *_args, **_kwargs: {"id": str(uuid.uuid4())},
    )

    result = asyncio.run(stages._semantic_review_narrative_blueprint(
        blueprint,
        episode={"id": "episode-canonical-review-contract"},
        source_text=SOURCE,
    ))

    assert result is blueprint
    assert len(calls) == 2
    messages, kwargs = calls[0]
    prompt = messages[1]["content"]
    expected_keys = [node.key for node in blueprint.nodes]
    alternatives = kwargs["output_schema"]["$defs"][
        "BlueprintSemanticIssue"
    ]["properties"]["node_keys"]["items"]["oneOf"]
    retry_contract = json.loads(kwargs["repair_context"])
    assert alternatives[0]["enum"] == expected_keys
    assert [item["identity"] for item in retry_contract[
        "node_reference_contract"
    ]["canonical_nodes"]] == expected_keys
    assert "本轮节点引用合同" in prompt
    assert "禁止根据文本相似度推断、拼接或改写 identity" in prompt


def test_blueprint_generation_reuses_validated_cached_artifact(
    monkeypatch,
) -> None:
    blueprint = _blueprint()
    derive_blueprint_scene_plans(blueprint)
    sql_seen: list[str] = []
    logged_kinds: list[str] = []
    reviewed: list[NarrativeBlueprint] = []

    class QueryResult:
        def __init__(self, *, one=None, many=None):
            self.one = one
            self.many = many or []

        def fetchone(self):
            return self.one

        def fetchall(self):
            return self.many

    class CacheConnection:
        @staticmethod
        def execute(sql, _params):
            sql_seen.append(sql)
            if "SELECT started_at" in sql:
                return QueryResult(one={
                    "started_at": None,
                    "input_fingerprint": "same-input",
                    "config_snapshot_json": json.dumps({
                        "blueprint_budget_lineage_fingerprint": "same-input",
                    }),
                })
            if "FROM provider_calls" in sql:
                return QueryResult(many=[])
            if "SELECT input_fingerprint" in sql:
                return QueryResult(one={"input_fingerprint": "same-input"})
            if "FROM artifacts a" in sql:
                assert "a.status='validated'" in sql
                return QueryResult(many=[{
                    "content_json": blueprint.model_dump_json(),
                }])
            raise AssertionError(sql)

    async def fake_review(value, **_kwargs):
        reviewed.append(value)
        return value

    async def fail_generate(*_args, **_kwargs):
        raise AssertionError("validated Blueprint cache was not reused")

    monkeypatch.setattr(stages, "get_conn", lambda: CacheConnection())
    monkeypatch.setattr(
        "app.observability.tracing.current_trace",
        lambda: SimpleNamespace(run_id="run-cache-replay"),
    )
    monkeypatch.setattr(
        stages,
        "log_provider_call",
        lambda kind, *_args, **_kwargs: logged_kinds.append(kind),
    )
    monkeypatch.setattr(
        stages,
        "validate_narrative_blueprint",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        stages,
        "_semantic_review_narrative_blueprint",
        fake_review,
    )
    monkeypatch.setattr(
        stages,
        "_generate_sharded_narrative_blueprint",
        fail_generate,
    )

    result = asyncio.run(stages._generate_screenplay_narrative_blueprint(
        {
            "id": "episode-cache-replay",
            "episode_no": 8,
        },
        SOURCE,
        SimpleNamespace(),
    ))

    assert result.model_dump(mode="json") == blueprint.model_dump(mode="json")
    assert reviewed and [node.key for node in reviewed[0].nodes] == [
        node.key for node in blueprint.nodes
    ]
    assert logged_kinds == ["screenplay_blueprint_local_recompile"]
    assert any("a.status='validated'" in sql for sql in sql_seen)


def test_blueprint_review_fails_closed_when_one_reviewer_is_unavailable(
    monkeypatch,
) -> None:
    blueprint = _blueprint()
    derive_blueprint_scene_plans(blueprint)

    async def fake_structured(*_args, **kwargs):
        if kwargs["call_meta"]["review_sample"] == 1:
            raise RuntimeError("review provider unavailable")
        return BlueprintSemanticReview(issues=[])

    monkeypatch.setattr(stages.model_gateway, "chat_structured", fake_structured)
    monkeypatch.setattr(
        stages,
        "get_setting",
        lambda key: "true" if key == "screenplay_targeted_blueprint_review_enabled" else "1",
    )
    with pytest.raises(ContentGenerationError, match="不足两份"):
        asyncio.run(stages._semantic_review_narrative_blueprint(
            blueprint,
            episode={"id": f"ep-blueprint-review-unavailable-{uuid.uuid4()}", "episode_no": 8},
            source_text=SOURCE,
        ))
