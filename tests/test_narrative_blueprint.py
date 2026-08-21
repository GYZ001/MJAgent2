import ast
import asyncio
import hashlib
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import uuid

import pytest

from app import hiagent, stages
from app.evidence import repository as evidence_repository
from app.errors import ContentGenerationError
from app.harness import model_gateway
from app.narrative_blueprint import (
    BlueprintDecision,
    BlueprintSourceOccurrenceError,
    BlueprintSemanticReview,
    BlueprintSemanticIssue,
    BlueprintStateSubjectOwnershipPatch,
    BlueprintStateSubjectOwnershipRepair,
    BlueprintStateChange,
    BlueprintStateRequirement,
    NarrativeBlueprint,
    NarrativeBlueprintPatch,
    NarrativeBlueprintShard,
    NarrativeNode,
    apply_blueprint_state_subject_misclassification_patch,
    apply_blueprint_state_subject_ownership_patch,
    apply_narrative_blueprint_patch,
    blueprint_authority_validator_fingerprint,
    blueprint_candidate_hash,
    blueprint_environment_subject_issue_has_exact_authority,
    blueprint_patch_schema,
    blueprint_shard_candidate_hash,
    blueprint_shard_provider_schema,
    blueprint_state_subject_misclassification_patch_schema,
    blueprint_state_subject_ownership_patch_schema,
    blueprint_state_subject_issues,
    blueprint_semantic_issue_is_resolved,
    blueprint_semantic_review_schema,
    derive_blueprint_scene_plans,
    filter_blueprint_semantic_review_voice_issues,
    normalize_blueprint_agency_continuity,
    normalize_blueprint_provider_payload,
    normalize_blueprint_requirement_state_keys,
    normalize_blueprint_semantic_review_payload,
    normalize_blueprint_state_subject_perception,
    recover_complete_blueprint_prefix,
    validate_and_apply_blueprint_scene_contract,
    validate_blueprint_semantic_review,
    validate_blueprint_scene_partition,
    validate_narrative_blueprint,
    validate_narrative_blueprint_patch_projection,
    validate_narrative_blueprint_shard,
)
from app.source_facts import SOURCE_FACT_VERSION, SourceFact, source_facts


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


def _state_subject_retry_fixture() -> dict:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "run_d67f041a6df4_calls29716_29717_state_subject.json"
    )
    return json.loads(fixture_path.read_text(encoding="utf-8"))


def _replay_source_fact(value: dict) -> SourceFact:
    return SourceFact.model_validate({
        **value,
        "contract_version": SOURCE_FACT_VERSION,
    })


def test_semantic_issue_producer_codes_are_declared_in_literal() -> None:
    module_path = Path(__file__).parents[1] / "app" / "narrative_blueprint.py"
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    producer_codes: set[str] = set()
    unresolved_expressions: list[str] = []

    for scope in (
        node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ):
        local_constants: dict[str, set[str]] = {}
        for node in ast.walk(scope):
            if (
                isinstance(node, ast.Assign)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
            ):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        local_constants.setdefault(target.id, set()).add(
                            node.value.value
                        )

        for node in ast.walk(scope):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "BlueprintSemanticIssue"
            ):
                continue
            code_keyword = next(
                keyword for keyword in node.keywords
                if keyword.arg == "code"
            )
            if (
                isinstance(code_keyword.value, ast.Constant)
                and isinstance(code_keyword.value.value, str)
            ):
                producer_codes.add(code_keyword.value.value)
            elif isinstance(code_keyword.value, ast.Name):
                values = local_constants.get(code_keyword.value.id, set())
                producer_codes.update(values)
                if not values:
                    unresolved_expressions.append(code_keyword.value.id)
            else:
                unresolved_expressions.append(ast.dump(code_keyword.value))

    declared_codes = set(
        BlueprintSemanticIssue.model_json_schema()["properties"]["code"]["enum"]
    )
    assert unresolved_expressions == []
    assert "state_subject_perception_missing" in producer_codes
    assert "state_subject_perception_missing" in declared_codes
    assert producer_codes <= declared_codes, (
        f"BlueprintSemanticIssue producer codes missing from Literal: "
        f"{sorted(producer_codes - declared_codes)}"
    )


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
                }, {
                    "identity_key": "白洁",
                    "source_segment_ids": ["SRC0001"],
                    "source_unit_keys": [],
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
                }, {
                    "identity_key": "白洁",
                    "source_segment_ids": ["SRC0002"],
                    "source_unit_keys": [],
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
                }, {
                    "identity_key": "白洁",
                    "source_segment_ids": ["SRC0003"],
                    "source_unit_keys": [],
                    "usage": "visible",
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
                }, {
                    "identity_key": "小张",
                    "source_segment_ids": ["SRC0004"],
                    "source_unit_keys": [],
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


def test_err_20260820_e7b06d_requirement_state_key_converges_to_fact() -> None:
    blueprint = _blueprint()
    requirement = blueprint.nodes[3].state_requirements[0]
    assert requirement.required_fact_key == "F001"
    # LLM authored a free-text label that disagrees with the authoritative
    # fact's state_key, so the deterministic gate cannot close on its own.
    requirement.state_key = "司机的自由文本标签"

    assert any(
        error.startswith("[BLUEPRINT_STATE_KEY_MISMATCH]")
        for error in validate_narrative_blueprint(blueprint, SOURCE)
    )

    changes = normalize_blueprint_requirement_state_keys(blueprint)

    assert changes >= 1
    assert requirement.state_key == "vehicle:wang:driver"
    assert not any(
        error.startswith("[BLUEPRINT_STATE_KEY_MISMATCH]")
        for error in validate_narrative_blueprint(blueprint, SOURCE)
    )
    assert validate_narrative_blueprint(blueprint, SOURCE) == []


def test_err_20260820_e7b06d_missing_fact_still_unestablished() -> None:
    blueprint = _blueprint()
    requirement = blueprint.nodes[3].state_requirements[0]
    requirement.required_fact_key = "F999-does-not-exist"
    requirement.state_key = "司机的自由文本标签"

    changes = normalize_blueprint_requirement_state_keys(blueprint)

    # A missing fact is a genuine "dependency not established" error and must
    # not be masked: the label is left untouched and the gate still fires.
    assert changes == 0
    assert requirement.state_key == "司机的自由文本标签"
    assert any(
        error.startswith("[BLUEPRINT_STATE_UNESTABLISHED]")
        for error in validate_narrative_blueprint(blueprint, SOURCE)
    )


def test_err_20260820_e7b06d_assumed_prior_untouched() -> None:
    blueprint = _blueprint()
    requirement = blueprint.nodes[3].state_requirements[0]
    requirement.assumed_prior = True
    requirement.required_fact_key = "F001"
    requirement.state_key = "assumed:自由文本标签"

    changes = normalize_blueprint_requirement_state_keys(blueprint)

    assert changes == 0
    assert requirement.state_key == "assumed:自由文本标签"


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
        ["SRC0001:unit:001", "SRC0002:unit:001"],
    )
    issue_properties = review_schema["$defs"][
        "BlueprintSemanticIssue"
    ]["properties"]
    assert review_schema["x-canonical-timeline-node-keys"] == ["n1", "n2"]
    assert issue_properties["source_segment_ids"]["items"]["enum"] == [
        "SRC0001", "SRC0002",
    ]
    assert issue_properties["source_unit_keys"]["items"]["enum"] == [
        "SRC0001:unit:001", "SRC0002:unit:001",
    ]
    misclassified_contract = review_schema["$defs"][
        "BlueprintSemanticIssue"
    ]["allOf"][-1]["then"]["properties"]
    assert misclassified_contract["node_keys"]["maxItems"] == 1
    assert misclassified_contract["source_unit_keys"]["minItems"] == 1

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
    subjects = [
        evidence.model_copy(deep=True)
        for evidence in node.participant_evidence
        if evidence.usage == "state_subject"
    ]
    node.participant_evidence = [
        evidence for evidence in node.participant_evidence
        if evidence.usage != "state_subject"
    ]
    replacement = node.model_copy(deep=True)
    replacement.participant_evidence.extend(subjects)
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


def test_blueprint_review_retries_replay_safe_not_sent_reviewer(
    monkeypatch,
) -> None:
    """A reviewer whose request was never sent (replay-safe ProviderError) is
    retried instead of failing the whole dual-review gate. The deterministic
    operation_id is unchanged, so the retry is the same semantic review."""
    clean_review = BlueprintSemanticReview.model_validate({"issues": []})
    reviewer_calls = 0

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
        if reviewer_calls == 1:
            raise hiagent.ProviderError(
                "connection_failed: not sent",
                delivery_state="not_sent",
                replay_safe=True,
            )
        return clean_review.model_copy(deep=True)

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
    monkeypatch.setattr(
        "app.evidence.repository.create_artifact",
        lambda *_args, **_kwargs: {"id": str(uuid.uuid4())},
    )

    result = asyncio.run(stages._semantic_review_narrative_blueprint(
        _blueprint(),
        episode={"id": "episode-review-retry"},
        source_text=SOURCE,
    ))

    assert result is not None
    # reviewer1: not_sent (1) + retry success (2); reviewer2: success (3).
    assert reviewer_calls == 3


def test_blueprint_review_does_not_retry_unknown_outcome_reviewer(
    monkeypatch,
) -> None:
    """A reviewer failure whose outcome is genuinely unknown (not replay-safe)
    is NOT retried — the dual-review gate still fails closed."""
    reviewer_calls = 0

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
        raise hiagent.ProviderError(
            "mid-stream interruption, outcome unknown",
            delivery_state="unknown",
            replay_safe=False,
        )

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
    monkeypatch.setattr(
        "app.evidence.repository.create_artifact",
        lambda *_args, **_kwargs: {"id": str(uuid.uuid4())},
    )

    with pytest.raises(
        ContentGenerationError,
        match="蓝图语义审稿人不足两份",
    ):
        asyncio.run(stages._semantic_review_narrative_blueprint(
            _blueprint(),
            episode={"id": "episode-review-no-retry"},
            source_text=SOURCE,
        ))

    assert reviewer_calls == 2


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
        if (
            evidence.usage != "state_subject"
            and evidence.identity_key != "白洁"
        )
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


