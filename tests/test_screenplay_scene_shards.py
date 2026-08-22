from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import math
from pathlib import Path
import uuid

import pytest
from pydantic import ValidationError

from app import db, errors as app_errors, generation_concurrency
from app import screenplay_scene_shards as scene_shards_module
from app.harness import model_gateway
from app.narrative_blueprint import (
    BlueprintScenePlan,
    NarrativeBlueprint,
    NarrativeBlueprintShard,
    NarrativeNode,
    NarrativeParticipantEvidence,
    apply_blueprint_state_subject_ownership_patch,
    blueprint_shard_candidate_hash,
    blueprint_state_subject_issues,
    blueprint_voice_identity_issues,
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
    compile_screenplay_ir,
)
from app.screenplay_scene_shards import (
    SCREENPLAY_SCENE_CREATIVE_VERSION,
    SCREENPLAY_SCENE_INPUT_VERSION,
    SCREENPLAY_SCENE_SEMANTIC_REVIEW_VERSION,
    SCREENPLAY_SCENE_SHARD_VERSION,
    ScreenplaySceneShardCreativeIR,
    ScreenplaySceneShardSemanticReview,
    ScreenplaySceneShardSemanticFinding,
    ScreenplaySceneShardCreativeUnit,
    ScreenplayEnvelopeExperience,
    ScreenplayEnvelopeIR,
    ScreenplayEnvelopeMetadata,
    ScreenplaySceneMergeError,
    ScreenplaySceneInputContract,
    ScreenplaySceneParticipantBinding,
    ScreenplaySceneCompiledUnitSlot,
    ScreenplaySceneShardError,
    ScreenplaySceneShardOwnershipLost,
    ScreenplaySceneShardIR,
    ScreenplaySceneShardPlan,
    ScreenplaySceneSourceSegment,
    ScreenplaySceneUnitSlotPlan,
    UnresolvedParticipant,
    blueprint_content_hash,
    build_screenplay_scene_shard_repair_schema,
    build_frozen_identity_registry,
    build_screenplay_scene_input_contract_set,
    build_screenplay_scene_input_contracts,
    build_screenplay_scene_shard_plans,
    compile_screenplay_scene_shard_draft,
    generate_screenplay_envelope,
    generate_screenplay_scene_shards,
    merge_screenplay_scene_shards,
    normalize_screenplay_scene_shard,
    normalize_screenplay_scene_shard_payload,
    screenplay_envelope_artifact_compatibility,
    screenplay_scene_identity_scaffold_hash,
    screenplay_scene_generation_scaffold_hash,
    screenplay_scene_shard_artifact_compatibility,
    validate_screenplay_scene_shard,
)
from app.source_excerpt import index_source_segments
from app.source_facts import source_segment_facts
from app.narrative_priority import picture_screenplay_projection


SOURCE = "甲推门进入。\n\n乙接过钥匙并回答。"
_REAL_SEMANTIC_REVIEW = (
    scene_shards_module._semantic_review_scene_shard_draft
)


@pytest.fixture(autouse=True)
def _clean_scene_shard_semantic_review(monkeypatch):
    async def clean_review(*, draft, **_kwargs):
        creative_hash = scene_shards_module._hash(
            draft.model_dump(mode="json")
        )
        return draft, [{
            "phase": "initial",
            "creative_hash": creative_hash,
            "reviews": [{"findings": []}, {"findings": []}],
            "consensus": [],
        }]

    monkeypatch.setattr(
        "app.screenplay_scene_shards._semantic_review_scene_shard_draft",
        clean_review,
    )
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
RUN_884443DC4404_REPLAY = (
    Path(__file__).parent
    / "fixtures"
    / "run_884443dc4404_semantic_review.json"
)
ATTEMPT6_CALL_63118_SEMANTIC_BUDGET = (
    Path(__file__).parent
    / "fixtures"
    / "attempt6_call_63118_semantic_budget.json"
)
ATTEMPT7_CALL_63139_SEMANTIC_RESPONSE = (
    Path(__file__).parent
    / "fixtures"
    / "attempt7_call_63139_semantic_response.json"
)
RUN_B0659B64B548_REPLAY = (
    Path(__file__).parent
    / "fixtures"
    / "run_b0659b64b548_semantic_review.json"
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
A78_ARTIFACT_REPLAY = (
    Path(__file__).parent
    / "fixtures"
    / "screenplay_scene_shard_a78_art_5e0650367127.json"
)
RUN_D6BA3C89_REPLAY = (
    Path(__file__).parent
    / "fixtures"
    / "screenplay_scene_shard_run_d6ba3c89a60f.json"
)
RUN_195A691_REPLAY = (
    Path(__file__).parent
    / "fixtures"
    / "screenplay_scene_shard_run_195a69113451_min.json"
)
SS001_FULL_ARTIFACT_FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "screenplay_scene_shard_ss001_art_bcebe2075a55_full.json"
)
RUN_E65D871AD2A0_FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "run_e65d871ad2a0_sc16_paratext.json"
)
RUN_64A2E395D6DF_FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "run_64a2e395d6df_blueprint_partition.json"
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
                "narrative_layer": "story",
                "event_priority": "causal",
                "render_policy": "standalone",
                "temporal_domain_key": "present",
                "time_label": "日",
                "time_relation": "episode_start",
                "location_key": "door",
                "location_label": "门口",
                "participants": [],
                "participant_evidence": [{
                    "identity_key": "甲",
                    "source_segment_ids": ["SRC0001"],
                    "source_unit_keys": ["SRC0001:unit:001"],
                    "usage": "state_subject",
                }, {
                    "identity_key": "甲",
                    "source_segment_ids": ["SRC0001"],
                    "source_unit_keys": ["SRC0001:unit:001"],
                    "usage": "visible",
                }],
                "action_logic": "甲推门进入",
                "scene_boundary_before": True,
            },
            {
                "key": "n2",
                "source_segment_ids": ["SRC0002"],
                "summary": "乙接过钥匙并回答",
                "narrative_layer": "story",
                "event_priority": "causal",
                "render_policy": "standalone",
                "temporal_domain_key": "later" if split_domain else "present",
                "time_label": "稍后" if split_domain else "日",
                "time_relation": "elapsed" if split_domain else "continuous",
                "location_key": "room",
                "location_label": "室内",
                "participants": [],
                "participant_evidence": [{
                    "identity_key": "乙",
                    "source_segment_ids": ["SRC0002"],
                    "source_unit_keys": ["SRC0002:unit:001"],
                    "usage": "state_subject",
                }, {
                    "identity_key": "乙",
                    "source_segment_ids": ["SRC0002"],
                    "source_unit_keys": ["SRC0002:unit:001"],
                    "usage": "visible",
                }],
                "action_logic": "乙接过钥匙并回答",
                "scene_boundary_before": True,
            },
        ],
    })
    derive_blueprint_scene_plans(value)
    return value


def _semantic_node(
    *,
    key: str,
    source_segment_ids: list[str],
    summary: str,
    location_key: str,
    location_label: str,
    story: bool,
    first: bool = False,
) -> dict:
    return {
        "key": key,
        "source_segment_ids": source_segment_ids,
        "summary": summary,
        "narrative_layer": "story" if story else "paratext",
        "event_priority": "causal" if story else "connective",
        "render_policy": (
            "standalone" if story else "exclude_from_spine"
        ),
        "temporal_domain_key": "present",
        "time_label": "连续时间",
        "time_relation": "episode_start" if first else "continuous",
        "location_key": location_key,
        "location_label": location_label,
        "participants": [],
        "environment_source_unit_keys": (
            [f"{source_id}:unit:001" for source_id in source_segment_ids]
            if story else []
        ),
        "action_logic": summary,
        "scene_boundary_before": story,
    }


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


def test_scene_shard_semantic_review_json_contract_is_strict() -> None:
    schema = ScreenplaySceneShardSemanticReview.model_json_schema()

    assert SCREENPLAY_SCENE_SEMANTIC_REVIEW_VERSION == (
        "screenplay-scene-semantic-review.v15"
    )
    assert schema["required"] == ["findings"]
    assert "default" not in schema["properties"]["findings"]
    finding_schema = schema["$defs"][
        "ScreenplaySceneShardSemanticFinding"
    ]
    assert "related_unit_keys" in finding_schema["required"]
    assert "default" not in finding_schema["properties"][
        "related_unit_keys"
    ]
    assert ScreenplaySceneShardSemanticReview.model_validate(
        {"findings": []}
    ).findings == []
    with pytest.raises(ValidationError, match="Field required"):
        ScreenplaySceneShardSemanticReview.model_validate({})
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ScreenplaySceneShardSemanticReview.model_validate({
            "findings": [],
            "explanation": "none",
        })


def test_scene_shard_semantic_review_schema_binds_exact_chunk_keys() -> None:
    unit_keys = [
        "bp-sc006:SRC0012:001:unit",
        "bp-sc006:SRC0012:002:unit",
        "bp-sc007:SRC0015:001:unit",
    ]

    schema = scene_shards_module._scene_shard_semantic_review_schema(
        unit_keys
    )
    finding_properties = schema["$defs"][
        "ScreenplaySceneShardSemanticFinding"
    ]["properties"]
    response_format = (
        scene_shards_module._scene_shard_semantic_review_response_format(
            schema
        )
    )

    assert finding_properties["unit_key"]["enum"] == unit_keys
    related_schema = finding_properties["related_unit_keys"]
    assert related_schema["items"]["enum"] == unit_keys
    assert related_schema["maxItems"] == 1
    assert finding_properties["violation_kinds"]["minItems"] == 1
    assert finding_properties["violation_kinds"]["maxItems"] == 5
    assert finding_properties["message"]["minLength"] == 1
    assert finding_properties["message"]["maxLength"] == 160
    assert schema["properties"]["findings"]["maxItems"] == 6
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["strict"] is True
    provider_schema = response_format["json_schema"]["schema"]
    assert provider_schema is not schema
    provider_finding_properties = provider_schema["$defs"][
        "ScreenplaySceneShardSemanticFinding"
    ]["properties"]
    assert provider_finding_properties["unit_key"]["enum"] == unit_keys
    assert provider_finding_properties["related_unit_keys"]["items"][
        "enum"
    ] == unit_keys

    provider_keywords: set[str] = set()

    def collect_provider_keywords(schema_node: dict) -> None:
        provider_keywords.update(schema_node)
        for mapping_keyword in ("$defs", "properties"):
            for child_schema in schema_node.get(
                mapping_keyword,
                {},
            ).values():
                collect_provider_keywords(child_schema)
        items = schema_node.get("items")
        if isinstance(items, dict):
            collect_provider_keywords(items)

    collect_provider_keywords(provider_schema)
    assert provider_keywords <= (
        scene_shards_module
        ._SCREENPLAY_SCENE_STRICT_PROVIDER_SCHEMA_KEYWORDS
    )
    assert provider_keywords.isdisjoint({
        "title",
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
        "uniqueItems",
    })

    excessive_review = ScreenplaySceneShardSemanticReview(findings=[
        ScreenplaySceneShardSemanticFinding(
            unit_key=f"unknown-{index}",
            related_unit_keys=[],
            code="source_semantic_drift",
            violation_kinds=["unsupported_action"],
            message="out of chunk",
        )
        for index in range(7)
    ])
    assert any(
        "actual=7，limit=6" in error
        for error in scene_shards_module._scene_shard_review_reference_errors(
            excessive_review,
            set(unit_keys),
            allow_local_omitted_unit_key=True,
        )
    )


def test_scene_shard_creative_response_format_binds_dynamic_slot_contract(
) -> None:
    blueprint = _blueprint(split_domain=False)
    plan = build_screenplay_scene_shard_plans(
        blueprint,
        source_text=SOURCE,
        identity_registry_hash="identity-hash",
    )[0]
    contracts = _contracts([plan], blueprint)[plan.shard_id]
    dialogue_slot = plan.unit_slots[0]
    dialogue_slot.kind = "dialogue"
    dialogue_slot.source_text = "“不得改写的来源对白”"
    local_schema = build_screenplay_scene_shard_repair_schema(
        plan=plan,
        scene_input_contracts=contracts,
    )
    local_schema_before = deepcopy(local_schema)
    response_format = scene_shards_module._scene_shard_strict_response_format(
        name="screenplay_scene_shard_creative",
        local_schema=local_schema,
    )
    provider_schema = response_format["json_schema"]["schema"]

    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["strict"] is True
    assert local_schema == local_schema_before
    assert response_format == (
        scene_shards_module._scene_shard_strict_response_format(
            name="screenplay_scene_shard_creative",
            local_schema=local_schema,
        )
    )
    assert local_schema["properties"]["contract_version"]["const"] == (
        SCREENPLAY_SCENE_CREATIVE_VERSION
    )
    assert provider_schema["properties"]["contract_version"]["enum"] == [
        SCREENPLAY_SCENE_CREATIVE_VERSION
    ]
    local_dialogue_schema = local_schema["properties"]["slots"][
        "properties"
    ][dialogue_slot.unit_key]
    assert local_dialogue_schema["allOf"][1]["properties"]["text"][
        "const"
    ] == dialogue_slot.source_text
    provider_slot_schema = provider_schema["properties"]["slots"][
        "properties"
    ][dialogue_slot.unit_key]
    provider_definition = provider_schema["$defs"][
        provider_slot_schema["$ref"].rsplit("/", 1)[-1]
    ]
    assert provider_definition["properties"]["text"]["enum"] == [
        dialogue_slot.source_text
    ]
    action_slot = plan.unit_slots[1]
    provider_action_schema = provider_schema["properties"]["slots"][
        "properties"
    ][action_slot.unit_key]
    assert provider_action_schema["$ref"] != provider_slot_schema["$ref"]
    provider_action_definition = provider_schema["$defs"][
        provider_action_schema["$ref"].rsplit("/", 1)[-1]
    ]
    assert "enum" not in provider_action_definition["properties"]["text"]
    assert all(
        value["$ref"].rsplit("/", 1)[-1] in provider_schema["$defs"]
        for value in provider_schema["properties"]["slots"][
            "properties"
        ].values()
    )

    provider_keywords: set[str] = set()

    def audit(node: dict) -> None:
        provider_keywords.update(node)
        properties = node.get("properties")
        if isinstance(properties, dict):
            assert node["additionalProperties"] is False
            assert node["required"] == list(properties)
            for child in properties.values():
                audit(child)
        definitions = node.get("$defs")
        if isinstance(definitions, dict):
            for child in definitions.values():
                audit(child)
        items = node.get("items")
        if isinstance(items, dict):
            audit(items)

    audit(provider_schema)
    assert provider_keywords <= (
        scene_shards_module
        ._SCREENPLAY_SCENE_STRICT_PROVIDER_SCHEMA_KEYWORDS
    )
    assert provider_keywords.isdisjoint({
        "allOf",
        "const",
        "default",
        "title",
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
        "uniqueItems",
    })


def test_attempt7_call_63139_has_no_complete_json_root_to_accept() -> None:
    case = json.loads(
        ATTEMPT7_CALL_63139_SEMANTIC_RESPONSE.read_text(encoding="utf-8")
    )
    content = (
        case["content_non_whitespace_prefix"]
        + "\n" * case["trailing_newlines"]
        + " " * case["trailing_spaces"]
    )

    assert len(content) == case["content_chars"] == 63869
    assert len(content.rstrip()) == case["content_non_whitespace_chars"] == 26
    assert case["finish_reason"] == "length"
    assert case["completion_tokens"] == case["effective_max_tokens"] == 31928
    assert case["reasoning_tokens"] == 0
    assert case["complete_top_level_roots"] == 0
    authority_root = model_gateway._latest_json_authority_root(content)
    assert authority_root == (
        "object",
        case["content_non_whitespace_prefix"],
    )
    assert model_gateway._json_candidates(content) == []
    with pytest.raises(json.JSONDecodeError, match="Invalid control character"):
        json.loads(content)


def test_scene_shard_semantic_finding_kind_and_message_are_bounded() -> None:
    finding = {
        "unit_key": "unit-1",
        "code": "source_semantic_drift",
        "violation_kinds": [
            "environment_personification",
            "wrong_subject",
        ],
        "message": "明确冲突",
    }

    assert ScreenplaySceneShardSemanticFinding.model_validate(
        finding
    ).model_dump(mode="json") == {
        **finding,
        "related_unit_keys": [],
        "violation_kinds": [
            "wrong_subject",
            "environment_personification",
        ],
    }
    with pytest.raises(ValidationError, match="Field required"):
        ScreenplaySceneShardSemanticFinding.model_validate({
            key: value
            for key, value in finding.items()
            if key != "violation_kinds"
        })
    with pytest.raises(ValidationError, match="at least 1 item"):
        ScreenplaySceneShardSemanticFinding.model_validate({
            **finding,
            "violation_kinds": [],
        })
    with pytest.raises(ValidationError, match="不得重复"):
        ScreenplaySceneShardSemanticFinding.model_validate({
            **finding,
            "violation_kinds": [
                "wrong_subject",
                "wrong_subject",
            ],
        })
    with pytest.raises(ValidationError, match="Extra inputs"):
        ScreenplaySceneShardSemanticFinding.model_validate({
            **finding,
            "violation_kind": "wrong_subject",
        })
    with pytest.raises(ValidationError, match="at least 1 character"):
        ScreenplaySceneShardSemanticFinding.model_validate({
            **finding,
            "message": "",
        })
    with pytest.raises(ValidationError, match="at most 160 characters"):
        ScreenplaySceneShardSemanticFinding.model_validate({
            **finding,
            "message": "冲" * 161,
        })
    cross_finding = {
        **finding,
        "violation_kinds": ["cross_slot_duplication"],
    }
    with pytest.raises(ValidationError, match="Field required"):
        ScreenplaySceneShardSemanticFinding.model_validate(cross_finding)
    with pytest.raises(ValidationError, match="恰好声明一个"):
        ScreenplaySceneShardSemanticFinding.model_validate({
            **cross_finding,
            "related_unit_keys": [],
        })
    with pytest.raises(ValidationError, match="不得包含 finding 自身"):
        ScreenplaySceneShardSemanticFinding.model_validate({
            **cross_finding,
            "related_unit_keys": ["unit-1"],
        })
    with pytest.raises(ValidationError, match="不得重复"):
        ScreenplaySceneShardSemanticFinding.model_validate({
            **cross_finding,
            "related_unit_keys": ["unit-2", "unit-2"],
        })
    with pytest.raises(ValidationError, match="必须为空"):
        ScreenplaySceneShardSemanticFinding.model_validate({
            **finding,
            "related_unit_keys": ["unit-2"],
        })


def test_semantic_review_repairs_unique_structural_unit_ordinal() -> None:
    review = ScreenplaySceneShardSemanticReview.model_validate({
        "findings": [{
            "unit_key": "bp-sc0002:026:unit",
            "related_unit_keys": [],
            "code": "source_semantic_drift",
            "violation_kinds": ["unsupported_action"],
            "message": "模型把 canonical unit key 的中间结构写坏",
        }],
    })

    changed = (
        scene_shards_module
        ._scene_shard_canonicalize_review_unit_references(
            review,
            {
                "bp-sc002:SRC0002:025:unit",
                "bp-sc002:SRC0002:026:unit",
            },
        )
    )

    assert changed == 1
    assert review.findings[0].unit_key == (
        "bp-sc002:SRC0002:026:unit"
    )


def test_semantic_review_does_not_guess_ambiguous_unit_ordinal() -> None:
    review = ScreenplaySceneShardSemanticReview.model_validate({
        "findings": [{
            "unit_key": "malformed:026:unit",
            "related_unit_keys": [],
            "code": "source_semantic_drift",
            "violation_kinds": ["unsupported_action"],
            "message": "相同 ordinal 对应多个候选时不能猜测",
        }],
    })

    changed = (
        scene_shards_module
        ._scene_shard_canonicalize_review_unit_references(
            review,
            {
                "bp-sc002:SRC0002:026:unit",
                "bp-sc002:SRC0003:026:unit",
            },
        )
    )

    assert changed == 0
    assert review.findings[0].unit_key == "malformed:026:unit"


@pytest.mark.parametrize("blank_unit_key", ["", " \t "])
def test_semantic_review_aligns_single_peer_proven_local_blank_scope(
    blank_unit_key: str,
) -> None:
    target_key = "bp-sc001:SRC0002:006:unit"
    shared_keys = [
        "bp-sc001:SRC0002:007:unit",
        "bp-sc001:SRC0002:010:unit",
    ]

    def finding(unit_key: str, reviewer_no: int):
        return ScreenplaySceneShardSemanticFinding(
            unit_key=unit_key,
            related_unit_keys=[],
            code="source_semantic_drift",
            violation_kinds=[
                "environment_personification",
                "unsupported_action",
            ],
            message=f"reviewer {reviewer_no} local source observation",
        )

    reviews = [
        ScreenplaySceneShardSemanticReview(findings=[
            finding(blank_unit_key, 1),
            *(finding(unit_key, 1) for unit_key in shared_keys),
        ]),
        ScreenplaySceneShardSemanticReview(findings=[
            finding(target_key, 2),
            *(finding(unit_key, 2) for unit_key in shared_keys),
        ]),
    ]

    errors = (
        scene_shards_module
        ._scene_shard_normalize_peer_review_unit_scopes(
            reviews,
            {target_key, *shared_keys},
        )
    )

    assert errors == []
    assert reviews[0].findings[0].unit_key == target_key
    assert {
        finding.unit_key
        for finding in scene_shards_module
        .screenplay_scene_semantic_consensus(*reviews)
    } == {target_key, *shared_keys}


def test_semantic_review_unresolved_local_blank_has_no_unilateral_authority(
) -> None:
    candidate_keys = {
        "bp-sc001:SRC0002:006:unit",
        "bp-sc001:SRC0002:007:unit",
    }

    def finding(unit_key: str):
        return ScreenplaySceneShardSemanticFinding(
            unit_key=unit_key,
            related_unit_keys=[],
            code="source_semantic_drift",
            violation_kinds=["unsupported_action"],
            message="local source observation",
        )

    reviews = [
        ScreenplaySceneShardSemanticReview(findings=[finding("")]),
        ScreenplaySceneShardSemanticReview(findings=[
            finding(unit_key) for unit_key in sorted(candidate_keys)
        ]),
    ]

    errors = (
        scene_shards_module
        ._scene_shard_normalize_peer_review_unit_scopes(
            reviews,
            candidate_keys,
        )
    )

    assert errors == []
    assert reviews[0].findings == []
    assert {
        finding.unit_key for finding in reviews[1].findings
    } == candidate_keys
    assert scene_shards_module.screenplay_scene_semantic_consensus(
        *reviews
    ) == []


@pytest.mark.parametrize(
    ("code", "violation_kinds"),
    [
        ("state_subject_semantic_drift", ["wrong_subject"]),
        ("source_semantic_drift", ["wrong_subject"]),
        ("source_semantic_drift", ["source_contradiction"]),
        (
            "source_semantic_drift",
            ["unsupported_action", "wrong_subject"],
        ),
    ],
)
def test_semantic_review_deterministic_issue_requires_unit_scope(
    code: str,
    violation_kinds: list[str],
) -> None:
    target_key = "bp-sc001:SRC0002:006:unit"
    reviews = [
        ScreenplaySceneShardSemanticReview(findings=[
            ScreenplaySceneShardSemanticFinding(
                unit_key=" \t ",
                related_unit_keys=[],
                code=code,
                violation_kinds=violation_kinds,
                message="deterministic issue missing target scope",
            ),
        ]),
        ScreenplaySceneShardSemanticReview(findings=[
            ScreenplaySceneShardSemanticFinding(
                unit_key=target_key,
                related_unit_keys=[],
                code=code,
                violation_kinds=violation_kinds,
                message="peer has a scoped observation",
            ),
        ]),
    ]

    errors = (
        scene_shards_module
        ._scene_shard_normalize_peer_review_unit_scopes(
            reviews,
            {target_key},
        )
    )

    assert any("缺少必需 unit_key scope" in error for error in errors)


def test_run_884443dc4404_semantic_budget_covers_worst_payload() -> None:
    case = json.loads(
        RUN_884443DC4404_REPLAY.read_text(encoding="utf-8")
    )
    assert case["semantic_review_version"] == (
        "screenplay-scene-semantic-review.v6"
    )
    budgets: list[int] = []
    payloads: dict[int, str] = {}

    for unit_count in case["budget_unit_counts"]:
        unit_keys = [
            f"{case['shard_id']}:unit:{index:03d}"
            for index in range(1, unit_count + 1)
        ]
        payload = (
            scene_shards_module
            .screenplay_scene_semantic_review_worst_case_payload(unit_keys)
        )
        review = ScreenplaySceneShardSemanticReview.model_validate_json(
            payload
        )
        required = (
            scene_shards_module
            .screenplay_scene_semantic_review_required_tokens(unit_keys)
        )

        assert len(review.findings) == 2 * unit_count
        assert len({
            (finding.unit_key, finding.code)
            for finding in review.findings
        }) == 2 * unit_count
        assert all(
            finding.violation_kinds
            == list(
                scene_shards_module
                .SCREENPLAY_SCENE_SEMANTIC_VIOLATION_KINDS
            )
            for finding in review.findings
        )
        assert all(
            len(finding.related_unit_keys) == 1
            and finding.related_unit_keys[0] != finding.unit_key
            for finding in review.findings
        )
        assert all(len(finding.message) == 160 for finding in review.findings)
        compact_required = math.ceil(len(payload) / 1.5 * 1.2)
        pretty_payload = json.dumps(
            json.loads(payload),
            ensure_ascii=False,
            indent=2,
        )
        assert required == max(
            2048,
            math.ceil(len(pretty_payload) / 1.5 * 1.2),
            compact_required * 2,
        )
        budgets.append(required)
        payloads[unit_count] = payload

    assert budgets[0] < budgets[1] < budgets[2]
    assert budgets[-1] > case["model_max_output_tokens"]
    partial = case["partial_review"]
    partial_payload = json.dumps({
        "findings": [
            {
                "unit_key": f"{case['shard_id']}:partial:{index:03d}",
                "code": "source_semantic_drift",
                "violation_kinds": ["source_contradiction"],
                "message": partial["message"],
            }
            for index in range(partial["finding_count"])
        ],
    }, ensure_ascii=False, separators=(",", ":"))
    partial_need = math.ceil(len(partial_payload) / 1.5 * 1.2)
    assert partial["unit_count"] == case["budget_unit_counts"][-1]
    assert budgets[-1] > partial_need
    assert len(payloads[partial["unit_count"]]) > len(partial_payload)


def test_attempt6_call_63118_semantic_budget_has_bounded_runaway_reserve(
) -> None:
    case = json.loads(
        ATTEMPT6_CALL_63118_SEMANTIC_BUDGET.read_text(encoding="utf-8")
    )
    unit_keys = case["unit_keys"]
    payload = (
        scene_shards_module
        .screenplay_scene_semantic_review_worst_case_payload(unit_keys)
    )
    compact_required = (
        scene_shards_module._screenplay_scene_semantic_token_estimate(
            len(payload)
        )
    )
    required = (
        scene_shards_module
        .screenplay_scene_semantic_review_required_tokens(unit_keys)
    )

    assert len(unit_keys) == 11
    assert len(case["scene_keys"]) == len(case["source_segment_ids"]) == 2
    assert case["prompt_chars"] == 10934
    assert compact_required == case["requested_max_tokens"] == 7641
    assert case["effective_max_tokens"] == case["completion_tokens"] == 7641
    assert case["finish_reason"] == "length"
    assert required == compact_required * 2 == 15282
    assert required < 32768


def test_semantic_review_reserve_does_not_inflate_one_or_two_unit_reviews(
) -> None:
    for unit_count in (1, 2):
        unit_keys = [
            f"bp-sc001:SRC0001:{index:03d}:unit"
            for index in range(1, unit_count + 1)
        ]
        assert all(
            scene_shards_module
            .screenplay_scene_semantic_review_required_tokens(
                unit_keys,
                output_reserve_percent=reserve_percent,
            )
            == scene_shards_module
            .SCREENPLAY_SCENE_SEMANTIC_REVIEW_MIN_OUTPUT_TOKENS
            for reserve_percent in (0, 100, 200)
        )


def test_semantic_review_reserve_setting_remains_provider_capped(
    monkeypatch,
) -> None:
    case = json.loads(
        ATTEMPT6_CALL_63118_SEMANTIC_BUDGET.read_text(encoding="utf-8")
    )
    monkeypatch.setattr(
        scene_shards_module.hiagent,
        "active_provider",
        lambda _kind: "budget-test-provider",
    )
    monkeypatch.setattr(
        scene_shards_module.hiagent,
        "active_model",
        lambda _kind, _provider=None: "budget-test-model",
    )
    monkeypatch.setattr(
        scene_shards_module.hiagent,
        "active_model_token_limits",
        lambda *_args: {
            "context_window_tokens": 128 * 1024,
            "max_output_tokens": 12000,
            "token_limits_source": "configured",
        },
    )

    configured_reserve = "0"
    monkeypatch.setattr(
        scene_shards_module,
        "get_setting",
        lambda key: (
            configured_reserve
            if key
            == "screenplay_scene_semantic_review_output_reserve_percent"
            else ""
        ),
    )
    no_reserve = scene_shards_module._scene_shard_semantic_review_budget(
        unit_keys=case["unit_keys"],
        review_prompt="production-sized semantic review",
    )

    configured_reserve = "100"
    default_reserve = (
        scene_shards_module._scene_shard_semantic_review_budget(
            unit_keys=case["unit_keys"],
            review_prompt="production-sized semantic review",
        )
    )

    assert no_reserve["output_reserve_percent"] == 0
    assert no_reserve["required"] < default_reserve["required"]
    assert default_reserve["output_reserve_percent"] == 100
    assert default_reserve["required"] == 15282
    assert no_reserve["ceiling"] == default_reserve["ceiling"] == 12000
    assert no_reserve["required"] <= no_reserve["ceiling"]
    assert default_reserve["required"] > default_reserve["ceiling"]


def test_scene_shard_semantic_review_rejects_duplicate_finding_keys() -> None:
    finding = {
        "unit_key": "unit-1",
        "code": "source_semantic_drift",
        "violation_kinds": ["source_contradiction"],
        "message": "first",
    }

    with pytest.raises(ValidationError, match="必须唯一"):
        ScreenplaySceneShardSemanticReview.model_validate({
            "findings": [
                finding,
                {**finding, "message": "duplicate key"},
            ],
        })


def test_run_b0659b64b548_multi_kind_consensus_is_canonical() -> None:
    case = json.loads(
        RUN_B0659B64B548_REPLAY.read_text(encoding="utf-8")
    )
    reviewer1 = ScreenplaySceneShardSemanticReview.model_validate(
        case["reviewer1"]
    )
    reviewer2 = ScreenplaySceneShardSemanticReview.model_validate(
        case["reviewer2"]
    )

    consensus = scene_shards_module.screenplay_scene_semantic_consensus(
        reviewer1,
        reviewer2,
    )
    expected_consensus = [
        {**finding, "related_unit_keys": []}
        for finding in case["expected_consensus"]
    ]

    assert case["run_id"] == "run_b0659b64b548"
    assert case["shard_id"] == "SS001"
    assert case["semantic_review_version"] == (
        "screenplay-scene-semantic-review.v6"
    )
    assert case["reviewer1"]["findings"][0]["violation_kinds"] == [
        "source_contradiction",
        "unsupported_action",
    ]
    assert all(
        kind in case["reviewer1"]["findings"][0]["message"]
        for kind in ("source_contradiction", "unsupported_action")
    )
    assert case["reviewer2"]["findings"][0]["violation_kinds"] == [
        "source_contradiction",
    ]
    assert (
        "unsupported_action"
        not in case["reviewer2"]["findings"][0]["message"]
    )
    assert [
        finding.model_dump(mode="json") for finding in consensus
    ] == expected_consensus
    assert all(
        finding.unit_key != case["empty_intersection_unit_key"]
        for finding in consensus
    )
    assert "source_contradiction" in consensus[0].message
    assert "unsupported_action" not in consensus[0].message
    assert consensus[0].message not in {
        reviewer1.findings[0].message,
        reviewer2.findings[0].message,
    }


def test_run_b0659b64b548_runtime_uses_multi_kind_consensus(
    monkeypatch,
) -> None:
    case = json.loads(
        RUN_B0659B64B548_REPLAY.read_text(encoding="utf-8")
    )
    blueprint = _blueprint(split_domain=False)
    plan = build_screenplay_scene_shard_plans(
        blueprint,
        source_text=SOURCE,
        identity_registry_hash="identity-hash",
    )[0]
    contracts = _contracts([plan], blueprint)[plan.shard_id]
    fixture_unit_keys = [
        finding["unit_key"]
        for finding in case["reviewer1"]["findings"]
    ]
    contract_slots = [
        (contract, slot)
        for contract in contracts
        for slot in contract.unit_slots
    ]
    contracts = [
        contract.model_copy(update={
            "unit_slots": [
                slot.model_copy(update={"unit_key": unit_key}),
            ],
        })
        for (contract, slot), unit_key in zip(
            contract_slots[:len(fixture_unit_keys)],
            fixture_unit_keys,
            strict=True,
        )
    ]
    draft = ScreenplaySceneShardCreativeIR(slots={
        finding["unit_key"]: ScreenplaySceneShardCreativeUnit(
            text=finding["unit_key"],
        )
        for finding in case["reviewer1"]["findings"]
    })
    expected_consensus = [
        {**finding, "related_unit_keys": []}
        for finding in case["expected_consensus"]
    ]

    async def fake_structured(*_args, **kwargs):
        if kwargs["model_type"] is ScreenplaySceneShardSemanticReview:
            if kwargs["call_meta"]["substage"] == "post_repair":
                return ScreenplaySceneShardSemanticReview(findings=[])
            return ScreenplaySceneShardSemanticReview.model_validate(
                case[f"reviewer{kwargs['call_meta']['reviewer_no']}"]
            )
        repair_context = json.loads(kwargs["repair_context"])
        assert repair_context["consensus_findings"] == (
            expected_consensus
        )
        consensus_message = repair_context["consensus_findings"][0][
            "message"
        ]
        assert "source_contradiction" in consensus_message
        assert "unsupported_action" not in consensus_message
        assert {
            finding["unit_key"]
            for finding in repair_context["consensus_findings"]
        } == {"SS001:SRC0004:unit:071"}
        flagged_key = repair_context["consensus_findings"][0]["unit_key"]
        return ScreenplaySceneShardCreativeIR(slots={
            flagged_key: draft.slots[flagged_key].model_copy(deep=True),
        })

    monkeypatch.setattr(
        "app.screenplay_scene_shards.model_gateway.chat_structured",
        fake_structured,
    )
    repaired, audit = asyncio.run(_REAL_SEMANTIC_REVIEW(
        draft=draft,
        scene_input_contracts=contracts,
        identity_registry=[],
        operation_id="run-b0659b64b548-runtime",
        shard_id=case["shard_id"],
        validate_draft=lambda _candidate: [],
    ))

    assert repaired == draft
    assert audit[0]["consensus"] == expected_consensus
    assert audit[1]["consensus"] == []


def _err_20260816_68234d_case() -> tuple[
    ScreenplaySceneShardCreativeIR,
    list[ScreenplaySceneInputContract],
    ScreenplaySceneShardSemanticFinding,
]:
    blueprint = _blueprint(split_domain=False)
    plan = build_screenplay_scene_shard_plans(
        blueprint,
        source_text=SOURCE,
        identity_registry_hash="identity-hash",
    )[0]
    base_contract = _contracts([plan], blueprint)[plan.shard_id][0]
    base_slot = base_contract.unit_slots[0]
    source_fact_text = "第1章 陨落的天才"
    source_text = f"{source_fact_text}\n{source_fact_text}"
    source_facts = source_segment_facts("SRC0001", source_text)
    assert [fact.text for fact in source_facts] == [
        source_fact_text,
        source_fact_text,
    ]
    unit_keys = [
        f"bp-sc001:SRC0001:{index:03d}:unit"
        for index in range(1, 3)
    ]
    slots = [
        base_slot.model_copy(update={
            "unit_key": unit_key,
            "source_unit_key": source_fact.source_unit_key,
            "source_segment_ids": ["SRC0001"],
            "source_text": "",
        })
        for unit_key, source_fact in zip(
            unit_keys,
            source_facts,
            strict=True,
        )
    ]
    contract = base_contract.model_copy(update={
        "source_segments": [
            ScreenplaySceneSourceSegment(
                source_segment_id="SRC0001",
                text=source_text,
            )
        ],
        "unit_slots": slots,
    })
    draft = ScreenplaySceneShardCreativeIR(slots={
        unit_key: ScreenplaySceneShardCreativeUnit(
            text=(
                "“第1章\n陨落的天才”"
                if index == 2
                else source_fact_text
            ),
            on_screen_text=source_fact_text,
        )
        for index, unit_key in enumerate(unit_keys, start=1)
    })
    finding = ScreenplaySceneShardSemanticFinding(
        unit_key=unit_keys[1],
        related_unit_keys=[unit_keys[0]],
        code="source_semantic_drift",
        violation_kinds=["cross_slot_duplication"],
        message="reviewer 错误认为 unit002 与 unit001 标题重复",
    )
    return draft, [contract], finding


