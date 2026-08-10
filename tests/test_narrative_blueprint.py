import asyncio
import json
from types import SimpleNamespace
import uuid

import pytest

from app import stages
from app.errors import ContentGenerationError
from app.narrative_blueprint import (
    BlueprintDecision,
    BlueprintSemanticReview,
    BlueprintSemanticIssue,
    BlueprintStateChange,
    BlueprintStateRequirement,
    NarrativeBlueprint,
    NarrativeBlueprintPatch,
    NarrativeBlueprintShard,
    apply_narrative_blueprint_patch,
    blueprint_semantic_issue_is_resolved,
    derive_blueprint_scene_plans,
    normalize_blueprint_agency_continuity,
    recover_complete_blueprint_prefix,
    validate_and_apply_blueprint_scene_contract,
    validate_blueprint_semantic_review,
    validate_narrative_blueprint,
    validate_narrative_blueprint_shard,
)


SOURCE = "\n\n".join([
    "白洁和王申回到家，白洁洗澡后躺到床上。",
    "白洁回忆冷小玉在咖啡店炫耀优渥生活。",
    "咖啡杯倒影转为卧室台灯，白洁回到现实。",
    "次日小张驾驶王局长的车来到学校。",
])


def _blueprint() -> NarrativeBlueprint:
    return NarrativeBlueprint.model_validate({
        "episode_no": 8,
        "nodes": [
            {
                "key": "n1",
                "source_segment_ids": ["SRC0001"],
                "summary": "两人回家，白洁洗澡后躺下",
                "temporal_domain_key": "present-night",
                "time_label": "夜",
                "time_relation": "episode_start",
                "location_key": "home-bedroom",
                "location_label": "白洁家卧室",
                "participants": ["白洁", "王申"],
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
                "temporal_domain_key": "memory-cafe",
                "time_label": "日前",
                "time_relation": "flashback_enter",
                "location_key": "cafe",
                "location_label": "咖啡店",
                "participants": ["白洁", "冷小玉"],
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
                "temporal_domain_key": "present-night",
                "time_label": "夜",
                "time_relation": "flashback_exit",
                "location_key": "home-bedroom",
                "location_label": "白洁家卧室",
                "participants": ["白洁"],
                "scene_boundary_before": True,
                "transition_cue": "咖啡杯倒影匹配剪辑为卧室台灯",
                "action_logic": "明确回到现在",
            },
            {
                "key": "n4",
                "source_segment_ids": ["SRC0004"],
                "summary": "次日小张开车到学校",
                "temporal_domain_key": "next-day",
                "time_label": "次日",
                "time_relation": "jump",
                "location_key": "school-gate",
                "location_label": "学校门口",
                "participants": ["白洁", "小张"],
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


def test_blueprint_patch_restores_authoritative_source_order() -> None:
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

    assert apply_narrative_blueprint_patch(
        blueprint,
        patch,
        allow_source_expansion=True,
        source_text=SOURCE,
    ) == 2
    assert [node.key for node in blueprint.nodes] == [
        "n1", "n3", "n2", "n4",
    ]
    assert validate_narrative_blueprint(blueprint, SOURCE) == []


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


def test_blueprint_patch_can_split_one_node_without_losing_sources() -> None:
    blueprint = _blueprint()
    original = blueprint.nodes[0]
    first = original.model_copy(deep=True)
    first.key = "n1a"
    first.source_segment_ids = ["SRC0001"]
    second = original.model_copy(deep=True)
    second.key = "n1b"
    second.source_segment_ids = ["SRC0001"]
    second.location_key = "home-bathroom"
    second.location_label = "白洁家浴室"
    second.transition_cue = "白洁走进浴室"
    patch = NarrativeBlueprintPatch.model_validate({
        "replacements": [{
            "node_key": "n1",
            "nodes": [
                first.model_dump(mode="json"),
                second.model_dump(mode="json"),
            ],
        }],
    })

    assert apply_narrative_blueprint_patch(blueprint, patch) == 1
    assert [node.key for node in blueprint.nodes[:2]] == ["n1a", "n1b"]


def test_blueprint_patch_merges_unknown_split_key_by_unique_sources() -> None:
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

    assert apply_narrative_blueprint_patch(blueprint, patch) == 1
    assert [node.key for node in blueprint.nodes[:2]] == [
        "n1a",
        "model-new-node",
    ]


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
