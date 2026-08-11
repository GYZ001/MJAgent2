import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.identity_authority import (
    IdentityAuthorityConflictError,
    identity_authority_registry,
)
from app.narrative_blueprint import (
    NarrativeBlueprint,
    NarrativeBlueprintPatch,
    NarrativeParticipantEvidence,
    apply_narrative_blueprint_patch,
    blueprint_prompt_contract,
    blueprint_semantic_review_schema,
    blueprint_voice_identity_issues,
    derive_blueprint_scene_plans,
    validate_narrative_blueprint,
)
from app.screenplay_scene_shards import (
    ScreenplaySceneShardError,
    build_screenplay_scene_input_contracts,
    build_screenplay_scene_shard_plans,
)
from app.source_excerpt import index_source_segments


FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "run_52997444a173_voice_identity_contract.json"
)


def _fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _source(payload: dict) -> str:
    overrides = payload["source_overrides"]
    return "\n\n".join(
        overrides.get(
            f"SRC{index:04d}",
            f"结构化来源动作 {index}。",
        )
        for index in range(1, payload["source_count"] + 1)
    )


def _blueprint(payload: dict, *, repaired: bool) -> NarrativeBlueprint:
    ranges = [
        (1, 8),
        (9, 16),
        (17, 24),
        (25, 32),
        (33, 40),
        (41, 48),
        (49, 56),
        (57, 59),
        (60, 62),
    ]
    nodes = []
    for index, (start, end) in enumerate(ranges, start=1):
        audit_only = start == 60
        node = {
            "key": f"node-{index}",
            "source_segment_ids": [
                f"SRC{source_index:04d}"
                for source_index in range(start, end + 1)
            ],
            "summary": f"来源动作组 {index}",
            "narrative_layer": "paratext" if audit_only else "story",
            "event_priority": "connective" if audit_only else "causal",
            "render_policy": (
                "exclude_from_spine" if audit_only else "standalone"
            ),
            "temporal_domain_key": "episode-present",
            "time_label": "当下",
            "time_relation": "episode_start" if index == 1 else "continuous",
            "location_key": "episode-location",
            "location_label": "当前地点",
            "action_logic": f"按来源顺序交付动作组 {index}",
        }
        if start == 49:
            node["participants"] = [
                payload["blueprint_identity_reference"],
            ]
            node["participant_evidence"] = payload[
                "repaired_participant_evidence"
                if repaired
                else "old_participant_evidence"
            ]
        nodes.append(node)
    return NarrativeBlueprint.model_validate({
        "episode_no": 1,
        "nodes": nodes,
    })


def test_old_blueprint_fails_with_typed_voice_issues_before_scene_input() -> None:
    payload = _fixture()
    blueprint = _blueprint(payload, repaired=False)
    source_text = _source(payload)

    issues = blueprint_voice_identity_issues(blueprint, source_text)
    errors = validate_narrative_blueprint(blueprint, source_text)

    assert [issue.code for issue in issues] == [
        "voice_identity_missing",
        "voice_identity_missing",
        "voice_identity_missing",
    ]
    assert [issue.source_segment_ids for issue in issues] == [
        ["SRC0052"],
        ["SRC0053"],
        ["SRC0056"],
    ]
    assert all(
        "[BLUEPRINT_VOICE_IDENTITY_MISSING]" in error
        for error in errors
    )


def test_voice_issue_contract_distinguishes_ambiguous_and_conflict() -> None:
    payload = _fixture()
    source_text = _source(payload)
    blueprint = _blueprint(payload, repaired=True)
    node = blueprint.nodes[6]
    node.participants.append("episode:other")
    node.participant_evidence.append(NarrativeParticipantEvidence(
        identity_key="episode:other",
        source_segment_ids=["SRC0052"],
        source_unit_keys=["SRC0052:unit:001"],
        usage="voice",
    ))

    issues = blueprint_voice_identity_issues(blueprint, source_text)
    assert issues[0].code == "voice_identity_ambiguous"

    node.participant_evidence[-1] = node.participant_evidence[1].model_copy(
        deep=True,
    )
    issues = blueprint_voice_identity_issues(blueprint, source_text)
    assert issues[0].code == "voice_identity_conflict"