def test_err_20260816_68234d_exact_own_source_duplication_is_filtered(
) -> None:
    draft, contracts, finding = _err_20260816_68234d_case()
    review = ScreenplaySceneShardSemanticReview(findings=[finding])

    filtered = (
        scene_shards_module
        ._scene_shard_filter_exact_source_duplication(
            review,
            draft=draft,
            scene_input_contracts=contracts,
        )
    )

    assert filtered.findings == []


@pytest.mark.parametrize("field", ["performance", "resulting_state"])
def test_err_20260816_68234d_extra_creative_content_is_not_filtered(
    field: str,
) -> None:
    draft, contracts, finding = _err_20260816_68234d_case()
    setattr(
        draft.slots[finding.unit_key],
        field,
        "来源没有支撑的额外创作内容",
    )
    review = ScreenplaySceneShardSemanticReview(findings=[finding])

    filtered = (
        scene_shards_module
        ._scene_shard_filter_exact_source_duplication(
            review,
            draft=draft,
            scene_input_contracts=contracts,
        )
    )

    assert filtered == review


def test_err_20260816_68234d_mixed_kinds_only_remove_duplication(
) -> None:
    draft, contracts, finding = _err_20260816_68234d_case()
    finding = finding.model_copy(update={
        "violation_kinds": [
            "wrong_subject",
            "unsupported_action",
            "source_contradiction",
            "cross_slot_duplication",
            "environment_personification",
        ],
    })
    review = ScreenplaySceneShardSemanticReview(findings=[finding])

    filtered = (
        scene_shards_module
        ._scene_shard_filter_exact_source_duplication(
            review,
            draft=draft,
            scene_input_contracts=contracts,
        )
    )

    assert filtered.findings[0].violation_kinds == [
        "wrong_subject",
        "unsupported_action",
        "source_contradiction",
        "environment_personification",
    ]


def test_err_20260816_68234d_dual_filtered_review_has_clean_consensus(
    monkeypatch,
) -> None:
    draft, contracts, finding = _err_20260816_68234d_case()
    reviewer_calls = 0

    async def fake_structured(*_args, **kwargs):
        nonlocal reviewer_calls
        assert kwargs["model_type"] is ScreenplaySceneShardSemanticReview
        reviewer_calls += 1
        return ScreenplaySceneShardSemanticReview(findings=[finding])

    monkeypatch.setattr(
        "app.screenplay_scene_shards.model_gateway.chat_structured",
        fake_structured,
    )
    result, audit = asyncio.run(_REAL_SEMANTIC_REVIEW(
        draft=draft,
        scene_input_contracts=contracts,
        identity_registry=[],
        operation_id="err-20260816-68234d",
        shard_id="SS001",
        validate_draft=lambda _candidate: [],
    ))

    assert result == draft
    assert reviewer_calls == 2
    assert audit[0]["reviews"] == [
        {"findings": []},
        {"findings": []},
    ]
    assert audit[0]["consensus"] == []


def _err_20260820_d05c0a_case() -> tuple[
    ScreenplaySceneShardCreativeIR,
    list[ScreenplaySceneInputContract],
    list[ScreenplaySceneShardSemanticFinding],
]:
    """SS008 SRC0051:004/005 own distinct comma-split source units.

    Two reviewers keep cross-marking the two eye-description clauses as a
    cross_slot_duplication because they read semantically similar, even though
    ``source_segment_facts`` splits them into independent source units with
    different keys and different frozen text. Each creative expands only its
    own source, so no slot borrows the other's frozen source.
    """
    blueprint = _blueprint(split_domain=False)
    plan = build_screenplay_scene_shard_plans(
        blueprint,
        source_text=SOURCE,
        identity_registry_hash="identity-hash",
    )[0]
    base_contract = _contracts([plan], blueprint)[plan.shard_id][0]
    base_slot = base_contract.unit_slots[0]
    source_text = (
        "在这女子的前方，有两个穿着绿色长袍的男子，年纪看起来都是二十许岁，"
        "但都是双眼凹陷，瞳孔绿油油的，让人望之生畏。"
    )
    source_facts = source_segment_facts("SRC0051", source_text)
    facts_by_unit = {fact.unit_order: fact for fact in source_facts}
    assert facts_by_unit[4].text == "但都是双眼凹陷，"
    assert facts_by_unit[5].text == "瞳孔绿油油的，"
    unit_keys = ["bp-sc033:SRC0051:004:unit", "bp-sc033:SRC0051:005:unit"]
    slots = [
        base_slot.model_copy(update={
            "unit_key": unit_key,
            "source_unit_key": facts_by_unit[unit_order].source_unit_key,
            "source_segment_ids": ["SRC0051"],
            "source_text": "",
        })
        for unit_key, unit_order in zip(unit_keys, (4, 5), strict=True)
    ]
    contract = base_contract.model_copy(update={
        "source_segments": [
            ScreenplaySceneSourceSegment(
                source_segment_id="SRC0051",
                text=source_text,
            )
        ],
        "unit_slots": slots,
    })
    draft = ScreenplaySceneShardCreativeIR(slots={
        unit_keys[0]: ScreenplaySceneShardCreativeUnit(
            text="两名绿袍男子的双眼深深凹陷进眼窝之中。",
        ),
        unit_keys[1]: ScreenplaySceneShardCreativeUnit(
            text="他们的瞳孔泛着诡异的绿油油光泽。",
        ),
    })
    findings = [
        ScreenplaySceneShardSemanticFinding(
            unit_key=unit_keys[1],
            related_unit_keys=[unit_keys[0]],
            code="source_semantic_drift",
            violation_kinds=["cross_slot_duplication"],
            message=message,
        )
        for message in (
            "reviewer1 认为 005 与 004 描述同一双眼睛属于重复",
            "reviewer2 认为 005 与 004 描述同一双眼睛属于重复",
        )
    ]
    return draft, [contract], findings


def test_err_20260820_d05c0a_distinct_source_ownership_not_duplication(
) -> None:
    draft, contracts, findings = _err_20260820_d05c0a_case()
    review = ScreenplaySceneShardSemanticReview(findings=[findings[0]])

    filtered = (
        scene_shards_module
        ._scene_shard_filter_distinct_source_ownership_duplication(
            review,
            draft=draft,
            scene_input_contracts=contracts,
        )
    )

    assert filtered.findings == []


def test_err_20260820_d05c0a_distinct_ownership_keeps_other_kinds(
) -> None:
    draft, contracts, findings = _err_20260820_d05c0a_case()
    finding = findings[0].model_copy(update={
        "violation_kinds": [
            "unsupported_action",
            "cross_slot_duplication",
        ],
    })
    review = ScreenplaySceneShardSemanticReview(findings=[finding])

    filtered = (
        scene_shards_module
        ._scene_shard_filter_distinct_source_ownership_duplication(
            review,
            draft=draft,
            scene_input_contracts=contracts,
        )
    )

    assert len(filtered.findings) == 1
    assert filtered.findings[0].violation_kinds == ["unsupported_action"]
    assert filtered.findings[0].related_unit_keys == []


def test_err_20260820_d05c0a_borrowed_source_stays_flagged() -> None:
    draft, contracts, findings = _err_20260820_d05c0a_case()
    borrowing_slot = draft.slots[findings[0].related_unit_keys[0]]
    borrowing_slot.text = "瞳孔绿油油的，"
    review = ScreenplaySceneShardSemanticReview(findings=[findings[0]])

    filtered = (
        scene_shards_module
        ._scene_shard_filter_distinct_source_ownership_duplication(
            review,
            draft=draft,
            scene_input_contracts=contracts,
        )
    )

    assert filtered == review


def test_err_20260820_d05c0a_dual_review_gate_closes(
    monkeypatch,
) -> None:
    draft, contracts, findings = _err_20260820_d05c0a_case()
    reviewer_calls = 0

    async def fake_structured(*_args, **kwargs):
        nonlocal reviewer_calls
        assert kwargs["model_type"] is ScreenplaySceneShardSemanticReview
        return ScreenplaySceneShardSemanticReview(findings=[
            findings[kwargs["call_meta"]["reviewer_no"] - 1],
        ])

    monkeypatch.setattr(
        "app.screenplay_scene_shards.model_gateway.chat_structured",
        fake_structured,
    )
    result, audit = asyncio.run(_REAL_SEMANTIC_REVIEW(
        draft=draft,
        scene_input_contracts=contracts,
        identity_registry=[],
        operation_id="err-20260820-d05c0a",
        shard_id="SS008",
        validate_draft=lambda _candidate: [],
    ))

    assert result == draft
    assert audit[0]["reviews"] == [
        {"findings": []},
        {"findings": []},
    ]
    assert audit[0]["consensus"] == []


def _err_20260816_77848a_case() -> tuple[
    ScreenplaySceneShardCreativeIR,
    list[ScreenplaySceneInputContract],
    list[ScreenplaySceneShardSemanticFinding],
]:
    blueprint = _blueprint(split_domain=False)
    plan = build_screenplay_scene_shard_plans(
        blueprint,
        source_text=SOURCE,
        identity_registry_hash="identity-hash",
    )[0]
    base_contract = _contracts([plan], blueprint)[plan.shard_id][0]
    base_slot = base_contract.unit_slots[0]
    source_rows = [
        ("unit43", "SRC0043", "停步。"),
        ("unit44", "SRC0044", "弯腰。"),
    ]
    slots = [
        base_slot.model_copy(update={
            "unit_key": unit_key,
            "source_unit_key": source_segment_facts(
                source_id,
                source_text,
            )[0].source_unit_key,
            "source_segment_ids": [source_id],
            "source_text": "",
        })
        for unit_key, source_id, source_text in source_rows
    ]
    contract = base_contract.model_copy(update={
        "source_segments": [
            ScreenplaySceneSourceSegment(
                source_segment_id=source_id,
                text=source_text,
            )
            for _unit_key, source_id, source_text in source_rows
        ],
        "unit_slots": slots,
    })
    draft = ScreenplaySceneShardCreativeIR(slots={
        "unit43": ScreenplaySceneShardCreativeUnit(text="弯腰。"),
        "unit44": ScreenplaySceneShardCreativeUnit(text="弯腰。"),
    })
    findings = [
        ScreenplaySceneShardSemanticFinding(
            unit_key="unit44",
            related_unit_keys=["unit43"],
            code="source_semantic_drift",
            violation_kinds=["cross_slot_duplication"],
            message=message,
        )
        for message in (
            "reviewer1 错把 unit44 归为与 unit43 重复",
            "reviewer2 错把 unit44 归为与 unit43 重复",
        )
    ]
    return draft, [contract], findings


def test_err_20260816_77848a_source_aware_canonicalization_swaps_target(
) -> None:
    draft, contracts, findings = _err_20260816_77848a_case()

    canonical_reviews = [
        scene_shards_module._scene_shard_filter_exact_source_duplication(
            scene_shards_module
            ._scene_shard_canonicalize_cross_slot_findings(
                ScreenplaySceneShardSemanticReview(findings=[finding]),
                draft=draft,
                scene_input_contracts=contracts,
            ),
            draft=draft,
            scene_input_contracts=contracts,
        )
        for finding in findings
    ]
    consensus = scene_shards_module.screenplay_scene_semantic_consensus(
        *canonical_reviews
    )

    assert [
        review.findings[0].unit_key
        for review in canonical_reviews
    ] == ["unit43", "unit43"]
    assert [
        review.findings[0].related_unit_keys
        for review in canonical_reviews
    ] == [["unit44"], ["unit44"]]
    assert [
        review.findings[0].message
        for review in canonical_reviews
    ] == [finding.message for finding in findings]
    assert len(consensus) == 1
    assert consensus[0].unit_key == "unit43"
    assert consensus[0].related_unit_keys == ["unit44"]


def test_cross_slot_retarget_merges_existing_same_key_finding() -> None:
    draft, contracts, findings = _err_20260816_77848a_case()
    existing = ScreenplaySceneShardSemanticFinding(
        unit_key="unit43",
        related_unit_keys=[],
        code="source_semantic_drift",
        violation_kinds=["unsupported_action"],
        message="unit43 还包含无来源动作",
    )

    canonical = (
        scene_shards_module
        ._scene_shard_canonicalize_cross_slot_findings(
            ScreenplaySceneShardSemanticReview(
                findings=[existing, findings[0]],
            ),
            draft=draft,
            scene_input_contracts=contracts,
        )
    )

    assert len(canonical.findings) == 1
    assert canonical.findings[0].unit_key == "unit43"
    assert canonical.findings[0].related_unit_keys == ["unit44"]
    assert canonical.findings[0].violation_kinds == [
        "unsupported_action",
        "cross_slot_duplication",
    ]
    assert "无来源动作" in canonical.findings[0].message
    assert "归为与 unit43 重复" in canonical.findings[0].message


def test_err_20260816_77848a_runtime_repairs_only_canonical_target(
    monkeypatch,
) -> None:
    draft, contracts, findings = _err_20260816_77848a_case()
    repair_calls = 0

    async def fake_structured(messages, **kwargs):
        nonlocal repair_calls
        if kwargs["model_type"] is ScreenplaySceneShardSemanticReview:
            if kwargs["call_meta"]["substage"] == "post_repair":
                return ScreenplaySceneShardSemanticReview(findings=[])
            return ScreenplaySceneShardSemanticReview(findings=[
                findings[kwargs["call_meta"]["reviewer_no"] - 1],
            ])

        repair_calls += 1
        slots_schema = kwargs["output_schema"]["properties"]["slots"]
        assert slots_schema["required"] == ["unit43"]
        assert set(slots_schema["properties"]) == {"unit43"}
        repair_context = json.loads(kwargs["repair_context"])
        assert repair_context["consensus_findings"][0][
            "unit_key"
        ] == "unit43"
        assert repair_context["consensus_findings"][0][
            "related_unit_keys"
        ] == ["unit44"]
        reviewer_findings = repair_context["reviewer_findings"]
        assert [finding["message"] for finding in reviewer_findings] == [
            finding.message for finding in findings
        ]
        assert all(
            finding["unit_key"] == "unit43"
            and finding["related_unit_keys"] == ["unit44"]
            for finding in reviewer_findings
        )
        assert all(
            finding.message in messages[1]["content"]
            for finding in findings
        )
        repaired_slot = draft.slots["unit43"].model_copy(deep=True)
        repaired_slot.text = "停步。"
        return ScreenplaySceneShardCreativeIR(
            slots={"unit43": repaired_slot},
        )

    monkeypatch.setattr(
        "app.screenplay_scene_shards.model_gateway.chat_structured",
        fake_structured,
    )
    repaired, audit = asyncio.run(_REAL_SEMANTIC_REVIEW(
        draft=draft,
        scene_input_contracts=contracts,
        identity_registry=[],
        operation_id="err-20260816-77848a",
        shard_id="SS001",
        validate_draft=lambda _candidate: [],
    ))

    assert repair_calls == 1
    assert repaired.slots["unit43"].text == "停步。"
    assert repaired.slots["unit44"] == draft.slots["unit44"]
    assert audit[0]["consensus"][0]["unit_key"] == "unit43"
    assert audit[0]["consensus"][0]["related_unit_keys"] == ["unit44"]


def test_err_20260816_77848a_post_retarget_stays_in_repair_scope(
    monkeypatch,
) -> None:
    draft, contracts, cross_findings = _err_20260816_77848a_case()
    initial_findings = [
        ScreenplaySceneShardSemanticFinding(
            unit_key="unit44",
            code="state_subject_semantic_drift",
            violation_kinds=["wrong_subject"],
            message=f"reviewer{reviewer_no} 首轮标记 unit44",
        )
        for reviewer_no in (1, 2)
    ]
    repair_schemas: list[list[str]] = []

    async def fake_structured(*_args, **kwargs):
        if kwargs["model_type"] is ScreenplaySceneShardSemanticReview:
            reviewer_index = kwargs["call_meta"]["reviewer_no"] - 1
            if not repair_schemas:
                finding = initial_findings[reviewer_index]
            elif len(repair_schemas) == 1:
                finding = cross_findings[reviewer_index]
            else:
                finding = None
            return ScreenplaySceneShardSemanticReview(
                findings=[] if finding is None else [finding],
            )

        required_keys = kwargs["output_schema"]["properties"]["slots"][
            "required"
        ]
        repair_schemas.append(required_keys)
        unit_key = required_keys[0]
        return ScreenplaySceneShardCreativeIR(slots={
            unit_key: draft.slots[unit_key].model_copy(deep=True),
        })

    monkeypatch.setattr(
        "app.screenplay_scene_shards.model_gateway.chat_structured",
        fake_structured,
    )
    repaired, audit = asyncio.run(_REAL_SEMANTIC_REVIEW(
        draft=draft,
        scene_input_contracts=contracts,
        identity_registry=[],
        operation_id="err-20260816-77848a-post-retarget",
        shard_id="SS001",
        validate_draft=lambda _candidate: [],
    ))

    assert repaired == draft
    assert repair_schemas == [["unit44"], ["unit43"]]
    assert [
        [finding["unit_key"] for finding in phase["consensus"]]
        for phase in audit
    ] == [["unit44"], ["unit43"], []]
    assert audit[1]["consensus"][0]["related_unit_keys"] == ["unit44"]


def test_err_20260816_77848a_different_related_keys_do_not_cross_consensus(
) -> None:
    reviews = [
        ScreenplaySceneShardSemanticReview(findings=[
            ScreenplaySceneShardSemanticFinding(
                unit_key="unit12",
                related_unit_keys=[related_unit_key],
                code="source_semantic_drift",
                violation_kinds=["cross_slot_duplication"],
                message=f"unit12 duplicates {related_unit_key}",
            )
        ])
        for related_unit_key in ("unit13", "unit14")
    ]

    assert scene_shards_module.screenplay_scene_semantic_consensus(
        *reviews
    ) == []


def test_err_20260816_77848a_unknown_related_key_is_hard_failure(
    monkeypatch,
) -> None:
    draft, contracts, findings = _err_20260816_77848a_case()
    unknown_finding = findings[0].model_copy(update={
        "related_unit_keys": ["UNKNOWN-RELATED"],
    })

    async def fake_structured(*_args, **_kwargs):
        return ScreenplaySceneShardSemanticReview(
            findings=[unknown_finding],
        )

    monkeypatch.setattr(
        "app.screenplay_scene_shards.model_gateway.chat_structured",
        fake_structured,
    )
    with pytest.raises(
        ScreenplaySceneShardError,
        match="未知 related_unit_key：UNKNOWN-RELATED",
    ):
        asyncio.run(_REAL_SEMANTIC_REVIEW(
            draft=draft,
            scene_input_contracts=contracts,
            identity_registry=[],
            operation_id="err-20260816-77848a-unknown-related",
            shard_id="SS001",
            validate_draft=lambda _candidate: [],
        ))


def test_err_20260816_77848a_unit12_raw_messages_reach_repair(
    monkeypatch,
) -> None:
    blueprint = _blueprint(split_domain=False)
    plan = build_screenplay_scene_shard_plans(
        blueprint,
        source_text=SOURCE,
        identity_registry_hash="identity-hash",
    )[0]
    base_contract = _contracts([plan], blueprint)[plan.shard_id][0]
    contract = base_contract.model_copy(update={
        "unit_slots": [
            base_contract.unit_slots[0].model_copy(
                update={"unit_key": "unit12"},
            ),
        ],
    })
    draft = ScreenplaySceneShardCreativeIR(slots={
        "unit12": ScreenplaySceneShardCreativeUnit(text="错误主体动作"),
    })
    messages = [
        "unit12 reviewer1：来源主体是甲，creative 写成乙",
        "unit12 reviewer2：状态归属被改给了乙",
    ]
    findings = [
        ScreenplaySceneShardSemanticFinding(
            unit_key="unit12",
            code="state_subject_semantic_drift",
            violation_kinds=["wrong_subject"],
            message=message,
        )
        for message in messages
    ]

    async def fake_structured(call_messages, **kwargs):
        if kwargs["model_type"] is ScreenplaySceneShardSemanticReview:
            if kwargs["call_meta"]["substage"] == "post_repair":
                return ScreenplaySceneShardSemanticReview(findings=[])
            return ScreenplaySceneShardSemanticReview(findings=[
                findings[kwargs["call_meta"]["reviewer_no"] - 1],
            ])
        repair_context = json.loads(kwargs["repair_context"])
        assert [
            finding["message"]
            for finding in repair_context["reviewer_findings"]
        ] == messages
        assert all(
            message in call_messages[1]["content"]
            for message in messages
        )
        return ScreenplaySceneShardCreativeIR(slots={
            "unit12": draft.slots["unit12"].model_copy(deep=True),
        })

    monkeypatch.setattr(
        "app.screenplay_scene_shards.model_gateway.chat_structured",
        fake_structured,
    )
    repaired, _audit = asyncio.run(_REAL_SEMANTIC_REVIEW(
        draft=draft,
        scene_input_contracts=[contract],
        identity_registry=[],
        operation_id="err-20260816-77848a-unit12-messages",
        shard_id="SS001",
        validate_draft=lambda _candidate: [],
    ))

    assert repaired == draft


def test_scene_shard_semantic_repair_budget_reaches_13121_token_root() -> None:
    seed = ScreenplaySceneShardCreativeIR(slots={
        "SS003:unit:001": ScreenplaySceneShardCreativeUnit(text="根"),
    })
    target_serialized_chars = 13120 * 3 // 2 + 1
    fixed_chars = len(seed.model_dump_json()) - 1
    draft = ScreenplaySceneShardCreativeIR(slots={
        "SS003:unit:001": ScreenplaySceneShardCreativeUnit(
            text="根" * (target_serialized_chars - fixed_chars),
        ),
    })
    draft_json = draft.model_dump_json()
    root_tokens = math.ceil(len(draft_json) / 1.5)

    required = (
        scene_shards_module
        .screenplay_scene_semantic_repair_required_tokens(
            draft_json=draft_json,
            repair_prompt="",
        )
    )

    assert len(draft_json) == target_serialized_chars
    assert root_tokens == 13121
    assert required == math.ceil(root_tokens * 1.2)
    assert required > 12288


def test_scene_shard_semantic_repair_allows_exact_ceiling(
    monkeypatch,
) -> None:
    blueprint = _blueprint(split_domain=False)
    plan = build_screenplay_scene_shard_plans(
        blueprint,
        source_text=SOURCE,
        identity_registry_hash="identity-hash",
    )[0]
    contracts = _contracts([plan], blueprint)[plan.shard_id]
    draft = _creative_shard(plan, blueprint, contracts)
    flagged_key = next(iter(draft.slots))
    finding = ScreenplaySceneShardSemanticFinding(
        unit_key=flagged_key,
        code="source_semantic_drift",
        violation_kinds=["source_contradiction"],
        message="来源主体漂移",
    )
    review = ScreenplaySceneShardSemanticReview(findings=[finding])
    finding = scene_shards_module.screenplay_scene_semantic_consensus(
        review,
        review,
    )[0]
    frozen_slots, _identity_labels = (
        scene_shards_module._scene_shard_semantic_authority_payload(
            scene_input_contracts=contracts,
            identity_registry=[],
        )
    )
    subset_draft = ScreenplaySceneShardCreativeIR(slots={
        flagged_key: draft.slots[flagged_key].model_copy(deep=True),
    })
    subset_draft_json = subset_draft.model_dump_json()
    subset_schema = (
        scene_shards_module._scene_shard_semantic_repair_subset_schema(
            [flagged_key],
        )
    )
    repair_prompt = scene_shards_module._scene_shard_semantic_repair_prompt(
        findings_payload=[finding.model_dump(mode="json")],
        reviewer_findings_payload=[
            reviewer_finding.model_dump(mode="json")
            for reviewer_review in (review, review)
            for reviewer_finding in reviewer_review.findings
        ],
        frozen_slots={flagged_key: frozen_slots[flagged_key]},
        draft_json=subset_draft_json,
        creative_schema=subset_schema,
    )
    generous_limits = {
        "context_window_tokens": 256 * 1024,
        "max_output_tokens": 64 * 1024,
        "token_limits_source": "configured",
    }
    monkeypatch.setattr(
        scene_shards_module.hiagent,
        "active_provider",
        lambda _kind: "budget-test-provider",
    )
    monkeypatch.setattr(
        scene_shards_module.hiagent,
        "active_model",
        lambda _kind, _provider=None: "budget-test-model",
    )
    monkeypatch.setattr(
        scene_shards_module.hiagent,
        "active_model_token_limits",
        lambda *_args: generous_limits,
    )
    preview = scene_shards_module._scene_shard_semantic_repair_budget(
        draft_json=subset_draft_json,
        repair_prompt=repair_prompt,
        unit_count=1,
    )
    critical_limits = {
        "context_window_tokens": (
            int(preview["input"])
            + scene_shards_module
            .SCREENPLAY_SCENE_SEMANTIC_REVIEW_CONTEXT_RESERVE_TOKENS
            + int(preview["required"])
        ),
        "max_output_tokens": int(preview["required"]),
        "token_limits_source": "configured",
    }
    budget_calls = 0

    def staged_limits(*_args):
        nonlocal budget_calls
        budget_calls += 1
        return critical_limits if budget_calls == 2 else generous_limits

    monkeypatch.setattr(
        scene_shards_module.hiagent,
        "active_model_token_limits",
        staged_limits,
    )
    repair_calls: list[dict] = []

    async def fake_structured(*_args, **kwargs):
        if kwargs["model_type"] is ScreenplaySceneShardSemanticReview:
            return (
                review
                if kwargs["call_meta"]["substage"] == "initial"
                else ScreenplaySceneShardSemanticReview(findings=[])
            )
        repair_calls.append(kwargs)
        return subset_draft

    monkeypatch.setattr(
        "app.screenplay_scene_shards.model_gateway.chat_structured",
        fake_structured,
    )
    repaired, _audit = asyncio.run(_REAL_SEMANTIC_REVIEW(
        draft=draft,
        scene_input_contracts=contracts,
        identity_registry=[],
        operation_id="scene-semantic-repair-critical-ceiling",
        shard_id=plan.shard_id,
        validate_draft=lambda _candidate: [],
    ))

    assert repaired == draft
    assert len(repair_calls) == 1
    repair_call = repair_calls[0]
    meta = repair_call["call_meta"]
    assert meta["required"] == meta["ceiling"] == preview["required"]
    assert meta["input"] == preview["input"]
    assert meta["unit_count"] == 1
    assert repair_call["max_tokens"] == meta["required"]


def test_production_ss003_79_slot_semantic_repair_uses_actual_root_budget(
    monkeypatch,
) -> None:
    slots = {
        f"SS003:unit:{index:03d}": ScreenplaySceneShardCreativeUnit(
            text=(
                f"第{index}个来源动作按既定主体、空间和先后关系完整展开，"
                + "不省略动作承接与可见结果。" * 5
            ),
            performance=(
                "表演保持身体重心、视线、手部动作和对手反应连续。" * 4
            ),
            resulting_state=(
                "本单元结束状态完整交给下一来源单元继续承接。" * 4
            ),
            function="交付来源事件并推进场内因果",
        )
        for index in range(1, 80)
    }
    draft = ScreenplaySceneShardCreativeIR(slots=slots)
    flagged_key = next(iter(slots))
    finding = ScreenplaySceneShardSemanticFinding(
        unit_key=flagged_key,
        code="source_semantic_drift",
        violation_kinds=["source_contradiction"],
        message="SS003 生产语义漂移",
    )
    draft_json = draft.model_dump_json()
    frozen_slots = {
        unit_key: {
            "kind": "action",
            "source_unit_key": f"SRC{index:04d}:unit:001",
            "source_text": (
                f"SS003 第 {index} 个来源单元的原始动作、主体与结果。"
            ),
            "source_fact": {
                "projection": "action",
                "actor_mentions": ["孟浩"],
                "action_claims": [f"完成第 {index} 个来源动作"],
            },
            "state_subject_key": "person_孟浩",
            "state_subject_keys": ["person_孟浩"],
            "environment_only": False,
            "actor_keys": ["person_孟浩"],
            "target_keys": [],
            "speaker_key": None,
            "onscreen_entity_keys": ["person_孟浩"],
        }
        for index, unit_key in enumerate(slots, start=1)
    }
    repair_prompt = scene_shards_module._scene_shard_semantic_repair_prompt(
        findings_payload=[finding.model_dump(mode="json")],
        reviewer_findings_payload=[],
        frozen_slots=frozen_slots,
        identity_labels={
            "person_孟浩": {
                "canonical_name": "孟浩",
                "source_labels": ["孟浩"],
                "authority_id": "bible:孟浩",
            },
        },
        draft_json=draft_json,
        creative_schema=ScreenplaySceneShardCreativeIR.model_json_schema(),
    )
    limits = {
        "context_window_tokens": 256 * 1024,
        "max_output_tokens": 64 * 1024,
        "token_limits_source": "configured",
    }
    monkeypatch.setattr(
        scene_shards_module.hiagent,
        "active_provider",
        lambda _kind: "production-provider",
    )
    monkeypatch.setattr(
        scene_shards_module.hiagent,
        "active_model",
        lambda _kind, _provider=None: "production-model",
    )
    monkeypatch.setattr(
        scene_shards_module.hiagent,
        "active_model_token_limits",
        lambda *_args: limits,
    )

    budget = scene_shards_module._scene_shard_semantic_repair_budget(
        draft_json=draft_json,
        repair_prompt=repair_prompt,
        unit_count=len(draft.slots),
    )
    root_tokens = math.ceil(len(draft_json) / 1.5)
    root_with_reserve = math.ceil(root_tokens * 1.2)

    assert budget["unit_count"] == 79
    assert budget["required"] == max(
        4096,
        root_with_reserve,
        len(repair_prompt) // 2,
    )
    assert budget["required"] > 12288
    assert budget["ceiling"] >= budget["required"]
    assert budget["input"] > 0


def test_scene_shard_semantic_repair_insufficient_ceiling_is_zero_call(
    monkeypatch,
) -> None:
    blueprint = _blueprint(split_domain=False)
    plan = build_screenplay_scene_shard_plans(
        blueprint,
        source_text=SOURCE,
        identity_registry_hash="identity-hash",
    )[0]
    contracts = _contracts([plan], blueprint)[plan.shard_id]
    draft = _creative_shard(plan, blueprint, contracts)
    flagged_key = next(iter(draft.slots))
    finding = ScreenplaySceneShardSemanticFinding(
        unit_key=flagged_key,
        code="source_semantic_drift",
        violation_kinds=["source_contradiction"],
        message="来源主体漂移",
    )
    limits_calls = 0

    monkeypatch.setattr(
        scene_shards_module.hiagent,
        "active_provider",
        lambda _kind: "budget-test-provider",
    )
    monkeypatch.setattr(
        scene_shards_module.hiagent,
        "active_model",
        lambda _kind, _provider=None: "budget-test-model",
    )

    def staged_limits(*_args):
        nonlocal limits_calls
        limits_calls += 1
        if limits_calls == 1:
            return {
                "context_window_tokens": 256 * 1024,
                "max_output_tokens": 64 * 1024,
                "token_limits_source": "configured",
            }
        return {
            "context_window_tokens": 1024,
            "max_output_tokens": 64 * 1024,
            "token_limits_source": "configured",
        }

    monkeypatch.setattr(
        scene_shards_module.hiagent,
        "active_model_token_limits",
        staged_limits,
    )
    review_calls = 0
    repair_calls = 0

    async def fake_structured(*_args, **kwargs):
        nonlocal review_calls, repair_calls
        if kwargs["model_type"] is ScreenplaySceneShardSemanticReview:
            review_calls += 1
            return ScreenplaySceneShardSemanticReview(findings=[finding])
        repair_calls += 1
        raise AssertionError("repair budget gate must run before provider")

    monkeypatch.setattr(
        "app.screenplay_scene_shards.model_gateway.chat_structured",
        fake_structured,
    )
    with pytest.raises(
        ScreenplaySceneShardError,
        match=r"语义 repair 输出预算不足.*required=\d+，ceiling=0",
    ):
        asyncio.run(_REAL_SEMANTIC_REVIEW(
            draft=draft,
            scene_input_contracts=contracts,
            identity_registry=[],
            operation_id="scene-semantic-repair-budget-block",
            shard_id=plan.shard_id,
            validate_draft=lambda _candidate: [],
        ))

    assert review_calls == 2
    assert repair_calls == 0