def test_joint_action_uses_typed_assignment_without_selecting_one_subject() -> None:
    source = "孟浩与小胖子走出了屋舍。"
    blueprint = NarrativeBlueprint.model_validate({
        "episode_no": 3,
        "nodes": [{
            "key": "joint-node",
            "source_segment_ids": ["SRC0001"],
            "summary": "两人共同离开屋舍",
            "narrative_layer": "story",
            "event_priority": "causal",
            "render_policy": "standalone",
            "temporal_domain_key": "morning",
            "time_label": "清晨",
            "time_relation": "episode_start",
            "location_key": "dorm",
            "location_label": "杂役屋",
            "participants": ["孟浩", "小胖子"],
            "participant_evidence": [{
                "identity_key": identity_key,
                "source_segment_ids": ["SRC0001"],
                "source_unit_keys": ["SRC0001:unit:001"],
                "usage": "visible",
            } for identity_key in ("孟浩", "小胖子")],
            "state_subject_assignments": [{
                "source_unit_key": "SRC0001:unit:001",
                "mode": "joint",
                "identity_keys": ["孟浩", "小胖子"],
            }],
            "action_logic": "两人共同走出屋舍",
        }],
    })

    assert blueprint_state_subject_issues(blueprint, source) == []


def test_state_subject_requires_applicable_perception_evidence() -> None:
    source = "孟浩抬头。"
    blueprint = NarrativeBlueprint.model_validate({
        "episode_no": 3,
        "nodes": [{
            "key": "subject-node",
            "source_segment_ids": ["SRC0001"],
            "summary": "孟浩抬头",
            "narrative_layer": "story",
            "event_priority": "causal",
            "render_policy": "standalone",
            "temporal_domain_key": "present",
            "time_label": "当下",
            "time_relation": "episode_start",
            "location_key": "yard",
            "location_label": "院中",
            "participants": ["孟浩"],
            "participant_evidence": [{
                "identity_key": "孟浩",
                "source_segment_ids": ["SRC0001"],
                "source_unit_keys": ["SRC0001:unit:001"],
                "usage": "state_subject",
            }],
            "action_logic": "孟浩抬头",
        }],
    })

    issues = blueprint_state_subject_issues(blueprint, source)

    assert [
        (
            issue.code,
            issue.source_unit_keys,
        )
        for issue in issues
    ] == [(
        "state_subject_perception_missing",
        ["SRC0001:unit:001"],
    )]
    blueprint.nodes[0].participant_evidence.append(
        blueprint.nodes[0].participant_evidence[0].model_copy(update={
            "source_unit_keys": [],
            "usage": "visible",
        })
    )
    assert blueprint_state_subject_issues(blueprint, source) == []
    blueprint.nodes[0].participant_evidence[-1].source_segment_ids = [
        "SRC0002"
    ]
    assert [
        issue.code
        for issue in blueprint_state_subject_issues(blueprint, source)
    ] == ["state_subject_perception_missing"]


def test_state_subject_perception_normalizer_groups_and_is_idempotent() -> None:
    source = "。".join(
        f"甲执行动作{index}" for index in range(1, 31)
    ) + "。"
    unit_keys = [
        fact.source_unit_key
        for fact in source_facts(source)
        if fact.projection == "action"
    ]
    blueprint = NarrativeBlueprint.model_validate({
        "episode_no": 3,
        "nodes": [{
            "key": "many-subjects",
            "source_segment_ids": ["SRC0001"],
            "summary": "甲连续行动",
            "narrative_layer": "story",
            "event_priority": "causal",
            "render_policy": "standalone",
            "temporal_domain_key": "present",
            "time_label": "当下",
            "time_relation": "episode_start",
            "location_key": "room",
            "location_label": "房间",
            "participants": ["甲"],
            "participant_evidence": [{
                "identity_key": "甲",
                "source_segment_ids": ["SRC0001"],
                "source_unit_keys": [unit_key],
                "usage": "state_subject",
            } for unit_key in unit_keys],
            "action_logic": "甲连续完成三十个动作",
        }],
    })

    assert len(blueprint_state_subject_issues(blueprint, source)) == 30
    assert normalize_blueprint_state_subject_perception(blueprint) == 1
    visible = [
        evidence
        for evidence in blueprint.nodes[0].participant_evidence
        if evidence.usage == "visible"
    ]
    assert len(visible) == 1
    assert visible[0].source_segment_ids == ["SRC0001"]
    assert visible[0].source_unit_keys == unit_keys
    assert validate_narrative_blueprint(blueprint, source) == []

    normalized = blueprint.model_dump(mode="json")
    assert normalize_blueprint_state_subject_perception(blueprint) == 0
    assert blueprint.model_dump(mode="json") == normalized


def test_state_subject_perception_normalizer_covers_all_joint_identities() -> None:
    source = "甲和乙抬桌。"
    unit_key = source_facts(source)[0].source_unit_key
    blueprint = NarrativeBlueprint.model_validate({
        "episode_no": 3,
        "nodes": [{
            "key": "joint-subjects",
            "source_segment_ids": ["SRC0001"],
            "summary": "甲乙共同抬桌",
            "narrative_layer": "story",
            "event_priority": "causal",
            "render_policy": "standalone",
            "temporal_domain_key": "present",
            "time_label": "当下",
            "time_relation": "episode_start",
            "location_key": "room",
            "location_label": "房间",
            "participants": ["甲", "乙"],
            "state_subject_assignments": [{
                "source_unit_key": unit_key,
                "mode": "joint",
                "identity_keys": ["甲", "乙"],
            }],
            "action_logic": "甲乙共同抬桌",
        }],
    })

    assert normalize_blueprint_state_subject_perception(blueprint) == 2
    assert {
        evidence.identity_key: evidence.source_unit_keys
        for evidence in blueprint.nodes[0].participant_evidence
        if evidence.usage == "visible"
    } == {
        "甲": [unit_key],
        "乙": [unit_key],
    }
    assert validate_narrative_blueprint(blueprint, source) == []


def test_state_subject_perception_normalizer_skips_environment_and_cross_src() -> None:
    blueprint = NarrativeBlueprint.model_validate({
        "episode_no": 3,
        "nodes": [{
            "key": "environment",
            "source_segment_ids": ["SRC0001"],
            "summary": "雨落下",
            "narrative_layer": "story",
            "event_priority": "causal",
            "render_policy": "standalone",
            "temporal_domain_key": "present",
            "time_label": "当下",
            "time_relation": "episode_start",
            "location_key": "yard",
            "location_label": "院中",
            "participants": ["甲"],
            "participant_evidence": [{
                "identity_key": "甲",
                "source_segment_ids": ["SRC0002"],
                "source_unit_keys": ["SRC0002:unit:001"],
                "usage": "state_subject",
            }],
            "environment_source_unit_keys": ["SRC0001:unit:001"],
            "action_logic": "雨落下",
        }, {
            "key": "owned-second-source",
            "source_segment_ids": ["SRC0002"],
            "summary": "甲抬头",
            "narrative_layer": "story",
            "event_priority": "causal",
            "render_policy": "standalone",
            "temporal_domain_key": "present",
            "time_label": "当下",
            "time_relation": "continuous",
            "location_key": "yard",
            "location_label": "院中",
            "environment_source_unit_keys": ["SRC0002:unit:001"],
            "action_logic": "甲抬头",
        }],
    })

    before = blueprint.model_dump(mode="json")
    assert normalize_blueprint_state_subject_perception(blueprint) == 0
    assert blueprint.model_dump(mode="json") == before
    assert all(
        evidence.usage != "visible"
        for node in blueprint.nodes
        for evidence in node.participant_evidence
    )