def test_voice_evidence_without_source_unit_keys_hard_fails() -> None:
    payload = _fixture()
    source_text = _source(payload)
    blueprint = _blueprint(payload, repaired=True)
    blueprint.nodes[6].participant_evidence[1].source_unit_keys = []

    issues = blueprint_voice_identity_issues(blueprint, source_text)

    assert any(
        issue.code == "voice_identity_conflict"
        and "source_unit_keys" in issue.message
        for issue in issues
    )
    assert any(
        issue.code == "voice_identity_missing"
        and issue.source_segment_ids == ["SRC0052"]
        for issue in issues
    )

    repaired = _blueprint(payload, repaired=True)
    assert apply_narrative_blueprint_patch(
        blueprint,
        NarrativeBlueprintPatch.model_validate({
            "replacements": [{
                "node_key": repaired.nodes[6].key,
                "node": repaired.nodes[6].model_dump(mode="json"),
            }],
        }),
        source_text=source_text,
    ) == 1
    plans = build_screenplay_scene_shard_plans(
        blueprint,
        source_text=source_text,
        identity_registry_hash="fixture-identity-registry",
    )
    plan = next(
        item for item in plans if "SRC0052" in item.source_segment_ids
    )
    scene_plans = [
        scene
        for scene in blueprint.scene_plans
        if scene.key in plan.scene_plan_keys
    ]
    blueprint.nodes[6].participant_evidence[1].source_unit_keys = []

    with pytest.raises(
        ScreenplaySceneShardError,
        match="voice identity evidence .*source_unit_keys",
    ):
        build_screenplay_scene_input_contracts(
            plan=plan,
            scene_plans=scene_plans,
            source_by_id={
                segment.segment_id: segment.text
                for segment in index_source_segments(source_text)
            },
            identity_registry=payload["identity_registry"],
            blueprint_nodes=blueprint.nodes,
        )


def test_voice_evidence_with_no_source_scope_hard_fails_early() -> None:
    payload = _fixture()
    source_text = _source(payload)
    blueprint = _blueprint(payload, repaired=True)
    blueprint.nodes[6].participant_evidence.append(
        NarrativeParticipantEvidence(
            identity_key="episode:traveler",
            usage="voice",
        )
    )

    issues = blueprint_voice_identity_issues(blueprint, source_text)

    assert any(
        issue.code == "voice_identity_conflict"
        and "source_unit_keys" in issue.message
        for issue in issues
    )


def test_segment_scoped_voice_without_dialogue_remains_valid() -> None:
    source_text = "远处传来守夜人的呼喊。"
    blueprint = NarrativeBlueprint.model_validate({
        "episode_no": 1,
        "nodes": [{
            "key": "node-1",
            "source_segment_ids": ["SRC0001"],
            "summary": "守夜人在远处呼喊",
            "narrative_layer": "story",
            "event_priority": "causal",
            "render_policy": "standalone",
            "temporal_domain_key": "episode-present",
            "time_label": "当下",
            "time_relation": "episode_start",
            "location_key": "courtyard",
            "location_label": "院中",
            "participants": ["episode:watchman"],
            "participant_evidence": [{
                "identity_key": "episode:watchman",
                "source_segment_ids": ["SRC0001"],
                "usage": "voice",
            }],
            "action_logic": "守夜人的呼喊从远处传入院中",
        }],
    })
    derive_blueprint_scene_plans(blueprint)

    assert blueprint_voice_identity_issues(blueprint, source_text) == []

    plan = build_screenplay_scene_shard_plans(
        blueprint,
        source_text=source_text,
        identity_registry_hash="identity-registry",
    )[0]
    contracts = build_screenplay_scene_input_contracts(
        plan=plan,
        scene_plans=blueprint.scene_plans,
        source_by_id={"SRC0001": source_text},
        identity_registry=[{
            "identity_key": "person_watchman",
            "authority_id": "functional:watchman",
            "canonical_name": "守夜人",
            "identity_group": "episode:watchman",
            "source_labels": ["守夜人"],
        }],
        blueprint_nodes=blueprint.nodes,
    )

    assert len(contracts[0].unit_slots) == 1
    slot = contracts[0].unit_slots[0]
    assert slot.kind == "action"
    assert slot.speaker_key == "person_watchman"
    assert [
        (delivery.participant_key, delivery.audible)
        for delivery in slot.participant_deliveries
    ] == [("person_watchman", True)]