def test_scene_shard_semantic_consensus_repairs_only_flagged_creative_slot(
    monkeypatch,
) -> None:
    blueprint = _blueprint(split_domain=False)
    plan = build_screenplay_scene_shard_plans(
        blueprint,
        source_text=SOURCE,
        identity_registry_hash="identity-hash",
    )[0]
    contracts = _contracts([plan], blueprint)[plan.shard_id]
    draft = _creative_shard(plan, blueprint, contracts)
    unit_keys = list(draft.slots)
    flagged_key = unit_keys[0]
    original_unflagged = json.dumps(
        draft.slots[unit_keys[1]].model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    calls = 0
    structured_calls: list[tuple[list[dict[str, str]], dict]] = []
    validated_drafts: list[ScreenplaySceneShardCreativeIR] = []

    async def fake_structured(messages, **kwargs):
        nonlocal calls
        calls += 1
        structured_calls.append((messages, kwargs))
        assert [message["role"] for message in messages] == [
            "system", "user",
        ]
        assert messages[0]["content"] == (
            scene_shards_module.SCREENPLAY_SCENE_JSON_ONLY_SYSTEM_PROMPT
        )
        if kwargs["model_type"] is ScreenplaySceneShardSemanticReview:
            prompt = messages[1]["content"]
            dynamic_schema = kwargs["output_schema"]
            assert "findings=[]" not in prompt
            assert '{"findings":[]}' in prompt
            assert "不得输出 Markdown、解释或任何对象外文本" in prompt
            assert kwargs["require_response_format"] is True
            assert kwargs["response_format"] == (
                scene_shards_module
                ._scene_shard_semantic_review_response_format(
                    dynamic_schema
                )
            )
            assert (
                kwargs["response_format"]["json_schema"]["schema"]
                is not dynamic_schema
            )
            assert json.dumps(
                dynamic_schema,
                ensure_ascii=False,
                separators=(",", ":"),
            ) in prompt
            if calls <= 2:
                assert "person_甲" in prompt
                assert '"canonical_name":"甲"' in prompt
                assert '"text":"甲推门进入。"' in prompt
                assert '"projection":"action"' in prompt
                assert '"canonical_name":"丙"' not in prompt
                return ScreenplaySceneShardSemanticReview(findings=[
                    ScreenplaySceneShardSemanticFinding(
                        unit_key=flagged_key,
                        code="state_subject_semantic_drift",
                        violation_kinds=["wrong_subject"],
                        message="冻结主体为甲，creative 却写成乙的动作",
                    )
                ])
            return ScreenplaySceneShardSemanticReview(findings=[])
        prompt = messages[1]["content"]
        assert '"projection":"action"' in prompt
        assert '"text":"甲推门进入。"' in prompt
        assert unit_keys[1] not in prompt
        subset_schema = kwargs["output_schema"]
        slots_schema = subset_schema["properties"]["slots"]
        assert list(slots_schema["properties"]) == [flagged_key]
        assert slots_schema["required"] == [flagged_key]
        assert slots_schema["additionalProperties"] is False
        assert subset_schema["properties"]["contract_version"]["const"] == (
            SCREENPLAY_SCENE_CREATIVE_VERSION
        )
        assert json.dumps(
            subset_schema,
            ensure_ascii=False,
            separators=(",", ":"),
        ) in prompt
        repair_context = json.loads(kwargs["repair_context"])
        assert set(repair_context) == {
            "consensus_findings",
            "reviewer_findings",
            "frozen_slots",
            "current_flagged_creative",
        }
        assert repair_context["consensus_findings"][0]["unit_key"] == (
            flagged_key
        )
        assert list(repair_context["frozen_slots"]) == [flagged_key]
        assert repair_context["frozen_slots"][flagged_key][
            "source_fact"
        ] is not None
        assert list(
            repair_context["current_flagged_creative"]["slots"]
        ) == [flagged_key]
        subset_slot = draft.slots[flagged_key].model_copy(deep=True)
        subset_slot.text = "甲推门进入。"
        return ScreenplaySceneShardCreativeIR(
            slots={flagged_key: subset_slot},
        )

    def validate_draft(
        candidate: ScreenplaySceneShardCreativeIR,
    ) -> list[str]:
        validated_drafts.append(candidate)
        assert set(candidate.slots) == set(draft.slots)
        return []

    monkeypatch.setattr(
        "app.screenplay_scene_shards.model_gateway.chat_structured",
        fake_structured,
    )
    repaired, audit = asyncio.run(_REAL_SEMANTIC_REVIEW(
        draft=draft,
        scene_input_contracts=contracts,
        identity_registry=[
            {
                "identity_key": f"person_{label}",
                "authority_id": f"bible:{label}",
                "canonical_name": label,
                "source_labels": [label],
            }
            for label in ("甲", "乙")
        ] + [{
            "identity_key": "person_丙",
            "authority_id": "bible:丙",
            "canonical_name": "丙",
            "source_labels": ["丙"],
        }],
        operation_id="scene-semantic-fixture",
        shard_id=plan.shard_id,
        validate_draft=validate_draft,
    ))

    assert repaired.slots[flagged_key].text == "甲推门进入。"
    assert json.dumps(
        repaired.slots[unit_keys[1]].model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode() == original_unflagged
    assert len(validated_drafts) == 1
    assert len(audit) == 2
    assert audit[0]["creative_hash"] != ""
    assert audit[-1]["creative_hash"] == scene_shards_module._hash(
        repaired.model_dump(mode="json")
    )
    review_calls = [
        kwargs
        for _messages, kwargs in structured_calls
        if kwargs["model_type"] is ScreenplaySceneShardSemanticReview
    ]
    repair_calls = [
        kwargs
        for _messages, kwargs in structured_calls
        if kwargs["model_type"] is ScreenplaySceneShardCreativeIR
    ]
    assert len(review_calls) == 4
    assert len(repair_calls) == 1
    assert repair_calls[0]["call_meta"]["schema_hash"] in (
        repair_calls[0]["operation_id"]
    )
    assert all(
        SCREENPLAY_SCENE_SEMANTIC_REVIEW_VERSION
        in kwargs["operation_id"]
        for kwargs in review_calls + repair_calls
    )
    assert all(
        kwargs["call_meta"]["contract_version"]
        == SCREENPLAY_SCENE_SEMANTIC_REVIEW_VERSION
        for kwargs in review_calls + repair_calls
    )
    assert all(
        kwargs["call_meta"]["reuse_successful_operation"] is True
        for kwargs in review_calls
    )
    assert all(
        kwargs["max_tokens"] == kwargs["call_meta"]["required"]
        for kwargs in review_calls
    )
    assert all(
        kwargs["call_meta"]["unit_count"] == len(unit_keys)
        for kwargs in review_calls
    )
    assert all(
        kwargs["call_meta"]["required"] >= 2048
        and kwargs["call_meta"]["ceiling"]
        >= kwargs["call_meta"]["required"]
        for kwargs in review_calls
    )


def test_scene_shard_semantic_single_reviewer_finding_does_not_repair(
    monkeypatch,
) -> None:
    blueprint = _blueprint(split_domain=False)
    plan = build_screenplay_scene_shard_plans(
        blueprint, source_text=SOURCE, identity_registry_hash="identity-hash",
    )[0]
    contracts = _contracts([plan], blueprint)[plan.shard_id]
    draft = _creative_shard(plan, blueprint, contracts)
    flagged_key = next(iter(draft.slots))
    calls = 0

    async def fake_structured(*_args, **kwargs):
        nonlocal calls
        calls += 1
        assert kwargs["model_type"] is ScreenplaySceneShardSemanticReview
        return ScreenplaySceneShardSemanticReview(findings=(
            [ScreenplaySceneShardSemanticFinding(
                unit_key=flagged_key,
                code="source_semantic_drift",
                violation_kinds=["source_contradiction"],
                message="single reviewer only",
            )] if calls == 1 else []
        ))

    monkeypatch.setattr(
        "app.screenplay_scene_shards.model_gateway.chat_structured",
        fake_structured,
    )
    result, audit = asyncio.run(_REAL_SEMANTIC_REVIEW(
        draft=draft,
        scene_input_contracts=contracts,
        identity_registry=[],
        operation_id="scene-semantic-single",
        shard_id=plan.shard_id,
        validate_draft=lambda _candidate: [],
    ))
    assert result == draft
    assert calls == 2
    assert audit[0]["consensus"] == []


def test_scene_shard_semantic_peer_proven_blank_scope_continues_runtime(
    monkeypatch,
) -> None:
    blueprint = _blueprint(split_domain=False)
    plan = build_screenplay_scene_shard_plans(
        blueprint,
        source_text=SOURCE,
        identity_registry_hash="identity-hash",
    )[0]
    contracts = _contracts([plan], blueprint)[plan.shard_id]
    draft = _creative_shard(plan, blueprint, contracts)
    target_key = next(iter(draft.slots))
    review_calls: list[tuple[str, int]] = []
    repair_calls = 0

    async def fake_structured(*_args, **kwargs):
        nonlocal repair_calls
        if kwargs["model_type"] is ScreenplaySceneShardSemanticReview:
            meta = kwargs["call_meta"]
            review_calls.append((meta["substage"], meta["reviewer_no"]))
            if meta["substage"] == "post_repair":
                return ScreenplaySceneShardSemanticReview(findings=[])
            return ScreenplaySceneShardSemanticReview(findings=[
                ScreenplaySceneShardSemanticFinding(
                    unit_key=(
                        " \t " if meta["reviewer_no"] == 1 else target_key
                    ),
                    related_unit_keys=[],
                    code="source_semantic_drift",
                    violation_kinds=[
                        "environment_personification",
                        "unsupported_action",
                    ],
                    message=f"reviewer {meta['reviewer_no']} local finding",
                ),
            ])
        repair_calls += 1
        return ScreenplaySceneShardCreativeIR(slots={
            target_key: draft.slots[target_key].model_copy(deep=True),
        })

    monkeypatch.setattr(
        "app.screenplay_scene_shards.model_gateway.chat_structured",
        fake_structured,
    )
    result, audit = asyncio.run(_REAL_SEMANTIC_REVIEW(
        draft=draft,
        scene_input_contracts=contracts,
        identity_registry=[],
        operation_id="scene-semantic-peer-proven-blank",
        shard_id=plan.shard_id,
        validate_draft=lambda _candidate: [],
    ))

    assert result == draft
    assert repair_calls == 1
    assert sorted(review_calls) == [
        ("initial", 1),
        ("initial", 2),
        ("post_repair", 1),
        ("post_repair", 2),
    ]
    assert audit[0]["reviews"][0]["findings"][0]["unit_key"] == (
        target_key
    )
    assert audit[0]["consensus"][0]["unit_key"] == target_key
    assert audit[-1]["consensus"] == []


def test_scene_shard_semantic_same_key_code_different_kind_is_not_consensus(
    monkeypatch,
) -> None:
    blueprint = _blueprint(split_domain=False)
    plan = build_screenplay_scene_shard_plans(
        blueprint,
        source_text=SOURCE,
        identity_registry_hash="identity-hash",
    )[0]
    contracts = _contracts([plan], blueprint)[plan.shard_id]
    draft = _creative_shard(plan, blueprint, contracts)
    flagged_key = next(iter(draft.slots))
    calls = 0

    async def fake_structured(*_args, **kwargs):
        nonlocal calls
        calls += 1
        assert kwargs["model_type"] is ScreenplaySceneShardSemanticReview
        violation_kind = (
            "wrong_subject"
            if kwargs["call_meta"]["reviewer_no"] == 1
            else "unsupported_action"
        )
        return ScreenplaySceneShardSemanticReview(findings=[
            ScreenplaySceneShardSemanticFinding(
                unit_key=flagged_key,
                code="source_semantic_drift",
                violation_kinds=[violation_kind],
                message=f"reviewer classified {violation_kind}",
            ),
        ])

    monkeypatch.setattr(
        "app.screenplay_scene_shards.model_gateway.chat_structured",
        fake_structured,
    )
    result, audit = asyncio.run(_REAL_SEMANTIC_REVIEW(
        draft=draft,
        scene_input_contracts=contracts,
        identity_registry=[],
        operation_id="scene-semantic-kind-disagreement",
        shard_id=plan.shard_id,
        validate_draft=lambda _candidate: [],
    ))

    assert result == draft
    assert calls == 2
    assert audit[0]["consensus"] == []


def test_scene_shard_semantic_insufficient_ceiling_blocks_provider(
    monkeypatch,
) -> None:
    blueprint = _blueprint(split_domain=False)
    plan = build_screenplay_scene_shard_plans(
        blueprint,
        source_text=SOURCE,
        identity_registry_hash="identity-hash",
    )[0]
    contracts = _contracts([plan], blueprint)[plan.shard_id]
    draft = _creative_shard(plan, blueprint, contracts)
    unit_key = next(iter(draft.slots))
    draft = ScreenplaySceneShardCreativeIR(
        slots={unit_key: draft.slots[unit_key]},
    )
    provider_calls = 0

    monkeypatch.setattr(
        scene_shards_module.hiagent,
        "active_provider",
        lambda _kind: "budget-test-provider",
    )
    monkeypatch.setattr(
        scene_shards_module.hiagent,
        "active_model",
        lambda _kind, _provider=None: "budget-test-model",
    )
    monkeypatch.setattr(
        scene_shards_module.hiagent,
        "active_model_token_limits",
        lambda *_args: {
            "context_window_tokens": 1024,
            "max_output_tokens": 32768,
            "token_limits_source": "configured",
        },
    )

    async def forbidden_structured(*_args, **_kwargs):
        nonlocal provider_calls
        provider_calls += 1
        raise AssertionError("budget gate must run before provider")

    monkeypatch.setattr(
        "app.screenplay_scene_shards.model_gateway.chat_structured",
        forbidden_structured,
    )
    with pytest.raises(
        ScreenplaySceneShardError,
        match=r"required=\d+，ceiling=0",
    ):
        asyncio.run(_REAL_SEMANTIC_REVIEW(
            draft=draft,
            scene_input_contracts=contracts,
            identity_registry=[],
            operation_id="scene-semantic-budget-block",
            shard_id=plan.shard_id,
            validate_draft=lambda _candidate: [],
        ))

    assert provider_calls == 0


def test_scene_shard_semantic_review_chunks_81_units_at_32768_ceiling(
    monkeypatch,
) -> None:
    blueprint = _blueprint(split_domain=False)
    plan = build_screenplay_scene_shard_plans(
        blueprint,
        source_text=SOURCE,
        identity_registry_hash="identity-hash",
    )[0]
    contracts = _contracts([plan], blueprint)[plan.shard_id]
    base_slot = contracts[0].unit_slots[0]
    unit_keys = [
        f"SS003:SRC0005:unit:{index:03d}"
        for index in range(1, 82)
    ]
    slots = [
        base_slot.model_copy(update={
            "unit_key": unit_key,
            "source_unit_key": f"SRC0005:unit:{index:03d}",
            "source_segment_ids": ["SRC0005"],
            "source_text": f"生产来源事实{index:03d}",
        })
        for index, unit_key in enumerate(unit_keys, start=1)
    ]
    contract = contracts[0].model_copy(update={"unit_slots": slots})
    draft = ScreenplaySceneShardCreativeIR(slots={
        unit_key: ScreenplaySceneShardCreativeUnit(
            text=f"按来源顺序交付生产单元{index:03d}",
            performance="保持动作、视线与主体连续",
            resulting_state="状态交给下一来源单元",
        )
        for index, unit_key in enumerate(unit_keys, start=1)
    })
    monkeypatch.setattr(
        scene_shards_module.hiagent,
        "active_provider",
        lambda _kind: "budget-test-provider",
    )
    monkeypatch.setattr(
        scene_shards_module.hiagent,
        "active_model",
        lambda _kind, _provider=None: "budget-test-model",
    )
    monkeypatch.setattr(
        scene_shards_module.hiagent,
        "active_model_token_limits",
        lambda *_args: {
            "context_window_tokens": 1024 * 1024,
            "max_output_tokens": 32768,
            "token_limits_source": "configured",
        },
    )
    calls: list[dict] = []

    async def fake_structured(messages, **kwargs):
        prompt = messages[1]["content"]
        authority = json.loads(
            prompt.split("\n冻结 slot 权威：\n", 1)[1].split(
                "\n冻结身份最小映射：\n",
                1,
            )[0]
        )
        creative = json.loads(
            prompt.split("\n待审 creative fields：\n", 1)[1].split(
                "\n完整输出 JSON Schema：\n",
                1,
            )[0]
        )
        chunk_unit_keys = list(creative["slots"])
        assert list(authority) == chunk_unit_keys
        calls.append({
            "unit_keys": chunk_unit_keys,
            "call_meta": kwargs["call_meta"],
            "operation_id": kwargs["operation_id"],
        })
        findings = []
        if kwargs["call_meta"]["reviewer_no"] == 1:
            findings.append(ScreenplaySceneShardSemanticFinding(
                unit_key=chunk_unit_keys[0],
                code="source_semantic_drift",
                violation_kinds=["source_contradiction"],
                message="chunk aggregation evidence",
            ))
        return ScreenplaySceneShardSemanticReview(findings=findings)

    monkeypatch.setattr(
        "app.screenplay_scene_shards.model_gateway.chat_structured",
        fake_structured,
    )
    result, audit = asyncio.run(_REAL_SEMANTIC_REVIEW(
        draft=draft,
        scene_input_contracts=[contract],
        identity_registry=[],
        operation_id="err-20260816-e35789",
        shard_id="SS003",
        validate_draft=lambda _candidate: [],
    ))

    assert result == draft
    chunk_count = calls[0]["call_meta"]["chunk_count"]
    assert chunk_count > 1
    assert len(calls) == chunk_count * 2
    for reviewer_no in (1, 2):
        reviewer_calls = [
            call
            for call in calls
            if call["call_meta"]["reviewer_no"] == reviewer_no
        ]
        assert [
            call["call_meta"]["chunk_index"]
            for call in reviewer_calls
        ] == list(range(1, chunk_count + 1))
        assert [
            unit_key
            for call in reviewer_calls
            for unit_key in call["unit_keys"]
        ] == unit_keys
        assert all(
            call["call_meta"]["required"]
            <= call["call_meta"]["ceiling"]
            for call in reviewer_calls
        )
        assert all(
            call["call_meta"]["unit_count"] == len(call["unit_keys"])
            for call in reviewer_calls
        )
        assert all(
            call["call_meta"]["substage"] == "initial"
            for call in reviewer_calls
        )
        for call in reviewer_calls:
            meta = call["call_meta"]
            chunk_marker = (
                f"chunk-{meta['chunk_index']}-of-"
                f"{meta['chunk_count']}:"
            )
            assert chunk_marker in call["operation_id"]
            chunk_hash = call["operation_id"].rsplit(":", 1)[-1]
            assert len(chunk_hash) == 64
            int(chunk_hash, 16)
    assert len(audit) == 1
    assert len(audit[0]["reviews"]) == 2
    assert len(audit[0]["reviews"][0]["findings"]) == chunk_count
    assert audit[0]["reviews"][1]["findings"] == []
    assert audit[0]["consensus"] == []


def test_scene_shard_semantic_unknown_finding_is_hard_failure(
    monkeypatch,
) -> None:
    blueprint = _blueprint(split_domain=False)
    plan = build_screenplay_scene_shard_plans(
        blueprint, source_text=SOURCE, identity_registry_hash="identity-hash",
    )[0]
    contracts = _contracts([plan], blueprint)[plan.shard_id]
    draft = _creative_shard(plan, blueprint, contracts)

    async def fake_structured(*_args, **_kwargs):
        return ScreenplaySceneShardSemanticReview(findings=[
            ScreenplaySceneShardSemanticFinding(
                unit_key=" UNKNOWN-SRC0031 ",
                code="source_semantic_drift",
                violation_kinds=["source_contradiction"],
                message="unknown",
            ),
        ])

    monkeypatch.setattr(
        "app.screenplay_scene_shards.model_gateway.chat_structured",
        fake_structured,
    )
    with pytest.raises(
        ScreenplaySceneShardError,
        match="未知 unit_key：UNKNOWN-SRC0031",
    ):
        asyncio.run(_REAL_SEMANTIC_REVIEW(
            draft=draft,
            scene_input_contracts=contracts,
            identity_registry=[],
            operation_id="scene-semantic-unknown",
            shard_id=plan.shard_id,
            validate_draft=lambda _candidate: [],
        ))


def test_scene_shard_semantic_post_repair_unknown_finding_is_hard_failure(
    monkeypatch,
) -> None:
    blueprint = _blueprint(split_domain=False)
    plan = build_screenplay_scene_shard_plans(
        blueprint,
        source_text=SOURCE,
        identity_registry_hash="identity-hash",
    )[0]
    contracts = _contracts([plan], blueprint)[plan.shard_id]
    draft = _creative_shard(plan, blueprint, contracts)
    flagged_key = next(iter(draft.slots))
    review_substages: list[str] = []
    repair_calls = 0

    async def fake_structured(*_args, **kwargs):
        nonlocal repair_calls
        if kwargs["model_type"] is ScreenplaySceneShardSemanticReview:
            substage = kwargs["call_meta"]["substage"]
            review_substages.append(substage)
            return ScreenplaySceneShardSemanticReview(findings=[
                ScreenplaySceneShardSemanticFinding(
                    unit_key=(
                        flagged_key
                        if substage == "initial"
                        else "UNKNOWN-SLOT"
                    ),
                    code="source_semantic_drift",
                    violation_kinds=["source_contradiction"],
                    message="unknown post-repair finding",
                ),
            ])
        repair_calls += 1
        return ScreenplaySceneShardCreativeIR(slots={
            flagged_key: draft.slots[flagged_key].model_copy(deep=True),
        })

    monkeypatch.setattr(
        "app.screenplay_scene_shards.model_gateway.chat_structured",
        fake_structured,
    )
    with pytest.raises(
        ScreenplaySceneShardError,
        match="未知 unit_key：UNKNOWN-SLOT",
    ):
        asyncio.run(_REAL_SEMANTIC_REVIEW(
            draft=draft,
            scene_input_contracts=contracts,
            identity_registry=[],
            operation_id="err-20260816-de71a8-post-unknown",
            shard_id=plan.shard_id,
            validate_draft=lambda _candidate: [],
        ))

    assert repair_calls == 1
    assert review_substages == [
        "initial",
        "initial",
        "post_repair",
    ]


def test_scene_shard_semantic_post_repair_consensus_failure_is_hard(
    monkeypatch,
) -> None:
    blueprint = _blueprint(split_domain=False)
    plan = build_screenplay_scene_shard_plans(
        blueprint, source_text=SOURCE, identity_registry_hash="identity-hash",
    )[0]
    contracts = _contracts([plan], blueprint)[plan.shard_id]
    draft = _creative_shard(plan, blueprint, contracts)
    flagged_key = next(iter(draft.slots))
    review_calls: list[dict] = []
    repair_calls: list[dict] = []

    async def fake_structured(*_args, **kwargs):
        if kwargs["model_type"] is ScreenplaySceneShardSemanticReview:
            review_calls.append(kwargs)
            return ScreenplaySceneShardSemanticReview(findings=[
                ScreenplaySceneShardSemanticFinding(
                    unit_key=flagged_key,
                    code="state_subject_semantic_drift",
                    violation_kinds=["wrong_subject"],
                    message="F43/F47/F60 drift remains",
                ),
            ])
        repair_calls.append(kwargs)
        return ScreenplaySceneShardCreativeIR(slots={
            flagged_key: draft.slots[flagged_key].model_copy(deep=True),
        })

    monkeypatch.setattr(
        "app.screenplay_scene_shards.model_gateway.chat_structured",
        fake_structured,
    )
    with pytest.raises(ScreenplaySceneShardError, match="semantic gate 未收口"):
        asyncio.run(_REAL_SEMANTIC_REVIEW(
            draft=draft,
            scene_input_contracts=contracts,
            identity_registry=[],
            operation_id="scene-semantic-post-fail",
            shard_id=plan.shard_id,
            validate_draft=lambda _candidate: [],
        ))

    assert len(review_calls) == 8
    assert len(repair_calls) == 3
    post_repair_calls = [
        call
        for call in review_calls
        if call["call_meta"]["substage"] == "post_repair"
    ]
    assert len(post_repair_calls) == 6
    assert all(
        call["call_meta"]["reuse_successful_operation"] is True
        for call in review_calls
    )
    for reviewer_no in (1, 2):
        reviewer_post_repair_calls = [
            call
            for call in post_repair_calls
            if call["call_meta"]["reviewer_no"] == reviewer_no
        ]
        assert len(reviewer_post_repair_calls) == 3
        assert len({
            call["operation_id"]
            for call in reviewer_post_repair_calls
        }) == 1
    assert [
        call["call_meta"]["repair_round"]
        for call in repair_calls
    ] == [1, 2, 3]
    assert ":repair:round-1:" in repair_calls[0]["operation_id"]
    assert ":repair:round-2:" in repair_calls[1]["operation_id"]
    assert ":repair:round-3:" in repair_calls[2]["operation_id"]
    assert (
        repair_calls[0]["operation_id"]
        != repair_calls[1]["operation_id"]
    )
    assert (
        repair_calls[1]["operation_id"]
        != repair_calls[2]["operation_id"]
    )


def test_scene_shard_semantic_second_repair_cleans_current_consensus_only(
    monkeypatch,
) -> None:
    blueprint = _blueprint(split_domain=False)
    plan = build_screenplay_scene_shard_plans(
        blueprint,
        source_text=SOURCE,
        identity_registry_hash="identity-hash",
    )[0]
    contracts = _contracts([plan], blueprint)[plan.shard_id]
    full_draft = _creative_shard(plan, blueprint, contracts)
    unit_keys = list(full_draft.slots)[:3]
    draft = ScreenplaySceneShardCreativeIR(slots={
        unit_key: full_draft.slots[unit_key]
        for unit_key in unit_keys
    })
    findings = [
        ScreenplaySceneShardSemanticFinding(
            unit_key=unit_keys[0],
            code="source_semantic_drift",
            violation_kinds=["unsupported_action"],
            message="第一轮待修复动作",
        ),
        ScreenplaySceneShardSemanticFinding(
            unit_key=unit_keys[1],
            code="source_semantic_drift",
            violation_kinds=["unsupported_action"],
            message="第二轮待修复动作",
        ),
    ]
    repair_calls: list[dict] = []
    changed_per_round: list[set[str]] = []
    review_calls = 0

    async def fake_structured(*_args, **kwargs):
        nonlocal review_calls
        if kwargs["model_type"] is ScreenplaySceneShardSemanticReview:
            review_calls += 1
            if not repair_calls:
                review_findings = [findings[0]]
            else:
                review_findings = [findings[1]]
            return ScreenplaySceneShardSemanticReview(
                findings=review_findings,
            )

        repair_calls.append(kwargs)
        repair_round = len(repair_calls)
        repair_context = json.loads(kwargs["repair_context"])
        before = ScreenplaySceneShardCreativeIR.model_validate(
            repair_context["current_flagged_creative"],
        )
        flagged_keys = {
            finding["unit_key"]
            for finding in repair_context["consensus_findings"]
        }
        expected_key = unit_keys[0]
        assert flagged_keys == {expected_key}
        assert set(before.slots) == {expected_key}
        assert kwargs["call_meta"]["repair_round"] == repair_round
        assert f":repair:round-{repair_round}:" in kwargs["operation_id"]
        slot_schema = kwargs["output_schema"]["properties"]["slots"]
        assert slot_schema["required"] == [expected_key]
        assert set(slot_schema["properties"]) == {expected_key}
        assert slot_schema["additionalProperties"] is False

        repaired = before.model_copy(deep=True)
        repaired.slots[expected_key].text = f"第{repair_round}轮已修复"
        changed_per_round.append({
            unit_key
            for unit_key in before.slots
            if (
                before.slots[unit_key].model_dump(mode="json")
                != repaired.slots[unit_key].model_dump(mode="json")
            )
        })
        return repaired

    monkeypatch.setattr(
        "app.screenplay_scene_shards.model_gateway.chat_structured",
        fake_structured,
    )
    repaired, audit = asyncio.run(_REAL_SEMANTIC_REVIEW(
        draft=draft,
        scene_input_contracts=contracts,
        identity_registry=[],
        operation_id="err-20260816-688fcb",
        shard_id=plan.shard_id,
        validate_draft=lambda _candidate: [],
    ))

    assert review_calls == 4
    assert len(repair_calls) == 1
    assert changed_per_round == [{unit_keys[0]}]
    assert repaired.slots[unit_keys[0]].text == "第1轮已修复"
    assert repaired.slots[unit_keys[1]] == draft.slots[unit_keys[1]]
    assert [phase["phase"] for phase in audit] == [
        "initial",
        "post_repair",
    ]
    assert all(len(phase["reviews"]) == 2 for phase in audit)
    assert [
        [finding["unit_key"] for finding in phase["consensus"]]
        for phase in audit
    ] == [[unit_keys[0]], []]
    assert audit[1]["reviews"] == [
        {"findings": []},
        {"findings": []},
    ]


def test_scene_shard_semantic_same_flagged_key_can_continue_repair(
    monkeypatch,
) -> None:
    blueprint = _blueprint(split_domain=False)
    plan = build_screenplay_scene_shard_plans(
        blueprint,
        source_text=SOURCE,
        identity_registry_hash="identity-hash",
    )[0]
    contracts = _contracts([plan], blueprint)[plan.shard_id]
    full_draft = _creative_shard(plan, blueprint, contracts)
    flagged_key = next(iter(full_draft.slots))
    draft = ScreenplaySceneShardCreativeIR(slots={
        flagged_key: full_draft.slots[flagged_key],
    })
    findings = [
        ScreenplaySceneShardSemanticFinding(
            unit_key=flagged_key,
            code="source_semantic_drift",
            violation_kinds=["unsupported_action"],
            message="第一轮来源动作问题",
        ),
        ScreenplaySceneShardSemanticFinding(
            unit_key=flagged_key,
            code="state_subject_semantic_drift",
            violation_kinds=["wrong_subject"],
            message="同一 slot 的新主体问题",
        ),
    ]
    repair_calls: list[dict] = []
    review_calls = 0

    async def fake_structured(*_args, **kwargs):
        nonlocal review_calls
        if kwargs["model_type"] is ScreenplaySceneShardSemanticReview:
            review_calls += 1
            finding = (
                findings[len(repair_calls)]
                if len(repair_calls) < len(findings)
                else None
            )
            return ScreenplaySceneShardSemanticReview(
                findings=[] if finding is None else [finding],
            )

        repair_calls.append(kwargs)
        repair_context = json.loads(kwargs["repair_context"])
        repaired = ScreenplaySceneShardCreativeIR.model_validate(
            repair_context["current_flagged_creative"],
        )
        repaired.slots[flagged_key].text = (
            f"第{len(repair_calls)}轮已修复"
        )
        return repaired

    monkeypatch.setattr(
        "app.screenplay_scene_shards.model_gateway.chat_structured",
        fake_structured,
    )
    repaired, audit = asyncio.run(_REAL_SEMANTIC_REVIEW(
        draft=draft,
        scene_input_contracts=contracts,
        identity_registry=[],
        operation_id="err-20260816-de71a8-same-key",
        shard_id=plan.shard_id,
        validate_draft=lambda _candidate: [],
    ))

    assert review_calls == 6
    assert len(repair_calls) == 2
    assert repaired.slots[flagged_key].text == "第2轮已修复"
    assert [
        [finding["code"] for finding in phase["consensus"]]
        for phase in audit
    ] == [
        ["source_semantic_drift"],
        ["state_subject_semantic_drift"],
        [],
    ]
    assert all(
        finding["unit_key"] == flagged_key
        for phase in audit[1:]
        for review in phase["reviews"]
        for finding in review["findings"]
    )


def test_scene_shard_semantic_prompt_reads_action_source_facts_for_production_drift() -> None:
    blueprint = _blueprint(split_domain=False)
    plan = build_screenplay_scene_shard_plans(
        blueprint, source_text=SOURCE, identity_registry_hash="identity-hash",
    )[0]
    base_contract = _contracts([plan], blueprint)[plan.shard_id][0]
    base_slot = base_contract.unit_slots[0]
    source_rows = [
        ("SRC0030", "孟浩顺着藤条问道：你还没说要带我去哪？"),
        ("SRC0031", "虎头虎脑的少年说飞行需要宝物"),
        ("SRC0033", "王有材解释他们被抓来的缘由"),
        ("SRC0044", "许师姐对孟浩宣告靠山宗规矩"),
    ]
    slots = [
        base_slot.model_copy(update={
            "unit_key": f"F{index}",
            "source_unit_key": f"{source_id}:unit:001",
            "source_segment_ids": [source_id],
            "source_text": "",
        })
        for index, (source_id, _text) in enumerate(source_rows, start=43)
    ]
    contract = base_contract.model_copy(update={
        "source_segments": [
            ScreenplaySceneSourceSegment(source_segment_id=source_id, text=text)
            for source_id, text in source_rows
        ],
        "unit_slots": slots,
    })
    draft = ScreenplaySceneShardCreativeIR(
        slots={
            slot.unit_key: ScreenplaySceneShardCreativeUnit(text="错误人物反应")
            for slot in slots
        },
    )
    prompt = scene_shards_module._scene_shard_semantic_review_prompt(
        draft=draft,
        scene_input_contracts=[contract],
        identity_registry=[],
    )
    review_schema = (
        scene_shards_module._scene_shard_semantic_review_schema(
            list(draft.slots)
        )
    )
    assert "findings=[]" not in prompt
    assert '{"findings":[]}' in prompt
    assert "不得输出 Markdown、解释或任何对象外文本" in prompt
    assert json.dumps(
        review_schema,
        ensure_ascii=False,
        separators=(",", ":"),
    ) in prompt
    assert all(
        source_segment_facts(source_id, text)[0].text in prompt
        for source_id, text in source_rows
    )
    assert all(f'"source_unit_key":"{source_id}:unit:001"' in prompt for source_id, _ in source_rows)
    assert all(slot.source_text == "" for slot in slots)


def test_run_884443dc4404_prompt_attributes_051_duplication_to_045() -> None:
    case = json.loads(
        RUN_884443DC4404_REPLAY.read_text(encoding="utf-8")
    )
    duplicate_case = case["cross_slot_duplication"]
    blueprint = _blueprint(split_domain=False)
    plan = build_screenplay_scene_shard_plans(
        blueprint,
        source_text=SOURCE,
        identity_registry_hash="identity-hash",
    )[0]
    base_contract = _contracts([plan], blueprint)[plan.shard_id][0]
    base_slot = base_contract.unit_slots[0]
    fixture_slots = [
        duplicate_case["early_slot"],
        duplicate_case["later_source_slot"],
    ]
    slots = [
        base_slot.model_copy(update={
            "unit_key": fixture_slot["unit_key"],
            "source_unit_key": (
                f"{fixture_slot['source_segment_id']}:unit:001"
            ),
            "source_segment_ids": [
                fixture_slot["source_segment_id"],
            ],
            "source_text": "",
        })
        for fixture_slot in fixture_slots
    ]
    contract = base_contract.model_copy(update={
        "source_segments": [
            ScreenplaySceneSourceSegment(
                source_segment_id=fixture_slot["source_segment_id"],
                text=fixture_slot["source_fact"],
            )
            for fixture_slot in fixture_slots
        ],
        "unit_slots": slots,
    })
    draft = ScreenplaySceneShardCreativeIR(slots={
        fixture_slot["unit_key"]: ScreenplaySceneShardCreativeUnit(
            text=fixture_slot["creative"],
        )
        for fixture_slot in fixture_slots
    })

    prompt = scene_shards_module._scene_shard_semantic_review_prompt(
        draft=draft,
        scene_input_contracts=[contract],
        identity_registry=[],
    )
    expected = ScreenplaySceneShardSemanticFinding.model_validate(
        {
            **duplicate_case["expected_finding"],
            "related_unit_keys": [
                duplicate_case["later_source_slot"]["unit_key"],
            ],
        }
    )

    assert "必须逐 slot 穷举核对" in prompt
    assert "finding 只能归因到 creative fields 与该 slot 自身 source_fact" in prompt
    assert "标记最早超出自身来源、或没有自身来源承载该内容的 slot" in prompt
    assert "不得标记后来正确承载其自身 source_fact 的 slot" in prompt
    assert all(slot["unit_key"] in prompt for slot in fixture_slots)
    assert all(slot["source_fact"] in prompt for slot in fixture_slots)
    assert expected.unit_key == duplicate_case["early_slot"]["unit_key"]
    assert expected.violation_kinds == ["cross_slot_duplication"]
    assert expected.unit_key != duplicate_case["forbidden_finding_unit_key"]


def test_ss004_generation_prompt_contains_representative_exact_facts() -> None:
    blueprint = _blueprint(split_domain=False)
    base_plan = build_screenplay_scene_shard_plans(
        blueprint,
        source_text=SOURCE,
        identity_registry_hash="identity-hash",
    )[0]
    base_contract = _contracts([base_plan], blueprint)[
        base_plan.shard_id
    ][0]
    source_text = "".join(
        f"精确来源事实{index:03d}，"
        for index in range(1, 62)
    )
    facts = source_segment_facts("SRC0004", source_text)
    selected_orders = (1, 21, 52, 61)
    selected_facts = {
        fact.unit_order: fact
        for fact in facts
        if fact.unit_order in selected_orders
    }
    base_slot = base_contract.unit_slots[0]
    slots = []
    for order in selected_orders:
        environment_only = order in {21, 61}
        identity_key = "person_甲" if order == 1 else "person_乙"
        slots.append(base_slot.model_copy(update={
            "unit_key": f"SS004:SRC0004:unit:{order:03d}",
            "source_segment_ids": ["SRC0004"],
            "source_unit_key": selected_facts[order].source_unit_key,
            "source_text": "",
            "state_subject_key": (
                "" if environment_only else identity_key
            ),
            "state_subject_keys": (
                [] if environment_only else [identity_key]
            ),
            "environment_only": environment_only,
            "actor_keys": (
                [] if environment_only else [identity_key]
            ),
            "target_keys": [],
            "speaker_key": None,
            "onscreen_entity_keys": (
                [] if environment_only else [identity_key]
            ),
        }))
    source_owners = {"SRC0004": base_contract.scene_plan_key}
    contract = base_contract.model_copy(update={
        "source_segment_ids": ["SRC0004"],
        "source_semantics": {
            "SRC0004": next(iter(base_contract.source_semantics.values())),
        },
        "source_segments": [
            ScreenplaySceneSourceSegment(
                source_segment_id="SRC0004",
                text=source_text,
            )
        ],
        "source_scene_owners": source_owners,
        "unit_slots": slots,
    })
    plan = base_plan.model_copy(update={
        "shard_id": "SS004",
        "source_segment_ids": ["SRC0004"],
        "source_scene_owners": source_owners,
        "unit_slots": slots,
        "estimated_units": len(slots),
    })
    identity_registry = [
        {
            "identity_key": f"person_{label}",
            "authority_id": f"bible:{label}",
            "canonical_name": label,
            "source_labels": [label],
        }
        for label in ("甲", "乙")
    ]

    prompt = scene_shards_module._scene_shard_prompt(
        episode_no=1,
        plan=plan,
        blueprint_scene_plans=[],
        blueprint_nodes=[],
        scene_input_contracts=[contract],
        identity_registry=identity_registry,
        output_schema={},
    )
    authority, identity_labels = (
        scene_shards_module._scene_shard_semantic_authority_payload(
            scene_input_contracts=[contract],
            identity_registry=identity_registry,
        )
    )

    assert "action slot 即使 source_text 为空也不授权自由改写" in prompt
    assert "每个 slot 只能改写自身 source_fact" in prompt
    assert "cross-slot 内容必须归因到最早越界 slot" in prompt
    assert json.dumps(
        authority,
        ensure_ascii=False,
        separators=(",", ":"),
    ) in prompt
    assert json.dumps(
        identity_labels,
        ensure_ascii=False,
        separators=(",", ":"),
    ) in prompt
    for order in selected_orders:
        unit_key = f"SS004:SRC0004:unit:{order:03d}"
        assert authority[unit_key]["source_fact"]["text"] == (
            selected_facts[order].text
        )
        assert selected_facts[order].text in prompt
    assert authority["SS004:SRC0004:unit:021"][
        "environment_only"
    ] is True
    assert authority["SS004:SRC0004:unit:052"]["actor_keys"] == [
        "person_乙"
    ]


def test_scene_shard_semantic_repair_rejects_unflagged_slot_rewrite(
    monkeypatch,
) -> None:
    blueprint = _blueprint(split_domain=False)
    plan = build_screenplay_scene_shard_plans(
        blueprint,
        source_text=SOURCE,
        identity_registry_hash="identity-hash",
    )[0]
    contracts = _contracts([plan], blueprint)[plan.shard_id]
    draft = _creative_shard(plan, blueprint, contracts)
    unit_keys = list(draft.slots)
    calls = 0

    async def fake_structured(*_args, **kwargs):
        nonlocal calls
        calls += 1
        if kwargs["model_type"] is ScreenplaySceneShardSemanticReview:
            return ScreenplaySceneShardSemanticReview(findings=[
                ScreenplaySceneShardSemanticFinding(
                    unit_key=unit_keys[0],
                    code="source_semantic_drift",
                    violation_kinds=["wrong_subject"],
                    message="production F43/F47/F60 型来源主体漂移",
                )
            ])
        flagged_slot = draft.slots[unit_keys[0]].model_copy(deep=True)
        flagged_slot.text = "甲推门进入。"
        unflagged_slot = draft.slots[unit_keys[1]].model_copy(deep=True)
        unflagged_slot.text = "越权重写另一来源动作"
        return ScreenplaySceneShardCreativeIR(slots={
            unit_keys[0]: flagged_slot,
            unit_keys[1]: unflagged_slot,
        })

    monkeypatch.setattr(
        "app.screenplay_scene_shards.model_gateway.chat_structured",
        fake_structured,
    )
    with pytest.raises(
        ScreenplaySceneShardError,
        match="语义 repair subset slots 必须完全等于 consensus 标记集合",
    ):
        asyncio.run(_REAL_SEMANTIC_REVIEW(
            draft=draft,
            scene_input_contracts=contracts,
            identity_registry=[],
            operation_id="scene-semantic-overreach",
            shard_id=plan.shard_id,
            validate_draft=lambda _candidate: [],
        ))


def test_scene_shard_semantic_repair_uses_plan_bound_strict_schema(
    monkeypatch,
) -> None:
    blueprint = _blueprint(split_domain=False)
    plan = build_screenplay_scene_shard_plans(
        blueprint,
        source_text=SOURCE,
        identity_registry_hash="identity-hash",
    )[0]
    contracts = _contracts([plan], blueprint)[plan.shard_id]
    dialogue_slot = plan.unit_slots[0]
    dialogue_slot.kind = "dialogue"
    dialogue_slot.source_text = "“不得改写的语义修复对白”"
    draft = _creative_shard(plan, blueprint, contracts)
    flagged_key = dialogue_slot.unit_key
    finding = ScreenplaySceneShardSemanticFinding(
        unit_key=flagged_key,
        code="source_semantic_drift",
        violation_kinds=["source_contradiction"],
        message="对白必须保持来源原文",
    )
    full_schema = build_screenplay_scene_shard_repair_schema(
        plan=plan,
        scene_input_contracts=contracts,
    )
    full_schema_before = deepcopy(full_schema)
    repair_calls: list[dict] = []

    async def fake_structured(messages, **kwargs):
        meta = kwargs["call_meta"]
        if meta["stage_key"] == "screenplay_scene_shard_semantic_review":
            return ScreenplaySceneShardSemanticReview(
                findings=(
                    [finding]
                    if meta["substage"] == "initial"
                    else []
                ),
            )
        repair_calls.append({
            "messages": messages,
            **kwargs,
        })
        return ScreenplaySceneShardCreativeIR.model_validate({
            "contract_version": SCREENPLAY_SCENE_CREATIVE_VERSION,
            "slots": {
                flagged_key: draft.slots[flagged_key].model_dump(
                    mode="json"
                ),
            },
        })

    monkeypatch.setattr(model_gateway, "chat_structured", fake_structured)
    repaired, audit = asyncio.run(_REAL_SEMANTIC_REVIEW(
        draft=draft,
        scene_input_contracts=contracts,
        identity_registry=[],
        operation_id="attempt9-semantic-repair-strict",
        shard_id=plan.shard_id,
        validate_draft=lambda _candidate: [],
        full_creative_schema=full_schema,
    ))

    assert repaired == draft
    assert audit[-1]["consensus"] == []
    assert full_schema == full_schema_before
    assert len(repair_calls) == 1
    repair_call = repair_calls[0]
    subset_schema = repair_call["output_schema"]
    assert set(subset_schema["properties"]["slots"]["properties"]) == {
        flagged_key,
    }
    assert subset_schema["properties"]["slots"]["required"] == [
        flagged_key,
    ]
    local_dialogue_schema = subset_schema["properties"]["slots"][
        "properties"
    ][flagged_key]
    assert local_dialogue_schema["allOf"][1]["properties"]["text"][
        "const"
    ] == dialogue_slot.source_text
    assert json.dumps(
        subset_schema,
        ensure_ascii=False,
        separators=(",", ":"),
    ) in repair_call["messages"][1]["content"]
    assert repair_call["require_response_format"] is True
    response_format = repair_call["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["strict"] is True
    provider_schema = response_format["json_schema"]["schema"]
    provider_slot_ref = provider_schema["properties"]["slots"][
        "properties"
    ][flagged_key]["$ref"]
    provider_dialogue_schema = provider_schema["$defs"][
        provider_slot_ref.removeprefix("#/$defs/")
    ]
    assert provider_dialogue_schema["properties"]["text"]["enum"] == [
        dialogue_slot.source_text,
    ]
    assert repair_call["repair_schema"](repaired) is subset_schema
    assert SCREENPLAY_SCENE_SEMANTIC_REVIEW_VERSION in repair_call[
        "operation_id"
    ]
    assert repair_call["call_meta"]["contract_version"] == (
        SCREENPLAY_SCENE_SEMANTIC_REVIEW_VERSION
    )


def test_scene_shard_semantic_repair_ignoring_schema_fails_closed(
    monkeypatch,
) -> None:
    blueprint = _blueprint(split_domain=False)
    plan = build_screenplay_scene_shard_plans(
        blueprint,
        source_text=SOURCE,
        identity_registry_hash="identity-hash",
    )[0]
    contracts = _contracts([plan], blueprint)[plan.shard_id]
    draft = _creative_shard(plan, blueprint, contracts)
    flagged_key = next(iter(draft.slots))
    finding = ScreenplaySceneShardSemanticFinding(
        unit_key=flagged_key,
        code="source_semantic_drift",
        violation_kinds=["source_contradiction"],
        message="来源内容矛盾",
    )
    full_schema = build_screenplay_scene_shard_repair_schema(
        plan=plan,
        scene_input_contracts=contracts,
    )
    repair_calls: list[dict] = []

    async def fake_chat(messages, **kwargs):
        meta = kwargs["call_meta"]
        if meta["stage_key"] == "screenplay_scene_shard_semantic_review":
            return ScreenplaySceneShardSemanticReview(
                findings=(
                    [finding]
                    if meta["substage"] == "initial"
                    else []
                ),
            ).model_dump_json()
        repair_calls.append({
            "messages": deepcopy(messages),
            "kwargs": deepcopy(kwargs),
        })
        # Simulate HTTP 200 from a provider that ignored strict `required`:
        # Pydantic can fill these defaults, but the local wire-contract gate
        # must not authorize that omission.
        return json.dumps({
            "contract_version": SCREENPLAY_SCENE_CREATIVE_VERSION,
            "slots": {
                flagged_key: {
                    "text": draft.slots[flagged_key].text,
                },
            },
        }, ensure_ascii=False)

    monkeypatch.setattr(model_gateway, "chat", fake_chat)
    with pytest.raises(
        model_gateway.StructuredSemanticError,
        match="显式提供全部 creative fields",
    ):
        asyncio.run(_REAL_SEMANTIC_REVIEW(
            draft=draft,
            scene_input_contracts=contracts,
            identity_registry=[],
            operation_id="attempt9-semantic-repair-ignore-schema",
            shard_id=plan.shard_id,
            validate_draft=lambda _candidate: [],
            full_creative_schema=full_schema,
        ))

    assert len(repair_calls) == 2
    first_call, retry_call = repair_calls
    assert all(
        call["kwargs"]["response_format"]["type"] == "json_schema"
        and call["kwargs"]["response_format"]["json_schema"]["strict"]
        is True
        and call["kwargs"]["call_meta"]["response_format_required"] is True
        for call in repair_calls
    )
    assert first_call["kwargs"]["max_tokens"] == retry_call["kwargs"][
        "max_tokens"
    ]
    assert first_call["kwargs"]["response_format"] == retry_call[
        "kwargs"
    ]["response_format"]
    assert first_call["kwargs"]["call_meta"]["semantic_attempt"] == 0
    assert retry_call["kwargs"]["call_meta"]["semantic_attempt"] == 1
    assert ":structured-attempt:" not in first_call["kwargs"][
        "call_meta"
    ]["operation_id"]
    base_operation_id = first_call["kwargs"]["call_meta"][
        "base_operation_id"
    ]
    subset_schema = (
        scene_shards_module._scene_shard_semantic_repair_subset_schema(
        [flagged_key],
        full_creative_schema=full_schema,
        )
    )
    expected_retry_identity = evidence_repository.content_hash({
        "base_operation_id": base_operation_id,
        "format_attempt": 0,
        "semantic_attempt": 1,
        "messages": retry_call["messages"],
        "max_tokens": retry_call["kwargs"]["max_tokens"],
        "temperature": retry_call["kwargs"]["temperature"],
        "structured_schema": subset_schema,
        "response_format": retry_call["kwargs"]["response_format"],
        "require_response_format": True,
    })
    assert retry_call["kwargs"]["call_meta"]["operation_id"] == (
        f"{base_operation_id}:structured-attempt:"
        f"{expected_retry_identity}"
    )


def test_scene_shard_semantic_clean_dual_review_uses_real_structured_signature(
    monkeypatch,
) -> None:
    blueprint = _blueprint(split_domain=False)
    plan = build_screenplay_scene_shard_plans(
        blueprint,
        source_text=SOURCE,
        identity_registry_hash="identity-hash",
    )[0]
    contracts = _contracts([plan], blueprint)[plan.shard_id]
    draft = _creative_shard(plan, blueprint, contracts)
    reviewer_calls: list[dict] = []
    reviewer_messages: list[list[dict[str, str]]] = []

    async def fake_chat(messages, **kwargs):
        meta = kwargs["call_meta"]
        reviewer_calls.append(meta)
        reviewer_messages.append(messages)
        return ScreenplaySceneShardSemanticReview(
            findings=[],
        ).model_dump_json()

    monkeypatch.setattr(model_gateway, "chat", fake_chat)
    result, audit = asyncio.run(_REAL_SEMANTIC_REVIEW(
        draft=draft,
        scene_input_contracts=contracts,
        identity_registry=[],
        operation_id="scene-semantic-real-clean",
        shard_id=plan.shard_id,
        validate_draft=lambda _candidate: [],
    ))

    assert result == draft
    assert len(reviewer_calls) == 2
    assert {item["reviewer_no"] for item in reviewer_calls} == {1, 2}
    assert all(item["semantic_attempt"] == 0 for item in reviewer_calls)
    assert all(
        item["contract_version"]
        == SCREENPLAY_SCENE_SEMANTIC_REVIEW_VERSION
        for item in reviewer_calls
    )
    assert all(
        item["reuse_successful_operation"] is True
        for item in reviewer_calls
    )
    assert all(
        SCREENPLAY_SCENE_SEMANTIC_REVIEW_VERSION
        in item["operation_id"]
        for item in reviewer_calls
    )
    assert all(
        [message["role"] for message in messages] == ["system", "user"]
        for messages in reviewer_messages
    )
    assert audit[0]["consensus"] == []


def test_scene_shard_semantic_real_reviewer_rejects_duplicate_finding_keys(
    monkeypatch,
) -> None:
    blueprint = _blueprint(split_domain=False)
    plan = build_screenplay_scene_shard_plans(
        blueprint,
        source_text=SOURCE,
        identity_registry_hash="identity-hash",
    )[0]
    contracts = _contracts([plan], blueprint)[plan.shard_id]
    draft = _creative_shard(plan, blueprint, contracts)
    unit_key = next(iter(draft.slots))
    call_meta: list[dict] = []
    finding = {
        "unit_key": unit_key,
        "code": "source_semantic_drift",
        "violation_kinds": ["source_contradiction"],
        "message": "first",
    }

    async def fake_chat(*_args, **kwargs):
        call_meta.append(kwargs["call_meta"])
        return json.dumps({
            "findings": [
                finding,
                {**finding, "message": "duplicate key"},
            ],
        }, ensure_ascii=False)

    monkeypatch.setattr(model_gateway, "chat", fake_chat)
    with pytest.raises(
        model_gateway.StructuredFormatError,
        match="必须唯一",
    ):
        asyncio.run(_REAL_SEMANTIC_REVIEW(
            draft=draft,
            scene_input_contracts=contracts,
            identity_registry=[],
            operation_id="scene-semantic-real-duplicate",
            shard_id=plan.shard_id,
            validate_draft=lambda _candidate: [],
        ))

    # Reviewer 1 exhausts its bounded format repair synchronously; strict
    # fail-fast cancels reviewer 2 before it creates a duplicate paid attempt.
    assert len(call_meta) == 3
    assert {item["reviewer_no"] for item in call_meta} == {1}
    assert [item["format_attempt"] for item in call_meta] == [0, 1, 2]
    assert all(
        item["reuse_successful_operation"] is True
        for item in call_meta
    )
    assert all(
        ":structured-attempt:" not in item["operation_id"]
        for item in call_meta
        if item["format_attempt"] == 0
    )
    assert all(
        ":structured-attempt:" in item["operation_id"]
        for item in call_meta
        if item["format_attempt"] in {1, 2}
    )


def test_err_20260816_e8ac9d_post_repair_allows_second_format_repair(
    monkeypatch,
) -> None:
    blueprint = _blueprint(split_domain=False)
    plan = build_screenplay_scene_shard_plans(
        blueprint,
        source_text=SOURCE,
        identity_registry_hash="identity-hash",
    )[0]
    contracts = _contracts([plan], blueprint)[plan.shard_id]
    draft = _creative_shard(plan, blueprint, contracts)
    flagged_key = next(iter(draft.slots))
    finding = ScreenplaySceneShardSemanticFinding(
        unit_key=flagged_key,
        code="state_subject_semantic_drift",
        violation_kinds=["wrong_subject"],
        message="来源状态主体归属错误",
    )
    invalid_code_response = json.dumps(
        {
            "findings": [{
                "unit_key": flagged_key,
                "code": "wrong_subject",
                "violation_kinds": ["wrong_subject"],
                "message": "code 使用了 violation kind",
            }],
        },
        ensure_ascii=False,
    )
    truncated_response = (
        '{"findings":[{"unit_key":"'
        + flagged_key
        + '","code":"state_subject_semantic_drift",'
        '"violation_kinds":["wrong_subject"],"message":"'
        + ("截断" * 400)
    )[:625]
    assert len(truncated_response) == 625
    post_repair_calls: list[dict] = []
    review_schema = scene_shards_module._scene_shard_semantic_review_schema(
        list(draft.slots)
    )

    async def fake_chat(messages, **kwargs):
        meta = kwargs["call_meta"]
        if meta["stage_key"] == "screenplay_scene_shard_semantic_repair":
            return ScreenplaySceneShardCreativeIR(slots={
                flagged_key: draft.slots[flagged_key].model_copy(deep=True),
            }).model_dump_json()
        if meta["substage"] == "initial":
            return ScreenplaySceneShardSemanticReview(
                findings=[finding],
            ).model_dump_json()
        if meta["reviewer_no"] != 1:
            return ScreenplaySceneShardSemanticReview(
                findings=[],
            ).model_dump_json()

        post_repair_calls.append({
            "messages": deepcopy(messages),
            "meta": deepcopy(meta),
            "max_tokens": kwargs["max_tokens"],
            "temperature": kwargs["temperature"],
            "output_schema": deepcopy(review_schema),
            "response_format": deepcopy(kwargs["response_format"]),
        })
        return {
            0: invalid_code_response,
            1: truncated_response,
        }.get(
            meta["format_attempt"],
            ScreenplaySceneShardSemanticReview(
                findings=[],
            ).model_dump_json(),
        )

    monkeypatch.setattr(model_gateway, "chat", fake_chat)
    repaired, audit = asyncio.run(_REAL_SEMANTIC_REVIEW(
        draft=draft,
        scene_input_contracts=contracts,
        identity_registry=[],
        operation_id="err-20260816-e8ac9d",
        shard_id=plan.shard_id,
        validate_draft=lambda _candidate: [],
    ))

    assert repaired == draft
    assert [call["meta"]["format_attempt"] for call in post_repair_calls] == [
        0, 1, 2,
    ]
    assert all(
        call["meta"]["semantic_attempt"] == 0
        for call in post_repair_calls
    )
    assert all(
        call["meta"]["reuse_successful_operation"] is True
        for call in post_repair_calls
    )
    base_operation_id = post_repair_calls[0]["meta"]["base_operation_id"]
    assert ":post_repair:reviewer-1:" in base_operation_id
    for call in post_repair_calls:
        meta = call["meta"]
        expected_operation_id = base_operation_id
        if meta["format_attempt"]:
            attempt_identity = evidence_repository.content_hash({
                "base_operation_id": base_operation_id,
                "format_attempt": meta["format_attempt"],
                "semantic_attempt": meta["semantic_attempt"],
                "messages": call["messages"],
                "max_tokens": call["max_tokens"],
                "temperature": call["temperature"],
                "structured_schema": call["output_schema"],
                "response_format": call["response_format"],
                "require_response_format": True,
            })
            expected_operation_id = (
                f"{base_operation_id}:structured-attempt:"
                f"{attempt_identity}"
            )
        assert meta["operation_id"] == expected_operation_id
    assert [phase["phase"] for phase in audit] == [
        "initial",
        "post_repair",
    ]
    assert audit[-1]["consensus"] == []


def test_scene_shard_semantic_post_repair_invalid_format_uses_three_calls_per_reviewer(
    monkeypatch,
) -> None:
    blueprint = _blueprint(split_domain=False)
    plan = build_screenplay_scene_shard_plans(
        blueprint,
        source_text=SOURCE,
        identity_registry_hash="identity-hash",
    )[0]
    contracts = _contracts([plan], blueprint)[plan.shard_id]
    draft = _creative_shard(plan, blueprint, contracts)
    flagged_key = next(iter(draft.slots))
    finding = ScreenplaySceneShardSemanticFinding(
        unit_key=flagged_key,
        code="state_subject_semantic_drift",
        violation_kinds=["wrong_subject"],
        message="持续错误的来源状态主体",
    )
    review_calls: list[dict] = []
    review_token_budgets: list[tuple[int, int]] = []

    async def fake_chat(*_args, **kwargs):
        meta = kwargs["call_meta"]
        if meta["stage_key"] == "screenplay_scene_shard_semantic_repair":
            return ScreenplaySceneShardCreativeIR(slots={
                flagged_key: draft.slots[flagged_key].model_copy(deep=True),
            }).model_dump_json()
        review_calls.append(deepcopy(meta))
        review_token_budgets.append((
            int(kwargs["max_tokens"]),
            int(meta["required"]),
        ))
        await asyncio.sleep(0)
        if meta["substage"] == "initial":
            return ScreenplaySceneShardSemanticReview(
                findings=[finding],
            ).model_dump_json()
        return json.dumps(
            {
                "findings": [{
                    "unit_key": flagged_key,
                    "code": "wrong_subject",
                    "violation_kinds": ["wrong_subject"],
                    "message": "持续 schema invalid",
                }],
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr(model_gateway, "chat", fake_chat)
    with pytest.raises(model_gateway.StructuredFormatError):
        asyncio.run(_REAL_SEMANTIC_REVIEW(
            draft=draft,
            scene_input_contracts=contracts,
            identity_registry=[],
            operation_id="scene-semantic-post-format-invalid",
            shard_id=plan.shard_id,
            validate_draft=lambda _candidate: [],
        ))

    initial_calls = [
        call for call in review_calls
        if call["substage"] == "initial"
    ]
    post_repair_calls = [
        call for call in review_calls
        if call["substage"] == "post_repair"
    ]
    assert len(initial_calls) == 2
    for reviewer_no in (1, 2):
        assert [
            call["format_attempt"]
            for call in post_repair_calls
            if call["reviewer_no"] == reviewer_no
        ] == [0, 1, 2]
    assert all(
        call["semantic_attempt"] == 0
        for call in post_repair_calls
    )
    assert review_token_budgets
    assert all(
        max_tokens == required
        for max_tokens, required in review_token_budgets
    )


def test_scene_shard_semantic_unknown_unit_fails_in_real_structured_validator(
    monkeypatch,
) -> None:
    blueprint = _blueprint(split_domain=False)
    plan = build_screenplay_scene_shard_plans(
        blueprint,
        source_text=SOURCE,
        identity_registry_hash="identity-hash",
    )[0]
    contracts = _contracts([plan], blueprint)[plan.shard_id]
    draft = _creative_shard(plan, blueprint, contracts)
    unknown_review = ScreenplaySceneShardSemanticReview(findings=[
        ScreenplaySceneShardSemanticFinding(
            unit_key="UNKNOWN-SLOT",
            code="source_semantic_drift",
            violation_kinds=["source_contradiction"],
            message="unknown",
        ),
    ])
    calls: list[dict] = []

    async def fake_chat(*_args, **kwargs):
        calls.append(kwargs)
        return unknown_review.model_dump_json()

    monkeypatch.setattr(model_gateway, "chat", fake_chat)
    with pytest.raises(
        model_gateway.StructuredSemanticError,
        match="语义审查引用未知 unit_key：UNKNOWN-SLOT",
    ):
        asyncio.run(_REAL_SEMANTIC_REVIEW(
            draft=draft,
            scene_input_contracts=contracts,
            identity_registry=[],
            operation_id="scene-semantic-real-unknown",
            shard_id=plan.shard_id,
            validate_draft=lambda _candidate: [],
        ))
    assert calls
    known_unit_keys = list(draft.slots)
    for call in calls:
        assert call["call_meta"]["response_format_required"] is True
        response_format = call["response_format"]
        assert response_format["type"] == "json_schema"
        assert response_format["json_schema"]["strict"] is True
        finding_properties = response_format["json_schema"]["schema"][
            "$defs"
        ]["ScreenplaySceneShardSemanticFinding"]["properties"]
        assert finding_properties["unit_key"]["enum"] == known_unit_keys
        assert "UNKNOWN-SLOT" not in finding_properties["unit_key"]["enum"]


def test_scene_shard_semantic_repair_overreach_fails_in_real_validator(
    monkeypatch,
) -> None:
    blueprint = _blueprint(split_domain=False)
    plan = build_screenplay_scene_shard_plans(
        blueprint,
        source_text=SOURCE,
        identity_registry_hash="identity-hash",
    )[0]
    contracts = _contracts([plan], blueprint)[plan.shard_id]
    draft = _creative_shard(plan, blueprint, contracts)
    unit_keys = list(draft.slots)
    finding = ScreenplaySceneShardSemanticFinding(
        unit_key=unit_keys[0],
        code="source_semantic_drift",
        violation_kinds=["source_contradiction"],
        message="source drift",
    )
    flagged_slot = draft.slots[unit_keys[0]].model_copy(deep=True)
    flagged_slot.text = "甲推门进入。"
    unflagged_slot = draft.slots[unit_keys[1]].model_copy(deep=True)
    unflagged_slot.text = "越权修改"
    overreaching = ScreenplaySceneShardCreativeIR(slots={
        unit_keys[0]: flagged_slot,
        unit_keys[1]: unflagged_slot,
    })
    repair_attempts = 0
    business_validations: list[ScreenplaySceneShardCreativeIR] = []

    async def fake_chat(*_args, **kwargs):
        nonlocal repair_attempts
        meta = kwargs["call_meta"]
        if meta["stage_key"] == "screenplay_scene_shard_semantic_review":
            if meta["substage"] != "initial":
                raise AssertionError("invalid repair must not reach post review")
            return ScreenplaySceneShardSemanticReview(
                findings=[finding],
            ).model_dump_json()
        repair_attempts += 1
        return overreaching.model_dump_json()

    def validate_draft(
        candidate: ScreenplaySceneShardCreativeIR,
    ) -> list[str]:
        business_validations.append(candidate)
        return []

    monkeypatch.setattr(model_gateway, "chat", fake_chat)
    with pytest.raises(
        model_gateway.StructuredSemanticError,
        match="语义 repair subset slots 必须完全等于 consensus 标记集合",
    ):
        asyncio.run(_REAL_SEMANTIC_REVIEW(
            draft=draft,
            scene_input_contracts=contracts,
            identity_registry=[],
            operation_id="scene-semantic-real-overreach",
            shard_id=plan.shard_id,
            validate_draft=validate_draft,
        ))

    assert repair_attempts == 2
    assert business_validations == []


def test_scene_shard_semantic_retry_keeps_authority_and_rejects_fabrication(
    monkeypatch,
) -> None:
    blueprint = _blueprint(split_domain=False)
    plan = build_screenplay_scene_shard_plans(
        blueprint,
        source_text=SOURCE,
        identity_registry_hash="identity-hash",
    )[0]
    contracts = _contracts([plan], blueprint)[plan.shard_id]
    draft = _creative_shard(plan, blueprint, contracts)
    unit_keys = list(draft.slots)
    flagged_key = unit_keys[0]
    finding = ScreenplaySceneShardSemanticFinding(
        unit_key=flagged_key,
        code="source_semantic_drift",
        violation_kinds=["source_contradiction"],
        message="creative 与冻结来源事实冲突",
    )
    repair_messages: list[list[dict[str, str]]] = []
    post_review_calls = 0

    async def fake_chat(messages, **kwargs):
        nonlocal post_review_calls
        meta = kwargs["call_meta"]
        if meta["stage_key"] == "screenplay_scene_shard_semantic_review":
            if meta["substage"] == "post_repair":
                post_review_calls += 1
                assert "乙凭空改写来源并杀死甲" in messages[1]["content"]
            return ScreenplaySceneShardSemanticReview(
                findings=[finding],
            ).model_dump_json()

        repair_messages.append(messages)
        flagged_slot = draft.slots[flagged_key].model_copy(deep=True)
        if meta["semantic_attempt"] == 0:
            unflagged_slot = draft.slots[unit_keys[1]].model_copy(deep=True)
            unflagged_slot.text = "越权改写未标记 slot"
            candidate = ScreenplaySceneShardCreativeIR(slots={
                flagged_key: flagged_slot,
                unit_keys[1]: unflagged_slot,
            })
        else:
            flagged_slot.text = "乙凭空改写来源并杀死甲"
            candidate = ScreenplaySceneShardCreativeIR(slots={
                flagged_key: flagged_slot,
            })
        return candidate.model_dump_json()

    monkeypatch.setattr(model_gateway, "chat", fake_chat)
    with pytest.raises(
        ScreenplaySceneShardError,
        match="semantic gate 未收口",
    ):
        asyncio.run(_REAL_SEMANTIC_REVIEW(
            draft=draft,
            scene_input_contracts=contracts,
            identity_registry=[
                {
                    "identity_key": f"person_{label}",
                    "authority_id": f"bible:{label}",
                    "canonical_name": label,
                    "source_labels": [label],
                }
                for label in ("甲", "乙")
            ],
            operation_id="scene-semantic-authority-retry",
            shard_id=plan.shard_id,
            validate_draft=lambda _candidate: [],
        ))

    assert len(repair_messages) == 6
    assert [message["role"] for message in repair_messages[1]] == ["user"]
    retry_prompt = repair_messages[1][0]["content"]
    assert '"consensus_findings":' in retry_prompt
    assert '"frozen_slots":' in retry_prompt
    assert '"source_fact":' in retry_prompt
    assert '"current_flagged_creative":' in retry_prompt
    assert unit_keys[1] in retry_prompt
    assert draft.slots[flagged_key].text in retry_prompt
    assert post_review_calls == 6


def _identities() -> list[IRIdentity]:
    return [IRIdentity(
        key="narrator",
        display_name="旁白",
        authority_id="narrator:narrator",
        kind="narrator",
        visual_policy="offscreen_only",
        asset_requirement="forbidden",
        role_type="narrator",
    ), *[
        IRIdentity(
            key=f"person_{label}",
            display_name=label,
            authority_id=f"bible:{label}",
            kind="source_character",
            visual_policy="canonical",
            asset_requirement="required",
            role_type="supporting",
        )
        for label in ("甲", "乙")
    ]]


def _shard(
    plan,
    blueprint: NarrativeBlueprint,
    identity_registry: list[dict] | None = None,
) -> ScreenplaySceneShardIR:
    contracts = _contracts(
        [plan],
        blueprint,
        identity_registry,
    )[plan.shard_id]
    return scene_shards_module.compile_screenplay_scene_shard_draft(
        _creative_shard(plan, blueprint, contracts),
        episode_no=1,
        plan=plan,
        scene_plans={
            scene.key: scene for scene in blueprint.scene_plans
        },
        scene_input_contracts=contracts,
    )


def _creative_shard(
    plan,
    blueprint: NarrativeBlueprint,
    contracts: list[ScreenplaySceneInputContract] | None = None,
) -> ScreenplaySceneShardCreativeIR:
    del blueprint, contracts

    return ScreenplaySceneShardCreativeIR.model_validate({
        "slots": {
            slot.unit_key: {
                "text": (
                    slot.source_text
                    if slot.kind == "dialogue"
                    else f"交付 {slot.source_segment_ids[0]}"
                ),
                "resulting_state": (
                    f"完成 {slot.source_segment_ids[0]}"
                ),
            }
            for slot in plan.unit_slots
        },
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
        identity_registry=identity_registry or [
            {
                "identity_key": f"person_{label}",
                "authority_id": f"bible:{label}",
                "canonical_name": label,
                "source_labels": [label],
            }
            for label in ("甲", "乙")
        ],
    )


def test_scene_shard_batch_failure_cancels_inflight_sibling(
    monkeypatch,
) -> None:
    blueprint = _blueprint(split_domain=True)
    plans = build_screenplay_scene_shard_plans(
        blueprint,
        source_text=SOURCE,
        identity_registry_hash="identity-hash",
    )
    contracts = _contracts(plans, blueprint)
    assert len(plans) == 2
    sibling_started = asyncio.Event()
    sibling_cancelled = asyncio.Event()
    provider_calls: list[str] = []
    review_calls: list[str] = []
    progress_rows: list[list[dict]] = []
    # A delivered failure, so the batch-cancellation semantics under test are
    # not entangled with the bounded retry for answers never delivered.
    original_error = scene_shards_module.hiagent.ProviderError(
        "injected shard provider failure",
        retryable=True,
        failure_kind="request_outcome_unknown",
        delivery_state="unknown",
        requires_explicit_retry=True,
        received_chars=1200,
    )

    def fixed_settings(
        key: str,
        default: int,
        *,
        minimum: int,
        maximum: int,
    ) -> int:
        value = 2 if key == "screenplay_scene_shard_parallelism" else default
        return max(minimum, min(maximum, value))

    async def fake_structured(*_args, **kwargs):
        shard_id = str(kwargs["call_meta"]["shard_id"])
        provider_calls.append(shard_id)
        if shard_id == plans[0].shard_id:
            await sibling_started.wait()
            raise original_error
        sibling_started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            sibling_cancelled.set()
            raise

    async def forbidden_review(*, shard_id: str, **_kwargs):
        review_calls.append(shard_id)
        raise AssertionError("failed batch must not enter semantic review")

    monkeypatch.setattr(scene_shards_module, "_setting_int", fixed_settings)
    monkeypatch.setattr(
        scene_shards_module.model_gateway,
        "chat_structured",
        fake_structured,
    )
    monkeypatch.setattr(
        scene_shards_module,
        "_semantic_review_scene_shard_draft",
        forbidden_review,
    )

    with pytest.raises(scene_shards_module.hiagent.ProviderError) as caught:
        asyncio.run(generate_screenplay_scene_shards(
            episode={"id": "ep-shard-fail-fast", "episode_no": 1},
            source_text=SOURCE,
            blueprint=blueprint,
            identity_registry=[],
            identities=_identities(),
            plans=plans,
            scene_input_contracts=contracts,
            progress=lambda rows: progress_rows.append(deepcopy(rows)),
        ))

    assert caught.value is original_error
    assert sibling_cancelled.is_set()
    assert provider_calls == [plan.shard_id for plan in plans]
    assert review_calls == []
    latest_rows = {
        row["shard_id"]: row for row in progress_rows[-1]
    }
    assert latest_rows[plans[0].shard_id]["status"] == "failed"
    assert latest_rows[plans[0].shard_id]["error_type"] == "ProviderError"
    assert latest_rows[plans[1].shard_id]["status"] == "running"
    assert "error_type" not in latest_rows[plans[1].shard_id]


def test_scene_shard_failure_fences_semaphore_waiter_before_provider(
    monkeypatch,
) -> None:
    blueprint = _blueprint(split_domain=True)
    plans = build_screenplay_scene_shard_plans(
        blueprint,
        source_text=SOURCE,
        identity_registry_hash="identity-hash",
    )
    contracts = _contracts(plans, blueprint)
    provider_calls: list[str] = []
    review_calls: list[str] = []
    progress_rows: list[list[dict]] = []
    # A delivered failure, so the queue-fencing semantics under test are not
    # entangled with the bounded retry for answers never delivered.
    original_error = scene_shards_module.hiagent.ProviderError(
        "injected queued-shard fence failure",
        retryable=True,
        failure_kind="request_outcome_unknown",
        delivery_state="unknown",
        requires_explicit_retry=True,
        received_chars=1200,
    )

    def fixed_settings(
        key: str,
        default: int,
        *,
        minimum: int,
        maximum: int,
    ) -> int:
        value = 1 if key == "screenplay_scene_shard_parallelism" else default
        return max(minimum, min(maximum, value))

    async def failing_structured(*_args, **kwargs):
        provider_calls.append(str(kwargs["call_meta"]["shard_id"]))
        raise original_error

    async def forbidden_review(*, shard_id: str, **_kwargs):
        review_calls.append(shard_id)
        raise AssertionError("failed batch must not enter semantic review")

    monkeypatch.setattr(scene_shards_module, "_setting_int", fixed_settings)
    monkeypatch.setattr(
        scene_shards_module.model_gateway,
        "chat_structured",
        failing_structured,
    )
    monkeypatch.setattr(
        scene_shards_module,
        "_semantic_review_scene_shard_draft",
        forbidden_review,
    )

    with pytest.raises(scene_shards_module.hiagent.ProviderError) as caught:
        asyncio.run(generate_screenplay_scene_shards(
            episode={"id": "ep-shard-queued-fence", "episode_no": 1},
            source_text=SOURCE,
            blueprint=blueprint,
            identity_registry=[],
            identities=_identities(),
            plans=plans,
            scene_input_contracts=contracts,
            progress=lambda rows: progress_rows.append(deepcopy(rows)),
        ))

    assert caught.value is original_error
    assert provider_calls == [plans[0].shard_id]
    assert review_calls == []
    latest_rows = {
        row["shard_id"]: row for row in progress_rows[-1]
    }
    assert latest_rows[plans[0].shard_id]["status"] == "failed"
    assert latest_rows[plans[1].shard_id]["status"] == "pending"
    assert "error_type" not in latest_rows[plans[1].shard_id]


@pytest.mark.parametrize("failure_kind", ["provider", "local_validation"])
def test_scene_shard_structured_lease_fences_real_provider_waiter(
    monkeypatch,
    failure_kind: str,
) -> None:
    blueprint = _blueprint(split_domain=True)
    plans = build_screenplay_scene_shard_plans(
        blueprint,
        source_text=SOURCE,
        identity_registry_hash="identity-hash",
    )
    contracts = _contracts(plans, blueprint)
    provider_calls: list[str] = []
    original_error = scene_shards_module.hiagent.ProviderError(
        "63178 output truncated",
        failure_kind="output_truncated",
    )

    def fixed_settings(
        key: str,
        default: int,
        *,
        minimum: int,
        maximum: int,
    ) -> int:
        values = {
            "screenplay_scene_shard_parallelism": 2,
            "screenplay_format_retry_limit": 0,
            "screenplay_semantic_retry_limit": 0,
        }
        return max(minimum, min(maximum, values.get(key, default)))

    monkeypatch.setattr(scene_shards_module, "_setting_int", fixed_settings)
    monkeypatch.setattr(
        generation_concurrency,
        "get_setting",
        lambda key: "1" if key == "text_generation_concurrency" else None,
    )

    async def wait_for_queued_peer() -> None:
        gate = generation_concurrency.gate_for("text_provider_calls")
        while not gate.waiters:
            await asyncio.sleep(0)

    async def fake_chat(*_args, **kwargs):
        meta = kwargs["call_meta"]
        provider_calls.append(str(meta["shard_id"]))
        assert meta["response_format_required"] is True
        assert kwargs["response_format"]["type"] == "json_schema"
        await asyncio.wait_for(wait_for_queued_peer(), timeout=1)
        if failure_kind == "provider":
            raise original_error
        return ScreenplaySceneShardCreativeIR(slots={}).model_dump_json()

    monkeypatch.setattr(model_gateway, "chat", fake_chat)
    expected_error = (
        scene_shards_module.hiagent.ProviderError
        if failure_kind == "provider"
        else model_gateway.StructuredSemanticError
    )
    with pytest.raises(expected_error) as caught:
        asyncio.run(generate_screenplay_scene_shards(
            episode={
                "id": f"ep-attempt8-gate-{failure_kind}-{uuid.uuid4()}",
                "episode_no": 1,
            },
            source_text=SOURCE,
            blueprint=blueprint,
            identity_registry=[],
            identities=_identities(),
            plans=plans,
            scene_input_contracts=contracts,
        ))

    if failure_kind == "provider":
        assert caught.value is original_error
    assert provider_calls == [plans[0].shard_id]


@pytest.mark.parametrize("failure_kind", ["provider", "local_validation"])
def test_semantic_repair_gate_fences_queued_next_scene_ledger(
    monkeypatch,
    failure_kind: str,
) -> None:
    monkeypatch.setattr(
        generation_concurrency,
        "get_setting",
        lambda key: (
            "1" if key == "text_generation_concurrency" else None
        ),
    )

    async def scenario() -> tuple[
        BaseException,
        BaseException,
        BaseException,
        list[str],
        bool,
    ]:
        batch_abort = asyncio.Event()
        gate = scene_shards_module._SceneStructuredOperationGate(
            batch_abort
        )
        repair_started = asyncio.Event()
        release_repair = asyncio.Event()
        ledger: list[str] = []
        if failure_kind == "provider":
            original: BaseException = scene_shards_module.hiagent.ProviderError(
                "semantic repair provider failed",
                failure_kind="request_outcome_unknown",
                delivery_state="unknown",
                requires_explicit_retry=True,
            )
        else:
            original = model_gateway.StructuredSemanticError(
                "semantic repair local validation failed"
            )

        async def repair_operation() -> None:
            ledger.append("repair-provider")
            repair_started.set()
            await release_repair.wait()
            raise original

        async def next_scene_operation() -> None:
            ledger.append("next-scene-provider")

        repair_task = asyncio.create_task(gate.run(repair_operation))
        await asyncio.wait_for(repair_started.wait(), timeout=1)
        next_scene_task = asyncio.create_task(
            gate.run(next_scene_operation)
        )
        provider_gate = generation_concurrency.gate_for(
            "text_provider_calls"
        )
        while not provider_gate.waiters:
            await asyncio.sleep(0)

        release_repair.set()
        repair_result, next_scene_result = await asyncio.gather(
            repair_task,
            next_scene_task,
            return_exceptions=True,
        )
        return (
            original,
            repair_result,
            next_scene_result,
            ledger,
            batch_abort.is_set(),
        )

    original, repair_result, next_scene_result, ledger, aborted = (
        asyncio.run(scenario())
    )
    assert repair_result is original
    assert isinstance(next_scene_result, asyncio.CancelledError)
    assert ledger == ["repair-provider"]
    assert aborted is True


def test_scene_shard_failure_cancels_peer_before_structured_retry(
    monkeypatch,
) -> None:
    blueprint = _blueprint(split_domain=True)
    plans = build_screenplay_scene_shard_plans(
        blueprint,
        source_text=SOURCE,
        identity_registry_hash="identity-hash",
    )
    contracts = _contracts(plans, blueprint)
    sibling_started = asyncio.Event()
    release_sibling = asyncio.Event()
    sibling_cancelled = asyncio.Event()
    sibling_attempts: list[int] = []
    # A delivered failure, so the cancellation semantics under test are
    # not entangled with the bounded retry for undelivered answers.
    original_error = scene_shards_module.hiagent.ProviderError(
        "injected peer structured failure",
        retryable=True,
        failure_kind="request_outcome_unknown",
        delivery_state="unknown",
        requires_explicit_retry=True,
        received_chars=1200,
    )

    def fixed_settings(
        key: str,
        default: int,
        *,
        minimum: int,
        maximum: int,
    ) -> int:
        value = 2 if key == "screenplay_scene_shard_parallelism" else default
        return max(minimum, min(maximum, value))

    async def fake_chat(*_args, **kwargs):
        meta = kwargs["call_meta"]
        shard_id = str(meta["shard_id"])
        if shard_id == plans[0].shard_id:
            await sibling_started.wait()
            release_sibling.set()
            raise original_error
        sibling_attempts.append(int(meta["format_attempt"]))
        if len(sibling_attempts) > 1:
            raise AssertionError(
                "peer entered structured retry after batch failure"
            )
        sibling_started.set()
        try:
            await release_sibling.wait()
        except asyncio.CancelledError:
            sibling_cancelled.set()
            raise
        return "not valid JSON"

    monkeypatch.setattr(scene_shards_module, "_setting_int", fixed_settings)
    monkeypatch.setattr(model_gateway, "chat", fake_chat)

    with pytest.raises(scene_shards_module.hiagent.ProviderError) as caught:
        asyncio.run(generate_screenplay_scene_shards(
            episode={"id": "ep-shard-structured-retry-fence", "episode_no": 1},
            source_text=SOURCE,
            blueprint=blueprint,
            identity_registry=[],
            identities=_identities(),
            plans=plans,
            scene_input_contracts=contracts,
        ))

    assert caught.value is original_error
    assert sibling_attempts == [0]
    assert sibling_cancelled.is_set()


def test_nested_reviewer_failure_cancels_other_shard_structured_retry(
    monkeypatch,
) -> None:
    blueprint = _blueprint(split_domain=True)
    plans = build_screenplay_scene_shard_plans(
        blueprint,
        source_text=SOURCE,
        identity_registry_hash="identity-hash",
    )
    contracts = _contracts(plans, blueprint)
    sibling_started = asyncio.Event()
    release_sibling = asyncio.Event()
    sibling_cancelled = asyncio.Event()
    sibling_attempts: list[int] = []
    # A delivered failure, so the cancellation semantics under test are
    # not entangled with the bounded retry for undelivered answers.
    original_error = scene_shards_module.hiagent.ProviderError(
        "injected nested reviewer failure",
        retryable=True,
        failure_kind="request_outcome_unknown",
        delivery_state="unknown",
        requires_explicit_retry=True,
        received_chars=1200,
    )

    def fixed_settings(
        key: str,
        default: int,
        *,
        minimum: int,
        maximum: int,
    ) -> int:
        value = 2 if key == "screenplay_scene_shard_parallelism" else default
        return max(minimum, min(maximum, value))

    async def fake_chat(*_args, **kwargs):
        meta = kwargs["call_meta"]
        shard_id = str(meta["shard_id"])
        stage_key = str(meta["stage_key"])
        if stage_key == "screenplay_scene_shard_semantic_review":
            if int(meta["reviewer_no"]) == 1:
                await sibling_started.wait()
                release_sibling.set()
                raise original_error
            await asyncio.Future()
        if shard_id == plans[0].shard_id:
            return _creative_shard(plans[0], blueprint).model_dump_json()
        sibling_attempts.append(int(meta["format_attempt"]))
        if len(sibling_attempts) > 1:
            raise AssertionError(
                "other shard retried after nested reviewer failure"
            )
        sibling_started.set()
        try:
            await release_sibling.wait()
        except asyncio.CancelledError:
            sibling_cancelled.set()
            raise
        return "not valid JSON"

    monkeypatch.setattr(scene_shards_module, "_setting_int", fixed_settings)
    monkeypatch.setattr(
        scene_shards_module,
        "_semantic_review_scene_shard_draft",
        _REAL_SEMANTIC_REVIEW,
    )
    monkeypatch.setattr(model_gateway, "chat", fake_chat)

    with pytest.raises(scene_shards_module.hiagent.ProviderError) as caught:
        asyncio.run(generate_screenplay_scene_shards(
            episode={"id": "ep-nested-reviewer-retry-fence", "episode_no": 1},
            source_text=SOURCE,
            blueprint=blueprint,
            identity_registry=[],
            identities=_identities(),
            plans=plans,
            scene_input_contracts=contracts,
        ))

    assert caught.value is original_error
    assert sibling_attempts == [0]
    assert sibling_cancelled.is_set()


def test_scene_shard_batch_user_cancellation_does_not_mark_failed(
    monkeypatch,
) -> None:
    blueprint = _blueprint(split_domain=True)
    plans = build_screenplay_scene_shard_plans(
        blueprint,
        source_text=SOURCE,
        identity_registry_hash="identity-hash",
    )
    contracts = _contracts(plans, blueprint)
    all_started = asyncio.Event()
    started: set[str] = set()
    cancelled: set[str] = set()
    progress_rows: list[list[dict]] = []

    def fixed_settings(
        key: str,
        default: int,
        *,
        minimum: int,
        maximum: int,
    ) -> int:
        value = 2 if key == "screenplay_scene_shard_parallelism" else default
        return max(minimum, min(maximum, value))

    async def blocking_structured(*_args, **kwargs):
        shard_id = str(kwargs["call_meta"]["shard_id"])
        started.add(shard_id)
        if len(started) == len(plans):
            all_started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancelled.add(shard_id)
            raise

    monkeypatch.setattr(scene_shards_module, "_setting_int", fixed_settings)
    monkeypatch.setattr(
        scene_shards_module.model_gateway,
        "chat_structured",
        blocking_structured,
    )

    async def run_and_cancel() -> None:
        task = asyncio.create_task(generate_screenplay_scene_shards(
            episode={"id": "ep-shard-user-cancel", "episode_no": 1},
            source_text=SOURCE,
            blueprint=blueprint,
            identity_registry=[],
            identities=_identities(),
            plans=plans,
            scene_input_contracts=contracts,
            progress=lambda rows: progress_rows.append(deepcopy(rows)),
        ))
        await asyncio.wait_for(all_started.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run_and_cancel())

    assert started == {plan.shard_id for plan in plans}
    assert cancelled == started
    assert progress_rows
    assert all(
        row["status"] != "failed"
        and "error_type" not in row
        for row in progress_rows[-1]
    )


def test_scene_shard_batch_success_preserves_plan_order(monkeypatch) -> None:
    blueprint = _blueprint(split_domain=True)
    plans = build_screenplay_scene_shard_plans(
        blueprint,
        source_text=SOURCE,
        identity_registry_hash="identity-hash",
    )
    contracts = _contracts(plans, blueprint)

    async def successful_structured(*_args, **kwargs):
        shard_id = str(kwargs["call_meta"]["shard_id"])
        plan = next(item for item in plans if item.shard_id == shard_id)
        if plan is plans[0]:
            await asyncio.sleep(0.01)
        return _creative_shard(plan, blueprint)

    monkeypatch.setattr(
        scene_shards_module.model_gateway,
        "chat_structured",
        successful_structured,
    )

    shards, artifact_ids, rows = asyncio.run(
        generate_screenplay_scene_shards(
            episode={"id": "ep-shard-success-order", "episode_no": 1},
            source_text=SOURCE,
            blueprint=blueprint,
            identity_registry=[],
            identities=_identities(),
            plans=plans,
            scene_input_contracts=contracts,
        )
    )

    assert [shard.shard_id for shard in shards] == [
        plan.shard_id for plan in plans
    ]
    assert len(artifact_ids) == len(plans)
    assert [row["shard_id"] for row in rows] == [
        plan.shard_id for plan in plans
    ]
    assert all(row["status"] == "validated" for row in rows)


def test_semantic_reviewer_failure_cancels_other_reviewer(
    monkeypatch,
) -> None:
    blueprint = _blueprint(split_domain=False)
    plan = build_screenplay_scene_shard_plans(
        blueprint,
        source_text=SOURCE,
        identity_registry_hash="identity-hash",
    )[0]
    contracts = _contracts([plan], blueprint)[plan.shard_id]
    reviewer_two_started = asyncio.Event()
    reviewer_two_cancelled = asyncio.Event()
    reviewer_calls: list[int] = []
    original_error = RuntimeError("injected reviewer failure")

    async def fake_review(*_args, **kwargs):
        reviewer_no = int(kwargs["call_meta"]["reviewer_no"])
        reviewer_calls.append(reviewer_no)
        if reviewer_no == 1:
            await reviewer_two_started.wait()
            raise original_error
        reviewer_two_started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            reviewer_two_cancelled.set()
            raise

    monkeypatch.setattr(
        scene_shards_module.model_gateway,
        "chat_structured",
        fake_review,
    )

    with pytest.raises(RuntimeError) as caught:
        asyncio.run(_REAL_SEMANTIC_REVIEW(
            draft=_creative_shard(plan, blueprint, contracts),
            scene_input_contracts=contracts,
            identity_registry=[],
            operation_id="semantic-review-fail-fast",
            shard_id=plan.shard_id,
            validate_draft=lambda _candidate: [],
        ))

    assert caught.value is original_error
    assert reviewer_two_cancelled.is_set()
    assert sorted(reviewer_calls) == [1, 2]


def test_semantic_reviewer_failure_fences_next_chunk(monkeypatch) -> None:
    blueprint = _blueprint(split_domain=False)
    plan = build_screenplay_scene_shard_plans(
        blueprint,
        source_text=SOURCE,
        identity_registry_hash="identity-hash",
    )[0]
    contracts = _contracts([plan], blueprint)[plan.shard_id]
    draft = _creative_shard(plan, blueprint, contracts)
    chunks = scene_shards_module._scene_shard_semantic_review_chunks(
        draft=draft,
        scene_input_contracts=contracts,
        identity_registry=[],
        shard_id=plan.shard_id,
    )
    assert chunks
    two_chunks = [
        deepcopy(chunks[0]),
        {
            **deepcopy(chunks[0]),
            "chunk_hash": "injected-second-review-chunk",
        },
    ]
    reviewer_two_finished = asyncio.Event()
    reviewer_calls: list[tuple[int, int]] = []
    original_error = RuntimeError("injected first-chunk reviewer failure")

    async def fake_review(*_args, **kwargs):
        meta = kwargs["call_meta"]
        call = (int(meta["reviewer_no"]), int(meta["chunk_index"]))
        reviewer_calls.append(call)
        if call == (1, 1):
            await reviewer_two_finished.wait()
            raise original_error
        if call == (2, 1):
            reviewer_two_finished.set()
            return ScreenplaySceneShardSemanticReview(findings=[])
        raise AssertionError("reviewer advanced after peer failure")

    monkeypatch.setattr(
        scene_shards_module,
        "_scene_shard_semantic_review_chunks",
        lambda **_kwargs: two_chunks,
    )
    monkeypatch.setattr(
        scene_shards_module.model_gateway,
        "chat_structured",
        fake_review,
    )

    with pytest.raises(RuntimeError) as caught:
        asyncio.run(_REAL_SEMANTIC_REVIEW(
            draft=draft,
            scene_input_contracts=contracts,
            identity_registry=[],
            operation_id="semantic-review-next-chunk-fence",
            shard_id=plan.shard_id,
            validate_draft=lambda _candidate: [],
        ))

    assert caught.value is original_error
    assert reviewer_calls == [(1, 1), (2, 1)]


def _a78_replay_models(
    *,
    creative_update: dict | None = None,
    bind_actor: bool = False,
):
    replay = json.loads(A78_ARTIFACT_REPLAY.read_text(encoding="utf-8"))
    plan = ScreenplaySceneShardPlan.model_validate(replay["plan"])
    scene_plan_payload = deepcopy(replay["scene_plan"])
    scene_plan_payload["source_semantics"] = _story_source_semantics(
        scene_plan_payload["source_segment_ids"]
    )
    scene_plan = BlueprintScenePlan.model_validate(scene_plan_payload)
    contract_payload = deepcopy(replay["scene_input_contract"])
    contract_payload["source_semantics"] = _story_source_semantics(
        contract_payload["source_segment_ids"]
    )
    if bind_actor:
        slot = contract_payload["unit_slots"][0]
        slot["actor_keys"] = ["person_8ff1cb1a5861"]
        slot["onscreen_entity_keys"] = ["person_8ff1cb1a5861"]
        slot["action_agency"] = {
            "kind": "character",
            "identity_bearing": True,
            "source_segment_ids": ["SRC0056"],
        }
    contract = ScreenplaySceneInputContract.model_validate(contract_payload)
    recorded_slot = replay["provider_creative_response"]["slots"][
        plan.unit_slots[0].unit_key
    ]
    creative_slot = {
        "text": recorded_slot["text"],
        "performance": recorded_slot["performance"],
        "resulting_state": recorded_slot["resulting_state"],
        "function": recorded_slot["function"],
    }
    creative_slot.update(creative_update or {})
    creative_payload = {
        "contract_version": SCREENPLAY_SCENE_CREATIVE_VERSION,
        "slots": {plan.unit_slots[0].unit_key: creative_slot},
    }
    creative = ScreenplaySceneShardCreativeIR.model_validate(creative_payload)
    return replay, plan, scene_plan, contract, creative


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
    blueprint.nodes[0].participant_evidence = [
        NarrativeParticipantEvidence(
            identity_key=identity_key,
            source_segment_ids=["SRC0001"],
            usage="visible",
        )
        for identity_key in first_participants
    ] + [NarrativeParticipantEvidence(
        identity_key="甲",
        source_segment_ids=["SRC0001"],
        source_unit_keys=["SRC0001:unit:001"],
        usage="state_subject",
    )]
    blueprint.nodes[1].participants = second_participants
    blueprint.nodes[1].participant_evidence = [
        NarrativeParticipantEvidence(
            identity_key=identity_key,
            source_segment_ids=["SRC0002"],
            usage="visible",
        )
        for identity_key in second_participants
    ] + [NarrativeParticipantEvidence(
        identity_key="乙",
        source_segment_ids=["SRC0002"],
        source_unit_keys=["SRC0002:unit:001"],
        usage="state_subject",
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
    blueprint.nodes[0].participant_evidence = [
        NarrativeParticipantEvidence(
            identity_key="甲",
            source_segment_ids=["SRC0001"],
            source_unit_keys=["SRC0001:unit:001"],
            usage="state_subject",
        ),
        NarrativeParticipantEvidence(
            identity_key="甲",
            source_segment_ids=["SRC0001"],
            source_unit_keys=["SRC0001:unit:001"],
            usage="visible",
        ),
    ]
    blueprint.nodes[1].participant_evidence = [
        NarrativeParticipantEvidence(
            identity_key="乙",
            source_segment_ids=["SRC0002"],
            source_unit_keys=["SRC0002:unit:001"],
            usage="state_subject",
        ),
        NarrativeParticipantEvidence(
            identity_key="乙",
            source_segment_ids=["SRC0002"],
            source_unit_keys=["SRC0002:unit:001"],
            usage="visible",
        ),
    ]
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
        blueprint_nodes=blueprint.nodes,
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


def test_written_quote_keeps_owner_out_of_executable_scene_identity() -> None:
    source_text = (
        "孟浩翻开小册子。“人当有靠山。”"
        "这是小册子里的开卷语，落款是靠山老祖。"
    )
    blueprint = NarrativeBlueprint.model_validate({
        "episode_no": 1,
        "nodes": [{
            "key": "n1",
            "source_segment_ids": ["SRC0001"],
            "summary": "孟浩阅读凝气卷开卷语",
            "narrative_layer": "story",
            "event_priority": "causal",
            "render_policy": "standalone",
            "temporal_domain_key": "present",
            "time_label": "午后",
            "time_relation": "episode_start",
            "location_key": "room",
            "location_label": "杂役房",
            "participants": ["孟浩", "靠山老祖"],
            "participant_evidence": [
                {
                    "identity_key": "孟浩",
                    "source_segment_ids": ["SRC0001"],
                    "usage": "visible",
                },
                *[
                    {
                        "identity_key": "孟浩",
                        "source_segment_ids": ["SRC0001"],
                        "source_unit_keys": [fact.source_unit_key],
                        "usage": "state_subject",
                    }
                    for fact in source_segment_facts("SRC0001", source_text)
                    if fact.projection == "action"
                ],
                    {
                        "identity_key": "孟浩",
                        "source_segment_ids": ["SRC0001"],
                        "source_unit_keys": ["SRC0001:unit:002"],
                        "usage": "state_subject",
                    },
                {
                    "identity_key": "靠山老祖",
                    "source_segment_ids": ["SRC0001"],
                    "source_unit_keys": ["SRC0001:unit:002"],
                    "usage": "mentioned",
                },
            ],
            "source_unit_deliveries": [{
                "source_unit_key": "SRC0001:unit:002",
                "mode": "written_text",
                "content_owner_key": "靠山老祖",
            }],
            "action_logic": "孟浩从书页读取开卷语并理解其含义",
        }],
    })

    assert blueprint_voice_identity_issues(blueprint, source_text) == []
    identity_registry = [
        {
            "identity_key": "person_menghao",
            "authority_id": "bible:孟浩",
            "canonical_name": "孟浩",
            "source_labels": ["孟浩"],
        },
        {
            "identity_key": "person_ancestor",
            "authority_id": "reference:kaoshan-ancestor",
            "canonical_name": "靠山老祖",
            "source_labels": ["靠山老祖"],
        },
    ]
    plans = build_screenplay_scene_shard_plans(
        blueprint,
        source_text=source_text,
        identity_registry_hash="identity-hash",
        identity_registry=identity_registry,
    )
    plan = plans[0]
    quoted_slot = next(
        slot for slot in plan.unit_slots
        if slot.source_surface == "quoted_span"
    )
    assert quoted_slot.kind == "action"
    assert quoted_slot.delivery_mode == "written_text"
    assert quoted_slot.content_owner_key == "person_ancestor"

    contracts = build_screenplay_scene_input_contracts(
        plan=plan,
        scene_plans=blueprint.scene_plans,
        source_by_id={"SRC0001": source_text},
        identity_registry=identity_registry,
        blueprint_nodes=blueprint.nodes,
    )
    compiled = compile_screenplay_scene_shard_draft(
        ScreenplaySceneShardCreativeIR(
            slots={
                slot.unit_key: ScreenplaySceneShardCreativeUnit(
                    text="孟浩阅读书页内容。"
                )
                for slot in plan.unit_slots
            },
        ),
        episode_no=1,
        plan=plan,
        scene_plans={
            scene.key: scene for scene in blueprint.scene_plans
        },
        scene_input_contracts=contracts,
    )
    written_unit = next(
        unit for unit in compiled.scenes[0].units
        if unit.unit_key == quoted_slot.unit_key
    )

    assert written_unit.kind == "action"
    assert written_unit.speaker_key is None
    assert written_unit.required_text == "人当有靠山。"
    assert written_unit.text_provenance.kind == "required_text"
    assert written_unit.text_provenance.content_owner_keys == [
        "person_ancestor"
    ]


def test_unspoken_quote_owner_becomes_exact_single_state_subject() -> None:
    source_text = (
        "萧炎微笑着安慰父亲。"
        "“一年四段，换作以前或许可能，现在却没有机会。”"
        "他心中自嘲地苦笑。"
    )
    facts = source_segment_facts("SRC0001", source_text)
    quoted_key = next(
        fact.source_unit_key
        for fact in facts
        if fact.projection == "quoted"
    )
    blueprint = NarrativeBlueprint.model_validate({
        "episode_no": 1,
        "nodes": [{
            "key": "n1",
            "source_segment_ids": ["SRC0001"],
            "summary": "萧炎表面安慰父亲，心中判断希望渺茫",
            "narrative_layer": "story",
            "event_priority": "causal",
            "render_policy": "standalone",
            "temporal_domain_key": "present",
            "time_label": "夜",
            "time_relation": "episode_start",
            "location_key": "cliff",
            "location_label": "山崖",
            "participants": ["萧炎"],
            "participant_evidence": [
                {
                    "identity_key": "萧炎",
                    "source_segment_ids": ["SRC0001"],
                    "source_unit_keys": [
                        fact.source_unit_key
                        for fact in facts
                        if fact.projection == "action"
                    ],
                    "usage": "visible",
                },
                *[
                    {
                        "identity_key": "萧炎",
                        "source_segment_ids": ["SRC0001"],
                        "source_unit_keys": [fact.source_unit_key],
                        "usage": "state_subject",
                    }
                    for fact in facts
                    if fact.projection == "action"
                ],
            ],
            "source_unit_deliveries": [{
                "source_unit_key": quoted_key,
                "mode": "unspoken_reference",
                "content_owner_key": "萧炎",
            }],
            "action_logic": "萧炎表面安慰父亲，内心作出相反判断",
        }],
    })
    identity_registry = [{
        "identity_key": "person_xiaoyan",
        "authority_id": "bible:萧炎",
        "canonical_name": "萧炎",
        "source_labels": ["萧炎"],
    }]
    plan = build_screenplay_scene_shard_plans(
        blueprint,
        source_text=source_text,
        identity_registry_hash="identity-hash",
        identity_registry=identity_registry,
    )[0]
    quoted_slot = next(
        slot for slot in plan.unit_slots
        if slot.source_unit_key == quoted_key
    )

    contracts = build_screenplay_scene_input_contracts(
        plan=plan,
        scene_plans=blueprint.scene_plans,
        source_by_id={"SRC0001": source_text},
        identity_registry=identity_registry,
        blueprint_nodes=blueprint.nodes,
    )
    compiled_slot = next(
        slot
        for contract in contracts
        for slot in contract.unit_slots
        if slot.unit_key == quoted_slot.unit_key
    )

    assert compiled_slot.delivery_mode == "unspoken_reference"
    assert compiled_slot.speaker_key is None
    assert compiled_slot.state_subject_keys == ["person_xiaoyan"]
    assert compiled_slot.state_subject_key == "person_xiaoyan"
    assert compiled_slot.actor_keys == ["person_xiaoyan"]
    assert compiled_slot.onscreen_entity_keys == ["person_xiaoyan"]
    assert compiled_slot.participant_deliveries == []
    assert compiled_slot.environment_only is False


def test_scene_input_contract_rejects_unfrozen_blueprint_participant() -> None:
    blueprint = _blueprint(split_domain=False)
    blueprint.nodes[0].participants = ["未冻结来客"]
    blueprint.nodes[0].participant_evidence = [
        NarrativeParticipantEvidence(
            identity_key="未冻结来客",
            source_segment_ids=["SRC0001"],
            source_unit_keys=["SRC0001:unit:001"],
            usage="state_subject",
        )
    ]
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
            blueprint_nodes=blueprint.nodes,
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


def test_frozen_reference_identity_has_no_visual_asset_requirement() -> None:
    identities, registry, _registry_hash = build_frozen_identity_registry(
        Bible(characters=[], world=World(visual_style_canonical="测试")),
        [{
            "source_label": "靠山老祖",
            "canonical_name": "靠山老祖",
            "resolution": "reference_identity",
            "identity_group": "episode:kaoshan-ancestor",
        }],
    )

    reference = next(
        item for item in identities
        if item.display_name == "靠山老祖"
    )
    authority = next(
        item for item in registry
        if item["canonical_name"] == "靠山老祖"
    )
    assert authority["identity_kind"] == "reference"
    assert reference.visual_policy == "offscreen_only"
    assert reference.asset_requirement == "forbidden"
    assert reference.visual_canonical == ""


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


def test_validated_shards_merge_in_blueprint_order_with_locked_event_keys() -> None:
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
        plans[0].unit_slots[0].event_key,
        plans[1].unit_slots[0].event_key,
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


def test_merge_requires_front_matter_source_coverage(
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

    with pytest.raises(
        ScreenplaySceneMergeError,
        match="未覆盖.*SRC0001",
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
    with pytest.raises(ScreenplaySceneShardError) as caught:
        normalize_screenplay_scene_shard(
            shard,
            episode_no=1,
            plan=plans[0],
            scene_plans={item.key: item for item in blueprint.scene_plans},
            scene_input_contracts=scene_input_contracts,
        )
    assert shard.scenes[0].units[0].source_segment_ids == ["SRC0002"]
    errors = caught.value.errors
    assert any("来源唯一归属冲突" in error for error in errors)
    assert any(
        "owner=bp-sc002" in error
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

    assert any("unit_key 重复" in error for error in errors)


def test_scene_shard_rejects_program_and_identity_field_drift() -> None:
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
    }, {
        "identity_key": "person_甲",
        "canonical_name": "甲",
        "source_labels": ["甲"],
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

    with pytest.raises(
        ScreenplaySceneShardError,
        match="episode_no.*generation scaffold",
    ):
        normalize_screenplay_scene_shard(
            shard,
            episode_no=1,
            plan=plan,
            scene_plans={item.key: item for item in blueprint.scene_plans},
            scene_input_contracts=scene_input_contracts,
        )
    assert shard.episode_no == 99
    assert shard.shard_id == "invented"
    assert shard.scenes[0].units[0].text == "说明性对白摘要"


def test_scene_shard_payload_normalizer_does_not_rewrite_contract_fields() -> None:
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

    assert normalized == payload
    with pytest.raises(ValidationError, match="story_function"):
        ScreenplaySceneShardIR.model_validate(normalized)


def test_scene_shard_requires_front_matter_in_declared_slots() -> None:
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
    assert with_exclusion == without_exclusion


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
    assert "未覆盖 picture SRC" in str(caught.value)


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
        match="unit_key 重复",
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
    assert SCREENPLAY_SCENE_SHARD_VERSION == "screenplay-scene-shard.v11"


def test_blueprint_and_slot_semantics_are_required_without_defaults() -> None:
    node_payload = _blueprint().nodes[0].model_dump(mode="json")
    for field in (
        "narrative_layer",
        "event_priority",
        "render_policy",
    ):
        node_payload.pop(field)

    with pytest.raises(ValidationError) as node_error:
        NarrativeNode.model_validate(node_payload)
    missing_node_fields = {
        error["loc"][-1] for error in node_error.value.errors()
    }
    assert {
        "narrative_layer",
        "event_priority",
        "render_policy",
    } <= missing_node_fields

    with pytest.raises(ValidationError) as slot_error:
        ScreenplaySceneUnitSlotPlan.model_validate({
            "unit_key": "u1",
            "event_key": "e1",
            "scene_key": "s1",
            "scene_order": 1,
            "unit_order": 1,
            "scene_unit_order": 1,
            "kind": "action",
            "source_segment_ids": ["SRC0001"],
        })
    missing_slot_fields = {
        error["loc"][-1] for error in slot_error.value.errors()
    }
    assert {
        "narrative_layer",
        "event_priority",
        "render_policy",
    } <= missing_slot_fields

    old_blueprint = _blueprint().model_dump(mode="json")
    old_blueprint["format_version"] = "screenplay-narrative-blueprint.v3"
    with pytest.raises(ValidationError, match="format_version"):
        NarrativeBlueprint.model_validate(old_blueprint)


def test_midstream_audit_only_source_never_enters_creative_or_picture_projection() -> None:
    source = "\n\n".join([
        "暴雨冲开沟渠，水位越过石阶。",
        "作者说明本周更新安排并感谢阅读。",
        "雨势减弱，沟渠水位退回警戒线下。",
    ])
    blueprint = NarrativeBlueprint.model_validate({
        "episode_no": 1,
        "nodes": [
            _semantic_node(
                key="environment-rise",
                source_segment_ids=["SRC0001"],
                summary="暴雨使沟渠水位越过石阶",
                location_key="ditch",
                location_label="山沟",
                story=True,
                first=True,
            ),
            _semantic_node(
                key="author-audit",
                source_segment_ids=["SRC0002"],
                summary="作者更新说明",
                location_key="paratext",
                location_label="来源审计",
                story=False,
            ),
            _semantic_node(
                key="environment-fall",
                source_segment_ids=["SRC0003"],
                summary="雨势减弱后水位回落",
                location_key="ditch",
                location_label="山沟",
                story=True,
            ),
        ],
    })
    blueprint.nodes[0].environment_source_unit_keys = [
        fact.source_unit_key
        for fact in source_segment_facts(
            "SRC0001", "暴雨冲开沟渠，水位越过石阶。",
        )
    ]
    blueprint.nodes[2].environment_source_unit_keys = [
        fact.source_unit_key
        for fact in source_segment_facts(
            "SRC0003", "雨势减弱，沟渠水位退回警戒线下。",
        )
    ]
    derive_blueprint_scene_plans(blueprint)
    plans = build_screenplay_scene_shard_plans(
        blueprint,
        source_text=source,
        identity_registry_hash="empty-registry",
    )
    contracts = build_screenplay_scene_input_contract_set(
        plans=plans,
        blueprint=blueprint,
        source_text=source,
        identity_registry=[],
    )
    shards = [
        compile_screenplay_scene_shard_draft(
            _creative_shard(plan, blueprint, contracts[plan.shard_id]),
            episode_no=1,
            plan=plan,
            scene_plans={
                scene.key: scene for scene in blueprint.scene_plans
            },
            scene_input_contracts=contracts[plan.shard_id],
        )
        for plan in plans
    ]
    merged = merge_screenplay_scene_shards(
        envelope=ScreenplayEnvelopeIR(
            episode_no=1,
            metadata=ScreenplayEnvelopeMetadata(title="环境事件"),
            experience=ScreenplayEnvelopeExperience(
                director_objective="呈现水位变化",
                satisfaction_criteria="环境状态清晰",
            ),
            blueprint_hash=blueprint_content_hash(blueprint),
            identity_registry_hash="empty-registry",
        ),
        identities=[],
        plans=plans,
        shards=shards,
        scene_input_contracts=contracts,
        blueprint=blueprint,
        source_text=source,
    )

    assert "SRC0002" not in blueprint.source_scene_owners
    assert [
        annotation.model_dump(mode="json")
        for annotation in blueprint.source_audit_annotations
    ] == [{
        "node_key": "author-audit",
        "source_segment_ids": ["SRC0002"],
        "narrative_layer": "paratext",
        "render_policy": "exclude_from_spine",
        "disposition": "audit_only",
        "projection_policy": "audit_only",
    }]
    assert all(
        "SRC0002" not in slot.source_segment_ids
        for plan in plans
        for slot in plan.unit_slots
    )
    assert all(
        "SRC0002" not in segment_ids
        for contract_set in contracts.values()
        for contract in contract_set
        for segment_ids in [contract.source_segment_ids]
    )
    assert merged.coverage[0].disposition == "audit_only"
    assert merged.coverage[0].projection_policy == "audit_only"

    screenplay = compile_screenplay_ir(
        merged,
        episode={"id": "ep-env", "episode_no": 1, "title": "环境事件"},
        source_text=source,
        bible=Bible(
            characters=[],
            world=World(visual_style_canonical="写实环境"),
        ),
    )
    projected, _report = picture_screenplay_projection(screenplay)
    assert {
        item.source_segment_id for item in screenplay.source_coverage
    } == {"SRC0001", "SRC0002", "SRC0003"}
    audit = next(
        item for item in screenplay.source_coverage
        if item.source_segment_id == "SRC0002"
    )
    assert audit.disposition == "audit_only"
    assert audit.beat_ids == []
    assert all(
        "SRC0002" not in event.source_segment_ids
        for event in merged.events
    )
    assert all(
        "SRC0002" not in beat.source_segment_ids
        for beat in screenplay.plot_spine.spine_beats
    )
    assert all(
        "作者说明" not in requirement
        for scene in screenplay.scene_outline
        for requirement in scene.context_requirements
    )
    assert len(projected.scene_outline) == 2
    assert all(not scene.characters for scene in projected.scene_outline)


def test_run_e65d871ad2a0_sc16_projects_fifteen_story_scenes() -> None:
    fixture = json.loads(
        RUN_E65D871AD2A0_FIXTURE.read_text(encoding="utf-8")
    )
    source = "\n\n".join(fixture["source_segments"])
    nodes = [
        _semantic_node(
            key=item["key"],
            source_segment_ids=item["source_segment_ids"],
            summary=item["summary"],
            location_key=item["location_key"],
            location_label=item["location_label"],
            story=True,
            first=index == 0,
        )
        for index, item in enumerate(fixture["story_nodes"])
    ]
    audit = fixture["paratext_node"]
    nodes.append(_semantic_node(
        key=audit["key"],
        source_segment_ids=audit["source_segment_ids"],
        summary=audit["summary"],
        location_key="paratext",
        location_label="来源审计",
        story=False,
    ))
    blueprint = NarrativeBlueprint.model_validate({
        "episode_no": 1,
        "nodes": nodes,
    })
    source_by_id = {
        segment.segment_id: segment.text
        for segment in index_source_segments(source)
    }
    for node in blueprint.nodes:
        if node.narrative_layer != "story":
            continue
        node.environment_source_unit_keys = [
            fact.source_unit_key
            for source_id in node.source_segment_ids
            for fact in source_segment_facts(
                source_id,
                source_by_id[source_id],
            )
            if fact.projection == "action"
        ]
    derive_blueprint_scene_plans(blueprint)
    identities, registry, registry_hash = build_frozen_identity_registry(
        Bible(
            characters=[],
            world=World(visual_style_canonical="写实环境"),
        ),
        [],
    )
    plans = build_screenplay_scene_shard_plans(
        blueprint,
        source_text=source,
        identity_registry_hash=registry_hash,
    )
    contracts = build_screenplay_scene_input_contract_set(
        plans=plans,
        blueprint=blueprint,
        source_text=source,
        identity_registry=registry,
    )
    shards = []
    for plan in plans:
        creative = ScreenplaySceneShardCreativeIR.model_validate({
            "slots": {
                slot.unit_key: {
                    "text": source_by_id[slot.source_segment_ids[0]],
                    "resulting_state": (
                        f"{slot.source_segment_ids[0]} 环境状态完成"
                    ),
                }
                for slot in plan.unit_slots
            },
        })
        shards.append(compile_screenplay_scene_shard_draft(
            creative,
            episode_no=1,
            plan=plan,
            scene_plans={
                scene.key: scene for scene in blueprint.scene_plans
            },
            scene_input_contracts=contracts[plan.shard_id],
        ))
    merged = merge_screenplay_scene_shards(
        envelope=ScreenplayEnvelopeIR(
            episode_no=1,
            metadata=ScreenplayEnvelopeMetadata(title="SC16 回归"),
            experience=ScreenplayEnvelopeExperience(
                director_objective="完整交付剧情",
                satisfaction_criteria="旁文本只审计",
            ),
            blueprint_hash=blueprint_content_hash(blueprint),
            identity_registry_hash=registry_hash,
        ),
        identities=identities,
        plans=plans,
        shards=shards,
        scene_input_contracts=contracts,
        blueprint=blueprint,
        source_text=source,
    )
    screenplay = compile_screenplay_ir(
        merged,
        episode={"id": "ep-sc16", "episode_no": 1, "title": "SC16 回归"},
        source_text=source,
        bible=Bible(
            characters=[],
            world=World(visual_style_canonical="写实环境"),
        ),
    )
    projected, projection_report = picture_screenplay_projection(screenplay)

    assert fixture["run_id"] == "run_e65d871ad2a0"
    assert len(blueprint.scene_plans) == fixture["expected_story_scene_count"]
    assert len(projected.scene_outline) == fixture["expected_story_scene_count"]
    assert {item.source_segment_id for item in screenplay.source_coverage} == {
        f"SRC{index:04d}" for index in range(1, 63)
    }
    audit_source_ids = {
        item.source_segment_id
        for item in screenplay.source_coverage
        if item.disposition == "audit_only"
    }
    assert audit_source_ids == {"SRC0060", "SRC0061", "SRC0062"}
    assert {
        source_id
        for annotation in blueprint.source_audit_annotations
        for source_id in annotation.source_segment_ids
    } == audit_source_ids
    assert {
        source_id
        for annotation in merged.source_audit_annotations
        for source_id in annotation.source_segment_ids
    } == audit_source_ids
    assert projection_report["excluded_source_segment_ids"] == []
    assert audit_source_ids.isdisjoint({
        source_id
        for beat in projected.plot_spine.spine_beats
        for source_id in beat.source_segment_ids
    })
    assert audit_source_ids.isdisjoint({
        source_id
        for action in projected.narrative_plan.atomic_actions
        for source_id in action.action_agency.source_segment_ids
    })
    assert all(
        all(source_id not in requirement for source_id in audit_source_ids)
        for scene in projected.scene_outline
        for requirement in scene.context_requirements
    )
    story_authority_identity_keys = {
        identity_key
        for event in merged.events
        if event.narrative_layer == "story"
        for identity_key in (
            *event.actor_keys,
            *event.target_keys,
            *event.onscreen_entity_keys,
        )
    }
    picture_identity_keys = {
        identity_key
        for event in projected.narrative_plan.events
        for identity_key in event.onscreen_entity_ids
    } | {
        identity_key
        for action in projected.narrative_plan.atomic_actions
        for identity_key in (*action.actor_ids, *action.target_ids)
    }
    assert picture_identity_keys.issubset(story_authority_identity_keys)
    assert {
        identity.identity_id
        for identity in projected.narrative_plan.identity_contracts
    }.issubset(story_authority_identity_keys)


def test_run_64a_fixture_projects_only_picture_sources_end_to_end() -> None:
    fixture = json.loads(
        RUN_64A2E395D6DF_FIXTURE.read_text(encoding="utf-8")
    )
    projection = fixture["candidate_projection"]
    audit_node_keys = set(projection["audit_node_keys"])
    scene_by_node = {
        node_key: scene_index
        for scene_index, node_keys in enumerate(
            projection["scene_node_keys"],
            start=1,
        )
        for node_key in node_keys
    }
    scene_start_keys = {
        node_keys[0] for node_keys in projection["scene_node_keys"]
    }
    nodes = [
        _semantic_node(
            key=node_key,
            source_segment_ids=source_ids,
            summary=f"{node_key} 环境状态推进",
            location_key=(
                "source-audit"
                if node_key in audit_node_keys
                else f"story-location-{scene_by_node[node_key]}"
            ),
            location_label=(
                "来源审计"
                if node_key in audit_node_keys
                else f"剧情环境 {scene_by_node[node_key]}"
            ),
            story=node_key not in audit_node_keys,
            first=index == 0,
        )
        for index, (node_key, source_ids) in enumerate(
            projection["node_sources"].items()
        )
    ]
    for node in nodes:
        node["scene_boundary_before"] = node["key"] in scene_start_keys
    blueprint = NarrativeBlueprint.model_validate({
        "episode_no": 1,
        "nodes": nodes,
    })
    derive_blueprint_scene_plans(blueprint)
    source = "\n\n".join(
        f"环境状态推进 {index}"
        for index in range(1, fixture["source_segment_count"] + 1)
    )
    identities, registry, registry_hash = build_frozen_identity_registry(
        Bible(
            characters=[],
            world=World(visual_style_canonical="写实环境"),
        ),
        [],
    )
    plans = build_screenplay_scene_shard_plans(
        blueprint,
        source_text=source,
        identity_registry_hash=registry_hash,
    )
    contracts = build_screenplay_scene_input_contract_set(
        plans=plans,
        blueprint=blueprint,
        source_text=source,
        identity_registry=registry,
    )
    shards = [
        compile_screenplay_scene_shard_draft(
            _creative_shard(plan, blueprint, contracts[plan.shard_id]),
            episode_no=1,
            plan=plan,
            scene_plans={
                scene.key: scene for scene in blueprint.scene_plans
            },
            scene_input_contracts=contracts[plan.shard_id],
        )
        for plan in plans
    ]
    merged = merge_screenplay_scene_shards(
        envelope=ScreenplayEnvelopeIR(
            episode_no=1,
            metadata=ScreenplayEnvelopeMetadata(title="run64 回归"),
            experience=ScreenplayEnvelopeExperience(
                director_objective="完整交付剧情环境",
                satisfaction_criteria="旁文本只保留审计",
            ),
            blueprint_hash=blueprint_content_hash(blueprint),
            identity_registry_hash=registry_hash,
        ),
        identities=identities,
        plans=plans,
        shards=shards,
        scene_input_contracts=contracts,
        blueprint=blueprint,
        source_text=source,
    )
    screenplay = compile_screenplay_ir(
        merged,
        episode={"id": "ep-run64", "episode_no": 1, "title": "run64 回归"},
        source_text=source,
        bible=Bible(
            characters=[],
            world=World(visual_style_canonical="写实环境"),
        ),
    )
    projected, _report = picture_screenplay_projection(screenplay)

    audit_source_ids = {"SRC0060", "SRC0061", "SRC0062"}
    assert [plan.node_keys for plan in blueprint.scene_plans] == projection[
        "scene_node_keys"
    ]
    assert "S003-N020" in {node.key for node in blueprint.nodes}
    assert any(
        "S003-N020" in plan.node_keys for plan in blueprint.scene_plans
    )
    assert {
        item.source_segment_id
        for item in screenplay.source_coverage
        if item.disposition == "audit_only"
    } == audit_source_ids
    picture_contract = {
        "events": [
            event.model_dump(mode="json")
            for event in projected.narrative_plan.events
        ],
        "beats": [
            beat.model_dump(mode="json")
            for beat in projected.plot_spine.spine_beats
        ],
        "context_requirements": [
            requirement
            for scene in projected.scene_outline
            for requirement in scene.context_requirements
        ],
        "scene_outline": [
            scene.model_dump(mode="json")
            for scene in projected.scene_outline
        ],
        "identity_contracts": [
            identity.model_dump(mode="json")
            for identity in projected.narrative_plan.identity_contracts
        ],
    }
    serialized_picture = json.dumps(
        picture_contract,
        ensure_ascii=False,
        sort_keys=True,
    )
    assert all(
        source_id not in serialized_picture for source_id in audit_source_ids
    )
    assert any(
        {"SRC0058", "SRC0059"}.intersection(
            action.action_agency.source_segment_ids
        )
        for action in projected.narrative_plan.atomic_actions
    )


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
        item["key"]: BlueprintScenePlan.model_validate({
            **item,
            "source_semantics": _story_source_semantics(
                item["source_segment_ids"]
            ),
        })
        for item in replay_input["scene_plans"]
    }
    hashes = replay_input["hashes"]
    source_by_id = {
        segment["source_segment_id"]: segment["text"]
        for item in replay_input["scene_inputs"]
        for segment in item["source_segments"]
    }
    unit_slots = scene_shards_module._build_group_unit_slots(
        list(scene_plans.values()),
        source_by_id=source_by_id,
        scene_order_by_key={
            key: index
            for index, key in enumerate(scene_plans, start=13)
        },
    )
    plan = ScreenplaySceneShardPlan(
        shard_id="SS004",
        scene_plan_keys=list(scene_plans),
        source_segment_ids=[
            source_id
            for scene_plan in scene_plans.values()
            for source_id in scene_plan.source_segment_ids
        ],
        source_scene_owners=replay_input["source_scene_owners"],
        unit_slots=unit_slots,
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
    contracts = []
    for item in replay_input["scene_inputs"]:
        contract = ScreenplaySceneInputContract(
            scene_plan_key=item["scene_plan_key"],
            node_keys=item["node_keys"],
            source_segment_ids=[
                segment["source_segment_id"]
                for segment in item["source_segments"]
            ],
            source_semantics=_story_source_semantics([
                segment["source_segment_id"]
                for segment in item["source_segments"]
            ]),
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
            unit_slots=[
                scene_shards_module.ScreenplaySceneCompiledUnitSlot(
                    **slot.model_dump(mode="python")
                )
                for slot in unit_slots
                if slot.scene_key == item["scene_plan_key"]
            ],
            source_ownership_hash=hashes["source_ownership_hash"],
        )
        contract.identity_scaffold_hash = (
            scene_shards_module._contract_identity_scaffold_hash(
                contract
            )
        )
        contracts.append(contract)
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
    plan, _scene_plans, contracts, _identity_keys = (
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
        plan=plan,
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
    _replay = json.loads(ERR_20260810_48009F_REPLAY.read_text(encoding="utf-8"))
    plan, _scene_plans, contracts, _identity_keys = (
        _ss004_replay_validation_context()
    )
    initial_schema = build_screenplay_scene_shard_repair_schema(
        plan=plan,
        scene_input_contracts=contracts,
    )
    repair_schema = build_screenplay_scene_shard_repair_schema(
        plan=plan,
        scene_input_contracts=contracts,
    )

    assert initial_schema == repair_schema
    assert initial_schema["x-schema-purpose"] == (
        "creative-content-for-deterministic-generation-slots"
    )


def test_scene_contract_schema_preserves_relation_cardinality_boundaries() -> None:
    _replay, plan, _scene_plans, contracts = (
        _ss004_533ac9_compile_context()
    )
    schema = build_screenplay_scene_shard_repair_schema(
        plan=plan,
        scene_input_contracts=contracts,
    )
    slots_schema = schema["properties"]["slots"]
    assert slots_schema["additionalProperties"] is False
    assert slots_schema["required"] == [
        slot.unit_key for slot in plan.unit_slots
    ]
    assert set(slots_schema["properties"]) == set(
        slots_schema["required"]
    )


def test_scene_contract_schema_does_not_interchange_similar_bound_identities() -> None:
    replay = json.loads(ERR_20260810_B66DDA_REPLAY.read_text(encoding="utf-8"))
    for provider_response in replay["provider_responses"]:
        with pytest.raises(ValidationError, match="extra_forbidden"):
            ScreenplaySceneShardCreativeIR.model_validate(
                provider_response["response"]
            )


def test_repair_schema_derives_relations_visibility_and_evidence_per_unit() -> None:
    replay, plan, _scene_plans, contracts = (
        _ss004_533ac9_compile_context()
    )
    draft = _recorded_response_slot_draft(
        replay["creative_response"],
        plan,
        contracts,
    )
    initial = build_screenplay_scene_shard_repair_schema(
        plan=plan,
        scene_input_contracts=contracts,
    )
    repair = build_screenplay_scene_shard_repair_schema(
        draft,
        plan=plan,
        scene_input_contracts=contracts,
    )
    assert initial == repair
    assert "IRActionParticipantDelivery" not in initial["$defs"]


def test_repair_schema_allows_only_a_genuine_empty_delivery_set() -> None:
    _replay, plan, _scene_plans, contracts = (
        _ss004_533ac9_compile_context()
    )
    schema = build_screenplay_scene_shard_repair_schema(
        plan=plan,
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
        _recorded_response_slot_draft(
            replay["creative_response"],
            plan,
            contracts,
        ),
        episode_no=1,
        plan=plan,
        scene_plans=scene_plans,
        scene_input_contracts=contracts,
    )
    answer = next(
        unit
        for unit in shard.scenes[1].units
        if (
            unit.speaker_key == "person_46e7e8b742ed"
            and unit.participant_deliveries
        )
    )
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
    plan, _scene_plans, contracts, _identity_keys = (
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
                plan=plan,
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


def test_ss004_scene_format_retry_keeps_exact_source_authority(
    monkeypatch,
) -> None:
    replay_input = json.loads(SS004_REPLAY_INPUT.read_text(encoding="utf-8"))
    _replay, plan, scene_plans, contracts = (
        _ss004_533ac9_compile_context()
    )
    identity_registry = [
        {
            **item,
            "authority_id": f"bible:{item['identity_key']}",
        }
        for item in replay_input["identity_registry"]
    ]
    blueprint = NarrativeBlueprint(
        episode_no=1,
        nodes=[],
        scene_plans=list(scene_plans.values()),
        source_scene_owners=dict(plan.source_scene_owners),
        source_semantics=_story_source_semantics(
            plan.source_segment_ids
        ),
    )
    source_facts = {
        fact.source_unit_key: fact.text
        for contract in contracts
        for segment in contract.source_segments
        for fact in source_segment_facts(
            segment.source_segment_id,
            segment.text,
        )
    }
    valid_payload = {
        "contract_version": SCREENPLAY_SCENE_CREATIVE_VERSION,
        "slots": {
            slot.unit_key: {
                "text": (
                    slot.source_text
                    if slot.kind == "dialogue"
                    else source_facts[slot.source_unit_key]
                ),
            }
            for slot in plan.unit_slots
        },
    }
    drift_slot = next(
        slot for slot in plan.unit_slots if slot.kind == "action"
    )
    drift_payload = deepcopy(valid_payload)
    drift_payload["slots"][drift_slot.unit_key]["text"] = (
        "王有材凭空改写来源并杀死孟浩"
    )
    malformed_drift = json.dumps(
        drift_payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).replace('","slots":', '" "slots":', 1)
    prompts: list[list[dict[str, str]]] = []

    async def fake_chat(messages, **_kwargs):
        prompts.append(messages)
        if len(prompts) == 1:
            return malformed_drift
        retry_prompt = messages[0]["content"]
        assert "格式修复权威上下文" in retry_prompt
        assert '"exact_slot_authority":' in retry_prompt
        assert '"source_fact":' in retry_prompt
        assert source_facts[drift_slot.source_unit_key] in retry_prompt
        assert '"identity_authority":' in retry_prompt
        assert '"final_gate_contract":' in retry_prompt
        assert drift_payload["slots"][drift_slot.unit_key]["text"] in (
            retry_prompt
        )
        return json.dumps(
            valid_payload,
            ensure_ascii=False,
            separators=(",", ":"),
        )

    monkeypatch.setattr(model_gateway, "chat", fake_chat)
    source_text = "\n\n".join([
        *[f"结构占位 {index}" for index in range(1, 54)],
        *[
            segment.text
            for contract in contracts
            for segment in contract.source_segments
        ],
    ])
    identities = [
        IRIdentity(
            key=item["identity_key"],
            display_name=item["canonical_name"],
            authority_id=item["authority_id"],
            source_names=item["source_labels"],
            visual_policy="canonical",
            asset_requirement="required",
            role_type="named_character",
        )
        for item in identity_registry
    ]

    shards, _artifact_ids, rows = asyncio.run(
        generate_screenplay_scene_shards(
            episode={
                "id": f"ep-ss004-format-authority-{uuid.uuid4()}",
                "episode_no": 1,
            },
            source_text=source_text,
            blueprint=blueprint,
            identity_registry=identity_registry,
            identities=identities,
            plans=[plan],
            scene_input_contracts={plan.shard_id: contracts},
        )
    )

    assert len(prompts) == 2
    assert [message["role"] for message in prompts[1]] == ["user"]
    assert "任务：只填写程序预声明 generation slot" not in (
        prompts[1][0]["content"]
    )
    assert [shard.shard_id for shard in shards] == ["SS004"]
    assert [row["status"] for row in rows] == ["validated"]


@pytest.mark.parametrize(
    "channel",
    ["audible", "visible_effect", "visible_reaction"],
)
def test_validator_and_merge_accept_source_authored_offscreen_evidence(
    channel: str,
) -> None:
    blueprint, plans, registry, identities, _shard_value = _participant_case()
    scene_input_contracts = _contracts(plans, blueprint, registry)
    contract = scene_input_contracts[plans[0].shard_id][0]
    contract.action_evidence = [
        scene_shards_module.ScreenplaySceneActionEvidence(
            node_key=contract.node_keys[0],
            source_segment_ids=["SRC0001"],
                participants=[
                    scene_shards_module.ScreenplaySceneActionParticipantEvidence(
                    identity_key="person_a",
                    source_segment_ids=["SRC0001"],
                    usage="voice",
                        perception_channels=[channel],
                    ),
                    scene_shards_module.ScreenplaySceneActionParticipantEvidence(
                        identity_key="person_a",
                        source_segment_ids=["SRC0001"],
                        source_unit_keys=["SRC0001:unit:001"],
                        usage="state_subject",
                    ),
                ],
            decision_actor_key="person_a",
        )
    ]
    compiled_slot, slot_errors = (
        scene_shards_module._compile_unit_identity_scaffold(
            plans[0].unit_slots[0],
            contract=contract,
        )
    )
    assert slot_errors == []
    contract.unit_slots = [compiled_slot]
    contract.identity_scaffold_hash = (
        scene_shards_module._contract_identity_scaffold_hash(
            contract
        )
    )
    shard = scene_shards_module.compile_screenplay_scene_shard_draft(
        _creative_shard(
            plans[0],
            blueprint,
            scene_input_contracts[plans[0].shard_id],
        ),
        episode_no=1,
        plan=plans[0],
        scene_plans={
            scene.key: scene for scene in blueprint.scene_plans
        },
        scene_input_contracts=scene_input_contracts[plans[0].shard_id],
    )

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
    assert any("identity scaffold drift" in error for error in errors)
    with pytest.raises(
        ScreenplaySceneMergeError,
        match="identity scaffold drift",
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


def test_normalization_rejects_offscreen_identity_scaffold_drift() -> None:
    blueprint, plans, registry, identities, shard = _participant_case()
    unit = shard.scenes[0].units[0]
    unit.target_keys = ["person_a"]
    unit.onscreen_entity_keys = []
    unit.participant_deliveries = []
    scene_input_contracts = _contracts(plans, blueprint, registry)

    with pytest.raises(
        ScreenplaySceneShardError,
        match="identity scaffold drift",
    ):
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
    assert any("identity scaffold drift" in error for error in errors)


def _scene_shard_cache_compatibility_case(
    *,
    with_repair: bool,
) -> tuple[dict, dict, dict]:
    blueprint = _blueprint(split_domain=False)
    plan = build_screenplay_scene_shard_plans(
        blueprint,
        source_text=SOURCE,
        identity_registry_hash="identity-hash",
    )[0]
    contracts = _contracts([plan], blueprint)[plan.shard_id]
    initial_creative = _creative_shard(plan, blueprint, contracts)
    reviewed_creative = initial_creative.model_copy(deep=True)
    unit_key = next(iter(reviewed_creative.slots))
    if with_repair:
        reviewed_creative.slots[unit_key].text += " 修复后"
    initial_hash = scene_shards_module._hash(
        initial_creative.model_dump(mode="json")
    )
    reviewed_hash = scene_shards_module._hash(
        reviewed_creative.model_dump(mode="json")
    )
    first_finding = {
        "unit_key": unit_key,
        "code": "source_semantic_drift",
        "violation_kinds": ["source_contradiction"],
        "message": "第一 reviewer 的 finding",
    }
    second_finding = {
        **first_finding,
        "message": "第二 reviewer 的同键 finding",
    }
    reviews = [
        ScreenplaySceneShardSemanticReview.model_validate(
            {"findings": [first_finding]}
        ),
        ScreenplaySceneShardSemanticReview.model_validate(
            {"findings": [second_finding]}
        ),
    ]
    consensus = [
        finding.model_dump(mode="json")
        for finding in scene_shards_module
        .screenplay_scene_semantic_consensus(*reviews)
    ]
    phases = [{
        "phase": "initial",
        "creative_hash": initial_hash,
        "reviews": (
            [{"findings": [first_finding]}, {"findings": [second_finding]}]
            if with_repair
            else [{"findings": []}, {"findings": []}]
        ),
        "consensus": consensus if with_repair else [],
    }]
    if with_repair:
        phases.append({
            "phase": "post_repair",
            "creative_hash": reviewed_hash,
            "reviews": [{"findings": []}, {"findings": []}],
            "consensus": [],
        })
    shard = compile_screenplay_scene_shard_draft(
        reviewed_creative,
        episode_no=1,
        plan=plan,
        scene_plans={
            scene.key: scene for scene in blueprint.scene_plans
        },
        scene_input_contracts=contracts,
    )
    artifact_content = shard.model_dump(mode="json")
    reviewed_shard_content_hash = evidence_repository.content_hash(
        artifact_content
    )
    raw_content = {
        "shard_id": plan.shard_id,
        "attempts": [],
        "semantic_review_evidence": {
            "contract_version": SCREENPLAY_SCENE_SEMANTIC_REVIEW_VERSION,
            "initial_creative_hash": initial_hash,
            "reviewed_creative_hash": reviewed_hash,
            "reviewed_shard_content_hash": reviewed_shard_content_hash,
            "phases": phases,
        },
    }
    raw_artifact = {
        "id": f"raw-scene-shard-{with_repair}",
        "type": "screenplay_scene_shard_raw",
        "scope_type": "episode",
        "scope_id": "ep-scene-shard-cache-compatibility",
        "status": "candidate",
        "content": raw_content,
        "content_hash": evidence_repository.content_hash(raw_content),
        "file_path": None,
        "parent_artifact_ids": ["blueprint-artifact", "identity-artifact"],
        "contract_version": SCREENPLAY_SCENE_SHARD_VERSION,
    }
    artifact = {
        "id": f"scene-shard-{with_repair}",
        "type": "screenplay_scene_shard",
        "scope_type": "episode",
        "scope_id": "ep-scene-shard-cache-compatibility",
        "status": "validated",
        "content": artifact_content,
        "content_hash": evidence_repository.content_hash(artifact_content),
        "parent_artifact_ids": [raw_artifact["id"]],
        "contract_version": SCREENPLAY_SCENE_SHARD_VERSION,
        "model_snapshot": {
            "semantic_review_version": (
                SCREENPLAY_SCENE_SEMANTIC_REVIEW_VERSION
            ),
            "reviewed_creative_hash": reviewed_hash,
            "reviewed_shard_content_hash": reviewed_shard_content_hash,
        },
    }
    compatibility_kwargs = {
        "expected_blueprint_hash": plan.blueprint_hash,
        "expected_identity_registry_hash": plan.identity_registry_hash,
        "expected_generation_scaffold_hash": (
            screenplay_scene_generation_scaffold_hash(plan, contracts)
        ),
        "raw_artifact": raw_artifact,
        "expected_authority_artifact_ids": {
            "blueprint-artifact", "identity-artifact",
        },
    }
    return artifact, raw_artifact, compatibility_kwargs


@pytest.mark.parametrize("with_repair", [False, True])
def test_scene_shard_cache_accepts_exact_semantic_review_evidence(
    with_repair: bool,
) -> None:
    artifact, _raw_artifact, compatibility_kwargs = (
        _scene_shard_cache_compatibility_case(with_repair=with_repair)
    )

    compatible, reason = screenplay_scene_shard_artifact_compatibility(
        artifact,
        **compatibility_kwargs,
    )

    assert compatible is True, reason


def test_scene_shard_cache_does_not_consensus_different_violation_kinds() -> None:
    artifact, raw_artifact, compatibility_kwargs = (
        _scene_shard_cache_compatibility_case(with_repair=False)
    )
    unit_key = artifact["content"]["scenes"][0]["units"][0]["unit_key"]
    first_finding = {
        "unit_key": unit_key,
        "code": "source_semantic_drift",
        "violation_kinds": ["wrong_subject"],
        "message": "reviewer one",
    }
    second_finding = {
        **first_finding,
        "violation_kinds": ["unsupported_action"],
        "message": "reviewer two",
    }
    phase = raw_artifact["content"]["semantic_review_evidence"][
        "phases"
    ][0]
    phase["reviews"] = [
        {"findings": [first_finding]},
        {"findings": [second_finding]},
    ]
    phase["consensus"] = []
    raw_artifact["content_hash"] = evidence_repository.content_hash(
        raw_artifact["content"]
    )

    compatible, reason = screenplay_scene_shard_artifact_compatibility(
        artifact,
        **compatibility_kwargs,
    )

    assert compatible is True, reason


def test_run_b0659b64b548_cache_recomputes_shared_multi_kind_consensus() -> None:
    case = json.loads(
        RUN_B0659B64B548_REPLAY.read_text(encoding="utf-8")
    )
    artifact, raw_artifact, compatibility_kwargs = (
        _scene_shard_cache_compatibility_case(with_repair=True)
    )
    unit_keys = [
        unit["unit_key"]
        for scene in artifact["content"]["scenes"]
        for unit in scene["units"]
    ]
    fixture_keys = [
        finding["unit_key"]
        for finding in case["reviewer1"]["findings"]
    ]
    key_map = dict(zip(fixture_keys, unit_keys, strict=True))

    def mapped_review(name: str) -> dict:
        return {
            "findings": [
                {
                    **finding,
                    "unit_key": key_map[finding["unit_key"]],
                }
                for finding in case[name]["findings"]
            ],
        }

    reviews = [mapped_review("reviewer1"), mapped_review("reviewer2")]
    typed_reviews = [
        ScreenplaySceneShardSemanticReview.model_validate(review)
        for review in reviews
    ]
    expected = [
        finding.model_dump(mode="json")
        for finding in scene_shards_module
        .screenplay_scene_semantic_consensus(*typed_reviews)
    ]
    phase = raw_artifact["content"]["semantic_review_evidence"][
        "phases"
    ][0]
    phase["reviews"] = reviews
    phase["consensus"] = expected
    raw_artifact["content_hash"] = evidence_repository.content_hash(
        raw_artifact["content"]
    )

    compatible, reason = screenplay_scene_shard_artifact_compatibility(
        artifact,
        **compatibility_kwargs,
    )

    assert compatible is True, reason
    assert len(expected) == 1
    assert expected[0]["violation_kinds"] == ["source_contradiction"]
    assert all(
        finding["unit_key"] != key_map[case["empty_intersection_unit_key"]]
        for finding in expected
    )


@pytest.mark.parametrize(
    "field",
    ["performance", "resulting_state", "function"],
)
def test_scene_shard_cache_rejects_reviewed_content_mutation(
    field: str,
) -> None:
    artifact, _raw_artifact, compatibility_kwargs = (
        _scene_shard_cache_compatibility_case(with_repair=False)
    )
    artifact["content"]["scenes"][0]["units"][0][field] += " 篡改"
    artifact["content_hash"] = evidence_repository.content_hash(
        artifact["content"],
        artifact.get("file_path"),
    )

    compatible, reason = screenplay_scene_shard_artifact_compatibility(
        artifact,
        **compatibility_kwargs,
    )

    assert compatible is False
    assert reason == "semantic_review_shard_hash"


def test_scene_shard_cache_rejects_coordinated_content_and_raw_hash_mutation(
) -> None:
    artifact, raw_artifact, compatibility_kwargs = (
        _scene_shard_cache_compatibility_case(with_repair=False)
    )
    artifact["content"]["scenes"][0]["units"][0][
        "performance"
    ] += " 协同篡改"
    tampered_hash = evidence_repository.content_hash(
        artifact["content"],
        artifact.get("file_path"),
    )
    artifact["content_hash"] = tampered_hash
    raw_artifact["content"]["semantic_review_evidence"][
        "reviewed_shard_content_hash"
    ] = tampered_hash
    raw_artifact["content_hash"] = evidence_repository.content_hash(
        raw_artifact["content"],
        raw_artifact.get("file_path"),
    )

    compatible, reason = screenplay_scene_shard_artifact_compatibility(
        artifact,
        **compatibility_kwargs,
    )

    assert compatible is False
    assert reason == "semantic_review_shard_hash"


def test_scene_shard_cache_rejects_snapshot_shard_hash_mutation() -> None:
    artifact, _raw_artifact, compatibility_kwargs = (
        _scene_shard_cache_compatibility_case(with_repair=False)
    )
    artifact["model_snapshot"]["reviewed_shard_content_hash"] = "0" * 64

    compatible, reason = screenplay_scene_shard_artifact_compatibility(
        artifact,
        **compatibility_kwargs,
    )

    assert compatible is False
    assert reason == "semantic_review_shard_hash"


@pytest.mark.parametrize(
    ("with_repair", "mutation"),
    [
        (False, "unreachable_two_clean"),
        (True, "post_repair_not_clean"),
    ],
)
def test_scene_shard_cache_rejects_invalid_semantic_review_phase_contract(
    with_repair: bool,
    mutation: str,
) -> None:
    artifact, raw_artifact, compatibility_kwargs = (
        _scene_shard_cache_compatibility_case(with_repair=with_repair)
    )
    evidence = raw_artifact["content"]["semantic_review_evidence"]
    if mutation == "unreachable_two_clean":
        evidence["phases"].append({
            "phase": "post_repair",
            "creative_hash": evidence["reviewed_creative_hash"],
            "reviews": [{"findings": []}, {"findings": []}],
            "consensus": [],
        })
    else:
        finding = evidence["phases"][0]["consensus"][0]
        evidence["phases"][-1]["reviews"] = [
            {"findings": [finding]},
            {
                "findings": [{
                    **finding,
                    "message": "post-repair second reviewer",
                }],
            },
        ]
        evidence["phases"][-1]["consensus"] = [finding]
    raw_artifact["content_hash"] = evidence_repository.content_hash(
        raw_artifact["content"],
        raw_artifact.get("file_path"),
    )

    compatible, reason = screenplay_scene_shard_artifact_compatibility(
        artifact,
        **compatibility_kwargs,
    )

    assert compatible is False
    assert reason == "semantic_review_phase_contract"


@pytest.mark.parametrize("mutation", ["review", "consensus"])
def test_scene_shard_cache_rejects_post_repair_unit_key_expansion(
    mutation: str,
) -> None:
    artifact, raw_artifact, compatibility_kwargs = (
        _scene_shard_cache_compatibility_case(with_repair=True)
    )
    evidence = raw_artifact["content"]["semantic_review_evidence"]
    initial_unit_key = evidence["phases"][0]["consensus"][0]["unit_key"]
    outside_unit_key = next(
        unit["unit_key"]
        for scene in artifact["content"]["scenes"]
        for unit in scene["units"]
        if unit["unit_key"] != initial_unit_key
    )
    first_finding = {
        "unit_key": outside_unit_key,
        "code": "source_semantic_drift",
        "violation_kinds": ["source_contradiction"],
        "message": "post-repair reviewer 引入未标记 slot",
    }
    second_finding = {
        **first_finding,
        "message": "第二 reviewer 引入同一未标记 slot",
    }
    if mutation == "review":
        evidence["phases"][-1]["reviews"][0]["findings"] = [
            first_finding
        ]
    else:
        reviews = [
            ScreenplaySceneShardSemanticReview.model_validate(
                {"findings": [first_finding]}
            ),
            ScreenplaySceneShardSemanticReview.model_validate(
                {"findings": [second_finding]}
            ),
        ]
        expanded_consensus = [
            finding.model_dump(mode="json")
            for finding in scene_shards_module
            .screenplay_scene_semantic_consensus(*reviews)
        ]
        evidence["phases"].insert(1, {
            "phase": "post_repair",
            "creative_hash": evidence["reviewed_creative_hash"],
            "reviews": [
                {"findings": [first_finding]},
                {"findings": [second_finding]},
            ],
            "consensus": expanded_consensus,
        })
    raw_artifact["content_hash"] = evidence_repository.content_hash(
        raw_artifact["content"],
        raw_artifact.get("file_path"),
    )

    compatible, reason = screenplay_scene_shard_artifact_compatibility(
        artifact,
        **compatibility_kwargs,
    )

    assert compatible is False
    assert reason == "semantic_review_phase_contract"


def test_scene_shard_cache_accepts_typed_pair_linked_post_retarget() -> None:
    artifact, raw_artifact, compatibility_kwargs = (
        _scene_shard_cache_compatibility_case(with_repair=True)
    )
    evidence = raw_artifact["content"]["semantic_review_evidence"]
    unit_keys = [
        unit["unit_key"]
        for scene in artifact["content"]["scenes"]
        for unit in scene["units"]
    ]
    retarget_unit_key, allowed_unit_key = unit_keys[:2]
    initial_reviews = [
        ScreenplaySceneShardSemanticReview.model_validate({
            "findings": [{
                "unit_key": allowed_unit_key,
                "code": "state_subject_semantic_drift",
                "violation_kinds": ["wrong_subject"],
                "message": f"initial reviewer {reviewer_no}",
            }],
        })
        for reviewer_no in (1, 2)
    ]
    retarget_reviews = [
        ScreenplaySceneShardSemanticReview.model_validate({
            "findings": [{
                "unit_key": retarget_unit_key,
                "related_unit_keys": [allowed_unit_key],
                "code": "source_semantic_drift",
                "violation_kinds": ["cross_slot_duplication"],
                "message": f"retarget reviewer {reviewer_no}",
            }],
        })
        for reviewer_no in (1, 2)
    ]

    def phase_payload(
        reviews: list[ScreenplaySceneShardSemanticReview],
        *,
        phase: str,
        creative_hash: str,
    ) -> dict:
        return {
            "phase": phase,
            "creative_hash": creative_hash,
            "reviews": [
                review.model_dump(mode="json")
                for review in reviews
            ],
            "consensus": [
                finding.model_dump(mode="json")
                for finding in scene_shards_module
                .screenplay_scene_semantic_consensus(*reviews)
            ],
        }

    evidence["phases"] = [
        phase_payload(
            initial_reviews,
            phase="initial",
            creative_hash=evidence["initial_creative_hash"],
        ),
        phase_payload(
            retarget_reviews,
            phase="post_repair",
            creative_hash=evidence["reviewed_creative_hash"],
        ),
        {
            "phase": "post_repair",
            "creative_hash": evidence["reviewed_creative_hash"],
            "reviews": [{"findings": []}, {"findings": []}],
            "consensus": [],
        },
    ]
    raw_artifact["content_hash"] = evidence_repository.content_hash(
        raw_artifact["content"],
        raw_artifact.get("file_path"),
    )

    compatible, reason = screenplay_scene_shard_artifact_compatibility(
        artifact,
        **compatibility_kwargs,
    )

    assert compatible is True, reason


def test_envelope_cache_binds_raw_artifact_content_hash() -> None:
    blueprint = _blueprint(split_domain=False)
    envelope_content = _envelope(blueprint).model_dump(mode="json")
    raw_content = {"operation_id": "envelope-op", "attempts": []}
    raw_artifact = {
        "id": "raw-envelope-hash-binding",
        "type": "screenplay_envelope_raw",
        "scope_type": "episode",
        "scope_id": "ep-envelope-hash-binding",
        "status": "candidate",
        "content": raw_content,
        "content_hash": evidence_repository.content_hash(raw_content),
        "file_path": None,
        "parent_artifact_ids": ["blueprint-artifact", "identity-artifact"],
        "contract_version": scene_shards_module.SCREENPLAY_ENVELOPE_VERSION,
    }
    artifact = {
        "id": "envelope-hash-binding",
        "type": "screenplay_envelope",
        "scope_type": "episode",
        "scope_id": "ep-envelope-hash-binding",
        "status": "validated",
        "content": envelope_content,
        "content_hash": evidence_repository.content_hash(envelope_content),
        "parent_artifact_ids": [raw_artifact["id"]],
        "contract_version": scene_shards_module.SCREENPLAY_ENVELOPE_VERSION,
    }
    kwargs = {
        "expected_blueprint_hash": envelope_content["blueprint_hash"],
        "expected_identity_registry_hash": (
            envelope_content["identity_registry_hash"]
        ),
        "raw_artifact": raw_artifact,
        "expected_authority_artifact_ids": {
            "blueprint-artifact", "identity-artifact",
        },
    }

    compatible, reason = screenplay_envelope_artifact_compatibility(
        artifact,
        **kwargs,
    )
    assert compatible is True, reason

    raw_artifact["content"]["attempts"].append({"outcome": "tampered"})
    compatible, reason = screenplay_envelope_artifact_compatibility(
        artifact,
        **kwargs,
    )
    assert compatible is False
    assert reason == "raw_artifact_content_hash"


def test_scene_shard_cache_binds_raw_artifact_content_hash() -> None:
    artifact, raw_artifact, compatibility_kwargs = (
        _scene_shard_cache_compatibility_case(with_repair=False)
    )

    compatible, reason = screenplay_scene_shard_artifact_compatibility(
        artifact,
        **compatibility_kwargs,
    )
    assert compatible is True, reason

    raw_artifact["content"]["attempts"].append({"outcome": "tampered"})
    compatible, reason = screenplay_scene_shard_artifact_compatibility(
        artifact,
        **compatibility_kwargs,
    )
    assert compatible is False
    assert reason == "raw_artifact_content_hash"


def test_validated_scene_shard_is_reused_without_provider_call(monkeypatch) -> None:
    blueprint = _blueprint(split_domain=True)
    plans = build_screenplay_scene_shard_plans(
        blueprint,
        source_text=SOURCE,
        identity_registry_hash="identity-hash",
    )
    episode_id = "ep-scene-shard-cache-test"
    blueprint_artifact = evidence_repository.create_artifact(EvidenceArtifact(
        type="screenplay_narrative_blueprint",
        scope_type="episode",
        scope_id=episode_id,
        status="validated",
        trust_level="T1",
        content=blueprint.model_dump(mode="json"),
        contract_version=blueprint.format_version,
    ))
    identity_artifact = evidence_repository.create_artifact(EvidenceArtifact(
        type="screenplay_identity_registry",
        scope_type="episode",
        scope_id=episode_id,
        status="validated",
        trust_level="T1",
        content={
            "contract_version": "screenplay-identity-registry.v1",
            "identity_registry_hash": "identity-hash",
            "identities": [],
        },
        parent_artifact_ids=[blueprint_artifact["id"]],
        contract_version="screenplay-identity-registry.v1",
    ))
    cached_ids: list[str] = []
    for plan in plans:
        shard = _shard(plan, blueprint)
        shard_payload = shard.model_dump(mode="json")
        creative = _creative_shard(
            plan,
            blueprint,
            _contracts([plan], blueprint)[plan.shard_id],
        )
        creative_hash = scene_shards_module._hash(
            creative.model_dump(mode="json")
        )
        reviewed_shard_content_hash = evidence_repository.content_hash(
            shard_payload
        )
        raw = evidence_repository.create_artifact(EvidenceArtifact(
            type="screenplay_scene_shard_raw",
            scope_type="episode",
            scope_id=episode_id,
            status="candidate",
            trust_level="T0",
            content={
                "shard_id": plan.shard_id,
                "attempts": [],
                "semantic_review_evidence": {
                    "contract_version": (
                        SCREENPLAY_SCENE_SEMANTIC_REVIEW_VERSION
                    ),
                    "initial_creative_hash": creative_hash,
                    "reviewed_creative_hash": creative_hash,
                    "reviewed_shard_content_hash": reviewed_shard_content_hash,
                    "phases": [{
                        "phase": "initial",
                        "creative_hash": creative_hash,
                        "reviews": [
                            {"findings": []}, {"findings": []},
                        ],
                        "consensus": [],
                    }],
                },
            },
            parent_artifact_ids=[
                blueprint_artifact["id"],
                identity_artifact["id"],
            ],
            contract_version=SCREENPLAY_SCENE_SHARD_VERSION,
        ))
        artifact = evidence_repository.create_artifact(EvidenceArtifact(
            type="screenplay_scene_shard",
            scope_type="episode",
            scope_id=episode_id,
            status="validated",
            trust_level="T1",
            content=shard_payload,
            parent_artifact_ids=[raw["id"]],
            contract_version=SCREENPLAY_SCENE_SHARD_VERSION,
            model_snapshot={
                "semantic_review_version": (
                    SCREENPLAY_SCENE_SEMANTIC_REVIEW_VERSION
                ),
                "reviewed_creative_hash": creative_hash,
                "reviewed_shard_content_hash": reviewed_shard_content_hash,
            },
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
        blueprint_artifact_id=blueprint_artifact["id"],
        identity_artifact_id=identity_artifact["id"],
    ))
    assert len(shards) == len(plans)
    assert artifact_ids == cached_ids
    assert all(row["status"] == "validated" for row in rows)
    assert all(row["attempt"] == 0 and row["reused"] for row in rows)


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ("missing", "semantic_review_evidence_missing"),
        ("evidence_version", "semantic_review_version"),
        ("legacy_v4", "semantic_review_version"),
        ("legacy_v5", "semantic_review_version"),
        ("legacy_v6", "semantic_review_version"),
        ("legacy_v8", "semantic_review_version"),
        ("legacy_v9", "semantic_review_version"),
        ("legacy_v10", "semantic_review_version"),
        ("legacy_v11", "semantic_review_version"),
        ("snapshot_version", "semantic_review_version"),
        ("missing_shard_hash", "semantic_review_shard_hash"),
        ("missing_snapshot_shard_hash", "semantic_review_shard_hash"),
        ("reviewed_hash", "semantic_review_hash_binding"),
        ("wrong_phase", "semantic_review_phase"),
        ("reordered_phases", "semantic_review_phase"),
        ("duplicate_phase", "semantic_review_phase"),
        ("initial_candidate", "semantic_review_initial_candidate"),
        ("not_clean", "semantic_review_phase_contract"),
        ("fake_clean", "semantic_review_consensus"),
        ("consensus_order", "semantic_review_consensus"),
        ("second_reviewer_consensus", "semantic_review_consensus"),
        ("duplicate_finding", "semantic_review_duplicate_finding"),
        ("malformed_phase", "semantic_review_artifacts_missing"),
        ("empty_review", "semantic_review_schema"),
        ("legacy_issues", "semantic_review_schema"),
        ("review_extra", "semantic_review_schema"),
        ("bad_finding", "semantic_review_schema"),
        ("unknown_unit", "semantic_review_unit_key"),
    ],
)
def test_scene_shard_review_evidence_is_exact_cache_authority(
    monkeypatch,
    mutation: str,
    reason: str,
) -> None:
    blueprint = _blueprint(split_domain=False)
    plan = build_screenplay_scene_shard_plans(
        blueprint, source_text=SOURCE, identity_registry_hash="identity-hash",
    )[0]
    episode_id = f"ep-semantic-review-evidence-{mutation}"
    blueprint_artifact = evidence_repository.create_artifact(EvidenceArtifact(
        type="screenplay_narrative_blueprint",
        scope_type="episode",
        scope_id=episode_id,
        status="validated",
        trust_level="T1",
        content=blueprint.model_dump(mode="json"),
        contract_version=blueprint.format_version,
    ))
    identity_artifact = evidence_repository.create_artifact(EvidenceArtifact(
        type="screenplay_identity_registry",
        scope_type="episode",
        scope_id=episode_id,
        status="validated",
        trust_level="T1",
        content={
            "contract_version": "screenplay-identity-registry.v1",
            "identity_registry_hash": "identity-hash",
            "identities": [],
        },
        parent_artifact_ids=[blueprint_artifact["id"]],
        contract_version="screenplay-identity-registry.v1",
    ))
    contracts = _contracts([plan], blueprint)

    async def creative_response(*_args, **_kwargs):
        return _creative_shard(plan, blueprint, contracts[plan.shard_id])

    monkeypatch.setattr(
        "app.screenplay_scene_shards.model_gateway.chat_structured",
        creative_response,
    )
    _shards, artifact_ids, _rows = asyncio.run(
        generate_screenplay_scene_shards(
            episode={"id": episode_id, "episode_no": 1},
            source_text=SOURCE,
            blueprint=blueprint,
            identity_registry=[],
            identities=_identities(),
            plans=[plan],
            scene_input_contracts=contracts,
            blueprint_artifact_id=blueprint_artifact["id"],
            identity_artifact_id=identity_artifact["id"],
        )
    )
    artifact = evidence_repository.get_artifact(artifact_ids[0])
    assert artifact is not None
    assert (
        artifact["model_snapshot"]["reviewed_shard_content_hash"]
        == artifact["content_hash"]
    )
    raw_id = artifact["parent_artifact_ids"][0]
    raw = evidence_repository.get_artifact(raw_id)
    assert raw is not None
    compatible, actual_reason = screenplay_scene_shard_artifact_compatibility(
        artifact,
        expected_blueprint_hash=plan.blueprint_hash,
        expected_identity_registry_hash=plan.identity_registry_hash,
        expected_generation_scaffold_hash=(
            screenplay_scene_generation_scaffold_hash(
                plan, contracts[plan.shard_id],
            )
        ),
        raw_artifact=raw,
        expected_authority_artifact_ids={
            blueprint_artifact["id"], identity_artifact["id"],
        },
    )
    assert compatible is True, actual_reason

    artifact = deepcopy(artifact)
    raw = deepcopy(raw)
    evidence = raw["content"]["semantic_review_evidence"]
    if mutation == "missing":
        raw["content"].pop("semantic_review_evidence")
    elif mutation == "evidence_version":
        evidence["contract_version"] = "screenplay-scene-semantic-review.v2"
    elif mutation == "legacy_v4":
        evidence["contract_version"] = "screenplay-scene-semantic-review.v4"
    elif mutation == "legacy_v5":
        evidence["contract_version"] = "screenplay-scene-semantic-review.v5"
    elif mutation == "legacy_v6":
        evidence["contract_version"] = "screenplay-scene-semantic-review.v6"
    elif mutation == "legacy_v8":
        evidence["contract_version"] = "screenplay-scene-semantic-review.v8"
    elif mutation == "legacy_v9":
        evidence["contract_version"] = "screenplay-scene-semantic-review.v9"
        artifact["model_snapshot"]["semantic_review_version"] = (
            "screenplay-scene-semantic-review.v9"
        )
    elif mutation == "legacy_v10":
        evidence["contract_version"] = "screenplay-scene-semantic-review.v10"
        artifact["model_snapshot"]["semantic_review_version"] = (
            "screenplay-scene-semantic-review.v10"
        )
    elif mutation == "legacy_v11":
        evidence["contract_version"] = "screenplay-scene-semantic-review.v11"
        artifact["model_snapshot"]["semantic_review_version"] = (
            "screenplay-scene-semantic-review.v11"
        )
    elif mutation == "snapshot_version":
        artifact["model_snapshot"]["semantic_review_version"] = (
            "screenplay-scene-semantic-review.v1"
        )
    elif mutation == "missing_shard_hash":
        evidence.pop("reviewed_shard_content_hash")
    elif mutation == "missing_snapshot_shard_hash":
        artifact["model_snapshot"].pop("reviewed_shard_content_hash")
    elif mutation == "reviewed_hash":
        artifact["model_snapshot"]["reviewed_creative_hash"] = "0" * 64
    elif mutation == "wrong_phase":
        evidence["phases"][0]["phase"] = "post_repair"
    elif mutation == "reordered_phases":
        second_phase = deepcopy(evidence["phases"][0])
        second_phase["phase"] = "post_repair"
        evidence["phases"] = [second_phase, evidence["phases"][0]]
    elif mutation == "duplicate_phase":
        evidence["phases"].append(deepcopy(evidence["phases"][0]))
    elif mutation == "initial_candidate":
        evidence["phases"][0]["creative_hash"] = "0" * 64
    elif mutation == "not_clean":
        unit_key = artifact["content"]["scenes"][0]["units"][0][
            "unit_key"
        ]
        reviews = [
            ScreenplaySceneShardSemanticReview.model_validate({
                "findings": [{
                    "unit_key": unit_key,
                    "code": "source_semantic_drift",
                    "violation_kinds": ["source_contradiction"],
                    "message": f"reviewer {reviewer_no}",
                }],
            })
            for reviewer_no in (1, 2)
        ]
        evidence["phases"][-1]["reviews"] = [
            review.model_dump(mode="json")
            for review in reviews
        ]
        evidence["phases"][-1]["consensus"] = [
            finding.model_dump(mode="json")
            for finding in scene_shards_module
            .screenplay_scene_semantic_consensus(*reviews)
        ]
    elif mutation == "fake_clean":
        unit_key = artifact["content"]["scenes"][0]["units"][0][
            "unit_key"
        ]
        finding = {
            "unit_key": unit_key,
            "code": "source_semantic_drift",
            "violation_kinds": ["source_contradiction"],
            "message": "被伪装成 clean",
        }
        evidence["phases"][0]["reviews"] = [
            {"findings": [finding]},
            {"findings": [finding]},
        ]
    elif mutation == "consensus_order":
        unit_key = artifact["content"]["scenes"][0]["units"][0][
            "unit_key"
        ]
        findings = [
            {
                "unit_key": unit_key,
                "code": "state_subject_semantic_drift",
                "violation_kinds": ["wrong_subject"],
                "message": "排序后应位于第二",
            },
            {
                "unit_key": unit_key,
                "code": "source_semantic_drift",
                "violation_kinds": ["source_contradiction"],
                "message": "排序后应位于第一",
            },
        ]
        evidence["phases"][0]["reviews"] = [
            {"findings": findings},
            {"findings": findings},
        ]
        evidence["phases"][0]["consensus"] = findings
    elif mutation == "second_reviewer_consensus":
        unit_key = artifact["content"]["scenes"][0]["units"][0][
            "unit_key"
        ]
        first_finding = {
            "unit_key": unit_key,
            "code": "source_semantic_drift",
            "violation_kinds": ["source_contradiction"],
            "message": "第一 reviewer",
        }
        second_finding = {**first_finding, "message": "第二 reviewer"}
        evidence["phases"][0]["reviews"] = [
            {"findings": [first_finding]},
            {"findings": [second_finding]},
        ]
        evidence["phases"][0]["consensus"] = [second_finding]
    elif mutation == "duplicate_finding":
        unit_key = artifact["content"]["scenes"][0]["units"][0][
            "unit_key"
        ]
        finding = {
            "unit_key": unit_key,
            "code": "source_semantic_drift",
            "violation_kinds": ["source_contradiction"],
            "message": "重复 finding",
        }
        evidence["phases"][0]["reviews"][0]["findings"] = [
            finding, finding,
        ]
    elif mutation == "malformed_phase":
        evidence["phases"] = [None]
    elif mutation == "empty_review":
        evidence["phases"][0]["reviews"][0] = {}
    elif mutation == "legacy_issues":
        evidence["phases"][0]["reviews"][0] = {"issues": []}
    elif mutation == "review_extra":
        evidence["phases"][0]["reviews"][0]["explanation"] = "伪 clean"
    elif mutation == "bad_finding":
        unit_key = artifact["content"]["scenes"][0]["units"][0][
            "unit_key"
        ]
        evidence["phases"][0]["reviews"][0]["findings"] = [{
            "unit_key": unit_key,
            "code": "unsupported_code",
            "violation_kinds": ["source_contradiction"],
            "message": "伪 finding",
        }]
    else:
        evidence["phases"][0]["reviews"][0]["findings"] = [{
            "unit_key": "UNKNOWN-V2-UNIT",
            "code": "source_semantic_drift",
            "violation_kinds": ["source_contradiction"],
            "message": "引用不存在的 unit",
        }]
    raw["content_hash"] = evidence_repository.content_hash(
        raw["content"],
        raw.get("file_path"),
    )
    compatible, actual_reason = screenplay_scene_shard_artifact_compatibility(
        artifact,
        expected_blueprint_hash=plan.blueprint_hash,
        expected_identity_registry_hash=plan.identity_registry_hash,
        expected_generation_scaffold_hash=(
            screenplay_scene_generation_scaffold_hash(
                plan, contracts[plan.shard_id],
            )
        ),
        raw_artifact=raw,
        expected_authority_artifact_ids={
            blueprint_artifact["id"], identity_artifact["id"],
        },
    )
    assert compatible is False
    assert actual_reason == reason


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
    with pytest.raises(ScreenplaySceneShardOwnershipLost, match="owner changed"):
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
    format_repair_contexts: dict[str, dict] = {}
    operation_ids: dict[str, str] = {}
    output_schemas: dict[str, dict] = {}
    response_formats: dict[str, dict] = {}
    response_format_required: dict[str, bool] = {}
    repair_schema_builders: dict[str, object] = {}
    identity_registry = [
        {
            "identity_key": f"person_{label}",
            "authority_id": f"bible:{label}",
            "canonical_name": label,
            "source_labels": [label],
        }
        for label in ("甲", "乙")
    ]
    scene_input_contracts = _contracts(
        plans,
        blueprint,
        identity_registry,
    )

    async def fake_structured(messages, **kwargs):
        meta = kwargs["call_meta"]
        prompt_key = str(meta["stage_key"] + ":" + meta.get("shard_id", ""))
        prompts[prompt_key] = messages[0]["content"]
        operation_ids[prompt_key] = kwargs["operation_id"]
        if kwargs.get("output_schema"):
            output_schemas[prompt_key] = kwargs["output_schema"]
        if kwargs.get("response_format"):
            response_formats[prompt_key] = kwargs["response_format"]
        response_format_required[prompt_key] = bool(
            kwargs.get("require_response_format")
        )
        if kwargs.get("repair_schema"):
            repair_schema_builders[prompt_key] = kwargs["repair_schema"]
        if kwargs.get("repair_context"):
            repair_contexts[prompt_key] = json.loads(kwargs["repair_context"])
        if kwargs.get("format_repair_context"):
            format_repair_contexts[prompt_key] = json.loads(
                kwargs["format_repair_context"]
            )
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
        identity_registry=identity_registry,
        identities=_identities(),
        plans=plans,
        scene_input_contracts=scene_input_contracts,
    ))
    envelope_prompt = prompts["screenplay_envelope:"]
    assert "甲推门进入。" not in envelope_prompt
    assert "乙接过钥匙并回答。" not in envelope_prompt
    first_prompt = prompts["screenplay_scene_shards:SS001"]
    second_prompt = prompts["screenplay_scene_shards:SS002"]
    assert "根对象只能包含 contract_version 与 slots" in first_prompt
    assert "模型无权输出或修改" in first_prompt
    assert "dialogue slot 的 text 已由 Schema 固定" in first_prompt
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
    first_repair_context = repair_contexts[
        "screenplay_scene_shards:SS001"
    ]
    assert format_repair_contexts[
        "screenplay_scene_shards:SS001"
    ] == first_repair_context
    first_authority = first_repair_context["exact_slot_authority"]
    first_unit_key = plans[0].unit_slots[0].unit_key
    assert first_authority[first_unit_key]["source_fact"]["text"] == (
        "甲推门进入。"
    )
    assert first_authority[first_unit_key]["state_subject_key"] == (
        "person_甲"
    )
    assert first_authority[first_unit_key]["actor_keys"] == ["person_甲"]
    assert first_authority[first_unit_key]["environment_only"] is False
    assert first_repair_context["identity_authority"]["person_甲"][
        "authority_id"
    ] == "bible:甲"
    assert first_repair_context["root_contract"]["contract_version"] == (
        "screenplay-scene-creative.v8"
    )
    assert (
        "empty action source_text does not authorize free rewriting; "
        "use only that slot's source_fact.text"
        in first_repair_context["final_gate_contract"]
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
    assert response_format_required[
        "screenplay_scene_shards:SS001"
    ] is True
    assert response_formats["screenplay_scene_shards:SS001"] == (
        scene_shards_module._scene_shard_strict_response_format(
            name="screenplay_scene_shard_creative",
            local_schema=output_schemas[
                "screenplay_scene_shards:SS001"
            ],
        )
    )
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
        "creative-content-for-deterministic-generation-slots"
    )
    assert operation_ids["screenplay_scene_shards:SS001"].startswith(
        f"screenplay.scene-shard:{SCREENPLAY_SCENE_SHARD_VERSION}:"
        f"{SCREENPLAY_SCENE_INPUT_VERSION}:"
    )
    assert operation_ids["screenplay_scene_shards:SS001"].endswith(
        screenplay_scene_generation_scaffold_hash(
            plans[0],
            scene_input_contracts[plans[0].shard_id],
        )
    )
    assert "逐 slot exact authority" in first_prompt
    assert "action slot 即使 source_text 为空也不授权自由改写" in (
        first_prompt
    )
    assert '"source_fact":' in first_prompt
    assert '"authority_id":"bible:甲"' in first_prompt
    assert SCREENPLAY_SCENE_INPUT_VERSION == "screenplay-scene-input.v10"
    assert "甲推门进入。" in first_prompt
    assert "乙接过钥匙并回答。" not in first_prompt
    assert "乙接过钥匙并回答。" in second_prompt
    assert "甲推门进入。" not in second_prompt
    latest_raw = evidence_repository.latest_artifact(
        "screenplay_scene_shard_raw",
        "episode",
        episode_id,
    )
    latest_shard = evidence_repository.latest_artifact(
        "screenplay_scene_shard",
        "episode",
        episode_id,
    )
    assert latest_raw is not None
    assert latest_shard is not None
    assert latest_raw["content"]["creative_contract_version"] == (
        SCREENPLAY_SCENE_CREATIVE_VERSION
    )
    assert latest_shard["model_snapshot"][
        "creative_contract_version"
    ] == SCREENPLAY_SCENE_CREATIVE_VERSION


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

    with pytest.raises(
        model_gateway.StructuredFormatError,
        match="extra_forbidden",
    ):
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

    with pytest.raises(
        ScreenplaySceneMergeError,
        match="identity scaffold drift",
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


def test_scene_contract_allows_identity_explicitly_shared_by_both_scenes() -> None:
    blueprint, plans, registry, identities, shard = _participant_case(
        shared_identity=True,
    )
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
        ["person_a", "person_shared"],
        ["person_b", "person_shared"],
    ]
    assert [
        scene.units[0].actor_keys for scene in merged.scenes
    ] == [
        ["person_a"],
        ["person_b"],
    ]


def test_normalization_rejects_unbound_target_scaffold_drift() -> None:
    blueprint = _blueprint(split_domain=True)
    plan = build_screenplay_scene_shard_plans(
        blueprint,
        source_text=SOURCE,
        identity_registry_hash="identity-hash",
    )[0]
    shard = _shard(plan, blueprint)
    shard.scenes[0].units[0].target_keys = ["unbound-person"]
    scene_input_contracts = _contracts([plan], blueprint)[plan.shard_id]

    with pytest.raises(
        ScreenplaySceneShardError,
        match="identity scaffold drift",
    ):
        normalize_screenplay_scene_shard(
            shard,
            episode_no=1,
            plan=plan,
            scene_plans={item.key: item for item in blueprint.scene_plans},
            scene_input_contracts=scene_input_contracts,
        )

    assert shard.scenes[0].units[0].target_keys == ["unbound-person"]
    errors = validate_screenplay_scene_shard(
        shard,
        plan=plan,
        scene_plans={item.key: item for item in blueprint.scene_plans},
        scene_input_contracts=scene_input_contracts,
        identity_keys={"narrator"},
    )
    assert any("identity scaffold drift" in error for error in errors)


def _ss004_533ac9_compile_context(*, current_contract: bool = True):
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
    source_by_id = {
        segment["source_segment_id"]: segment["text"]
        for item in replay_input["scene_inputs"]
        if item["scene_plan_key"] in selected_keys
        for segment in item["source_segments"]
    }
    selected_slots = scene_shards_module._build_group_unit_slots(
        list(scene_plans.values()),
        source_by_id=source_by_id,
        scene_order_by_key={
            "bp-sc014": 14,
            "bp-sc015": 15,
        },
    )
    selected_plan = plan.model_copy(update={
        "scene_plan_keys": selected_keys,
        "source_segment_ids": [
            source_id
            for key in selected_keys
            for source_id in scene_plans[key].source_segment_ids
        ],
        "source_scene_owners": source_owners,
        "unit_slots": selected_slots,
        "estimated_units": len(selected_slots),
    })
    if current_contract:
        dialogue_unit_keys_by_source = {
            source_id: [
                slot.source_unit_key
                for slot in selected_slots
                if (
                    slot.kind == "dialogue"
                    and source_id in slot.source_segment_ids
                )
            ]
            for source_id in source_by_id
        }
        for action_evidence in replay["action_evidence"]:
            for participant in action_evidence["participant_evidence"]:
                if (
                    participant["usage"] != "voice"
                    or participant.get("source_unit_keys")
                ):
                    continue
                source_unit_keys = [
                    source_unit_key
                    for source_id in participant["source_segment_ids"]
                    for source_unit_key in dialogue_unit_keys_by_source.get(
                        source_id, []
                    )
                ]
                if source_unit_keys:
                    participant["source_unit_keys"] = source_unit_keys
        # This branch is the explicit current-contract clone used by compiler
        # positives.  The untouched legacy replay remains invalid.  These
        # owners are source-unit authority, not inferred from visible roster.
        state_subject_by_source = {
            "SRC0054": "孟浩",
            "SRC0055": "孟浩",
            "SRC0056": "绿袍执事乙",
            "SRC0057": "绿袍执事甲",
            "SRC0058": "王有材",
        }
        state_subjects = {
            fact.source_unit_key: identity_key
            for source_id, identity_key in state_subject_by_source.items()
            for fact in source_segment_facts(
                source_id,
                source_by_id[source_id],
            )
            if fact.projection == "action"
        }
        for action_evidence in replay["action_evidence"]:
            for source_unit_key, identity_key in state_subjects.items():
                source_id = source_unit_key.split(":unit:", 1)[0]
                if source_id not in action_evidence["source_segment_ids"]:
                    continue
                action_evidence["participant_evidence"].append({
                    "identity_key": identity_key,
                    "source_segment_ids": [source_id],
                    "source_unit_keys": [source_unit_key],
                    "usage": "state_subject",
                })
                if source_id in {"SRC0056", "SRC0057", "SRC0058"}:
                    action_evidence["participant_evidence"].append({
                        "identity_key": identity_key,
                        "source_segment_ids": [source_id],
                        "source_unit_keys": [source_unit_key],
                        "usage": "visible",
                    })
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
            "narrative_layer": "story",
            "event_priority": "causal",
            "render_policy": "standalone",
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


def _recorded_response_slot_draft(
    response: dict,
    plan: ScreenplaySceneShardPlan,
    contracts: list[ScreenplaySceneInputContract],
) -> ScreenplaySceneShardCreativeIR:
    replay_input = json.loads(
        SS004_REPLAY_INPUT.read_text(encoding="utf-8")
    )
    source_text_by_id = {
        segment["source_segment_id"]: segment["text"]
        for scene_input in replay_input["scene_inputs"]
        for segment in scene_input["source_segments"]
    }
    content_by_signature: dict[tuple[str, str, str], dict] = {}
    del contracts
    for scene in response["scenes"]:
        for unit in scene["units"]:
            assert len(unit["source_segment_ids"]) == 1
            signature = (
                scene["scene_plan_key"],
                unit["source_segment_ids"][0],
                unit["kind"],
            )
            assert signature not in content_by_signature
            content_by_signature[signature] = unit
    slots: dict[str, dict] = {}
    for slot in plan.unit_slots:
        signature = (
            slot.scene_key,
            slot.source_segment_ids[0],
            slot.kind,
        )
        recorded = content_by_signature.get(signature, {})
        slots[slot.unit_key] = {
            "text": (
                slot.source_text
                if slot.kind == "dialogue"
                else str(
                    recorded.get("text")
                    or source_text_by_id[slot.source_segment_ids[0]]
                )
            ),
            "performance": "",
            "resulting_state": recorded.get("resulting_state", ""),
            "function": recorded.get("function", "statement"),
        }
    return ScreenplaySceneShardCreativeIR.model_validate({
        "slots": slots,
    })


def test_scene_shard_creative_schema_is_closed_and_rejects_identity_authority() -> None:
    replay, plan, _scene_plans, contracts = (
        _ss004_533ac9_compile_context()
    )
    draft_type = getattr(
        scene_shards_module,
        "ScreenplaySceneShardCreativeIR",
        None,
    )
    assert draft_type is not None

    schema = build_screenplay_scene_shard_repair_schema(
        plan=plan,
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


def test_err_533ac9_legacy_voice_contract_requires_baseline_rebuild() -> None:
    with pytest.raises(
        ScreenplaySceneShardError,
        match="voice identity evidence .*source_unit_keys",
    ):
        _ss004_533ac9_compile_context(current_contract=False)


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
    draft = _recorded_response_slot_draft(
        replay["creative_response"],
        plan,
        contracts,
    )
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
    assignment_line = next(
        unit
        for unit in scene_14.units
        if (
            unit.kind == "dialogue"
            and unit.source_segment_ids == ["SRC0057"]
        )
    )
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
    for unit in (
        unit
        for unit in scene_14.units
        if set(unit.source_segment_ids) <= {"SRC0056", "SRC0057"}
    ):
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
    location_answer = next(
        unit
        for unit in scene_15.units
        if (
            unit.kind == "dialogue"
            and unit.source_segment_ids == ["SRC0059"]
        )
    )
    assert location_answer.speaker_key == "person_46e7e8b742ed"
    assert location_answer.onscreen_entity_keys == [meng_hao]
    assert [
        delivery.participant_key
        for delivery in location_answer.participant_deliveries
    ] == ["person_46e7e8b742ed"]
    assert location_answer.participant_deliveries[0].audible is True


def test_scene_shard_contract_fingerprint_is_upgraded(
    monkeypatch,
) -> None:
    assert SCREENPLAY_SCENE_SHARD_VERSION == "screenplay-scene-shard.v11"
    assert SCREENPLAY_SCENE_INPUT_VERSION == "screenplay-scene-input.v10"
    assert SCREENPLAY_SCENE_CREATIVE_VERSION == (
        "screenplay-scene-creative.v8"
    )
    with pytest.raises(ValidationError, match="screenplay-scene-creative.v8"):
        ScreenplaySceneShardCreativeIR.model_validate({
            "contract_version": "screenplay-scene-creative.v7",
            "slots": {},
        })

    blueprint = _blueprint(split_domain=False)
    plan = build_screenplay_scene_shard_plans(
        blueprint,
        source_text=SOURCE,
        identity_registry_hash="identity-hash",
    )[0]
    contracts = _contracts([plan], blueprint)[plan.shard_id]
    current_hash = screenplay_scene_generation_scaffold_hash(
        plan,
        contracts,
    )
    monkeypatch.setattr(
        scene_shards_module,
        "SCREENPLAY_SCENE_CREATIVE_VERSION",
        "screenplay-scene-creative.v7",
    )
    legacy_hash = screenplay_scene_generation_scaffold_hash(
        plan,
        contracts,
    )
    monkeypatch.setattr(
        scene_shards_module,
        "SCREENPLAY_SCENE_CREATIVE_VERSION",
        SCREENPLAY_SCENE_CREATIVE_VERSION,
    )

    assert legacy_hash != current_hash
    artifact, _raw_artifact, compatibility_kwargs = (
        _scene_shard_cache_compatibility_case(with_repair=False)
    )
    artifact["content"]["generation_scaffold_hash"] = legacy_hash
    artifact["content_hash"] = evidence_repository.content_hash(
        artifact["content"]
    )
    compatible, reason = screenplay_scene_shard_artifact_compatibility(
        artifact,
        **compatibility_kwargs,
    )
    assert compatible is False
    assert reason == "generation_scaffold_hash"


def test_run_195a691_replays_ten_ownership_overreaches() -> None:
    replay = json.loads(RUN_195A691_REPLAY.read_text(encoding="utf-8"))

    assert replay["run_id"] == "run_195a69113451"
    assert replay["error_id"] == "ERR-20260811-ec6240"
    assert [call["provider_call_id"] for call in replay["calls"]] == [
        61019, 61020, 61022, 61023, 61025,
        61026, 61027, 61028, 61029, 61030,
    ]
    assert {call["shard_id"] for call in replay["calls"]} == {
        "SS001", "SS002", "SS003", "SS004", "SS005",
    }
    for call in replay["calls"]:
        payload = deepcopy(call["response"])
        payload["contract_version"] = SCREENPLAY_SCENE_CREATIVE_VERSION
        with pytest.raises(ValidationError) as caught:
            ScreenplaySceneShardCreativeIR.model_validate(payload)
        forbidden = {
            error["loc"][-1]
            for error in caught.value.errors()
            if error["type"] == "extra_forbidden"
        }
        assert forbidden == {"agency_kind", "text_provenance"}

    with pytest.raises(ValidationError, match="identity_keys"):
        ScreenplaySceneShardCreativeUnit.model_validate({
            "text": "模型不得声明身份",
            "identity_keys": ["person_b67de643afe6"],
        })


def test_compiled_unit_slot_round_trip_preserves_derived_action_agency() -> None:
    slot = ScreenplaySceneCompiledUnitSlot.model_validate({
        "unit_key": "unit-1",
        "event_key": "event-1",
        "scene_key": "scene-1",
        "scene_order": 1,
        "unit_order": 1,
        "scene_unit_order": 1,
        "kind": "action",
        "narrative_layer": "story",
        "event_priority": "causal",
        "render_policy": "standalone",
        "source_segment_ids": ["SRC0001"],
        "actor_keys": [],
        "target_keys": [],
    })

    serialized = slot.model_dump(mode="json")

    assert serialized["action_agency"] == {
        "kind": "unattributed",
        "identity_bearing": False,
        "source_segment_ids": ["SRC0001"],
    }
    assert ScreenplaySceneCompiledUnitSlot.model_validate(serialized) == slot


def test_a78_replay_cannot_create_character_relation_from_text() -> None:
    replay, plan, scene_plan, contract, creative = _a78_replay_models()

    assert replay["published_artifact"]["artifact_id"] == "art_5e0650367127"
    assert replay["source_lineage"]["merged_ir_artifact_id"] == (
        "art_f3e3b246d77e"
    )
    assert replay["source_lineage"]["scene_shard_artifact_id"] == (
        "art_d1de89b55073"
    )
    assert replay["source_lineage"]["provider_call_id"] == 61001
    raw_response = deepcopy(replay["provider_creative_response"])
    raw_response["contract_version"] = SCREENPLAY_SCENE_CREATIVE_VERSION
    with pytest.raises(ValidationError) as caught:
        ScreenplaySceneShardCreativeIR.model_validate(raw_response)
    assert {
        error["loc"][-1]
        for error in caught.value.errors()
        if error["type"] == "extra_forbidden"
    } == {"agency_kind"}

    shard = scene_shards_module.compile_screenplay_scene_shard_draft(
        creative,
        episode_no=1,
        plan=plan,
        scene_plans={scene_plan.key: scene_plan},
        scene_input_contracts=[contract],
    )
    unit = shard.scenes[0].units[0]
    assert unit.actor_keys == []
    assert unit.target_keys == []
    assert unit.action_agency.kind == "unattributed"
    assert unit.text_provenance.kind == "creative_action"
    assert unit.text_provenance.identity_keys == []


@pytest.mark.parametrize(
    ("text_field", "text"),
    [
        pytest.param(
            "on_screen_text",
            "片头题字“孟浩”浮现在画面中央",
            id="on_screen_title",
        ),
        pytest.param(
            "required_text",
            "画面必须准确显示“孟浩”",
            id="required_text",
        ),
        pytest.param(
            "prop_text",
            "腰牌刻字“孟浩”清晰可见",
            id="prop_text",
        ),
    ],
)
def test_compiler_owned_text_provenance_does_not_create_character_relation(
    text_field: str,
    text: str,
) -> None:
    _replay, plan, scene_plan, contract, creative = _a78_replay_models(
        creative_update={
            "text": text,
            text_field: text,
        },
    )

    shard = scene_shards_module.compile_screenplay_scene_shard_draft(
        creative,
        episode_no=1,
        plan=plan,
        scene_plans={scene_plan.key: scene_plan},
        scene_input_contracts=[contract],
    )

    unit = shard.scenes[0].units[0]
    assert unit.actor_keys == []
    assert unit.target_keys == []
    assert unit.onscreen_entity_keys == []
    assert unit.action_agency.kind == text_field
    assert unit.action_agency.identity_bearing is False
    assert unit.text_provenance.kind == text_field
    assert unit.text_provenance.identity_keys == []


def test_anonymous_group_action_does_not_require_identity_relation() -> None:
    _replay, plan, scene_plan, contract, creative = _a78_replay_models(
        creative_update={
            "text": "四个少年同时后退一步",
        },
    )

    shard = scene_shards_module.compile_screenplay_scene_shard_draft(
        creative,
        episode_no=1,
        plan=plan,
        scene_plans={scene_plan.key: scene_plan},
        scene_input_contracts=[contract],
    )

    unit = shard.scenes[0].units[0]
    assert unit.actor_keys == []
    assert unit.target_keys == []
    assert unit.action_agency.kind == "unattributed"
    assert unit.action_agency.identity_bearing is False
    assert unit.text_provenance.identity_keys == []


def test_character_action_with_scaffold_relation_compiles() -> None:
    _replay, plan, scene_plan, contract, creative = _a78_replay_models(
        bind_actor=True,
    )

    shard = scene_shards_module.compile_screenplay_scene_shard_draft(
        creative,
        episode_no=1,
        plan=plan,
        scene_plans={scene_plan.key: scene_plan},
        scene_input_contracts=[contract],
    )

    unit = shard.scenes[0].units[0]
    assert unit.actor_keys == ["person_8ff1cb1a5861"]
    assert unit.onscreen_entity_keys == ["person_8ff1cb1a5861"]
    assert unit.action_agency.kind == "character"
    assert unit.action_agency.identity_bearing is True
    assert unit.text_provenance.identity_keys == [
        "person_8ff1cb1a5861"
    ]


def test_full_production_ss001_missing_agency_round_trip_preserves_fields() -> None:
    fixture = json.loads(
        SS001_FULL_ARTIFACT_FIXTURE.read_text(encoding="utf-8")
    )
    raw_units = [
        unit
        for scene in fixture["content"]["scenes"]
        for unit in scene["units"]
    ]

    assert fixture["artifact_id"] == "art_bcebe2075a55"
    assert fixture["artifact_type"] == "screenplay_scene_shard"
    assert fixture["artifact_status"] == "validated"
    assert fixture["contract_version"] == "screenplay-scene-shard.v6"
    assert fixture["content_hash"] == (
        "19c41c704b3524969a0169c66da1e7a829aa2eaba023245ba9eb983fe23fc2f8"
    )
    assert len(raw_units) == 23
    assert all("action_agency" not in unit for unit in raw_units)

    upgraded_content = deepcopy(fixture["content"])
    upgraded_content["contract_version"] = (
        SCREENPLAY_SCENE_SHARD_VERSION
    )
    shard = ScreenplaySceneShardIR.model_validate(upgraded_content)
    serialized = shard.model_dump(mode="json")
    round_trip = ScreenplaySceneShardIR.model_validate(serialized)
    units = [unit for scene in shard.scenes for unit in scene.units]

    assert sum(
        not unit.actor_keys and not unit.target_keys and not unit.speaker_key
        for unit in units
    ) == 6
    assert all(
        unit.action_agency.identity_bearing
        == bool(unit.actor_keys or unit.target_keys or unit.speaker_key)
        for unit in units
    )
    assert all(
        unit.action_agency.source_segment_ids == unit.source_segment_ids
        for unit in units
    )
    assert all(
        "action_agency" in unit
        for scene in serialized["scenes"]
        for unit in scene["units"]
    )
    assert all(
        "text_provenance" in unit
        for scene in serialized["scenes"]
        for unit in scene["units"]
    )
    assert round_trip == shard


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


def test_blueprint_retry_grant_deadlock_has_distinct_classification() -> None:
    from app.stages import StageError

    error = StageError(
        "剧本时空因果蓝图分片",
        [
            "[BLUEPRINT_PROVIDER_RETRY_GRANT_REQUIRED] "
            "上次供应商结果未知；必须由新的 Production Grant 显式授权同一语义 operation 的下一 attempt"
        ],
    )
    assert app_errors.classify(error) == (
        "generation_retry_grant",
        "GEN-RETRY-GRANT",
    )
    record = app_errors.log_error(error)
    assert record.code == "GEN-RETRY-GRANT"
    # The hint must make clear that retrying / 修复重试上限 will not help here.
    assert "无效" in record.public
    assert "GEN-RETRY-GRANT" in record.public


def test_generic_stage_error_still_classifies_as_generation() -> None:
    from app.stages import StageError

    assert app_errors.classify(
        StageError("剧本时空因果蓝图分片", ["某个通用校验失败"])
    ) == ("generation", "GEN")


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
            source_unit_keys=["SRC0001:unit:001"],
            usage="state_subject",
        ), NarrativeParticipantEvidence(
            identity_key="甲",
            source_segment_ids=["SRC0001"],
            usage="visible",
        )]
    blueprint.nodes[1].participants = ["乙"]
    blueprint.nodes[1].participant_evidence = [NarrativeParticipantEvidence(
            identity_key="乙",
            source_segment_ids=["SRC0002"],
            source_unit_keys=["SRC0002:unit:001"],
            usage="state_subject",
        ), NarrativeParticipantEvidence(
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
            _creative_shard(
                plan,
                blueprint,
                contracts[plan.shard_id],
            ),
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
    generation_hash = (
        scene_shards_module.screenplay_scene_generation_scaffold_hash(
            plan,
            contracts,
        )
    )
    episode_id = "ep-creative-provider-recovery"
    operation_id = (
        f"screenplay.scene-shard:{SCREENPLAY_SCENE_SHARD_VERSION}:"
        f"{SCREENPLAY_SCENE_INPUT_VERSION}:"
        f"{episode_id}:{plan.shard_id}:{plan.source_hash}:"
        f"{plan.boundary_hash}:{plan.blueprint_hash}:"
        f"{plan.identity_registry_hash}:{plan.source_ownership_hash}:"
        f"{generation_hash}"
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
    assert shards[0].generation_scaffold_hash == generation_hash
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


def test_run_d6ba3c89_replay_records_all_provider_outcomes() -> None:
    replay = json.loads(RUN_D6BA3C89_REPLAY.read_text(encoding="utf-8"))
    calls = replay["provider_responses"]

    assert replay["run_id"] == "run_d6ba3c89a60f"
    assert [call["provider_call_id"] for call in calls] == list(
        range(60908, 60918)
    )
    assert [
        call["shard_id"] for call in calls
    ] == [
        "SS001", "SS002", "SS002", "SS001", "SS003",
        "SS004", "SS003", "SS004", "SS005", "SS004",
    ]
    assert sum(call["shard_id"] != "SS005" for call in calls) == 9
    assert next(
        call for call in calls if call["provider_call_id"] == 60916
    )["validator_errors"] == []
    format_retry = next(
        call for call in calls if call["provider_call_id"] == 60915
    )
    assert format_retry["validation_phase"] == "format"
    assert {
        "speaker",
        "action",
        "evidence",
    } <= set(format_retry["response"]["scenes"][0]["units"][3])
    assert all(
        "Extra inputs are not permitted" in error
        for error in format_retry["validator_errors"]
    )


def test_scene_shard_plan_owns_unique_structural_unit_slots() -> None:
    blueprint = _blueprint(split_domain=False)
    plan = build_screenplay_scene_shard_plans(
        blueprint,
        source_text=SOURCE,
        identity_registry_hash="identity-hash",
    )[0]

    slots = plan.unit_slots
    assert slots
    assert [slot.scene_key for slot in slots] == [
        "bp-sc001",
        "bp-sc002",
    ]
    assert len({slot.unit_key for slot in slots}) == len(slots)
    assert len({slot.event_key for slot in slots}) == len(slots)
    assert [slot.unit_order for slot in slots] == list(range(1, len(slots) + 1))
    assert [slot.source_segment_ids for slot in slots] == [
        ["SRC0001"],
        ["SRC0002"],
    ]
    assert all(
        plan.source_scene_owners[source_id] == slot.scene_key
        for slot in slots
        for source_id in slot.source_segment_ids
    )


def test_structural_divider_only_source_creates_no_executable_slot() -> None:
    blueprint = _blueprint(split_domain=False)
    source = "－－－－－－－－\n\n乙接过钥匙并回答。"

    plans = build_screenplay_scene_shard_plans(
        blueprint,
        source_text=source,
        identity_registry_hash="identity-hash",
    )

    assert plans
    assert all(
        slot.source_segment_ids != ["SRC0001"]
        for plan in plans
        for slot in plan.unit_slots
    )
    assert [
        slot.source_segment_ids
        for plan in plans
        for slot in plan.unit_slots
    ] == [["SRC0002"]]


def test_slot_content_compiles_by_key_and_rejects_contract_drift() -> None:
    blueprint = _blueprint(split_domain=False)
    plan = build_screenplay_scene_shard_plans(
        blueprint,
        source_text=SOURCE,
        identity_registry_hash="identity-hash",
    )[0]
    contracts = _contracts([plan], blueprint)[plan.shard_id]
    slot_content = {
        slot.unit_key: {
            "text": f"交付 {slot.source_segment_ids[0]}",
            "resulting_state": f"完成 {slot.source_segment_ids[0]}",
        }
        for slot in reversed(plan.unit_slots)
    }
    draft = ScreenplaySceneShardCreativeIR.model_validate({
        "slots": slot_content,
    })

    compiled = scene_shards_module.compile_screenplay_scene_shard_draft(
        draft,
        episode_no=1,
        plan=plan,
        scene_plans={
            scene.key: scene for scene in blueprint.scene_plans
        },
        scene_input_contracts=contracts,
    )
    assert [
        unit.unit_key
        for scene in compiled.scenes
        for unit in scene.units
    ] == [slot.unit_key for slot in plan.unit_slots]
    assert [
        unit.event_key
        for scene in compiled.scenes
        for unit in scene.units
    ] == [slot.event_key for slot in plan.unit_slots]

    missing = ScreenplaySceneShardCreativeIR.model_validate({
        "slots": dict(list(slot_content.items())[1:]),
    })
    with pytest.raises(ScreenplaySceneShardError, match="缺失 slot"):
        scene_shards_module.compile_screenplay_scene_shard_draft(
            missing,
            episode_no=1,
            plan=plan,
            scene_plans={
                scene.key: scene for scene in blueprint.scene_plans
            },
            scene_input_contracts=contracts,
        )

    extra_content = deepcopy(slot_content)
    extra_content["unexpected-unit"] = {
        "text": "越权单元",
    }
    extra = ScreenplaySceneShardCreativeIR.model_validate({
        "slots": extra_content,
    })
    with pytest.raises(ScreenplaySceneShardError, match="多余 slot"):
        scene_shards_module.compile_screenplay_scene_shard_draft(
            extra,
            episode_no=1,
            plan=plan,
            scene_plans={
                scene.key: scene for scene in blueprint.scene_plans
            },
            scene_input_contracts=contracts,
        )

    first_key = plan.unit_slots[0].unit_key
    overreach = deepcopy(slot_content)
    overreach[first_key]["event_key"] = "model-owned-event"
    with pytest.raises(ValidationError, match="extra_forbidden"):
        ScreenplaySceneShardCreativeIR.model_validate({"slots": overreach})


def test_run_961abd54eb1c_structural_repair_receives_dialogue_mismatch(
    monkeypatch,
) -> None:
    blueprint = _blueprint(split_domain=False)
    plan = build_screenplay_scene_shard_plans(
        blueprint,
        source_text=SOURCE,
        identity_registry_hash="identity-hash",
    )[0]
    contracts = _contracts([plan], blueprint)[plan.shard_id]
    dialogue_slot = plan.unit_slots[0]
    dialogue_slot.kind = "dialogue"
    dialogue_slot.source_text = "“来源原文”"
    dialogue_slot.source_surface = "quoted_span"
    dialogue_slot.delivery_mode = "spoken_dialogue"
    compiled_dialogue_slot = next(
        slot
        for contract in contracts
        for slot in contract.unit_slots
        if slot.unit_key == dialogue_slot.unit_key
    )
    compiled_dialogue_slot.kind = dialogue_slot.kind
    compiled_dialogue_slot.source_text = dialogue_slot.source_text
    compiled_dialogue_slot.source_surface = dialogue_slot.source_surface
    compiled_dialogue_slot.delivery_mode = dialogue_slot.delivery_mode

    valid_draft = _creative_shard(plan, blueprint)
    invalid_payload = valid_draft.model_dump(mode="json")
    dialogue_key = dialogue_slot.unit_key
    missing_key = plan.unit_slots[1].unit_key
    extra_key = f"unexpected:{missing_key}"
    invalid_payload["slots"][dialogue_key]["text"] = "“模型改写”"
    invalid_payload["slots"][extra_key] = invalid_payload["slots"].pop(
        missing_key
    )
    invalid_draft = ScreenplaySceneShardCreativeIR.model_validate(
        invalid_payload
    )
    scene_plans = {
        scene.key: scene for scene in blueprint.scene_plans
    }
    expected_errors = [
        f"[GENERATION_CONTRACT] 缺失 slot：{missing_key}",
        f"[GENERATION_CONTRACT] 多余 slot：{extra_key}",
        (
            f"{dialogue_key} dialogue.text 必须等于 "
            "scaffold source_text"
        ),
    ]

    def validate_draft(
        draft: ScreenplaySceneShardCreativeIR,
    ) -> list[str]:
        try:
            compile_screenplay_scene_shard_draft(
                draft,
                episode_no=1,
                plan=plan,
                scene_plans=scene_plans,
                scene_input_contracts=contracts,
            )
        except ScreenplaySceneShardError as exc:
            return list(exc.errors)
        return []

    with pytest.raises(ScreenplaySceneShardError) as caught:
        compile_screenplay_scene_shard_draft(
            invalid_draft,
            episode_no=1,
            plan=plan,
            scene_plans=scene_plans,
            scene_input_contracts=contracts,
        )
    assert caught.value.errors == expected_errors
    assert invalid_draft.slots[dialogue_key].text == "“模型改写”"

    prompts: list[str] = []
    attempts: list[dict] = []
    strict_schema = build_screenplay_scene_shard_repair_schema(
        plan=plan,
        scene_input_contracts=contracts,
    )
    strict_response_format = (
        scene_shards_module._scene_shard_strict_response_format(
            name="screenplay_scene_shard_creative",
            local_schema=strict_schema,
        )
    )

    async def fake_chat(messages, **kwargs):
        assert kwargs["response_format"] == strict_response_format
        assert kwargs["call_meta"]["response_format_required"] is True
        prompts.append(messages[0]["content"])
        draft = invalid_draft if len(prompts) == 1 else valid_draft
        return json.dumps(draft.model_dump(mode="json"), ensure_ascii=False)

    monkeypatch.setattr(model_gateway, "chat", fake_chat)
    repaired = asyncio.run(model_gateway.chat_structured(
        [{"role": "user", "content": "SS002 run replay"}],
        model_type=ScreenplaySceneShardCreativeIR,
        validate=validate_draft,
        operation_id="test.run-961abd54eb1c.ss002:v1",
        max_tokens=1024,
        format_retry_limit=0,
        semantic_retry_limit=1,
        output_schema=strict_schema,
        response_format=strict_response_format,
        require_response_format=True,
        on_attempt=attempts.append,
    ))

    assert repaired == valid_draft
    assert len(prompts) == 2
    assert all(error in prompts[1] for error in expected_errors)
    assert [attempt["semantic_attempt"] for attempt in attempts] == [0, 1]
    assert attempts[0]["validation_errors"] == expected_errors
    assert attempts[1]["validation_errors"] == []


def test_generation_scaffold_fingerprint_binds_slot_structure() -> None:
    blueprint = _blueprint(split_domain=False)
    plan = build_screenplay_scene_shard_plans(
        blueprint,
        source_text=SOURCE,
        identity_registry_hash="identity-hash",
    )[0]
    contracts = _contracts([plan], blueprint)[plan.shard_id]
    fingerprint = (
        scene_shards_module.screenplay_scene_generation_scaffold_hash(
            plan,
            contracts,
        )
    )
    schema = build_screenplay_scene_shard_repair_schema(
        plan=plan,
        scene_input_contracts=contracts,
    )

    assert schema["x-generation-scaffold-hash"] == fingerprint
    changed = plan.model_copy(deep=True)
    changed.unit_slots[0].event_key += "-changed"
    assert (
        scene_shards_module.screenplay_scene_generation_scaffold_hash(
            changed,
            contracts,
        )
        != fingerprint
    )
    assert SCREENPLAY_SCENE_SHARD_VERSION == "screenplay-scene-shard.v11"
    assert SCREENPLAY_SCENE_INPUT_VERSION == "screenplay-scene-input.v10"


def test_dialogue_mismatch_is_not_silently_normalized() -> None:
    blueprint = _blueprint(split_domain=True)
    plan = build_screenplay_scene_shard_plans(
        blueprint,
        source_text=SOURCE,
        identity_registry_hash="identity-hash",
    )[0]
    contracts = _contracts([plan], blueprint)[plan.shard_id]
    shard = _shard(plan, blueprint)
    unit = shard.scenes[0].units[0]
    unit.kind = "dialogue"
    unit.text = "模型改写"
    unit.source_text = "来源原文"

    with pytest.raises(ScreenplaySceneShardError, match="dialogue.text"):
        normalize_screenplay_scene_shard(
            shard,
            episode_no=1,
            plan=plan,
            scene_plans={
                scene.key: scene for scene in blueprint.scene_plans
            },
            scene_input_contracts=contracts,
        )
    assert unit.text == "模型改写"


def test_ambiguous_dialogue_authority_fails_before_provider_dispatch() -> None:
    source = "“跟我走。”甲和乙同时抬头。"
    blueprint = NarrativeBlueprint.model_validate({
        "episode_no": 1,
        "nodes": [{
            "key": "n1",
            "source_segment_ids": ["SRC0001"],
            "summary": "甲和乙听到命令后抬头",
            "narrative_layer": "story",
            "event_priority": "causal",
            "render_policy": "standalone",
            "temporal_domain_key": "present",
            "time_label": "日",
            "time_relation": "episode_start",
            "location_key": "yard",
            "location_label": "院中",
            "participants": ["甲", "乙"],
            "participant_evidence": [
                {
                    "identity_key": "甲",
                    "source_segment_ids": ["SRC0001"],
                    "usage": "visible",
                },
                {
                    "identity_key": "乙",
                    "source_segment_ids": ["SRC0001"],
                    "usage": "visible",
                },
                {
                    "identity_key": "甲",
                    "source_segment_ids": ["SRC0001"],
                    "source_unit_keys": ["SRC0001:unit:001"],
                    "usage": "voice",
                },
                {
                    "identity_key": "乙",
                    "source_segment_ids": ["SRC0001"],
                    "source_unit_keys": ["SRC0001:unit:001"],
                    "usage": "voice",
                },
            ],
            "source_unit_deliveries": [{
                "source_unit_key": "SRC0001:unit:001",
                "mode": "spoken_dialogue",
                "content_owner_key": "甲",
                "performer_key": "甲",
            }],
            "action_logic": "命令发出后两人抬头",
        }],
    })
    derive_blueprint_scene_plans(blueprint)
    plan = build_screenplay_scene_shard_plans(
        blueprint,
        source_text=source,
        identity_registry_hash="identity-hash",
    )[0]
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

    with pytest.raises(
        ScreenplaySceneShardError,
        match="dialogue.*多个 voice speaker",
    ):
        build_screenplay_scene_input_contracts(
            plan=plan,
            scene_plans=blueprint.scene_plans,
            source_by_id={
                segment.segment_id: segment.text
                for segment in index_source_segments(source)
            },
            identity_registry=registry,
            blueprint_nodes=blueprint.nodes,
        )


def _state_subject_contract(
    *,
    participants: list[dict],
    environment_only: bool = False,
) -> tuple[ScreenplaySceneUnitSlotPlan, ScreenplaySceneInputContract]:
    source_unit_key = "SRC0001:unit:001"
    slot = ScreenplaySceneUnitSlotPlan(
        unit_key="unit-1",
        event_key="event-1",
        scene_key="scene-1",
        scene_order=1,
        unit_order=1,
        scene_unit_order=1,
        kind="action",
        narrative_layer="story",
        event_priority="causal",
        render_policy="standalone",
        source_segment_ids=["SRC0001"],
        source_unit_key=source_unit_key,
        source_text="甲抬头。",
    )
    action = scene_shards_module.ScreenplaySceneActionEvidence(
        node_key="node-1",
        source_segment_ids=["SRC0001"],
        participants=[
            scene_shards_module.ScreenplaySceneActionParticipantEvidence(
                identity_key=item["identity_key"],
                source_segment_ids=["SRC0001"],
                source_unit_keys=[source_unit_key],
                usage=item["usage"],
            )
            for item in participants
        ],
        environment_source_unit_keys=(
            [source_unit_key] if environment_only else []
        ),
    )
    contract = ScreenplaySceneInputContract(
        scene_plan_key="scene-1",
        node_keys=["node-1"],
        source_segment_ids=["SRC0001"],
        source_semantics=_story_source_semantics(["SRC0001"]),
        source_segments=[ScreenplaySceneSourceSegment(
            source_segment_id="SRC0001",
            text="甲抬头。",
        )],
        participant_bindings=[],
        source_scene_owners={"SRC0001": "scene-1"},
        action_evidence=[action],
        unit_slots=[],
        source_ownership_hash="test",
    )
    return slot, contract


def test_exact_state_subject_wins_with_multiple_visible_people() -> None:
    slot, contract = _state_subject_contract(participants=[
        {"identity_key": "person_a", "usage": "state_subject"},
        {"identity_key": "person_a", "usage": "visible"},
        {"identity_key": "person_b", "usage": "visible"},
    ])

    compiled, errors = scene_shards_module._compile_unit_identity_scaffold(
        slot,
        contract=contract,
    )

    assert errors == []
    assert compiled.state_subject_key == "person_a"
    assert compiled.actor_keys == ["person_a"]
    assert compiled.onscreen_entity_keys == ["person_a", "person_b"]


def test_unit074_ownership_patch_adds_visible_before_scene_input() -> None:
    source = ("环境变化，" * 73) + "孟浩抬头。"
    target_unit_key = "SRC0001:unit:074"
    shard = NarrativeBlueprintShard.model_validate({
        "episode_no": 1,
        "shard_index": 1,
        "source_segment_ids": ["SRC0001"],
        "nodes": [{
            "key": "unit074-node",
            "source_segment_ids": ["SRC0001"],
            "summary": "环境变化后孟浩抬头",
            "narrative_layer": "story",
            "event_priority": "causal",
            "render_policy": "standalone",
            "temporal_domain_key": "present",
            "time_label": "当下",
            "time_relation": "episode_start",
            "location_key": "yard",
            "location_label": "院中",
            "participants": ["孟浩", "王有材"],
            "participant_evidence": [{
                "identity_key": "王有材",
                "source_segment_ids": ["SRC0001"],
                "source_unit_keys": ["SRC0001:unit:001"],
                "usage": "visible",
            }],
            "environment_source_unit_keys": [
                f"SRC0001:unit:{index:03d}"
                for index in range(1, 74)
            ],
            "action_logic": "环境变化后孟浩抬头",
        }],
    })
    existing_visible = (
        shard.nodes[0].participant_evidence[0].model_dump(mode="json")
    )

    repaired = apply_blueprint_state_subject_ownership_patch(
        shard,
        {
            "base_candidate_hash": blueprint_shard_candidate_hash(shard),
            "repairs": {
                target_unit_key: {
                    "mode": "single",
                    "identity_keys": ["孟浩"],
                },
            },
        },
        target_unit_keys=[target_unit_key],
        source_text=source,
    )

    assert (
        repaired.nodes[0].participant_evidence[0].model_dump(mode="json")
        == existing_visible
    )
    assert [
        evidence.model_dump(mode="json")
        for evidence in repaired.nodes[0].participant_evidence
        if evidence.usage == "visible"
        and evidence.identity_key == "孟浩"
    ] == [{
        "identity_key": "孟浩",
        "source_segment_ids": ["SRC0001"],
        "source_unit_keys": [target_unit_key],
        "usage": "visible",
    }]
    blueprint = NarrativeBlueprint(
        episode_no=1,
        nodes=repaired.nodes,
    )
    assert blueprint_state_subject_issues(blueprint, source) == []
    derive_blueprint_scene_plans(blueprint)
    registry = [{
        "identity_key": identity_key,
        "authority_id": f"bible:{identity_key}",
        "canonical_name": identity_key,
        "source_labels": [identity_key],
    } for identity_key in ("孟浩", "王有材")]
    plan = build_screenplay_scene_shard_plans(
        blueprint,
        source_text=source,
        identity_registry_hash="identity-hash",
        identity_registry=registry,
    )[0]

    contracts = build_screenplay_scene_input_contracts(
        plan=plan,
        scene_plans=blueprint.scene_plans,
        source_by_id={"SRC0001": source},
        identity_registry=registry,
        blueprint_nodes=blueprint.nodes,
    )
    compiled = next(
        slot
        for contract in contracts
        for slot in contract.unit_slots
        if slot.source_unit_key == target_unit_key
    )

    assert compiled.actor_keys == ["孟浩"]
    assert compiled.onscreen_entity_keys == ["孟浩"]
    assert compiled.participant_deliveries == []


def test_joint_state_subject_preserves_all_exact_unit_actors() -> None:
    slot, contract = _state_subject_contract(participants=[
        {"identity_key": "person_a", "usage": "visible"},
        {"identity_key": "person_b", "usage": "visible"},
    ])
    contract.action_evidence[0].state_subject_assignments = [
        scene_shards_module.ScreenplaySceneStateSubjectAssignment(
            source_unit_key=slot.source_unit_key,
            mode="joint",
            identity_keys=["person_a", "person_b"],
        )
    ]

    compiled, errors = scene_shards_module._compile_unit_identity_scaffold(
        slot,
        contract=contract,
    )

    assert errors == []
    assert compiled.state_subject_key == ""
    assert compiled.state_subject_keys == ["person_a", "person_b"]
    assert compiled.actor_keys == ["person_a", "person_b"]


def test_unique_visible_person_is_not_an_implicit_state_subject() -> None:
    slot, contract = _state_subject_contract(participants=[
        {"identity_key": "person_a", "usage": "visible"},
    ])

    compiled, errors = scene_shards_module._compile_unit_identity_scaffold(
        slot,
        contract=contract,
    )

    assert compiled.actor_keys == []
    assert compiled.state_subject_key == ""
    assert compiled.onscreen_entity_keys == ["person_a"]
    assert any("缺少 single/joint state_subject" in error for error in errors)


def test_explicit_environment_keeps_visible_people_out_of_actor_relation() -> None:
    slot, contract = _state_subject_contract(
        participants=[{"identity_key": "person_a", "usage": "visible"}],
        environment_only=True,
    )

    compiled, errors = scene_shards_module._compile_unit_identity_scaffold(
        slot,
        contract=contract,
    )

    assert errors == []
    assert compiled.environment_only is True
    assert compiled.state_subject_key == ""
    assert compiled.actor_keys == []
    assert compiled.onscreen_entity_keys == ["person_a"]


@pytest.mark.parametrize(
    ("mutation", "expected_codes"),
    [
        ("exact", set()),
        ("missing", {"state_subject_missing"}),
        ("multiple", {"state_subject_ambiguous"}),
        ("environment_conflict", {"state_subject_environment_conflict"}),
        ("environment_visible", set()),
        ("missing_unit_key", {"state_subject_unit_missing", "state_subject_missing"}),
        ("out_of_scope", {"state_subject_unit_invalid", "state_subject_missing"}),
        ("wrong_segment", {"state_subject_unit_invalid", "state_subject_missing"}),
        ("duplicate_same_identity", {"state_subject_ambiguous"}),
    ],
)
def test_blueprint_state_subject_early_gate_matrix(
    mutation: str,
    expected_codes: set[str],
) -> None:
    blueprint = _blueprint(split_domain=False)
    node = blueprint.nodes[0]
    subject = next(
        evidence for evidence in node.participant_evidence
        if evidence.usage == "state_subject"
    )
    unit_key = "SRC0001:unit:001"
    if mutation in {"missing", "environment_visible"}:
        node.participant_evidence = [
            evidence for evidence in node.participant_evidence
            if evidence.usage != "state_subject"
        ]
    if mutation == "environment_visible":
        node.environment_source_unit_keys = [unit_key]
    elif mutation == "multiple":
        node.participant_evidence.append(subject.model_copy(update={
            "identity_key": "乙",
        }))
    elif mutation == "environment_conflict":
        node.environment_source_unit_keys = [unit_key]
    elif mutation == "missing_unit_key":
        subject.source_unit_keys = []
    elif mutation == "out_of_scope":
        subject.source_unit_keys = ["SRC9999:unit:001"]
    elif mutation == "wrong_segment":
        subject.source_segment_ids = ["SRC0002"]
    elif mutation == "duplicate_same_identity":
        node.participant_evidence.append(subject.model_copy(deep=True))

    codes = {
        issue.code
        for issue in blueprint_state_subject_issues(blueprint, SOURCE)
        if node.key in issue.node_keys
    }
    assert codes == expected_codes


def test_paratext_forbids_environment_subject_contract() -> None:
    payload = _blueprint(split_domain=False).nodes[0].model_dump(mode="json")
    payload.update({
        "narrative_layer": "paratext",
        "event_priority": "connective",
        "render_policy": "exclude_from_spine",
        "participants": [],
        "participant_evidence": [],
        "environment_source_unit_keys": ["SRC0001:unit:001"],
        "state_requirements": [],
        "state_changes": [],
        "released_constraints_for": [],
        "decision": None,
    })
    with pytest.raises(ValidationError, match="paratext 节点不得承载"):
        NarrativeNode.model_validate(payload)


def test_fail_fast_reports_the_real_cause_when_owner_is_still_unwinding() -> None:
    """A failure owner registered from an abort callback has no result yet.

    Production EP1: ``abort_outer_batch`` registers the running shard task as
    the batch's failure owner while that task is still executing.  The waiter
    then called ``result()`` on an unfinished task and the whole episode died
    with asyncio's ``InvalidStateError: Result is not set``, which says nothing
    about what actually went wrong.
    """
    scope = scene_shards_module._FailFastScope()

    async def owner():
        # Let _gather_fail_fast bind the batch before the abort callback runs.
        await asyncio.sleep(0)
        current = asyncio.current_task()
        assert current is not None
        scope.fail(current)          # the out-of-band registration
        # The peer's cancellation lands first, so the waiter wakes while this
        # registered owner still has no result at all.
        await asyncio.sleep(0.05)
        raise ValueError("真实失败原因")

    async def peer():
        await asyncio.sleep(10)
        return "peer"

    async def run():
        return await scene_shards_module._gather_fail_fast(
            owner, peer, scope=scope,
        )

    with pytest.raises(ValueError, match="真实失败原因"):
        asyncio.run(run())


def test_fail_fast_still_propagates_a_normally_raised_child() -> None:
    """The ordinary path is unchanged: first raiser wins, peers are cancelled."""
    peer_finished = False

    async def failing():
        raise RuntimeError("first")

    async def slow_peer():
        nonlocal peer_finished
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            raise
        peer_finished = True

    async def run():
        return await scene_shards_module._gather_fail_fast(failing, slow_peer)

    with pytest.raises(RuntimeError, match="first"):
        asyncio.run(run())
    assert peer_finished is False


def _delivery_blueprint(deliveries: list[dict]) -> object:
    """One picture node carrying exactly the given source-unit deliveries."""
    payload = _blueprint(split_domain=False).nodes[0].model_dump(mode="json")
    payload["source_unit_deliveries"] = deliveries
    node = NarrativeNode.model_validate(payload)
    return NarrativeBlueprint(episode_no=1, nodes=[node])


def test_blueprint_attribution_that_nobody_performs_is_registered_reference() -> None:
    """A sect owning engraved text must not abort scene planning.

    The Blueprint contract allows a quoted unit's content owner to be a text or
    object attribution.  Everything downstream resolves content owners as
    identity references, so the attribution has to exist in the frozen
    registry -- as the ``reference`` kind, which is offscreen-only and cannot
    own assets.
    """
    blueprint = _delivery_blueprint([{
        "source_unit_key": "SRC0001:unit:002",
        "mode": "written_text",
        "content_owner_key": "靠山宗",
        "performer_key": "",
    }])
    owners = scene_shards_module.blueprint_referenced_content_owners(blueprint)
    assert owners == ["靠山宗"]

    identities, registry, _hash = scene_shards_module.build_frozen_identity_registry(
        Bible(world=World(visual_style_canonical="国风"), characters=[]),
        [],
        referenced_content_owners=owners,
    )
    entry = next(
        item for item in registry if item["canonical_name"] == "靠山宗"
    )
    assert entry["authority_id"] == "reference:靠山宗"
    identity = next(
        item for item in identities if item.display_name == "靠山宗"
    )
    assert identity.kind == "referenced_identity"
    assert identity.visual_policy == "offscreen_only"
    assert identity.asset_requirement == "forbidden"
    # The freeze resolves it, so scene planning no longer aborts on it.
    assert scene_shards_module._identity_aliases(registry)["靠山宗"] == identity.key


def test_a_performer_is_never_auto_registered_as_a_reference() -> None:
    """Whoever performs a line must stay a frozen person; that stays strict."""
    blueprint = _delivery_blueprint([{
        "source_unit_key": "SRC0001:unit:002",
        "mode": "spoken_dialogue",
        "content_owner_key": "未登记的人",
        "performer_key": "未登记的人",
    }])
    assert scene_shards_module.blueprint_referenced_content_owners(blueprint) == []


def test_scene_shard_undelivered_answer_is_retried_inside_the_batch() -> None:
    """A stream cut before [DONE] is re-issued, not an episode-ending failure.

    Production round 5: one scene-shard review stream delivered 22 characters
    and stopped without the provider's own completion marker.  The transport
    discards that partial text, so nothing was authored -- yet the batch was
    torn down and the episode paused.
    """
    calls: list[str] = []
    interrupted = scene_shards_module.hiagent.ProviderError(
        "流式响应在 [DONE] 前中断",
        retryable=True,
        failure_kind="stream_interrupted",
        delivery_state="unknown",
        requires_explicit_retry=True,
        received_chars=22,
    )

    async def issue(attempt_operation_id: str) -> str:
        calls.append(attempt_operation_id)
        if len(calls) == 1:
            raise interrupted
        return "ok"

    slept: list[float] = []

    async def no_wait(delay: float) -> None:
        slept.append(delay)

    monkeypatch_sleep = getattr(asyncio, "sleep")
    asyncio.sleep = no_wait  # type: ignore[assignment]
    try:
        result = asyncio.run(
            scene_shards_module._scene_structured_with_undelivered_retry(
                issue, operation_id="op-scene",
            )
        )
    finally:
        asyncio.sleep = monkeypatch_sleep  # type: ignore[assignment]

    assert result == "ok"
    # The retry carries its own operation id, so it is not replaying the
    # unknown outcome of the interrupted call.
    assert calls == ["op-scene", "op-scene:undelivered:1"]
    # It also waits first: an immediate re-issue lands inside the same
    # load-shedding window and fails for the same reason.
    assert slept == [
        scene_shards_module.SCENE_SHARD_UNDELIVERED_BACKOFF_S[0]
    ]


def test_scene_shard_delivered_failure_is_never_retried() -> None:
    """An answer the provider did deliver still fails on the first call."""
    calls: list[str] = []
    delivered = scene_shards_module.hiagent.ProviderError(
        "provider rejected the request",
        retryable=False,
        failure_kind="provider_rejected",
        delivery_state="responded",
        received_chars=900,
    )

    async def issue(attempt_operation_id: str) -> str:
        calls.append(attempt_operation_id)
        raise delivered

    with pytest.raises(scene_shards_module.hiagent.ProviderError):
        asyncio.run(
            scene_shards_module._scene_structured_with_undelivered_retry(
                issue, operation_id="op-scene",
            )
        )

    assert calls == ["op-scene"]


def test_scene_shard_backoff_is_short_enough_not_to_starve_the_pool() -> None:
    """The pause happens while holding a provider slot, so it must stay small.

    A long schedule left the few provider slots idle and starved other stages
    badly enough to cancel whole episodes -- worse than the failure it absorbs.
    """
    delays = scene_shards_module.SCENE_SHARD_UNDELIVERED_BACKOFF_S
    assert len(delays) == scene_shards_module.SCENE_SHARD_UNDELIVERED_RETRIES
    assert list(delays) == sorted(delays)
    assert sum(delays) <= 6.0


def test_single_reviewer_fallback_is_strictly_stricter_than_consensus() -> None:
    """Dropping to one reviewer can only add findings, never suppress them.

    The consensus rule intersects the two reviewers, so an abstaining reviewer
    contributing an empty review would suppress every finding for that chunk --
    fail-open.  Counting the surviving reviewer for both sides makes the gate
    strictly stricter instead, which is what a provider refusing to audit
    justifies.
    """
    finding = ScreenplaySceneShardSemanticFinding(
        unit_key="SRC0001:unit:001",
        related_unit_keys=[],
        code="source_semantic_drift",
        violation_kinds=["unsupported_action"],
        message="来源与正文冲突",
    )
    survivor = ScreenplaySceneShardSemanticReview(findings=[finding])
    empty = ScreenplaySceneShardSemanticReview(findings=[])

    # An empty peer would silently erase the surviving reviewer's finding.
    assert scene_shards_module.screenplay_scene_semantic_consensus(
        survivor, empty,
    ) == []
    # Counting the survivor on both sides keeps it.
    kept = scene_shards_module.screenplay_scene_semantic_consensus(
        survivor, survivor,
    )
    assert [item.unit_key for item in kept] == ["SRC0001:unit:001"]


def test_deterministic_rejection_is_the_only_tolerated_review_failure() -> None:
    """Only a reproduced provider refusal counts as an abstention."""
    tolerated = scene_shards_module.hiagent.deterministic_undelivered_error(
        scene_shards_module.hiagent.ProviderError(
            "流式响应在 [DONE] 前中断",
            retryable=True,
            raw="抱歉，该问题不符合安全合规要求，暂时无法回答",
            failure_kind="stream_interrupted",
            delivery_state="unknown",
            requires_explicit_retry=True,
            received_chars=22,
        ),
        attempts=2,
    )
    assert tolerated.failure_kind == "deterministic_rejection"

    ordinary = scene_shards_module.hiagent.ProviderError(
        "provider rejected the request",
        retryable=False,
        failure_kind="provider_rejected",
        delivery_state="responded",
        received_chars=900,
    )
    assert ordinary.failure_kind != "deterministic_rejection"


def test_attributed_text_unit_needs_no_person_state_subject() -> None:
    """Engraved text belongs to its owner, not to anyone's present state.

    Production EP2: `SRC0020:008:unit` is the 「杂」 carved on a wooden token,
    a quoted unit delivered as written_text and attributed to the sect.  It is
    neither a character's state nor ownerless environment, so both branches of
    the usual rule rejected it and the episode paused every single round.
    """
    modes = scene_shards_module._ATTRIBUTED_TEXT_DELIVERY_MODES
    assert modes == frozenset({"written_text", "sound_effect"})
    # Embodied speech is deliberately absent: someone performs it, and that
    # performer must still be a frozen person with a state subject.
    assert "spoken_dialogue" not in modes
    assert "offscreen_voice" not in modes
    assert "unspoken_reference" not in modes


def test_chunk_review_backoff_outlasts_a_burst_without_holding_a_slot() -> None:
    """The chunk-level wait is the one that may be long.

    The in-lease retry occupies a provider slot while it waits, so it has to
    stay small; this one runs between chunk attempts, outside every lease, so
    it can actually outlast a provider burst.
    """
    chunk_delays = scene_shards_module.SCENE_SHARD_CHUNK_REVIEW_BACKOFF_S
    lease_delays = scene_shards_module.SCENE_SHARD_UNDELIVERED_BACKOFF_S

    assert list(chunk_delays) == sorted(chunk_delays)
    assert sum(lease_delays) <= 6.0
    assert sum(chunk_delays) >= 60.0
    assert sum(chunk_delays) <= 180.0