def test_repair_normalizes_perception_without_full_node_patch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = "孟浩抬头。"
    blueprint = NarrativeBlueprint.model_validate({
        "episode_no": 3,
        "nodes": [{
            "key": "subject-node",
            "source_segment_ids": ["SRC0001"],
            "summary": "孟浩抬头",
            "narrative_layer": "story",
            "event_priority": "causal",
            "render_policy": "standalone",
            "temporal_domain_key": "present",
            "time_label": "当下",
            "time_relation": "episode_start",
            "location_key": "yard",
            "location_label": "院中",
            "participants": ["孟浩"],
            "participant_evidence": [{
                "identity_key": "孟浩",
                "source_segment_ids": ["SRC0001"],
                "source_unit_keys": ["SRC0001:unit:001"],
                "usage": "state_subject",
            }],
            "action_logic": "孟浩抬头",
        }],
    })
    calls = 0

    async def forbidden_patch(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("perception-only repair must stay local")

    monkeypatch.setattr(
        stages.model_gateway,
        "chat_structured",
        forbidden_patch,
    )
    monkeypatch.setattr(
        "app.evidence.repository.create_artifact",
        lambda _artifact, **_kwargs: {"id": str(uuid.uuid4())},
    )
    monkeypatch.setattr(
        "app.observability.tracing.current_trace",
        lambda: SimpleNamespace(step_run_id="step-local-perception"),
    )
    budget = stages._BlueprintGenerationBudget()

    repaired = asyncio.run(stages._repair_narrative_blueprint(
        blueprint,
        episode={"id": "episode-local-perception"},
        source_text=source,
        generation_budget=budget,
    ))

    assert calls == 0
    assert budget.provider_calls == 0
    assert budget.requested_output_tokens == 0
    assert validate_narrative_blueprint(repaired, source) == []


def _ownership_repair_fixture() -> tuple[
    str,
    NarrativeBlueprintShard,
    list[str],
]:
    source = "“出发。”甲推门，乙和丙抬桌，雨落下，甲坐下。"
    facts = stages.source_segment_facts("SRC0001", source)
    quoted_key = next(
        fact.source_unit_key
        for fact in facts
        if fact.projection == "quoted"
    )
    action_keys = [
        fact.source_unit_key
        for fact in facts
        if fact.projection == "action"
    ]
    shard = NarrativeBlueprintShard.model_validate({
        "episode_no": 1,
        "shard_index": 1,
        "source_segment_ids": ["SRC0001"],
        "nodes": [{
            "key": "ownership-node",
            "source_segment_ids": ["SRC0001"],
            "summary": "三人搬桌，雨落下",
            "narrative_layer": "story",
            "event_priority": "causal",
            "render_policy": "standalone",
            "temporal_domain_key": "present",
            "time_label": "当下",
            "time_relation": "episode_start",
            "location_key": "room",
            "location_label": "房间",
            "participants": ["甲", "乙", "丙"],
            "participant_evidence": [{
                "identity_key": "甲",
                "source_segment_ids": ["SRC0001"],
                "source_unit_keys": [quoted_key],
                "usage": "voice",
            }, {
                "identity_key": "乙",
                "source_segment_ids": ["SRC0001"],
                "source_unit_keys": [action_keys[1]],
                "usage": "visible",
            }, {
                "identity_key": "丙",
                "source_segment_ids": ["SRC0001"],
                "source_unit_keys": [action_keys[1]],
                "usage": "visible",
            }, {
                "identity_key": "甲",
                "source_segment_ids": ["SRC0001"],
                "source_unit_keys": [action_keys[3]],
                "usage": "visible",
            }, {
                "identity_key": "乙",
                "source_segment_ids": ["SRC0001"],
                "source_unit_keys": [action_keys[0], action_keys[1]],
                "usage": "state_subject",
            }, {
                "identity_key": "甲",
                "source_segment_ids": ["SRC0001"],
                "source_unit_keys": [action_keys[1], action_keys[3]],
                "usage": "state_subject",
            }, {
                "identity_key": "丙",
                "source_segment_ids": ["SRC0001"],
                "source_unit_keys": [action_keys[2]],
                "usage": "state_subject",
            }],
            "source_unit_deliveries": [{
                "source_unit_key": quoted_key,
                "mode": "spoken_dialogue",
                "performer_key": "甲",
            }],
            "state_requirements": [{
                "required_fact_key": "prior-weather",
                "state_key": "weather",
                "assumed_prior": True,
                "reason": "保留状态字段",
            }],
            "state_changes": [{
                "fact_key": "door-open",
                "state_key": "door",
                "value": "open",
                "reason": "甲推门",
            }],
            "exit_state": "门已打开",
            "action_logic": "甲推门，乙丙搬桌，随后下雨",
        }],
    })
    return source, shard, action_keys


def _environment_misclassification_fixture() -> tuple[
    str,
    NarrativeBlueprint,
    list[str],
]:
    source = "甲推门，乙抬桌，雨落下。\n\n丙关窗。"
    facts = source_facts(source)
    first_source_keys = [
        fact.source_unit_key
        for fact in facts
        if fact.source_segment_id == "SRC0001"
    ]
    second_source_key = next(
        fact.source_unit_key
        for fact in facts
        if fact.source_segment_id == "SRC0002"
    )
    blueprint = NarrativeBlueprint.model_validate({
        "episode_no": 1,
        "nodes": [{
            "key": "misclassified-owner",
            "source_segment_ids": ["SRC0001"],
            "summary": "甲推门，乙抬桌，随后下雨",
            "narrative_layer": "story",
            "event_priority": "causal",
            "render_policy": "standalone",
            "temporal_domain_key": "present",
            "time_label": "当下",
            "time_relation": "episode_start",
            "location_key": "room",
            "location_label": "房间",
            "participants": ["甲", "乙", "旁观者"],
            "participant_evidence": [{
                "identity_key": "甲",
                "source_segment_ids": ["SRC0001"],
                "source_unit_keys": [first_source_keys[0]],
                "usage": "visible",
            }, {
                "identity_key": "乙",
                "source_segment_ids": ["SRC0001"],
                "source_unit_keys": first_source_keys[:2],
                "usage": "visible",
            }, {
                "identity_key": "旁观者",
                "source_segment_ids": ["SRC0001"],
                "source_unit_keys": [],
                "usage": "mentioned",
            }, {
                "identity_key": "乙",
                "source_segment_ids": ["SRC0001"],
                "source_unit_keys": [first_source_keys[1]],
                "usage": "state_subject",
            }],
            "environment_source_unit_keys": [
                first_source_keys[0],
                first_source_keys[2],
            ],
            "state_changes": [{
                "fact_key": "door-open",
                "state_key": "door",
                "value": "open",
                "reason": "保留非 ownership 字段",
            }],
            "exit_state": "门已打开",
            "action_logic": "按来源顺序交付动作",
        }, {
            "key": "frozen-neighbor",
            "source_segment_ids": ["SRC0002"],
            "summary": "丙关窗",
            "narrative_layer": "story",
            "event_priority": "causal",
            "render_policy": "standalone",
            "temporal_domain_key": "present",
            "time_label": "当下",
            "time_relation": "continuous",
            "location_key": "room",
            "location_label": "房间",
            "participants": ["丙"],
            "participant_evidence": [{
                "identity_key": "丙",
                "source_segment_ids": ["SRC0002"],
                "source_unit_keys": [second_source_key],
                "usage": "visible",
            }, {
                "identity_key": "丙",
                "source_segment_ids": ["SRC0002"],
                "source_unit_keys": [second_source_key],
                "usage": "state_subject",
            }],
            "action_logic": "丙关窗",
        }],
    })
    return source, blueprint, first_source_keys


def test_environment_misclassification_review_contract_has_semantic_authority() -> None:
    source, blueprint, action_keys = _environment_misclassification_fixture()
    issue = BlueprintSemanticIssue(
        code="state_subject_environment_misclassified",
        node_keys=["misclassified-owner"],
        source_segment_ids=["SRC0001"],
        source_unit_keys=[action_keys[0]],
        message="该 exact unit 的环境归属遮蔽了人物状态主体",
        required_resolution="仅替换该 unit 的 ownership",
    )

    review = BlueprintSemanticReview(issues=[issue])
    assert validate_blueprint_semantic_review(
        review,
        blueprint,
        source,
    ) == []
    assert blueprint_environment_subject_issue_has_exact_authority(
        issue,
        blueprint,
        source,
    )

    invalid_issues = [
        issue.model_copy(update={
            "node_keys": ["misclassified-owner", "frozen-neighbor"],
        }),
        issue.model_copy(update={"node_keys": ["missing-node"]}),
        issue.model_copy(update={"source_unit_keys": []}),
        issue.model_copy(update={"source_unit_keys": [action_keys[1]]}),
        issue.model_copy(update={"source_segment_ids": []}),
        issue.model_copy(update={
            "source_unit_keys": ["SRC0001:unit:999"],
        }),
    ]
    for invalid_issue in invalid_issues:
        invalid_review = BlueprintSemanticReview(issues=[invalid_issue])
        assert validate_blueprint_semantic_review(
            invalid_review,
            blueprint,
            source,
        )
        assert not blueprint_environment_subject_issue_has_exact_authority(
            invalid_issue,
            blueprint,
            source,
        )


def test_environment_misclassification_text_units_must_match_exact_fields() -> None:
    source = "，".join(
        f"甲执行动作{index}" for index in range(1, 25)
    ) + "。"
    _, blueprint, _ = _environment_misclassification_fixture()
    owner = blueprint.nodes[0]
    unit_12 = "SRC0001:unit:012"
    unit_24 = "SRC0001:unit:024"
    owner.environment_source_unit_keys = [unit_12, unit_24]
    for evidence in owner.participant_evidence:
        if evidence.usage in {"visible", "voice"}:
            evidence.source_unit_keys = [unit_12]

    misaligned_issue = BlueprintSemanticIssue(
        code="state_subject_environment_misclassified",
        node_keys=[owner.key],
        source_segment_ids=["SRC0001"],
        source_unit_keys=[unit_12],
        message=f"{unit_24} 的 environment ownership 错误",
        required_resolution=f"仅修改 {unit_24} 的 state subject",
    )
    misaligned_review = BlueprintSemanticReview(issues=[misaligned_issue])
    assert any(
        "[BLUEPRINT_REVIEW_STATE_SUBJECT_ENVIRONMENT_CONTRACT]" in error
        and unit_24 in error
        for error in validate_blueprint_semantic_review(
            misaligned_review,
            blueprint,
            source,
        )
    )
    assert filter_blueprint_semantic_review_voice_issues(
        misaligned_review,
        blueprint,
        source,
    ) == 1
    assert misaligned_review.issues == []

    complete_issue = misaligned_issue.model_copy(update={
        "source_unit_keys": [unit_12, unit_24],
    })
    unsupported_review = BlueprintSemanticReview(issues=[complete_issue])
    assert filter_blueprint_semantic_review_voice_issues(
        unsupported_review,
        blueprint,
        source,
    ) == 1

    owner.participant_evidence[0].source_unit_keys = [unit_12, unit_24]
    supported_review = BlueprintSemanticReview(issues=[complete_issue])
    assert filter_blueprint_semantic_review_voice_issues(
        supported_review,
        blueprint,
        source,
    ) == 0
    assert supported_review.issues == [complete_issue]


def test_environment_misclassification_filter_requires_exact_repair_authority() -> None:
    source, blueprint, action_keys = _environment_misclassification_fixture()
    issue = BlueprintSemanticIssue(
        code="state_subject_environment_misclassified",
        node_keys=["misclassified-owner"],
        source_segment_ids=["SRC0001"],
        source_unit_keys=[action_keys[0]],
        message="该 environment action unit 应改为人物主体",
        required_resolution="仅使用该 exact unit 的 existing participant authority",
    )

    without_exact_evidence = blueprint.model_copy(deep=True)
    for evidence in without_exact_evidence.nodes[0].participant_evidence:
        if evidence.usage in {"visible", "voice"}:
            evidence.source_unit_keys = [action_keys[1]]
    unsupported = BlueprintSemanticReview(issues=[issue])

    assert filter_blueprint_semantic_review_voice_issues(
        unsupported,
        without_exact_evidence,
        source,
    ) == 1
    assert unsupported.issues == []
    assert validate_blueprint_semantic_review(
        unsupported,
        without_exact_evidence,
        source,
    ) == []

    supported = BlueprintSemanticReview(issues=[issue])
    assert filter_blueprint_semantic_review_voice_issues(
        supported,
        blueprint,
        source,
    ) == 0
    assert supported.issues == [issue]
    assert validate_blueprint_semantic_review(
        supported,
        blueprint,
        source,
    ) == []


def test_full_blueprint_misclassification_schema_exposes_only_authorized_owners() -> None:
    source, blueprint, action_keys = _environment_misclassification_fixture()
    target = action_keys[0]

    schema = blueprint_state_subject_ownership_patch_schema(
        blueprint,
        [target],
        source,
    )
    assert schema == blueprint_state_subject_misclassification_patch_schema(
        blueprint,
        [target],
        source,
    )
    assert schema["properties"]["base_candidate_hash"]["const"] == (
        blueprint_candidate_hash(blueprint)
    )
    definition = next(iter(schema["$defs"].values()))
    modes = {
        option["properties"]["mode"]["const"]
        for option in definition["oneOf"]
    }
    identity_enums = {
        identity
        for option in definition["oneOf"]
        for identity in option["properties"]["identity_keys"]["items"]["enum"]
    }

    assert modes == {"single", "joint"}
    assert "environment" not in json.dumps(schema)
    assert identity_enums == {"甲", "乙"}
    assert "旁观者" not in identity_enums


def test_full_blueprint_misclassification_patch_freezes_non_targets() -> None:
    source, blueprint, action_keys = _environment_misclassification_fixture()
    target = action_keys[0]
    before = blueprint.model_dump(mode="json")

    repaired = apply_blueprint_state_subject_ownership_patch(
        blueprint,
        {
            "base_candidate_hash": blueprint_candidate_hash(blueprint),
            "repairs": {
                target: {
                    "mode": "single",
                    "identity_keys": ["甲"],
                },
            },
        },
        target_unit_keys=[target],
        source_text=source,
    )
    assert isinstance(repaired, NarrativeBlueprint)
    after = repaired.model_dump(mode="json")

    assert blueprint.model_dump(mode="json") == before
    assert after["nodes"][1] == before["nodes"][1]
    for field_name in before["nodes"][0]:
        if field_name not in {
            "participant_evidence",
            "state_subject_assignments",
            "environment_source_unit_keys",
        }:
            assert after["nodes"][0][field_name] == (
                before["nodes"][0][field_name]
            )
    assert [
        evidence
        for evidence in after["nodes"][0]["participant_evidence"]
        if evidence["usage"] != "state_subject"
    ] == [
        evidence
        for evidence in before["nodes"][0]["participant_evidence"]
        if evidence["usage"] != "state_subject"
    ]
    assert [
        evidence
        for evidence in after["nodes"][0]["participant_evidence"]
        if (
            evidence["usage"] == "state_subject"
            and target not in evidence["source_unit_keys"]
        )
    ] == [
        evidence
        for evidence in before["nodes"][0]["participant_evidence"]
        if (
            evidence["usage"] == "state_subject"
            and target not in evidence["source_unit_keys"]
        )
    ]
    assert repaired.nodes[0].environment_source_unit_keys == [action_keys[2]]
    assert any(
        evidence.identity_key == "甲"
        and evidence.usage == "state_subject"
        and evidence.source_unit_keys == [target]
        for evidence in repaired.nodes[0].participant_evidence
    )
    assert validate_narrative_blueprint(repaired, source) == []


def test_full_blueprint_misclassification_patch_supports_joint() -> None:
    source, blueprint, action_keys = _environment_misclassification_fixture()
    target = action_keys[0]

    repaired = apply_blueprint_state_subject_misclassification_patch(
        blueprint,
        {
            "base_candidate_hash": blueprint_candidate_hash(blueprint),
            "repairs": {
                target: {
                    "mode": "joint",
                    "identity_keys": ["甲", "乙"],
                },
            },
        },
        target_unit_keys=[target],
        source_text=source,
    )

    assert repaired.nodes[0].environment_source_unit_keys == [action_keys[2]]
    assert [
        assignment.model_dump(mode="json")
        for assignment in repaired.nodes[0].state_subject_assignments
    ] == [{
        "source_unit_key": target,
        "mode": "joint",
        "identity_keys": ["甲", "乙"],
    }]
    assert validate_narrative_blueprint(repaired, source) == []


def test_true_environment_and_patch_drift_fail_closed() -> None:
    source = "雨落下。"
    target = "SRC0001:unit:001"
    blueprint = NarrativeBlueprint.model_validate({
        "episode_no": 1,
        "nodes": [{
            "key": "weather",
            "source_segment_ids": ["SRC0001"],
            "summary": "雨落下",
            "narrative_layer": "story",
            "event_priority": "causal",
            "render_policy": "standalone",
            "temporal_domain_key": "present",
            "time_label": "当下",
            "time_relation": "episode_start",
            "location_key": "yard",
            "location_label": "院中",
            "environment_source_unit_keys": [target],
            "action_logic": "雨落下",
        }],
    })
    issue = BlueprintSemanticIssue(
        code="state_subject_environment_misclassified",
        node_keys=["weather"],
        source_segment_ids=["SRC0001"],
        source_unit_keys=[target],
        message="语义审稿判断由双 reviewer 提供",
        required_resolution="只能使用已有感知 authority",
    )

    assert not blueprint_environment_subject_issue_has_exact_authority(
        issue,
        blueprint,
        source,
    )
    with pytest.raises(ValueError, match="visible/voice authority"):
        blueprint_state_subject_ownership_patch_schema(
            blueprint,
            [target],
            source,
        )

    source, blueprint, action_keys = _environment_misclassification_fixture()
    target = action_keys[0]
    base_hash = blueprint_candidate_hash(blueprint)
    with pytest.raises(ValueError, match="不允许 environment"):
        apply_blueprint_state_subject_misclassification_patch(
            blueprint,
            {
                "base_candidate_hash": base_hash,
                "repairs": {
                    target: {
                        "mode": "environment",
                        "identity_keys": [],
                    },
                },
            },
            target_unit_keys=[target],
            source_text=source,
        )
    with pytest.raises(ValueError, match="hash 漂移"):
        apply_blueprint_state_subject_misclassification_patch(
            blueprint,
            {
                "base_candidate_hash": "stale",
                "repairs": {
                    target: {
                        "mode": "single",
                        "identity_keys": ["甲"],
                    },
                },
            },
            target_unit_keys=[target],
            source_text=source,
        )
    with pytest.raises(ValueError, match="target 集合"):
        apply_blueprint_state_subject_misclassification_patch(
            blueprint,
            {
                "base_candidate_hash": base_hash,
                "repairs": {
                    target: {
                        "mode": "single",
                        "identity_keys": ["甲"],
                    },
                    action_keys[2]: {
                        "mode": "single",
                        "identity_keys": ["甲"],
                    },
                },
            },
            target_unit_keys=[target],
            source_text=source,
        )
    with pytest.raises(ValueError, match="visible/voice authority"):
        apply_blueprint_state_subject_misclassification_patch(
            blueprint,
            {
                "base_candidate_hash": base_hash,
                "repairs": {
                    target: {
                        "mode": "single",
                        "identity_keys": ["旁观者"],
                    },
                },
            },
            target_unit_keys=[target],
            source_text=source,
        )

    duplicate_owner = blueprint.model_copy(deep=True)
    duplicate_node = duplicate_owner.nodes[1].model_copy(deep=True)
    duplicate_node.key = "duplicate-src-owner"
    duplicate_node.source_segment_ids = ["SRC0001"]
    duplicate_owner.nodes.append(duplicate_node)
    with pytest.raises(ValueError, match="唯一 SRC owner"):
        blueprint_state_subject_misclassification_patch_schema(
            duplicate_owner,
            [target],
            source,
        )


def test_ownership_patch_schema_has_exact_53_targets_and_stays_compact() -> None:
    source = "".join(f"甲完成动作{index}，" for index in range(53))
    target_keys = [
        fact.source_unit_key
        for fact in stages.source_segment_facts("SRC0001", source)
        if fact.projection == "action"
    ]
    shard = NarrativeBlueprintShard.model_validate({
        "episode_no": 1,
        "shard_index": 1,
        "source_segment_ids": ["SRC0001"],
        "nodes": [{
            "key": "bulk-owner",
            "source_segment_ids": ["SRC0001"],
            "summary": "批量动作",
            "narrative_layer": "story",
            "event_priority": "causal",
            "render_policy": "standalone",
            "temporal_domain_key": "present",
            "time_label": "当下",
            "time_relation": "episode_start",
            "location_key": "room",
            "location_label": "房间",
            "participants": ["甲", "乙"],
            "participant_evidence": [{
                "identity_key": identity_key,
                "source_segment_ids": ["SRC0001"],
                "source_unit_keys": [target_keys[0]],
                "usage": "visible",
            } for identity_key in ("甲", "乙")],
            "action_logic": "连续完成动作",
        }],
    })

    schema = blueprint_state_subject_ownership_patch_schema(
        shard,
        target_keys,
        source,
    )
    repairs = schema["properties"]["repairs"]

    assert len(target_keys) == 53
    assert list(repairs["properties"]) == target_keys
    assert repairs["required"] == target_keys
    assert repairs["additionalProperties"] is False
    assert schema["properties"]["base_candidate_hash"]["const"] == (
        blueprint_shard_candidate_hash(shard)
    )
    assert len(json.dumps(
        schema,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")) < 10_000


@pytest.mark.parametrize(
    ("mode", "identity_keys"),
    [
        ("single", []),
        ("single", ["甲", "乙"]),
        ("joint", ["甲"]),
        ("joint", ["甲", "甲"]),
        ("environment", ["甲"]),
    ],
)
def test_ownership_repair_mode_shape_fails_closed(
    mode: str,
    identity_keys: list[str],
) -> None:
    with pytest.raises(ValueError):
        BlueprintStateSubjectOwnershipRepair(
            mode=mode,
            identity_keys=identity_keys,
        )


def test_ownership_patch_apply_is_atomic_and_preserves_other_authority() -> None:
    source, shard, action_keys = _ownership_repair_fixture()
    before = shard.model_dump(mode="json")
    target_keys = action_keys[:3]
    patch = BlueprintStateSubjectOwnershipPatch.model_validate({
        "base_candidate_hash": blueprint_shard_candidate_hash(shard),
        "repairs": {
            target_keys[0]: {
                "mode": "single",
                "identity_keys": ["甲"],
            },
            target_keys[1]: {
                "mode": "joint",
                "identity_keys": ["乙", "丙"],
            },
            target_keys[2]: {
                "mode": "environment",
                "identity_keys": [],
            },
        },
    })

    repaired = apply_blueprint_state_subject_ownership_patch(
        shard,
        patch,
        target_unit_keys=target_keys,
        source_text=source,
    )
    node = repaired.nodes[0]
    after = repaired.model_dump(mode="json")

    assert shard.model_dump(mode="json") == before
    before_perception = [
        evidence
        for evidence in before["nodes"][0]["participant_evidence"]
        if evidence["usage"] in {"voice", "visible"}
    ]
    assert [
        evidence.model_dump(mode="json")
        for evidence in node.participant_evidence
        if evidence.usage in {"voice", "visible"}
    ][:len(before_perception)] == before_perception
    assert [
        evidence.model_dump(mode="json")
        for evidence in node.participant_evidence
        if (
            evidence.usage == "visible"
            and evidence.identity_key == "甲"
            and evidence.source_unit_keys == [target_keys[0]]
        )
    ] == [{
        "identity_key": "甲",
        "source_segment_ids": ["SRC0001"],
        "source_unit_keys": [target_keys[0]],
        "usage": "visible",
    }]
    for field_name in (
        "source_unit_deliveries",
        "state_requirements",
        "state_changes",
        "exit_state",
        "summary",
        "action_logic",
    ):
        assert after["nodes"][0][field_name] == before["nodes"][0][field_name]
    assert any(
        evidence.identity_key == "甲"
        and action_keys[3] in evidence.source_unit_keys
        for evidence in node.participant_evidence
        if evidence.usage == "state_subject"
    )
    assert {
        assignment.source_unit_key: assignment.identity_keys
        for assignment in node.state_subject_assignments
    } == {target_keys[1]: ["乙", "丙"]}
    assert node.environment_source_unit_keys == [target_keys[2]]
    assert validate_narrative_blueprint_shard(
        repaired,
        expected_episode_no=1,
        expected_shard_index=1,
        expected_source_segment_ids=["SRC0001"],
        source_text=source,
    ) == []


@pytest.mark.parametrize("usage", ["visible", "voice"])
def test_ownership_patch_does_not_duplicate_exact_perception(
    usage: str,
) -> None:
    source = "甲推门。"
    unit_key = "SRC0001:unit:001"
    shard = NarrativeBlueprintShard.model_validate({
        "episode_no": 1,
        "shard_index": 1,
        "source_segment_ids": ["SRC0001"],
        "nodes": [{
            "key": "owner",
            "source_segment_ids": ["SRC0001"],
            "summary": "甲推门",
            "narrative_layer": "story",
            "event_priority": "causal",
            "render_policy": "standalone",
            "temporal_domain_key": "present",
            "time_label": "当下",
            "time_relation": "episode_start",
            "location_key": "door",
            "location_label": "门前",
            "participants": ["甲"],
            "participant_evidence": [{
                "identity_key": "甲",
                "source_segment_ids": ["SRC0001"],
                "source_unit_keys": [unit_key],
                "usage": usage,
            }],
            "source_unit_deliveries": ([{
                "source_unit_key": unit_key,
                "mode": "offscreen_voice",
                "performer_key": "甲",
            }] if usage == "voice" else []),
            "action_logic": "甲推门",
        }],
    })
    before_perception = [
        evidence.model_dump(mode="json")
        for evidence in shard.nodes[0].participant_evidence
    ]

    repaired = apply_blueprint_state_subject_ownership_patch(
        shard,
        {
            "base_candidate_hash": blueprint_shard_candidate_hash(shard),
            "repairs": {
                unit_key: {
                    "mode": "single",
                    "identity_keys": ["甲"],
                },
            },
        },
        target_unit_keys=[unit_key],
        source_text=source,
    )

    assert [
        evidence.model_dump(mode="json")
        for evidence in repaired.nodes[0].participant_evidence
        if evidence.usage in {"visible", "voice"}
    ] == before_perception
    assert repaired.nodes[0].source_unit_deliveries == (
        shard.nodes[0].source_unit_deliveries
    )


def test_ownership_patch_environment_adds_no_character_evidence() -> None:
    source = "雨落下。"
    unit_key = "SRC0001:unit:001"
    shard = NarrativeBlueprintShard.model_validate({
        "episode_no": 1,
        "shard_index": 1,
        "source_segment_ids": ["SRC0001"],
        "nodes": [{
            "key": "owner",
            "source_segment_ids": ["SRC0001"],
            "summary": "雨落下",
            "narrative_layer": "story",
            "event_priority": "causal",
            "render_policy": "standalone",
            "temporal_domain_key": "present",
            "time_label": "当下",
            "time_relation": "episode_start",
            "location_key": "yard",
            "location_label": "院中",
            "action_logic": "雨落下",
        }],
    })

    repaired = apply_blueprint_state_subject_ownership_patch(
        shard,
        {
            "base_candidate_hash": blueprint_shard_candidate_hash(shard),
            "repairs": {
                unit_key: {
                    "mode": "environment",
                    "identity_keys": [],
                },
            },
        },
        target_unit_keys=[unit_key],
        source_text=source,
    )

    assert repaired.nodes[0].participant_evidence == []
    assert repaired.nodes[0].participants == []
    assert repaired.nodes[0].environment_source_unit_keys == [unit_key]


def test_ownership_patch_rejects_target_hash_action_and_identity_drift() -> None:
    source, shard, action_keys = _ownership_repair_fixture()
    base_hash = blueprint_shard_candidate_hash(shard)
    valid_repairs = {
        action_keys[0]: {
            "mode": "single",
            "identity_keys": ["甲"],
        },
    }

    with pytest.raises(ValueError, match="不得为空|target 集合"):
        apply_blueprint_state_subject_ownership_patch(
            shard,
            {
                "base_candidate_hash": base_hash,
                "repairs": {},
            },
            target_unit_keys=[action_keys[0]],
            source_text=source,
        )
    with pytest.raises(ValueError, match="target 集合"):
        apply_blueprint_state_subject_ownership_patch(
            shard,
            {
                "base_candidate_hash": base_hash,
                "repairs": {
                    **valid_repairs,
                    action_keys[1]: {
                        "mode": "environment",
                        "identity_keys": [],
                    },
                },
            },
            target_unit_keys=[action_keys[0]],
            source_text=source,
        )
    with pytest.raises(ValueError, match="hash 漂移"):
        apply_blueprint_state_subject_ownership_patch(
            shard,
            {
                "base_candidate_hash": "drifted",
                "repairs": valid_repairs,
            },
            target_unit_keys=[action_keys[0]],
            source_text=source,
        )
    quoted_key = next(
        fact.source_unit_key
        for fact in stages.source_segment_facts("SRC0001", source)
        if fact.projection == "quoted"
    )
    with pytest.raises(ValueError, match="action unit"):
        apply_blueprint_state_subject_ownership_patch(
            shard,
            {
                "base_candidate_hash": base_hash,
                "repairs": {
                    quoted_key: {
                        "mode": "environment",
                        "identity_keys": [],
                    },
                },
            },
            target_unit_keys=[quoted_key],
            source_text=source,
        )
    with pytest.raises(ValueError, match="owner participants"):
        apply_blueprint_state_subject_ownership_patch(
            shard,
            {
                "base_candidate_hash": base_hash,
                "repairs": {
                    action_keys[0]: {
                        "mode": "single",
                        "identity_keys": ["非法身份"],
                    },
                },
            },
            target_unit_keys=[action_keys[0]],
            source_text=source,
        )


def test_call29716_ambiguous_resolution_preserves_joint_source_authority(
    monkeypatch,
) -> None:
    fixture = _state_subject_retry_fixture()
    facts = [
        SourceFact(
            source_unit_key=unit_key,
            source_segment_id=fixture["source_segment_id"],
            unit_order=int(unit_key.rsplit(":", 1)[1]),
            projection="action",
            surface_form="prose",
            text=value["text"],
        )
        for unit_key, value in fixture["ambiguous_units"].items()
    ]
    monkeypatch.setattr(
        "app.narrative_blueprint.source_facts",
        lambda _source_text: facts,
    )
    participants = ["王有材", "虎头少年", "胖少年"]
    node = {
        "key": "S004-S001-NODE0003",
        "source_segment_ids": [fixture["source_segment_id"]],
        "summary": "三名少年共同后退并颤抖",
        "narrative_layer": "story",
        "event_priority": "causal",
        "render_policy": "standalone",
        "temporal_domain_key": "TD001",
        "time_label": "四月黄昏",
        "time_relation": "continuous",
        "location_key": "LOC002",
        "location_label": "大青山山崖裂缝内",
        "participants": participants,
        "participant_evidence": [
            {
                "identity_key": identity_key,
                "source_segment_ids": [fixture["source_segment_id"]],
                "source_unit_keys": [
                    unit_key
                    for unit_key, value in fixture["ambiguous_units"].items()
                    if identity_key in value["identity_keys"]
                ],
                "usage": "state_subject",
            }
            for identity_key in participants
        ],
        "action_logic": "三名少年因许清出现而共同惊恐后退",
    }
    blueprint = NarrativeBlueprint.model_validate({
        "episode_no": 1,
        "nodes": [node],
    })

    issues = blueprint_state_subject_issues(blueprint, "fixture")

    assert [issue.code for issue in issues] == [
        "state_subject_ambiguous",
    ] * 4
    assert all(
        "仅修此报错 unit" in issue.required_resolution
        and "移除该 unit 的全部 single state_subject claims"
        in issue.required_resolution
        and "identity_keys 列出全部有来源共同主体且至少 2 个"
        in issue.required_resolution
        and "其他 unit ownership 不得变化"
        in issue.required_resolution
        for issue in issues
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


def test_provider_normalization_derives_roster_only_from_source_evidence() -> None:
    payload = _blueprint_cross_field_run_fixtures()[0]["payload"]
    payload = json.loads(json.dumps(payload, ensure_ascii=False))
    node = payload["nodes"][0]
    node["participant_evidence"].append({
        "identity_key": "CHAR_SOURCE_BACKED_EXTRA",
        "source_segment_ids": ["SRC0004"],
        "source_unit_keys": [],
        "usage": "visible",
    })
    unsupported_identity = "CHAR_UNKNOWN_BOY_FAT"
    assert unsupported_identity in node["participants"]

    normalized = normalize_blueprint_provider_payload(payload)

    evidence_identities = list(dict.fromkeys(
        evidence["identity_key"]
        for evidence in node["participant_evidence"]
    ))
    assert normalized["nodes"][0]["participants"] == evidence_identities
    assert "CHAR_SOURCE_BACKED_EXTRA" in evidence_identities
    assert unsupported_identity not in evidence_identities


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
        "content_hash": evidence_repository.content_hash(
            blueprint.model_dump(mode="json")
        ),
        "contract_version": stages.BLUEPRINT_VERSION,
        "prompt_version": stages.SCREENPLAY_BLUEPRINT_PROMPT_VERSION,
        "model_snapshot_json": "{}",
    }, {
        "id": "art-current-wrapper",
        "content_json": blueprint.model_dump_json(),
        "content_hash": evidence_repository.content_hash(
            blueprint.model_dump(mode="json")
        ),
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
            "content_hash": evidence_repository.content_hash(
                blueprint.model_dump(mode="json")
            ),
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


def test_current_blueprint_selector_rejects_drifted_outer_hash() -> None:
    blueprint = _blueprint()
    current_snapshot = stages._current_blueprint_authority_snapshot(
        SOURCE,
        generation_mode="test",
    )

    selected, legacy = stages._select_current_blueprint_artifact(
        [{
            "id": "art-tampered",
            "content_json": blueprint.model_dump_json(),
            "content_hash": "stale-hash",
            "contract_version": stages.BLUEPRINT_VERSION,
            "prompt_version": stages.SCREENPLAY_BLUEPRINT_PROMPT_VERSION,
            "model_snapshot_json": json.dumps(current_snapshot),
        }],
        blueprint,
        SOURCE,
    )

    assert selected is None
    assert legacy is None


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


def test_targeted_one_sided_falls_back_once_and_full_residual_validates(
    monkeypatch,
) -> None:
    blueprint = _blueprint()
    derive_blueprint_scene_plans(blueprint)
    calls: list[tuple[int, int, str]] = []
    patch_calls: list[str] = []
    created: list = []
    empty_rows = SimpleNamespace(fetchall=lambda: [])
    empty_connection = SimpleNamespace(
        execute=lambda *_args, **_kwargs: empty_rows,
    )

    def issue(code: str, message: str) -> BlueprintSemanticReview:
        return BlueprintSemanticReview(issues=[BlueprintSemanticIssue(
            code=code,
            node_keys=["n2"],
            source_segment_ids=["SRC0002"],
            message=message,
            required_resolution="仅在双审形成共识后修复",
        )])

    async def fake_structured(*_args, **kwargs):
        meta = kwargs["call_meta"]
        review_round = meta["review_round"]
        sample_no = meta["review_sample"]
        calls.append((review_round, sample_no, meta["substage"]))
        if review_round == 1:
            code = (
                "timeline_conflict"
                if sample_no == 1
                else "spatial_action_gap"
            )
            return issue(code, "定向审稿结论不一致")
        if review_round == 2 and sample_no == 1:
            return issue("timeline_conflict", "完整审稿仍有单侧残留")
        return BlueprintSemanticReview(issues=[])

    async def forbidden_patch(*_args, **_kwargs):
        patch_calls.append("called")
        raise AssertionError("one-sided review issue must not enter patch")

    def create_artifact(artifact, **_kwargs):
        created.append(artifact)
        return {"id": f"artifact-{len(created)}"}

    monkeypatch.setattr(stages, "get_conn", lambda: empty_connection)
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
        stages,
        "_repair_narrative_blueprint",
        forbidden_patch,
    )
    monkeypatch.setattr(
        stages,
        "_repair_reviewed_blueprint_state_subject_ownership",
        forbidden_patch,
    )
    monkeypatch.setattr(
        "app.evidence.repository.create_artifact",
        create_artifact,
    )

    result = asyncio.run(stages._semantic_review_narrative_blueprint(
        blueprint,
        episode={"id": "episode-full-one-sided-then-clean", "episode_no": 8},
        source_text=SOURCE,
    ))

    assert result is blueprint
    assert patch_calls == []
    assert calls == [
        (1, 1, "risk_nodes"),
        (1, 2, "risk_nodes"),
        (2, 1, "full"),
        (2, 2, "full"),
    ]
    consensus = [
        artifact
        for artifact in created
        if artifact.type == "screenplay_narrative_blueprint_review_consensus"
    ]
    assert [
        artifact.content["review_outcome"] for artifact in consensus
    ] == [
        "full_fallback_required",
        "non_authoritative_one_sided_residual",
    ]
    assert [artifact.status for artifact in consensus] == [
        "needs_revision",
        "validated",
    ]
    assert consensus[-1].content["authoritative_issue_count"] == 0
    assert consensus[-1].content[
        "non_authoritative_residual_issue_count"
    ] == 1


def test_full_persistent_one_sided_residual_validates_without_recheck(
    monkeypatch,
) -> None:
    blueprint = _blueprint()
    derive_blueprint_scene_plans(blueprint)
    calls: list[tuple[int, int, str]] = []
    patch_calls: list[str] = []
    created: list = []
    empty_rows = SimpleNamespace(fetchall=lambda: [])
    empty_connection = SimpleNamespace(
        execute=lambda *_args, **_kwargs: empty_rows,
    )

    def issue(code: str, message: str) -> BlueprintSemanticReview:
        return BlueprintSemanticReview(issues=[BlueprintSemanticIssue(
            code=code,
            node_keys=["n2"],
            source_segment_ids=["SRC0002"],
            message=message,
            required_resolution="仅在双审形成共识后修复",
        )])

    async def fake_structured(*_args, **kwargs):
        meta = kwargs["call_meta"]
        review_round = meta["review_round"]
        sample_no = meta["review_sample"]
        calls.append((review_round, sample_no, meta["substage"]))
        if sample_no == 1:
            return issue("timeline_conflict", "完整审稿持续单侧残留")
        return BlueprintSemanticReview(issues=[])

    async def forbidden_patch(*_args, **_kwargs):
        patch_calls.append("called")
        raise AssertionError("one-sided review issue must not enter patch")

    def create_artifact(artifact, **_kwargs):
        created.append(artifact)
        return {"id": f"artifact-{len(created)}"}

    monkeypatch.setattr(stages, "get_conn", lambda: empty_connection)
    monkeypatch.setattr(
        stages,
        "get_setting",
        lambda key: "false"
        if key == "screenplay_targeted_blueprint_review_enabled"
        else "1",
    )
    monkeypatch.setattr(
        stages.model_gateway,
        "chat_structured",
        fake_structured,
    )
    monkeypatch.setattr(
        stages,
        "_repair_narrative_blueprint",
        forbidden_patch,
    )
    monkeypatch.setattr(
        stages,
        "_repair_reviewed_blueprint_state_subject_ownership",
        forbidden_patch,
    )
    monkeypatch.setattr(
        "app.evidence.repository.create_artifact",
        create_artifact,
    )

    result = asyncio.run(stages._semantic_review_narrative_blueprint(
        blueprint,
        episode={
            "id": "episode-full-one-sided-no-recheck",
            "episode_no": 8,
        },
        source_text=SOURCE,
    ))

    assert result is blueprint
    assert patch_calls == []
    assert calls == [
        (1, 1, "full"),
        (1, 2, "full"),
    ]
    consensus = [
        artifact
        for artifact in created
        if artifact.type == "screenplay_narrative_blueprint_review_consensus"
    ]
    assert [
        artifact.content["review_outcome"] for artifact in consensus
    ] == [
        "non_authoritative_one_sided_residual",
    ]
    assert consensus[0].status == "validated"


def test_deterministic_supported_one_sided_issue_remains_repair_authority(
    monkeypatch,
) -> None:
    blueprint = _blueprint()
    blueprint.nodes[-1].participant_evidence = [
        evidence
        for evidence in blueprint.nodes[-1].participant_evidence
        if evidence.usage != "state_subject"
    ]
    derive_blueprint_scene_plans(blueprint)
    deterministic_issue = blueprint_state_subject_issues(
        blueprint,
        SOURCE,
    )[0]
    reviewer_subscope_issue = deterministic_issue.model_copy(
        update={"source_unit_keys": []},
        deep=True,
    )
    calls: list[tuple[int, int]] = []
    repair_errors: list[str] = []
    created: list = []
    empty_rows = SimpleNamespace(fetchall=lambda: [])
    empty_connection = SimpleNamespace(
        execute=lambda *_args, **_kwargs: empty_rows,
    )

    async def fake_structured(*_args, **kwargs):
        meta = kwargs["call_meta"]
        calls.append((meta["review_round"], meta["review_sample"]))
        if meta["review_round"] == 1 and meta["review_sample"] == 1:
            return BlueprintSemanticReview(issues=[
                reviewer_subscope_issue.model_copy(deep=True),
            ])
        return BlueprintSemanticReview(issues=[])

    async def record_repair(value, **kwargs):
        repair_errors.extend(kwargs["additional_errors"])
        return value

    def create_artifact(artifact, **_kwargs):
        created.append(artifact)
        return {"id": f"artifact-{len(created)}"}

    monkeypatch.setattr(stages, "get_conn", lambda: empty_connection)
    monkeypatch.setattr(
        stages,
        "get_setting",
        lambda key: "false"
        if key == "screenplay_targeted_blueprint_review_enabled"
        else "1",
    )
    monkeypatch.setattr(
        stages.model_gateway,
        "chat_structured",
        fake_structured,
    )
    monkeypatch.setattr(stages, "_repair_narrative_blueprint", record_repair)
    monkeypatch.setattr(
        "app.evidence.repository.create_artifact",
        create_artifact,
    )

    result = asyncio.run(stages._semantic_review_narrative_blueprint(
        blueprint,
        episode={"id": "episode-deterministic-one-sided", "episode_no": 8},
        source_text=SOURCE,
    ))

    assert result is blueprint
    assert calls == [(1, 1), (1, 2), (2, 1), (2, 2)]
    assert len(repair_errors) == 1
    assert deterministic_issue.code.upper() in repair_errors[0]
    consensus = [
        artifact
        for artifact in created
        if artifact.type == "screenplay_narrative_blueprint_review_consensus"
    ]
    assert consensus[0].status == "needs_revision"
    assert consensus[0].content["review_outcome"] == (
        "deterministic_authority_issues"
    )
    assert consensus[0].content["authoritative_issue_count"] == 1
    assert consensus[0].content[
        "non_authoritative_residual_issue_count"
    ] == 0


def test_reviewer_quorum_filters_unsupported_guesses_before_validation(
    monkeypatch,
) -> None:
    blueprint = _blueprint()
    derive_blueprint_scene_plans(blueprint)
    reviews = {
        1: BlueprintSemanticReview.model_validate({"issues": [{
            "code": "state_subject_missing",
            "node_keys": ["n1"],
            "source_segment_ids": ["SRC0001"],
            "message": "无权威的状态主体猜测",
            "required_resolution": "不应进入共识",
        }, {
            "code": "state_subject_environment_misclassified",
            "node_keys": ["n1"],
            "source_segment_ids": ["SRC0001"],
            "source_unit_keys": ["SRC0001:unit:001"],
            "message": "无 environment ownership 的猜测",
            "required_resolution": "不应进入共识",
        }]}),
        2: BlueprintSemanticReview.model_validate({"issues": [{
            "code": "state_subject_missing",
            "node_keys": ["n1"],
            "source_segment_ids": ["SRC0001"],
            "message": "无权威的状态主体猜测",
            "required_resolution": "不应进入共识",
        }, {
            "code": "state_subject_ambiguous",
            "node_keys": ["n2"],
            "source_segment_ids": ["SRC0002"],
            "message": "无权威的状态主体歧义猜测",
            "required_resolution": "不应进入共识",
        }, {
            "code": "state_subject_environment_misclassified",
            "node_keys": ["n3"],
            "source_segment_ids": ["SRC0003"],
            "source_unit_keys": ["SRC0003:unit:001"],
            "message": "无 environment ownership 的猜测",
            "required_resolution": "不应进入共识",
        }, {
            "code": "state_subject_environment_misclassified",
            "node_keys": ["n4"],
            "source_segment_ids": ["SRC0004"],
            "source_unit_keys": ["SRC0004:unit:001"],
            "message": "无 environment ownership 的猜测",
            "required_resolution": "不应进入共识",
        }]}),
    }
    validation_errors: dict[int, list[str]] = {}
    invalid_reference_errors: list[str] = []
    created: list = []

    empty_rows = SimpleNamespace(fetchall=lambda: [])
    empty_connection = SimpleNamespace(
        execute=lambda *_args, **_kwargs: empty_rows,
    )

    async def fake_structured(*_args, **kwargs):
        sample_no = kwargs["call_meta"]["review_sample"]
        if sample_no == 1:
            invalid_review = BlueprintSemanticReview.model_validate({
                "issues": [{
                    "code": "timeline_conflict",
                    "node_keys": ["missing-node"],
                    "source_segment_ids": ["SRC9999"],
                    "message": "非法引用不得被 authority filter 移除",
                    "required_resolution": "拒绝整份非法 review",
                }],
            })
            invalid_reference_errors.extend(
                kwargs["validate"](invalid_review)
            )
            assert len(invalid_review.issues) == 1
        review = reviews[sample_no].model_copy(deep=True)
        validation_errors[sample_no] = kwargs["validate"](review)
        return review

    def create_artifact(artifact, **_kwargs):
        created.append(artifact)
        return {"id": f"artifact-{len(created)}"}

    monkeypatch.setattr(stages, "get_conn", lambda: empty_connection)
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
        create_artifact,
    )

    result = asyncio.run(stages._semantic_review_narrative_blueprint(
        blueprint,
        episode={"id": "episode-reviewer-filter-quorum", "episode_no": 8},
        source_text=SOURCE,
    ))

    assert result is blueprint
    assert validation_errors == {1: [], 2: []}
    assert any("NODE_UNKNOWN" in error for error in invalid_reference_errors)
    assert any("SOURCE_UNKNOWN" in error for error in invalid_reference_errors)
    review_artifacts = sorted(
        (
            artifact for artifact in created
            if artifact.type == "screenplay_narrative_blueprint_review"
        ),
        key=lambda artifact: artifact.model_snapshot["review_sample"],
    )
    assert len(review_artifacts) == 2
    assert [artifact.content["issues"] for artifact in review_artifacts] == [
        [],
        [],
    ]
    assert [
        artifact.model_snapshot["dropped_unsupported_voice_issue_count"]
        for artifact in review_artifacts
    ] == [2, 4]
    consensus = next(
        artifact for artifact in created
        if artifact.type == "screenplay_narrative_blueprint_review_consensus"
    )
    assert consensus.content["review_outcome"] == "clean"
    assert consensus.content["consensus_issue_keys"] == []
    assert consensus.content["dropped_unsupported_voice_issue_count"] == 6


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
    stale_content = {
        "blueprint_hash": blueprint_hash,
        "consensus_issue_keys": [],
        "review_outcome": "clean",
    }

    class CachedRows:
        @staticmethod
        def fetchall():
            return [{
                "id": "stale-clean-consensus",
                "content_json": json.dumps(stale_content),
                "content_hash": evidence_repository.content_hash(stale_content),
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


@pytest.mark.parametrize(
    ("review_outcome", "review_mode", "residual_issue_count"),
    [
        ("clean", "targeted", 0),
        ("non_authoritative_one_sided_residual", "full", 1),
    ],
)
def test_v5_validated_no_authority_review_outcomes_are_cacheable(
    monkeypatch,
    review_outcome: str,
    review_mode: str,
    residual_issue_count: int,
) -> None:
    blueprint = _blueprint()
    derive_blueprint_scene_plans(blueprint)
    episode_id = f"episode-v5-cache-{review_outcome}"
    blueprint_hash = hashlib.sha256(
        json.dumps(
            blueprint.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    source_hash = hashlib.sha256(SOURCE.encode("utf-8")).hexdigest()
    authority_fingerprint = blueprint_authority_validator_fingerprint()
    review_input_fingerprint = hashlib.sha256(
        json.dumps(
            {
                "episode_id": episode_id,
                "blueprint_hash": blueprint_hash,
                "source_corpus_hash": source_hash,
                "review_policy_version": (
                    stages.BLUEPRINT_SEMANTIC_REVIEW_POLICY_VERSION
                ),
                "authority_fingerprint": authority_fingerprint,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    cached_content = {
        "blueprint_hash": blueprint_hash,
        "consensus_issue_keys": [],
        "deterministic_authority_issue_keys": [],
        "authoritative_issue_count": 0,
        "non_consensus_issue_count": residual_issue_count,
        "non_authoritative_residual_issue_count": residual_issue_count,
        "review_mode": review_mode,
        "review_outcome": review_outcome,
    }

    class CachedRows:
        @staticmethod
        def fetchall():
                return [{
                    "id": f"cached-{review_outcome}",
                    "content_json": json.dumps(cached_content),
                    "content_hash": evidence_repository.content_hash(
                        cached_content
                    ),
                "model_snapshot_json": json.dumps({
                    "review_policy_version": (
                        "blueprint-semantic-review.v5"
                    ),
                    "authority_fingerprint": authority_fingerprint,
                    "source_corpus_hash": source_hash,
                    "review_input_fingerprint": review_input_fingerprint,
                }),
            }]

    class CachedConnection:
        @staticmethod
        def execute(*_args, **_kwargs):
            return CachedRows()

    async def forbidden_reviewer(*_args, **_kwargs):
        raise AssertionError("validated v5 review outcome was not reused")

    monkeypatch.setattr(stages, "get_conn", lambda: CachedConnection())
    monkeypatch.setattr(
        stages.model_gateway,
        "chat_structured",
        forbidden_reviewer,
    )
    monkeypatch.setattr(
        "app.observability.tracing.current_trace",
        lambda: SimpleNamespace(run_id="", step_run_id="", trace_id=""),
    )

    result = asyncio.run(stages._semantic_review_narrative_blueprint(
        blueprint,
        episode={"id": episode_id, "episode_no": 8},
        source_text=SOURCE,
    ))

    assert stages.BLUEPRINT_SEMANTIC_REVIEW_POLICY_VERSION == (
        "blueprint-semantic-review.v5"
    )
    assert result is blueprint


def test_semantic_reviewed_wrapper_does_not_reuse_v4_policy(
    monkeypatch,
) -> None:
    from app.evidence import repository as evidence_repository

    blueprint = _blueprint()
    derive_blueprint_scene_plans(blueprint)
    episode_id = "episode-semantic-wrapper-v5"
    source_hash = hashlib.sha256(SOURCE.encode("utf-8")).hexdigest()
    content = blueprint.model_dump(mode="json")
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        """CREATE TABLE artifacts (
               id TEXT,
               type TEXT,
               scope_type TEXT,
               scope_id TEXT,
               status TEXT,
               content_hash TEXT,
               contract_version TEXT,
               prompt_version TEXT,
               model_snapshot_json TEXT,
               content_json TEXT,
               created_at TEXT
           )"""
    )
    connection.execute(
        """INSERT INTO artifacts VALUES (
               ?, 'screenplay_narrative_blueprint', 'episode', ?,
               'validated', ?, ?, ?, ?, ?, '2026-08-20T00:00:00Z'
           )""",
        (
            "old-v4-wrapper",
            episode_id,
            evidence_repository.content_hash(content),
            stages.BLUEPRINT_VERSION,
            stages.SCREENPLAY_BLUEPRINT_PROMPT_VERSION,
            json.dumps({
                "generation_mode": "semantic_reviewed",
                "source_corpus_hash": source_hash,
                "review_policy_version": "blueprint-semantic-review.v4",
            }),
            json.dumps(content, ensure_ascii=False),
        ),
    )
    created: list = []

    async def clean_review(*_args, **_kwargs):
        return BlueprintSemanticReview(issues=[])

    def create_artifact(artifact, **_kwargs):
        created.append(artifact)
        return {"id": f"artifact-{len(created)}"}

    monkeypatch.setattr(stages, "get_conn", lambda: connection)
    monkeypatch.setattr(
        stages,
        "get_setting",
        lambda key: "false"
        if key == "screenplay_targeted_blueprint_review_enabled"
        else "1",
    )
    monkeypatch.setattr(
        stages.model_gateway,
        "chat_structured",
        clean_review,
    )
    monkeypatch.setattr(
        "app.evidence.repository.create_artifact",
        create_artifact,
    )
    monkeypatch.setattr(
        "app.observability.tracing.current_trace",
        lambda: SimpleNamespace(
            run_id="run-semantic-wrapper-v5",
            step_run_id="step-semantic-wrapper-v5",
            trace_id="trace-semantic-wrapper-v5",
        ),
    )

    result = asyncio.run(stages._semantic_review_narrative_blueprint(
        blueprint,
        episode={"id": episode_id, "episode_no": 8},
        source_text=SOURCE,
    ))

    wrappers = [
        artifact
        for artifact in created
        if artifact.type == "screenplay_narrative_blueprint"
    ]
    assert result is blueprint
    assert len(wrappers) == 1
    assert wrappers[0].model_snapshot["review_policy_version"] == (
        "blueprint-semantic-review.v5"
    )
    assert not stages._blueprint_authority_snapshot_is_current(
        {
            **wrappers[0].model_snapshot,
            "review_policy_version": "blueprint-semantic-review.v4",
        },
        SOURCE,
    )


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
    assert {
        call_kwargs["format_retry_limit"]
        for _, call_kwargs in calls
    } == {1}
    assert {
        call_kwargs["semantic_retry_limit"]
        for _, call_kwargs in calls
    } == {0}
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
                    "content_hash": evidence_repository.content_hash(
                        blueprint.model_dump(mode="json")
                    ),
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


# --- ERR-20260821-2def08 -----------------------------------------------------
# run_23f773b2ec1c finished all 25 blueprint leaves, then one of the two
# independent reviewers stalled for 618.9s with received_chars=0.  Consensus was
# one clean sample short, and the gate discarded ~30 minutes of validated
# blueprint.  A reviewer that never delivered an opinion is re-drawn once as a
# NEW deterministic sample; a reviewer that *authored* an unacceptable opinion
# is never re-drawn.


class _ReviewEmptyRows:
    @staticmethod
    def fetchall():
        return []


class _ReviewEmptyConnection:
    @staticmethod
    def execute(*_args, **_kwargs):
        return _ReviewEmptyRows()


def _install_review_harness(monkeypatch, fake_chat_structured) -> None:
    monkeypatch.setattr(stages, "get_conn", lambda: _ReviewEmptyConnection())
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
    monkeypatch.setattr(
        "app.evidence.repository.create_artifact",
        lambda *_args, **_kwargs: {"id": str(uuid.uuid4())},
    )


def test_undelivered_reviewer_is_supplemented_instead_of_discarding_blueprint(
    monkeypatch,
) -> None:
    clean_review = BlueprintSemanticReview.model_validate({"issues": []})
    samples: list[int] = []

    async def fake_chat_structured(*_args, **kwargs):
        sample_no = int(kwargs["call_meta"]["review_sample"])
        samples.append(sample_no)
        if sample_no == 2:
            raise hiagent.ProviderError(
                "ReadTimeout(phase=read): outcome unknown",
                delivery_state="unknown",
                replay_safe=False,
            )
        return clean_review.model_copy(deep=True)

    _install_review_harness(monkeypatch, fake_chat_structured)

    result = asyncio.run(stages._semantic_review_narrative_blueprint(
        _blueprint(),
        episode={"id": "episode-review-supplement"},
        source_text=SOURCE,
    ))

    assert result is not None
    # Sample 3 is a distinct operation, never a replay of the unresolved call.
    assert samples == [1, 2, stages.BLUEPRINT_REVIEW_SUPPLEMENTARY_SAMPLE]


def test_authored_but_invalid_review_is_never_redrawn(monkeypatch) -> None:
    """Re-drawing an authored opinion until one passes is coached compliance."""
    clean_review = BlueprintSemanticReview.model_validate({"issues": []})
    samples: list[int] = []

    async def fake_chat_structured(*_args, **kwargs):
        sample_no = int(kwargs["call_meta"]["review_sample"])
        samples.append(sample_no)
        if sample_no == 2:
            raise stages.model_gateway.StructuredSemanticError(
                "风险审稿引用了范围外节点",
            )
        return clean_review.model_copy(deep=True)

    _install_review_harness(monkeypatch, fake_chat_structured)

    with pytest.raises(
        ContentGenerationError,
        match="蓝图语义审稿人不足两份",
    ):
        asyncio.run(stages._semantic_review_narrative_blueprint(
            _blueprint(),
            episode={"id": "episode-review-authored-invalid"},
            source_text=SOURCE,
        ))

    assert samples == [1, 2]


def test_off_schema_review_is_not_redrawn_but_corrupt_bytes_are(
    monkeypatch,
) -> None:
    clean_review = BlueprintSemanticReview.model_validate({"issues": []})

    def run(unparseable: bool) -> list[int]:
        samples: list[int] = []

        async def fake_chat_structured(*_args, **kwargs):
            sample_no = int(kwargs["call_meta"]["review_sample"])
            samples.append(sample_no)
            if sample_no == 2 and len(samples) <= 2:
                error = stages.model_gateway.StructuredFormatError("bad body")
                error.unparseable = unparseable
                raise error
            return clean_review.model_copy(deep=True)

        _install_review_harness(monkeypatch, fake_chat_structured)
        try:
            asyncio.run(stages._semantic_review_narrative_blueprint(
                _blueprint(),
                episode={"id": f"episode-review-shape-{unparseable}"},
                source_text=SOURCE,
            ))
        except ContentGenerationError:
            pass
        return samples

    # Decoded-but-off-schema is an authored answer: one call each, no re-draw.
    assert run(False) == [1, 2]
    # Corrupt bytes carry no opinion, so the missing sample is drawn again.
    assert run(True) == [1, 2, stages.BLUEPRINT_REVIEW_SUPPLEMENTARY_SAMPLE]


def test_both_reviewers_undelivered_still_fails_closed(monkeypatch) -> None:
    """A systemic provider outage must not be chased with extra samples."""
    samples: list[int] = []

    async def fake_chat_structured(*_args, **kwargs):
        samples.append(int(kwargs["call_meta"]["review_sample"]))
        raise hiagent.ProviderError(
            "ReadTimeout(phase=read): outcome unknown",
            delivery_state="unknown",
            replay_safe=False,
        )

    _install_review_harness(monkeypatch, fake_chat_structured)

    with pytest.raises(
        ContentGenerationError,
        match="蓝图语义审稿人不足两份",
    ):
        asyncio.run(stages._semantic_review_narrative_blueprint(
            _blueprint(),
            episode={"id": "episode-review-both-down"},
            source_text=SOURCE,
        ))

    assert samples == [1, 2]


def test_supplementary_reviewer_failure_is_bounded_to_one_extra_sample(
    monkeypatch,
) -> None:
    clean_review = BlueprintSemanticReview.model_validate({"issues": []})
    samples: list[int] = []

    async def fake_chat_structured(*_args, **kwargs):
        sample_no = int(kwargs["call_meta"]["review_sample"])
        samples.append(sample_no)
        if sample_no == 1:
            return clean_review.model_copy(deep=True)
        raise hiagent.ProviderError(
            "ReadTimeout(phase=read): outcome unknown",
            delivery_state="unknown",
            replay_safe=False,
        )

    _install_review_harness(monkeypatch, fake_chat_structured)

    with pytest.raises(
        ContentGenerationError,
        match="蓝图语义审稿人不足两份",
    ):
        asyncio.run(stages._semantic_review_narrative_blueprint(
            _blueprint(),
            episode={"id": "episode-review-supplement-fails"},
            source_text=SOURCE,
        ))

    # Exactly one supplementary sample: never a loop.
    assert samples == [1, 2, stages.BLUEPRINT_REVIEW_SUPPLEMENTARY_SAMPLE]


def test_generation_breaker_in_reviewer_is_not_masked_as_missing_reviewer(
    monkeypatch,
) -> None:
    """A call/token/wall breaker must surface, not read as 审稿人不足两份."""
    async def fake_chat_structured(*_args, **_kwargs):
        raise stages.StageError(
            "剧本时空因果蓝图分片",
            ["[BLUEPRINT_GENERATION_CALL_BUDGET] 超过全局调用上限"],
        )

    _install_review_harness(monkeypatch, fake_chat_structured)

    with pytest.raises(stages.StageError, match="CALL_BUDGET"):
        asyncio.run(stages._semantic_review_narrative_blueprint(
            _blueprint(),
            episode={"id": "episode-review-breaker"},
            source_text=SOURCE,
        ))


# Production shape: the provider stopped emitting key names and produced runs
# of tabs and spaces instead, so nothing ever decoded into a JSON object.
_DEGENERATE_PATCH_RESPONSE = (
    '{\n    "    ' + "\t" * 60 + " " * 80 + "\t" * 40 + ':": [{" '
    + "\t" * 120 + "," + "\t" * 120 + '"},{" ' + "\t" * 120
    + ': "n4",\n            "node": {\n                "key": "n4",\n'
)


def test_undelivered_blueprint_patch_spends_a_round_not_the_episode(
    monkeypatch,
) -> None:
    """An answer that never decoded is a failed round, not a dead episode.

    The repair loop owns a bounded budget of six separately reserved rounds.
    A response whose keys degenerated into whitespace carries no authored
    repair to preserve, so it costs one of those rounds; aborting on round one
    threw the remaining budgeted rounds away.
    """
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
    responses = iter([_DEGENERATE_PATCH_RESPONSE, valid_patch])
    operation_ids: list[str] = []

    async def fake_chat(_messages, **_kwargs):
        return next(responses)

    async def fake_structured(messages, **kwargs):
        operation_ids.append(str(kwargs["operation_id"]))
        # The gateway must see the strict one-call fence this path sets.
        assert kwargs["format_retry_limit"] == 0
        assert kwargs["semantic_retry_limit"] == 0
        return await original_structured(messages, **kwargs)

    original_structured = stages.model_gateway.chat_structured
    monkeypatch.setattr(stages.model_gateway, "chat", fake_chat)
    monkeypatch.setattr(stages.model_gateway, "chat_structured", fake_structured)
    monkeypatch.setattr(
        "app.evidence.repository.create_artifact",
        lambda artifact, **_kwargs: {"id": "art-undelivered"},
    )
    monkeypatch.setattr(
        stages.hiagent,
        "text_request_token_limits",
        lambda **_kwargs: ("hiagent", "model", 16384),
    )

    budget = stages._BlueprintGenerationBudget()
    budget.retry_grant_id = "grant-undelivered"
    repaired = asyncio.run(stages._repair_narrative_blueprint(
        blueprint,
        episode={"id": "ep-undelivered-patch"},
        source_text=SOURCE,
        additional_errors=["[BLUEPRINT_TEST] n4 需要局部修复"],
        generation_budget=budget,
    ))

    assert len(operation_ids) == 2
    assert operation_ids[0] != operation_ids[1]
    assert budget.provider_calls == 2
    assert repaired.nodes[3].transition_cue == "次日字幕后切到学校门口"


def test_schema_invalid_blueprint_patch_still_fails_the_first_call(
    monkeypatch,
) -> None:
    """A decoded-but-invalid patch is authored output and is never re-rolled."""
    calls = 0

    async def fake_chat(_messages, **_kwargs):
        nonlocal calls
        calls += 1
        return '{"replacements": "not-a-list"}'

    monkeypatch.setattr(stages.model_gateway, "chat", fake_chat)
    monkeypatch.setattr(
        "app.evidence.repository.create_artifact",
        lambda artifact, **_kwargs: {"id": "art-schema-invalid"},
    )
    monkeypatch.setattr(
        stages.hiagent,
        "text_request_token_limits",
        lambda **_kwargs: ("hiagent", "model", 16384),
    )

    budget = stages._BlueprintGenerationBudget()
    budget.retry_grant_id = "grant-schema-invalid"
    with pytest.raises(model_gateway.StructuredFormatError):
        asyncio.run(stages._repair_narrative_blueprint(
            _blueprint(),
            episode={"id": "ep-schema-invalid-patch"},
            source_text=SOURCE,
            additional_errors=["[BLUEPRINT_TEST] n4 需要局部修复"],
            generation_budget=budget,
        ))

    assert calls == 1