def test_repair_preserves_timeline_and_compiles_ss004_canonical_speakers() -> None:
    payload = _fixture()
    source_text = _source(payload)
    blueprint = _blueprint(payload, repaired=False)
    repaired_node = _blueprint(payload, repaired=True).nodes[6]
    canonical_before = [
        (
            node.key,
            list(node.source_segment_ids),
            node.narrative_layer,
            node.event_priority,
            node.render_policy,
        )
        for node in blueprint.nodes
    ]
    patch = NarrativeBlueprintPatch.model_validate({
        "replacements": [{
            "node_key": repaired_node.key,
            "node": repaired_node.model_dump(mode="json"),
        }],
    })

    assert apply_narrative_blueprint_patch(
        blueprint,
        patch,
        source_text=source_text,
    ) == 1
    assert validate_narrative_blueprint(blueprint, source_text) == []
    assert canonical_before == [
        (
            node.key,
            list(node.source_segment_ids),
            node.narrative_layer,
            node.event_priority,
            node.render_policy,
        )
        for node in blueprint.nodes
    ]

    plans = build_screenplay_scene_shard_plans(
        blueprint,
        source_text=source_text,
        identity_registry_hash="fixture-identity-registry",
    )
    plan = next(
        item for item in plans if "SRC0052" in item.source_segment_ids
    ).model_copy(update={"shard_id": payload["expected_scene_shard_id"]})
    scene_plans = [
        scene
        for scene in blueprint.scene_plans
        if scene.key in plan.scene_plan_keys
    ]
    source_by_id = {
        segment.segment_id: segment.text
        for segment in index_source_segments(source_text)
    }
    contracts = build_screenplay_scene_input_contracts(
        plan=plan,
        scene_plans=scene_plans,
        source_by_id=source_by_id,
        identity_registry=payload["identity_registry"],
        blueprint_nodes=blueprint.nodes,
    )
    dialogue_slots = [
        slot
        for contract in contracts
        for slot in contract.unit_slots
        if slot.kind == "dialogue"
    ]

    assert plan.shard_id == "SS004"
    assert {
        slot.source_segment_ids[0] for slot in dialogue_slots
    } == {"SRC0052", "SRC0053", "SRC0056"}
    assert {
        slot.speaker_key for slot in dialogue_slots
    } == {"person_traveler"}
    assert all(
        slot.onscreen_entity_keys == ["person_traveler"]
        for slot in dialogue_slots
    )
    assert {
        source_id
        for annotation in blueprint.source_audit_annotations
        for source_id in annotation.source_segment_ids
    } == {"SRC0060", "SRC0061", "SRC0062"}


def test_dynamic_prompt_review_and_repair_contracts_expose_voice() -> None:
    contract = blueprint_prompt_contract()
    review_schema = blueprint_semantic_review_schema(
        ["node-7"],
        ["SRC0052"],
    )
    issue_code = review_schema["$defs"]["BlueprintSemanticIssue"][
        "properties"
    ]["code"]

    assert "source_unit_keys" in contract[
        "participant_evidence_required"
    ]["fields"]
    assert "voice_identity_missing" in issue_code["enum"]
    assert "voice_identity_ambiguous" in issue_code["enum"]
    assert "voice_identity_conflict" in issue_code["enum"]


def test_same_identity_group_with_two_canonical_keys_hard_fails() -> None:
    payload = _fixture()
    bible = SimpleNamespace(characters=[])

    with pytest.raises(IdentityAuthorityConflictError) as exc_info:
        identity_authority_registry(
            bible,
            payload["conflicting_resolutions"],
        )

    assert exc_info.value.issues[0]["reason"] == (
        "identity_group_multiple_canonical_identities"
    )

    separate = [
        {
            **resolution,
            "identity_group": f"group-{index}",
        }
        for index, resolution in enumerate(
            payload["conflicting_resolutions"],
            start=1,
        )
    ]
    registry = identity_authority_registry(bible, separate)
    assert len(registry) == 2
