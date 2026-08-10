from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app import db
from app.narrative_blueprint import BlueprintScenePlan, NarrativeBlueprint
from app.screenplay_ir import IRScene, IRSceneUnit
from app.screenplay_scene_shards import (
    ScreenplaySceneInputContract,
    ScreenplaySceneParticipantBinding,
    ScreenplaySceneShardIR,
    ScreenplaySceneShardPlan,
    generate_screenplay_scene_shards,
)


FIXTURE_PATH = (
    Path(__file__).parent
    / "fixtures"
    / "screenplay_scene_shard_ss004.json"
)


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "ss004-budget.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()


def _fixture() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _plan(case: dict) -> ScreenplaySceneShardPlan:
    hashes = case["hashes"]
    return ScreenplaySceneShardPlan(
        shard_id="SS004",
        scene_plan_keys=[
            scene["key"] for scene in case["scene_plans"]
        ],
        source_segment_ids=[
            source_id
            for source_id in case["source_scene_owners"]
            if "SRC0049" <= source_id <= "SRC0059"
        ],
        source_scene_owners=case["source_scene_owners"],
        source_ownership_hash=hashes["source_ownership_hash"],
        estimated_units=case["recorded_request"]["estimated_units"],
        estimated_output_chars=case["recorded_request"][
            "estimated_output_chars"
        ],
        boundary_state_in={
            "scene_key": "bp-sc012",
            "temporal_domain_key": "T001",
            "location_key": "L003",
        },
        boundary_state_out={
            "scene_key": "bp-sc015",
            "temporal_domain_key": "T002",
            "location_key": "L004",
        },
        source_hash=hashes["source_hash"],
        boundary_hash=hashes["boundary_hash"],
        blueprint_hash=hashes["blueprint_hash"],
        identity_registry_hash=hashes["identity_registry_hash"],
    )


def _contracts(
    case: dict,
    plan: ScreenplaySceneShardPlan,
) -> list[ScreenplaySceneInputContract]:
    return [
        ScreenplaySceneInputContract(
            scene_plan_key=value["scene_plan_key"],
            node_keys=value["node_keys"],
            source_segment_ids=[
                segment["source_segment_id"]
                for segment in value["source_segments"]
            ],
            source_segments=value["source_segments"],
            participant_bindings=[
                ScreenplaySceneParticipantBinding(
                    blueprint_key=blueprint_key,
                    identity_key=identity_key,
                )
                for blueprint_key, identity_key
                in value["participant_bindings"]
            ],
            source_scene_owners=plan.source_scene_owners,
            source_ownership_hash=plan.source_ownership_hash,
        )
        for value in case["scene_inputs"]
    ]


def _complete_shard(
    case: dict,
    plan: ScreenplaySceneShardPlan,
) -> ScreenplaySceneShardIR:
    source_event = {
        source_id: event_key
        for event_key, source_ids in case["event_sources"].items()
        for source_id in source_ids
    }
    scenes: list[IRScene] = []
    consumed_source_ids: list[str] = []
    for value in case["scene_inputs"]:
        units: list[IRSceneUnit] = []
        for segment in value["source_segments"]:
            source_id = segment["source_segment_id"]
            consumed_source_ids.append(source_id)
            units.append(IRSceneUnit(
                kind="action",
                text=segment["text"],
                event_key=source_event[source_id],
                source_segment_ids=[source_id],
                resulting_state=f"完成 {source_id}",
            ))
        scene_plan = next(
            item for item in case["scene_plans"]
            if item["key"] == value["scene_plan_key"]
        )
        scenes.append(IRScene(
            key=scene_plan["key"],
            scene_heading=scene_plan["scene_heading"],
            story_function="完整交付本场全部来源事件",
            summary=scene_plan["exit_state"],
            entry_state=scene_plan["previous_scene_exit_state"],
            exit_state=scene_plan["exit_state"],
            units=units,
        ))
    return ScreenplaySceneShardIR(
        episode_no=1,
        shard_id=plan.shard_id,
        scene_plan_keys=plan.scene_plan_keys,
        scenes=scenes,
        consumed_source_ids=consumed_source_ids,
        source_hash=plan.source_hash,
        boundary_hash=plan.boundary_hash,
        blueprint_hash=plan.blueprint_hash,
        identity_registry_hash=plan.identity_registry_hash,
        source_ownership_hash=plan.source_ownership_hash,
    )


def test_ss004_budget_replay_preserves_all_sources_and_events(
    monkeypatch,
) -> None:
    case = _fixture()
    plan = _plan(case)
    contracts = _contracts(case, plan)
    blueprint = NarrativeBlueprint(
        episode_no=1,
        nodes=[],
        scene_plans=[
            BlueprintScenePlan.model_validate(value)
            for value in case["scene_plans"]
        ],
        source_scene_owners=case["source_scene_owners"],
    )
    complete_shard = _complete_shard(case, plan)
    captured: dict = {}

    async def fake_structured(messages, **kwargs):
        captured["prompt"] = messages[0]["content"]
        captured["max_tokens"] = kwargs["max_tokens"]
        captured["repair_context"] = kwargs["repair_context"]
        captured["call_meta"] = kwargs["call_meta"]
        return complete_shard

    monkeypatch.setattr(
        "app.screenplay_scene_shards.model_gateway.chat_structured",
        fake_structured,
    )
    source_text = "\n\n".join([
        *[f"结构占位 {index}" for index in range(1, 49)],
        *[
            segment["text"]
            for value in case["scene_inputs"]
            for segment in value["source_segments"]
        ],
        "后续结构占位 60",
        "后续结构占位 61",
        "后续结构占位 62",
    ])

    shards, _artifact_ids, rows = asyncio.run(
        generate_screenplay_scene_shards(
            episode=case["episode"],
            source_text=source_text,
            blueprint=blueprint,
            identity_registry=case["identity_registry"],
            identities=[],
            plans=[plan],
            scene_input_contracts={plan.shard_id: contracts},
        )
    )

    recorded = case["recorded_request"]
    assert captured["max_tokens"] > recorded["requested_max_tokens"]
    assert captured["max_tokens"] <= 16384
    assert captured["call_meta"]["estimated_units"] == 12
    assert captured["call_meta"]["estimated_output_chars"] == 6788
    assert [row["status"] for row in rows] == ["validated"]

    expected_sources = plan.source_segment_ids
    actual_sources = [
        source_id
        for scene in shards[0].scenes
        for unit in scene.units
        for source_id in unit.source_segment_ids
    ]
    actual_events = {
        unit.event_key
        for scene in shards[0].scenes
        for unit in scene.units
    }
    assert actual_sources == expected_sources
    assert shards[0].consumed_source_ids == expected_sources
    assert actual_events == set(case["event_sources"])

    for source_input in case["scene_inputs"]:
        for segment in source_input["source_segments"]:
            assert segment["text"] in captured["prompt"]
            assert segment["text"] in captured["repair_context"]
    assert '"SRC0001":"bp-sc001"' not in captured["prompt"]
    assert '"SRC0001":"bp-sc001"' not in captured["repair_context"]
    assert "person_59b6805875cd" not in captured["prompt"]
    assert "person_59b6805875cd" not in captured["repair_context"]

    assert [
        attempt["finish_reason"]
        for attempt in case["recorded_attempts"]
    ] == ["length", "stop", "length"]
    assert sum(
        attempt["finish_reason"] == "length"
        for attempt in case["recorded_attempts"]
    ) == 2
