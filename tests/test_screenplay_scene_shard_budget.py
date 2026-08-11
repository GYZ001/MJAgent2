from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app import db
from app.narrative_blueprint import BlueprintScenePlan, NarrativeBlueprint
from app.screenplay_ir import IRIdentity
from app.screenplay_scene_shards import (
    ScreenplaySceneInputContract,
    ScreenplaySceneShardCreativeIR,
    ScreenplaySceneShardCreativeUnit,
    ScreenplaySceneShardPlan,
    ScreenplaySceneUnitSlotPlan,
    build_screenplay_scene_input_contracts,
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


def _story_source_semantics(
    source_segment_ids: list[str],
) -> dict[str, dict[str, str]]:
    return {
        source_id: {
            "narrative_layer": "story",
            "event_priority": "causal",
            "render_policy": "standalone",
            "disposition": "deliver",
            "projection_policy": "picture",
        }
        for source_id in source_segment_ids
    }


def _plan(case: dict) -> ScreenplaySceneShardPlan:
    hashes = case["hashes"]
    unit_slots: list[ScreenplaySceneUnitSlotPlan] = []
    unit_order = 0
    for scene_order, scene_input in enumerate(
        case["scene_inputs"],
        start=1,
    ):
        for scene_unit_order, segment in enumerate(
            scene_input["source_segments"],
            start=1,
        ):
            unit_order += 1
            source_id = segment["source_segment_id"]
            key_base = (
                f"{scene_input['scene_plan_key']}:{source_id}:001"
            )
            unit_slots.append(ScreenplaySceneUnitSlotPlan(
                unit_key=f"{key_base}:unit",
                event_key=f"{key_base}:event",
                scene_key=scene_input["scene_plan_key"],
                scene_order=scene_order,
                unit_order=unit_order,
                scene_unit_order=scene_unit_order,
                kind="action",
                narrative_layer="story",
                event_priority="causal",
                render_policy="standalone",
                source_segment_ids=[source_id],
            ))
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
        unit_slots=unit_slots,
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
    blueprint: NarrativeBlueprint,
) -> list[ScreenplaySceneInputContract]:
    source_by_id = {
        segment["source_segment_id"]: segment["text"]
        for value in case["scene_inputs"]
        for segment in value["source_segments"]
    }
    return build_screenplay_scene_input_contracts(
        plan=plan,
        scene_plans=list(blueprint.scene_plans),
        source_by_id=source_by_id,
        identity_registry=case["identity_registry"],
    )


def _complete_shard(
    case: dict,
    plan: ScreenplaySceneShardPlan,
) -> ScreenplaySceneShardCreativeIR:
    source_by_id = {
        segment["source_segment_id"]: segment["text"]
        for value in case["scene_inputs"]
        for segment in value["source_segments"]
    }
    return ScreenplaySceneShardCreativeIR(
        slots={
            slot.unit_key: ScreenplaySceneShardCreativeUnit(
                text=source_by_id[slot.source_segment_ids[0]],
                resulting_state=(
                    f"完成 {slot.source_segment_ids[0]}"
                ),
            )
            for slot in plan.unit_slots
        },
    )


def _identities(case: dict) -> list[IRIdentity]:
    return [
        IRIdentity(
            key=value["identity_key"],
            display_name=value["canonical_name"],
            authority_id=f"fixture:{value['identity_key']}",
            source_names=list(value["source_labels"]),
            kind="named_character",
            visual_policy="canonical",
            asset_requirement="required",
            role_type="named_character",
        )
        for value in case["identity_registry"]
    ]


def test_ss004_budget_replay_preserves_all_sources_and_events(
    monkeypatch,
) -> None:
    case = _fixture()
    plan = _plan(case)
    blueprint = NarrativeBlueprint(
        episode_no=1,
        nodes=[],
        scene_plans=[
            BlueprintScenePlan.model_validate({
                **value,
                "source_semantics": _story_source_semantics(
                    value["source_segment_ids"]
                ),
            })
            for value in case["scene_plans"]
        ],
        source_scene_owners=case["source_scene_owners"],
        source_semantics=_story_source_semantics(
            list(case["source_scene_owners"])
        ),
    )
    contracts = _contracts(case, plan, blueprint)
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
            identities=_identities(case),
            plans=[plan],
            scene_input_contracts={plan.shard_id: contracts},
        )
    )

    recorded = case["recorded_request"]
    assert captured["max_tokens"] == 9118
    assert captured["max_tokens"] > recorded["requested_max_tokens"]
    assert captured["max_tokens"] <= 16384
    assert captured["call_meta"]["estimated_units"] == 12
    assert captured["call_meta"]["estimated_output_chars"] == 6788
    assert captured["call_meta"]["required_output_tokens"] == 9118
    assert captured["call_meta"]["output_budget_tokens"] == 9118
    assert captured["call_meta"]["output_budget_limited"] is False
    assert len(captured["prompt"]) < recorded["input_chars"]
    assert len(captured["repair_context"]) < case["recorded_attempts"][2][
        "request_chars"
    ]
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
    assert actual_events == {
        slot.event_key for slot in plan.unit_slots
    }

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
