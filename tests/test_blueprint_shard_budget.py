from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app import db, hiagent, stages
from app.source_excerpt import index_source_segments


ERR_653AC6_FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "blueprint_shard_err_20260814_653ac6.json"
)


def _source(count: int) -> str:
    return "\n\n".join(f"第{index}段山风掠过石阶。" for index in range(1, count + 1))


def test_blueprint_planner_is_deterministic_and_exact_for_production_shape() -> None:
    segments = index_source_segments(_source(62))

    first = stages._partition_blueprint_segments(segments)
    second = stages._partition_blueprint_segments(segments)

    expected = [segment.segment_id for segment in segments]
    assert [segment.segment_id for shard in first for segment in shard] == expected
    assert [
        [segment.segment_id for segment in shard]
        for shard in first
    ] == [
        [segment.segment_id for segment in shard]
        for shard in second
    ]
    assert all(
        len(shard)
        <= stages.BLUEPRINT_TARGET_SOURCE_SEGMENTS_PER_SHARD
        for shard in first
    )
    assert all(
        sum(stages._blueprint_segment_output_weight(segment) for segment in shard)
        <= stages.BLUEPRINT_TARGET_SOURCE_FACTS_PER_SHARD
        for shard in first
    )


def test_blueprint_split_uses_stable_source_fact_midpoint() -> None:
    segments = index_source_segments(_source(14))

    first = stages._split_blueprint_segments(segments)
    second = stages._split_blueprint_segments(segments)

    assert [[item.segment_id for item in shard] for shard in first] == [
        [item.segment_id for item in shard] for shard in second
    ]
    assert [item.segment_id for shard in first for item in shard] == [
        item.segment_id for item in segments
    ]
    assert all(first)


def test_run_be31_shard2_shape_is_split_by_40_fact_output_pressure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    segments = index_source_segments(_source(28))
    weights = {
        segment.segment_id: (2 if index < 12 else 1)
        for index, segment in enumerate(segments)
    }
    assert sum(weights.values()) == 40
    monkeypatch.setattr(
        stages,
        "_blueprint_segment_output_weight",
        lambda segment: weights[segment.segment_id],
    )

    shards = stages._partition_blueprint_segments(segments)

    assert len(shards) >= 3
    assert [item.segment_id for shard in shards for item in shard] == [
        item.segment_id for item in segments
    ]
    assert all(
        sum(weights[item.segment_id] for item in shard)
        <= stages.BLUEPRINT_TARGET_SOURCE_FACTS_PER_SHARD
        for shard in shards
    )
    assert all(
        stages._blueprint_shard_token_budget(shard)
        <= stages.BLUEPRINT_SHARD_MAX_TOKENS
        for shard in shards
    )


class _EmptyRows:
    @staticmethod
    def fetchall() -> list[dict]:
        return []


class _NoCacheConnection:
    @staticmethod
    def execute(_sql: str, _params=()) -> _EmptyRows:
        return _EmptyRows()


class _Rows:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows

    def fetchall(self) -> list[dict]:
        return self.rows


class _StaticCacheConnection:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows

    def execute(self, _sql: str, _params=()) -> _Rows:
        return _Rows(self.rows)


def _cached_leaf_row(
    *,
    source_ids: list[str],
    shard_index: int,
    source_hash: str = "source",
    boundary_hash: str = "boundary",
) -> dict:
    content = json.loads(_shard_response(
        source_ids=source_ids,
        shard_index=shard_index,
    ))
    content["source_hash"] = source_hash
    content["boundary_hash"] = boundary_hash
    from app.evidence import repository

    return {
        "id": f"art-leaf-{shard_index}-{'-'.join(source_ids)}",
        "content_json": json.dumps(content, ensure_ascii=False),
        "content_hash": repository.content_hash(content),
        "model_snapshot_json": json.dumps({
            "source_fact_version": stages.SOURCE_FACT_VERSION,
            "shard_policy_version": stages.BLUEPRINT_SHARD_POLICY_VERSION,
            "local_authority_validator_version": (
                stages.BLUEPRINT_SHARD_LOCAL_AUTHORITY_VERSION
            ),
            "split_manifest_version": stages.BLUEPRINT_SPLIT_MANIFEST_VERSION,
            "split_depth": 1,
        }),
    }


class _DurableBudgetConnection:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows

    def execute(self, _sql: str, _params=()) -> _Rows:
        return _Rows(self.rows)


def _shard_response(*, source_ids: list[str], shard_index: int) -> str:
    return json.dumps({
        "format_version": stages.BLUEPRINT_VERSION,
        "episode_no": 1,
        "shard_index": shard_index,
        "source_segment_ids": source_ids,
        "nodes": [{
            "key": "N001",
            "source_segment_ids": source_ids,
            "summary": "山风掠过石阶",
            "narrative_layer": "story",
            "event_priority": "causal",
            "render_policy": "standalone",
            "temporal_domain_key": "present",
            "time_label": "日",
            "time_relation": "episode_start",
            "location_key": "stone-steps",
            "location_label": "石阶",
            "environment_source_unit_keys": [],
            "action_logic": "环境状态连续变化",
        }],
    }, ensure_ascii=False)


def _prompt_source_ids(prompt: str) -> list[str]:
    payload = prompt.split("target_sources=", 1)[1].split("\nschema=", 1)[0]
    return [
        str(item["source_segment_id"])
        for item in json.loads(payload)
    ]


def test_blueprint_prompt_keeps_multi_action_src_under_one_node() -> None:
    source_payload = [{
        "source_segment_id": "SRC0003",
        "text": "孟浩决定下山，听见呼救，认出王有材并找来藤条施救。",
        "source_facts": [
            {
                "source_unit_key": f"SRC0003:unit:{index:03d}",
                "projection": "quoted" if index % 2 == 0 else "action",
                "text": f"来源单元{index}",
            }
            for index in range(1, 17)
        ],
    }]

    prompt = stages._blueprint_shard_prompt(
        episode_no=1,
        shard_index=2,
        shard_count=4,
        errors=[],
        bible_context={},
        boundary={},
        source_payload=source_payload,
    )

    assert stages.SCREENPLAY_BLUEPRINT_PROMPT_VERSION == (
        "screenplay-blueprint-1.7.0"
    )
    assert "每个SRC必须整体且只归一个节点" in prompt
    assert "节点只能在SRC边界拆分" in prompt
    assert "连续动作压缩为一个核心因果进程" in prompt
    assert "identity_key集合完全相等" in prompt
    assert "禁止删除角色、合并多个身份或改用默认身份" in prompt
    assert "同一SRC内部跨越多个主要地点" in prompt
    assert "performer_key不能替代这条typed voice evidence" in prompt
    assert "source_unit_keys只含该delivery的source_unit_key" in prompt
    assert "跨时空或过载则拆节点" not in prompt
    assert _prompt_source_ids(prompt) == ["SRC0003"]
    schema = json.loads(prompt.split("\nschema=", 1)[1])
    node_schema = schema["$defs"]["NarrativeNode"]
    assert "participant_evidence" in node_schema["required"]
    assert any(
        conditional.get("if", {}).get("properties", {}).get(
            "source_unit_deliveries", {}
        ).get("contains", {}).get("properties", {}).get(
            "source_unit_key"
        ) == {"const": "SRC0003:unit:002"}
        and conditional.get("then", {}).get("properties", {}).get(
            "participant_evidence", {}
        ).get("not") is not None
        for conditional in node_schema["allOf"]
    )
    assert node_schema["properties"]["location_label"]["pattern"] == (
        r"^(?!.*(?:、|/|\+|内外)).+$"
    )
    audible_contract = node_schema["allOf"][0]
    assert audible_contract["then"]["properties"][
        "participant_evidence"
    ]["contains"]["properties"]["usage"] == {"const": "voice"}
    evidence_contract = schema["$defs"]["NarrativeParticipantEvidence"][
        "allOf"
    ][0]["then"]
    assert evidence_contract["properties"]["source_unit_keys"] == {
        "minItems": 1
    }
    evidence_properties = schema["$defs"][
        "NarrativeParticipantEvidence"
    ]["properties"]
    assert evidence_properties["identity_key"]["minLength"] == 1
    assert evidence_properties["source_segment_ids"]["minItems"] == 1
    action_contracts = [
        contract
        for contract in node_schema["allOf"]
        if "oneOf" in contract.get("then", {})
    ]
    assert len(action_contracts) == 8
    for contract in action_contracts:
        state_subject = contract["then"]["oneOf"][0]["properties"][
            "participant_evidence"
        ]
        assert state_subject["minContains"] == 1
        assert state_subject["maxContains"] == 1
    delivery_contract = schema["$defs"]["NarrativeSourceUnitDelivery"][
        "allOf"
    ][0]["then"]
    assert delivery_contract["properties"]["performer_key"] == {
        "minLength": 1
    }
    assert "performer_key" in delivery_contract["required"]


def test_err_653ac6_provider_responses_require_explicit_voice_evidence() -> None:
    replay = json.loads(ERR_653AC6_FIXTURE.read_text(encoding="utf-8"))

    assert replay["error_id"] == "ERR-20260814-653ac6"
    assert replay["provider_call_ids"] == [29660, 29661]
    assert replay["raw_artifact_ids"] == [
        "art_271c1f4e96b0",
        "art_f8cd88c96583",
    ]
    assert replay["run_id"] == "run_9ad1461b9bcf"
    assert replay["step_run_id"] == "step_3eb446ed0f50"
    assert replay["contract_version"] == stages.BLUEPRINT_VERSION

    payloads = [
        replay["first_response_payload"],
        replay["response_payload"],
    ]
    for payload in payloads:
        node = payload["nodes"][0]
        deliveries = node["source_unit_deliveries"]
        assert len(deliveries) == 5
        assert {item["mode"] for item in deliveries} == {"spoken_dialogue"}
        assert not any(
            evidence["usage"] == "voice"
            for evidence in node["participant_evidence"]
        )

        isolated = json.loads(json.dumps(payload, ensure_ascii=False))
        isolated_node = isolated["nodes"][0]
        if isinstance(isolated_node["participants"][0], dict):
            isolated_node["participants"] = [
                item["character_key"]
                for item in isolated_node["participants"]
            ]
        with pytest.raises(
            ValidationError,
            match="performer_key 不能替代该证据",
        ):
            stages.NarrativeBlueprintShard.model_validate(isolated)

        isolated_node["participant_evidence"].extend([
            {
                "identity_key": delivery["performer_key"],
                "source_segment_ids": ["SRC0002"],
                "source_unit_keys": [delivery["source_unit_key"]],
                "usage": "voice",
            }
            for delivery in isolated_node["source_unit_deliveries"]
        ])
        shard = stages.NarrativeBlueprintShard.model_validate(isolated)
        voice_evidence = [
            evidence
            for evidence in shard.nodes[0].participant_evidence
            if evidence.usage == "voice"
        ]
        assert len(voice_evidence) == 5
        assert {
            evidence.source_unit_keys[0]
            for evidence in voice_evidence
        } == {
            delivery.source_unit_key
            for delivery in shard.nodes[0].source_unit_deliveries
        }


def test_output_truncated_splits_once_without_accepting_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    artifacts: list[object] = []

    async def fake_chat(messages, **kwargs):
        source_ids = _prompt_source_ids(messages[1]["content"])
        calls.append(source_ids)
        if len(calls) == 1:
            raise hiagent.ProviderError(
                "typed truncation",
                failure_kind=hiagent.ProviderFailureKind.OUTPUT_TRUNCATED,
            )
        return _shard_response(
            source_ids=source_ids,
            shard_index=int(kwargs["call_meta"]["shard_index"]),
        )

    monkeypatch.setattr(stages, "get_conn", lambda: _NoCacheConnection())
    monkeypatch.setattr(stages.model_gateway, "chat", fake_chat)
    monkeypatch.setattr(stages, "validate_narrative_blueprint_shard", lambda *_a, **_k: [])
    monkeypatch.setattr(stages, "validate_narrative_blueprint", lambda *_a, **_k: [])
    monkeypatch.setattr(stages, "derive_blueprint_scene_plans", lambda *_a, **_k: [])
    monkeypatch.setattr(
        "app.evidence.repository.create_artifact",
        lambda artifact, **_kwargs: artifacts.append(artifact),
    )
    monkeypatch.setattr(
        "app.observability.tracing.current_trace",
        lambda: SimpleNamespace(step_run_id="step-budget"),
    )

    result = asyncio.run(stages._generate_sharded_narrative_blueprint(
        {"id": "ep-budget", "episode_no": 1},
        _source(14),
        {},
    ))

    assert len(calls) == 3
    assert len(calls[0]) == 14
    assert calls[1] + calls[2] == calls[0]
    assert calls[1] and calls[2]
    assert [source_id for node in result.nodes for source_id in node.source_segment_ids] == calls[0]
    raw_outputs = [
        artifact.content.get("raw_output")
        for artifact in artifacts
        if artifact.type == "screenplay_narrative_blueprint_shard_raw"
    ]
    assert raw_outputs == [
        _shard_response(source_ids=calls[1], shard_index=1),
        _shard_response(source_ids=calls[2], shard_index=2),
    ]


def test_production_src0001_paratext_root_split_validates_without_retry_exhaustion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = "第一章 书生孟浩\n\n孟浩推开木门。"
    calls: list[list[str]] = []
    prompts: list[str] = []
    artifacts: list[object] = []

    async def fake_chat(messages, **kwargs):
        source_ids = _prompt_source_ids(messages[1]["content"])
        calls.append(source_ids)
        prompts.append(messages[1]["content"])
        if len(source_ids) > 1:
            raise hiagent.ProviderError(
                "root needs deterministic split",
                failure_kind=hiagent.ProviderFailureKind.OUTPUT_TRUNCATED,
            )
        source_id = source_ids[0]
        if source_id == "SRC0001":
            node = {
                "key": "TITLE",
                "source_segment_ids": [source_id],
                "summary": "第一章 书生孟浩",
                "narrative_layer": "paratext",
                "event_priority": "connective",
                "render_policy": "exclude_from_spine",
                "temporal_domain_key": "paratext",
                "time_label": "章节外",
                "time_relation": "episode_start",
                "location_key": "title-card",
                "location_label": "字幕卡",
                # Exact production failure shape: these provider-authored
                # fields are deterministically projected empty before the
                # unchanged strict validator runs.
                "source_unit_deliveries": [{
                    "source_unit_key": "SRC0001:unit:001",
                    "mode": "written_text",
                }],
                "exit_state": "标题展示完成，正片即将开始",
                "action_logic": "展示章节标题",
            }
        else:
            node = {
                "key": "STORY",
                "source_segment_ids": [source_id],
                "summary": "孟浩推开木门",
                "narrative_layer": "story",
                "event_priority": "causal",
                "render_policy": "standalone",
                "temporal_domain_key": "present",
                "time_label": "当下",
                "time_relation": "jump",
                "transition_cue": "标题卡淡出后切入木门前",
                "location_key": "door",
                "location_label": "木门前",
                "participants": ["孟浩"],
                "participant_evidence": [{
                    "identity_key": "孟浩",
                    "source_segment_ids": [source_id],
                    "source_unit_keys": [f"{source_id}:unit:001"],
                    "usage": "state_subject",
                }],
                "exit_state": "木门已打开",
                "action_logic": "孟浩主动推门",
            }
        return json.dumps({
            "format_version": stages.BLUEPRINT_VERSION,
            "episode_no": 1,
            "shard_index": int(kwargs["call_meta"]["shard_index"]),
            "source_segment_ids": source_ids,
            "nodes": [node],
        }, ensure_ascii=False)

    monkeypatch.setattr(stages, "get_conn", lambda: _NoCacheConnection())
    monkeypatch.setattr(stages.model_gateway, "chat", fake_chat)
    monkeypatch.setattr(
        "app.evidence.repository.create_artifact",
        lambda artifact, **_kwargs: artifacts.append(artifact),
    )
    monkeypatch.setattr(
        "app.observability.tracing.current_trace",
        lambda: SimpleNamespace(step_run_id="step-src0001"),
    )

    result = asyncio.run(stages._generate_sharded_narrative_blueprint(
        {"id": "ep-src0001", "episode_no": 1},
        source,
        {},
    ))

    assert calls == [["SRC0001", "SRC0002"], ["SRC0001"], ["SRC0002"]]
    assert all("仅story/picture节点" in prompt for prompt in prompts)
    assert all(
        "paratext/audit_only无论原文unit是quoted还是action" in prompt
        for prompt in prompts
    )
    assert all('"exit_state":{"const":""}' in prompt for prompt in prompts)
    title = next(node for node in result.nodes if node.narrative_layer == "paratext")
    assert title.source_segment_ids == ["SRC0001"]
    assert title.source_unit_deliveries == []
    assert title.exit_state == ""
    assert [
        artifact for artifact in artifacts
        if artifact.type == "screenplay_narrative_blueprint_shard"
    ]
    assert len(calls) < stages.BLUEPRINT_GENERATION_MAX_PROVIDER_CALLS


def test_legacy_cached_shard_without_current_policy_is_not_reused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    segment = index_source_segments(_source(1))[0]
    source_payload = [{
        "source_segment_id": segment.segment_id,
        "text": segment.text,
        "source_facts": [
            fact.model_dump(mode="json")
            for fact in stages.source_segment_facts(
                segment.segment_id,
                segment.text,
            )
        ],
    }]
    source_hash = hashlib.sha256(json.dumps(
        source_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()).hexdigest()
    boundary = stages._blueprint_shard_boundary_context([])
    boundary_hash = hashlib.sha256(json.dumps(
        boundary,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()).hexdigest()
    cached = json.loads(_shard_response(
        source_ids=[segment.segment_id],
        shard_index=1,
    ))
    cached["source_hash"] = source_hash
    cached["boundary_hash"] = boundary_hash
    calls = 0

    async def fake_chat(messages, **kwargs):
        nonlocal calls
        calls += 1
        return _shard_response(
            source_ids=_prompt_source_ids(messages[1]["content"]),
            shard_index=int(kwargs["call_meta"]["shard_index"]),
        )

    monkeypatch.setattr(
        stages,
        "get_conn",
        lambda: _StaticCacheConnection([{
            "content_json": json.dumps(cached, ensure_ascii=False),
            "model_snapshot_json": "{}",
        }]),
    )
    monkeypatch.setattr(stages.model_gateway, "chat", fake_chat)
    monkeypatch.setattr(stages, "validate_narrative_blueprint_shard", lambda *_a, **_k: [])
    monkeypatch.setattr(stages, "validate_narrative_blueprint", lambda *_a, **_k: [])
    monkeypatch.setattr(stages, "derive_blueprint_scene_plans", lambda *_a, **_k: [])
    monkeypatch.setattr(
        "app.evidence.repository.create_artifact",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "app.observability.tracing.current_trace",
        lambda: SimpleNamespace(step_run_id="step-policy-rebuild"),
    )

    asyncio.run(stages._generate_sharded_narrative_blueprint(
        {"id": "ep-policy-rebuild", "episode_no": 1},
        _source(1),
        {},
    ))

    assert calls == 1


def test_current_cached_shard_with_local_authority_errors_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    segment = index_source_segments(_source(1))[0]
    source_payload = [{
        "source_segment_id": segment.segment_id,
        "text": segment.text,
        "source_facts": [
            fact.model_dump(mode="json")
            for fact in stages.source_segment_facts(
                segment.segment_id,
                segment.text,
            )
        ],
    }]
    source_hash = hashlib.sha256(json.dumps(
        source_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()).hexdigest()
    boundary = stages._blueprint_shard_boundary_context([])
    boundary_hash = hashlib.sha256(json.dumps(
        boundary,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()).hexdigest()
    cached = json.loads(_shard_response(
        source_ids=[segment.segment_id],
        shard_index=1,
    ))
    cached["source_hash"] = source_hash
    cached["boundary_hash"] = boundary_hash
    cached["nodes"][0]["summary"] = "polluted cached authority"
    from app.evidence import repository

    cached_hash = repository.content_hash(cached)
    calls = 0

    async def fake_chat(messages, **kwargs):
        nonlocal calls
        calls += 1
        return _shard_response(
            source_ids=_prompt_source_ids(messages[1]["content"]),
            shard_index=int(kwargs["call_meta"]["shard_index"]),
        )

    def fake_validate(shard, **_kwargs):
        if shard.nodes[0].summary == "polluted cached authority":
            return ["[BLUEPRINT_SHARD_STATE_SUBJECT_MISSING] cached"]
        return []

    monkeypatch.setattr(
        stages,
        "get_conn",
        lambda: _StaticCacheConnection([{
            "id": "art-polluted-current-leaf",
            "content_json": json.dumps(cached, ensure_ascii=False),
            "content_hash": cached_hash,
            "model_snapshot_json": json.dumps({
                    "source_fact_version": stages.SOURCE_FACT_VERSION,
                "shard_policy_version": stages.BLUEPRINT_SHARD_POLICY_VERSION,
                "local_authority_validator_version": (
                    stages.BLUEPRINT_SHARD_LOCAL_AUTHORITY_VERSION
                ),
                "split_manifest_version": stages.BLUEPRINT_SPLIT_MANIFEST_VERSION,
                "source_corpus_hash": hashlib.sha256(
                    _source(1).encode("utf-8")
                ).hexdigest(),
            }),
        }]),
    )
    monkeypatch.setattr(stages.model_gateway, "chat", fake_chat)
    monkeypatch.setattr(stages, "validate_narrative_blueprint_shard", fake_validate)
    monkeypatch.setattr(stages, "validate_narrative_blueprint", lambda *_a, **_k: [])
    monkeypatch.setattr(stages, "derive_blueprint_scene_plans", lambda *_a, **_k: [])
    monkeypatch.setattr(
        "app.evidence.repository.create_artifact",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "app.observability.tracing.current_trace",
        lambda: SimpleNamespace(step_run_id="step-local-authority-rebuild"),
    )

    with pytest.raises(stages.StageError, match="SPLIT_MANIFEST_VALIDATION"):
        asyncio.run(stages._generate_sharded_narrative_blueprint(
            {"id": "ep-local-authority-rebuild", "episode_no": 1},
            _source(1),
            {},
        ))

    assert calls == 0


def test_split_manifest_rebuilds_exact_cached_cover_plus_gap_before_calls() -> None:
    segments = index_source_segments(_source(6))
    rows = [
        _cached_leaf_row(
            source_ids=[segment.segment_id for segment in segments[:2]],
            shard_index=1,
        ),
        _cached_leaf_row(
            source_ids=[segment.segment_id for segment in segments[2:4]],
            shard_index=2,
        ),
    ]

    plan, depths, cached = stages._blueprint_leaf_plan_from_cache(
        segments,
        rows,
    )

    assert [[segment.segment_id for segment in group] for group in plan] == [
        [segment.segment_id for segment in segments[:2]],
        [segment.segment_id for segment in segments[2:4]],
        [segment.segment_id for segment in segments[4:]],
    ]
    assert depths == [1, 1, 0]
    assert set(cached) == {1, 2}


def test_split_manifest_overlap_and_hash_drift_fail_closed() -> None:
    segments = index_source_segments(_source(5))
    overlap = [
        _cached_leaf_row(
            source_ids=[segment.segment_id for segment in segments[:3]],
            shard_index=1,
        ),
        _cached_leaf_row(
            source_ids=[segment.segment_id for segment in segments[2:4]],
            shard_index=2,
        ),
    ]
    with pytest.raises(stages.StageError, match="SPLIT_MANIFEST_OVERLAP"):
        stages._blueprint_leaf_plan_from_cache(segments, overlap)

    corrupted = _cached_leaf_row(
        source_ids=[segment.segment_id for segment in segments[:2]],
        shard_index=1,
    )
    corrupted["content_hash"] = "0" * 64
    with pytest.raises(stages.StageError, match="SPLIT_MANIFEST_HASH"):
        stages._blueprint_leaf_plan_from_cache(segments, [corrupted])


def test_split_manifest_reuses_prefix_and_calls_only_uncovered_gap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_text = _source(4)
    segments = index_source_segments(source_text)
    prefix = segments[:2]
    prefix_payload = [{
        "source_segment_id": segment.segment_id,
        "text": segment.text,
        "source_facts": [
            fact.model_dump(mode="json")
            for fact in stages.source_segment_facts(
                segment.segment_id,
                segment.text,
            )
        ],
    } for segment in prefix]
    source_hash = hashlib.sha256(json.dumps(
        prefix_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()).hexdigest()
    boundary_hash = hashlib.sha256(json.dumps(
        stages._blueprint_shard_boundary_context([]),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()).hexdigest()
    row = _cached_leaf_row(
        source_ids=[segment.segment_id for segment in prefix],
        shard_index=1,
        source_hash=source_hash,
        boundary_hash=boundary_hash,
    )
    snapshot = json.loads(row["model_snapshot_json"])
    snapshot["source_corpus_hash"] = hashlib.sha256(
        source_text.encode("utf-8")
    ).hexdigest()
    row["model_snapshot_json"] = json.dumps(snapshot)
    calls: list[list[str]] = []

    async def fake_chat(messages, **kwargs):
        source_ids = _prompt_source_ids(messages[1]["content"])
        calls.append(source_ids)
        return _shard_response(
            source_ids=source_ids,
            shard_index=int(kwargs["call_meta"]["shard_index"]),
        )

    monkeypatch.setattr(stages, "get_conn", lambda: _StaticCacheConnection([row]))
    monkeypatch.setattr(stages.model_gateway, "chat", fake_chat)
    monkeypatch.setattr(stages, "validate_narrative_blueprint_shard", lambda *_a, **_k: [])
    monkeypatch.setattr(stages, "validate_narrative_blueprint", lambda *_a, **_k: [])
    monkeypatch.setattr(stages, "derive_blueprint_scene_plans", lambda *_a, **_k: [])
    monkeypatch.setattr(stages, "log_provider_call", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "app.evidence.repository.create_artifact",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "app.observability.tracing.current_trace",
        lambda: SimpleNamespace(step_run_id="step-exact-cover"),
    )

    asyncio.run(stages._generate_sharded_narrative_blueprint(
        {"id": "ep-exact-cover", "episode_no": 1},
        source_text,
        {},
    ))

    assert calls == [[segment.segment_id for segment in segments[2:]]]


def test_split_manifest_complete_six_leaf_cover_makes_zero_provider_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_text = _source(6)
    segments = index_source_segments(source_text)
    corpus_hash = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
    rows: list[dict] = []
    prior_nodes: list = []
    for shard_index, segment in enumerate(segments, start=1):
        source_payload = [{
            "source_segment_id": segment.segment_id,
            "text": segment.text,
            "source_facts": [
                fact.model_dump(mode="json")
                for fact in stages.source_segment_facts(
                    segment.segment_id,
                    segment.text,
                )
            ],
        }]
        source_hash = hashlib.sha256(json.dumps(
            source_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()).hexdigest()
        boundary_hash = hashlib.sha256(json.dumps(
            stages._blueprint_shard_boundary_context(prior_nodes),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()).hexdigest()
        row = _cached_leaf_row(
            source_ids=[segment.segment_id],
            shard_index=shard_index,
            source_hash=source_hash,
            boundary_hash=boundary_hash,
        )
        snapshot = json.loads(row["model_snapshot_json"])
        snapshot["source_corpus_hash"] = corpus_hash
        row["model_snapshot_json"] = json.dumps(snapshot)
        rows.append(row)
        prior_nodes.extend(
            stages.NarrativeBlueprintShard.model_validate(
                json.loads(row["content_json"])
            ).nodes
        )

    async def forbidden_chat(*_args, **_kwargs):
        raise AssertionError("complete exact cover must not call provider")

    monkeypatch.setattr(stages, "get_conn", lambda: _StaticCacheConnection(rows))
    monkeypatch.setattr(stages.model_gateway, "chat", forbidden_chat)
    monkeypatch.setattr(
        stages,
        "validate_narrative_blueprint_shard",
        lambda *_a, **_k: [],
    )
    monkeypatch.setattr(stages, "validate_narrative_blueprint", lambda *_a, **_k: [])
    monkeypatch.setattr(stages, "derive_blueprint_scene_plans", lambda *_a, **_k: [])
    monkeypatch.setattr(stages, "log_provider_call", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "app.evidence.repository.create_artifact",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "app.observability.tracing.current_trace",
        lambda: SimpleNamespace(step_run_id="step-complete-cover"),
    )

    result = asyncio.run(stages._generate_sharded_narrative_blueprint(
        {"id": "ep-complete-cover", "episode_no": 1},
        source_text,
        {},
    ))

    assert len(result.nodes) == 6


def test_split_manifest_boundary_drift_fails_before_provider_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_text = _source(1)
    segment = index_source_segments(source_text)[0]
    row = _cached_leaf_row(
        source_ids=[segment.segment_id],
        shard_index=1,
        source_hash="wrong-source-hash",
        boundary_hash="wrong-boundary-hash",
    )
    snapshot = json.loads(row["model_snapshot_json"])
    snapshot["source_corpus_hash"] = hashlib.sha256(
        source_text.encode("utf-8")
    ).hexdigest()
    row["model_snapshot_json"] = json.dumps(snapshot)
    calls = 0

    async def forbidden_chat(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("authority drift must fail before provider")

    monkeypatch.setattr(stages, "get_conn", lambda: _StaticCacheConnection([row]))
    monkeypatch.setattr(stages.model_gateway, "chat", forbidden_chat)
    monkeypatch.setattr(stages, "validate_narrative_blueprint_shard", lambda *_a, **_k: [])

    with pytest.raises(stages.StageError, match="SPLIT_MANIFEST_AUTHORITY"):
        asyncio.run(stages._generate_sharded_narrative_blueprint(
            {"id": "ep-boundary-drift", "episode_no": 1},
            source_text,
            {},
        ))

    assert calls == 0


def test_non_truncation_provider_error_is_not_split(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    async def fake_chat(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise hiagent.ProviderError(
            "stream outcome unknown",
            failure_kind=hiagent.ProviderFailureKind.EXECUTION_FAILED,
        )

    monkeypatch.setattr(stages, "get_conn", lambda: _NoCacheConnection())
    monkeypatch.setattr(stages.model_gateway, "chat", fake_chat)

    with pytest.raises(hiagent.ProviderError, match="stream outcome unknown"):
        asyncio.run(stages._generate_sharded_narrative_blueprint(
            {"id": "ep-provider-fail", "episode_no": 1},
            _source(14),
            {},
        ))

    assert calls == 1


def test_invalid_single_segment_stops_at_bounded_attempt_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    async def fake_chat(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return "{}"

    monkeypatch.setattr(stages, "get_conn", lambda: _NoCacheConnection())
    monkeypatch.setattr(stages.model_gateway, "chat", fake_chat)
    monkeypatch.setattr(
        "app.evidence.repository.create_artifact",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "app.observability.tracing.current_trace",
        lambda: SimpleNamespace(step_run_id="step-bounded-attempt"),
    )

    with pytest.raises(stages.StageError, match="BLUEPRINT_SHARD_JSON"):
        asyncio.run(stages._generate_sharded_narrative_blueprint(
            {"id": "ep-attempt-limit", "episode_no": 1},
            _source(1),
            {},
        ))

    assert calls == stages.BLUEPRINT_SHARD_MAX_ATTEMPTS


def test_provider_call_is_bounded_by_remaining_wall_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def slow_chat(*_args, **_kwargs):
        await asyncio.sleep(1)
        raise AssertionError("wait_for did not enforce the deadline")

    monkeypatch.setattr(stages, "get_conn", lambda: _NoCacheConnection())
    monkeypatch.setattr(stages.model_gateway, "chat", slow_chat)
    monkeypatch.setattr(stages, "BLUEPRINT_GENERATION_MAX_WALL_SECONDS", 0.01)

    with pytest.raises(stages.StageError, match="TIME_BUDGET"):
        asyncio.run(stages._generate_sharded_narrative_blueprint(
            {"id": "ep-wall-limit", "episode_no": 1},
            _source(1),
            {},
        ))


def test_blueprint_generation_budget_caps_calls_tokens_and_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    budget = stages._BlueprintGenerationBudget()
    budget.provider_calls = stages.BLUEPRINT_GENERATION_MAX_PROVIDER_CALLS
    with pytest.raises(stages.StageError, match="CALL_BUDGET"):
        budget.claim(max_tokens=1)

    budget = stages._BlueprintGenerationBudget()
    reservation = budget.claim(
        max_tokens=stages.BLUEPRINT_GENERATION_MAX_OUTPUT_TOKENS,
    )
    budget.record_usage(reservation, {
        "completion_tokens": None,
        "reused": False,
    })
    budget.settle(reservation)
    with pytest.raises(stages.StageError, match="TOKEN_BUDGET"):
        budget.claim(max_tokens=1)

    budget = stages._BlueprintGenerationBudget()
    monkeypatch.setattr(
        stages.time,
        "monotonic",
        lambda: budget.started_at + stages.BLUEPRINT_GENERATION_MAX_WALL_SECONDS,
    )
    with pytest.raises(stages.StageError, match="TIME_BUDGET"):
        budget.claim(max_tokens=1)


def test_run_bd33_dynamic_split_releases_requested_reservations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    segments = index_source_segments(_source(6))
    calls: list[tuple[list[str], int]] = []
    artifacts: list[object] = []
    validations = 0

    async def fake_chat(messages, **kwargs):
        source_ids = _prompt_source_ids(messages[1]["content"])
        calls.append((source_ids, int(kwargs["call_meta"]["attempt"])))
        kwargs["usage_callback"]({
            "completion_tokens": 2,
            "reused": False,
        })
        return _shard_response(
            source_ids=source_ids,
            shard_index=int(kwargs["call_meta"]["shard_index"]),
        )

    def fake_validate(*_args, **_kwargs):
        nonlocal validations
        validations += 1
        if validations == 6:
            return ["[BLUEPRINT_TEST_RETRY] shard6 attempt1 invalid"]
        return []

    monkeypatch.setattr(
        stages,
        "_partition_blueprint_segments",
        lambda _segments: [[segment] for segment in segments],
    )
    monkeypatch.setattr(stages, "_blueprint_shard_token_budget", lambda _s: 10)
    monkeypatch.setattr(
        stages.hiagent,
        "text_request_token_limits",
        lambda **_kwargs: ("hiagent", "test-model", 10),
    )
    monkeypatch.setattr(stages, "BLUEPRINT_GENERATION_MAX_OUTPUT_TOKENS", 25)
    monkeypatch.setattr(stages, "get_conn", lambda: _NoCacheConnection())
    monkeypatch.setattr(stages.model_gateway, "chat", fake_chat)
    monkeypatch.setattr(stages, "validate_narrative_blueprint_shard", fake_validate)
    monkeypatch.setattr(stages, "validate_narrative_blueprint", lambda *_a, **_k: [])
    monkeypatch.setattr(stages, "derive_blueprint_scene_plans", lambda *_a, **_k: [])
    monkeypatch.setattr(
        "app.evidence.repository.create_artifact",
        lambda artifact, **_kwargs: artifacts.append(artifact),
    )
    monkeypatch.setattr(
        "app.observability.tracing.current_trace",
        lambda: SimpleNamespace(step_run_id="step-run-bd33"),
    )

    result = asyncio.run(stages._generate_sharded_narrative_blueprint(
        {"id": "ep-run-bd33", "episode_no": 1},
        _source(6),
        {},
    ))

    assert calls == [
        ([segment.segment_id], 1) for segment in segments[:5]
    ] + [([segments[5].segment_id], 1), ([segments[5].segment_id], 2)]
    assert len(result.nodes) == 6
    raw = [
        artifact for artifact in artifacts
        if artifact.type == "screenplay_narrative_blueprint_shard_raw"
    ]
    validated = [
        artifact for artifact in artifacts
        if artifact.type == "screenplay_narrative_blueprint_shard"
    ]
    assert len(raw) == 7
    assert len(validated) == 6
    assert raw[-1].content["token_settlement"] == {
        "requested_max_tokens": 10,
        "effective_max_tokens": 10,
        "actual_completion_tokens": 2,
        "usage_reported": True,
        "fresh_responses": 1,
        "reused_responses": 0,
        "unknown_responses": 0,
        "durable_replay": False,
        "charged_output_tokens": 2,
        "global_charged_output_tokens": 14,
    }


def test_missing_usage_is_charged_at_full_reservation() -> None:
    budget = stages._BlueprintGenerationBudget()
    reservation = budget.claim(max_tokens=10)
    budget.record_usage(reservation, {
        "completion_tokens": None,
        "reused": False,
    })

    settlement = budget.settle(reservation)

    assert settlement["actual_completion_tokens"] is None
    assert settlement["charged_output_tokens"] == 10
    assert budget.charged_output_tokens == 10


def test_cached_response_costs_no_new_output_tokens() -> None:
    budget = stages._BlueprintGenerationBudget()
    reservation = budget.claim(max_tokens=10)
    budget.record_usage(reservation, {
        "completion_tokens": 9,
        "reused": True,
    })

    settlement = budget.settle(reservation)

    assert settlement["reused_responses"] == 1
    assert settlement["charged_output_tokens"] == 0
    assert budget.charged_output_tokens == 0


def test_durable_success_replay_does_not_double_count_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = {
        "response_json": json.dumps({
            "usage": {"completion_tokens": 3},
        }),
        "meta": json.dumps({
            "operation_id": "blueprint-op-1",
            "requested_max_tokens": 10,
        }),
        "status": "OK",
        "recovery_disposition": None,
    }
    monkeypatch.setattr(
        stages,
        "get_conn",
        lambda: _DurableBudgetConnection([row]),
    )

    for _ in range(3):
        budget = stages._BlueprintGenerationBudget.from_durable_calls(
            run_id="run-crash-replay",
        )
        reservation = budget.claim(
            max_tokens=10,
            operation_id="blueprint-op-1",
        )
        budget.record_usage(reservation, {
            "completion_tokens": 3,
            "reused": True,
        })
        settlement = budget.settle(reservation)
        assert budget.provider_calls == 1
        assert budget.requested_output_tokens == 10
        assert budget.actual_output_tokens == 3
        assert settlement["durable_replay"] is True
        assert settlement["charged_output_tokens"] == 0


def test_two_fresh_success_rows_for_same_operation_are_both_charged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        {
            "response_json": json.dumps({
                "usage": {"completion_tokens": completion},
            }),
            "meta": json.dumps({
                "operation_id": "blueprint-op-duplicate",
                "requested_max_tokens": 10,
            }),
            "status": "OK",
            "recovery_disposition": None,
        }
        for completion in (3, 4)
    ]
    monkeypatch.setattr(
        stages,
        "get_conn",
        lambda: _DurableBudgetConnection(rows),
    )

    budget = stages._BlueprintGenerationBudget.from_durable_calls(
        run_id="run-double-send",
    )

    assert budget.provider_calls == 2
    assert budget.requested_output_tokens == 20
    assert budget.actual_output_tokens == 7
    assert budget.charged_output_tokens == 7


def test_durable_unknown_call_is_charged_at_full_requested_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        stages,
        "get_conn",
        lambda: _DurableBudgetConnection([{
            "response_json": None,
            "meta": json.dumps({
                "operation_id": "blueprint-op-unknown",
                "requested_max_tokens": 12,
            }),
            "status": "RUNNING",
            "recovery_disposition": None,
        }]),
    )

    budget = stages._BlueprintGenerationBudget.from_durable_calls(
        run_id="run-unknown",
    )

    assert budget.provider_calls == 1
    assert budget.unknown_output_tokens == 12
    assert budget.charged_output_tokens == 12


def test_durable_unknown_blueprint_patch_is_restored_as_budget_liability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        stages,
        "get_conn",
        lambda: _DurableBudgetConnection([{
            "response_json": None,
            "meta": json.dumps({
                "stage_key": "screenplay_blueprint_patch",
                "requested_max_tokens": 16384,
                "effective_max_tokens": 8192,
            }),
            "operation_id": "screenplay.blueprint.patch:v6:run-x:hash:1",
            "status": "INTERRUPTED",
            "recovery_disposition": "REQUIRES_EXPLICIT_RETRY",
        }]),
    )

    budget = stages._BlueprintGenerationBudget.from_durable_calls(
        run_id="run-x",
    )

    assert budget.provider_calls == 1
    assert budget.requested_output_tokens == 16384
    assert budget.unknown_output_tokens == 8192
    assert budget.charged_output_tokens == 8192


def test_later_unknown_attempt_is_not_misclassified_as_durable_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    common_meta = {
        "stage_key": "screenplay_blueprint_patch",
        "requested_max_tokens": 100,
        "effective_max_tokens": 100,
    }
    monkeypatch.setattr(
        stages,
        "get_conn",
        lambda: _DurableBudgetConnection([{
            "response_json": json.dumps({
                "usage": {"completion_tokens": 10},
            }),
            "meta": json.dumps(common_meta),
            "operation_id": "same-operation",
            "status": "OK",
            "recovery_disposition": None,
        }, {
            "response_json": None,
            "meta": json.dumps(common_meta),
            "operation_id": "same-operation",
            "status": "INTERRUPTED",
            "recovery_disposition": "REQUIRES_EXPLICIT_RETRY",
        }]),
    )

    budget = stages._BlueprintGenerationBudget.from_durable_calls(
        run_id="run-latest-unknown",
    )

    assert "same-operation" not in budget._durable_successful_operations
    assert "same-operation" in budget._durable_unknown_operations
    assert budget.actual_output_tokens == 10
    assert budget.unknown_output_tokens == 100


def test_blueprint_budget_lineage_crosses_fresh_activation_and_requires_new_grant(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "blueprint-lineage.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()
    conn = db.get_conn()
    current_time = db.now()
    for run_id in ("run-old", "run-fresh"):
        conn.execute(
            """INSERT INTO workflow_runs(
                   id,workflow_type,scope_type,scope_id,status,
                   input_fingerprint,started_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?)""",
            (
                run_id,
                "screenplay_production",
                "episode",
                "ep-lineage",
                "FAILED" if run_id == "run-old" else "RUNNING",
                "same-authority-fingerprint",
                current_time - 2,
                current_time,
            ),
        )
    conn.execute(
        """INSERT INTO provider_calls(
               ts,kind,model,status,latency_ms,meta,run_id,operation_id,
               attempt_no,recovery_disposition
           ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (
            current_time - 1,
            "chat",
            "model",
            "INTERRUPTED",
            300000,
            json.dumps({
                "stage_key": "screenplay_blueprint_patch",
                "episode_id": "ep-lineage",
                "requested_max_tokens": 16384,
                "effective_max_tokens": 8192,
                "production_grant_id": "grant-old",
            }),
            "run-old",
            "stable-patch-operation",
            1,
            "REQUIRES_EXPLICIT_RETRY",
        ),
    )
    conn.commit()

    blocked = stages._BlueprintGenerationBudget.from_durable_calls(
        run_id="run-fresh",
        episode_id="ep-lineage",
        input_fingerprint="same-authority-fingerprint",
        retry_grant_id="grant-old",
    )
    assert blocked.provider_calls == 1
    assert blocked.unknown_output_tokens == 8192
    with pytest.raises(stages.StageError, match="RETRY_GRANT_REQUIRED"):
        blocked.claim(max_tokens=4096, operation_id="stable-patch-operation")

    unrelated_grant = stages._BlueprintGenerationBudget.from_durable_calls(
        run_id="run-fresh",
        episode_id="ep-lineage",
        input_fingerprint="same-authority-fingerprint",
        retry_grant_id="grant-new",
    )
    with pytest.raises(stages.StageError, match="RETRY_GRANT_REQUIRED"):
        unrelated_grant.claim(
            max_tokens=4096,
            operation_id="stable-patch-operation",
        )

    allowed = stages._BlueprintGenerationBudget.from_durable_calls(
        run_id="run-fresh",
        episode_id="ep-lineage",
        input_fingerprint="same-authority-fingerprint",
        retry_grant_id="grant-new",
    )
    allowed.authorize_unknown_retry("grant-new")
    reservation = allowed.claim(
        max_tokens=4096,
        operation_id="stable-patch-operation",
    )
    assert allowed.provider_calls == 2
    assert allowed.unknown_output_tokens == 8192
    assert allowed.reserved_output_tokens == 4096
    allowed.settle(reservation, unreported_outcome="not_sent")


def test_production_truncated_meta_is_scoped_by_workflow_episode(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "blueprint-truncated-meta.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()
    conn = db.get_conn()
    stamp = db.now()
    for run_id, status in (("run-61453", "FAILED"), ("run-fresh", "RUNNING")):
        conn.execute(
            """INSERT INTO workflow_runs(
                   id,workflow_type,scope_type,scope_id,status,
                   input_fingerprint,started_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?)""",
            (
                run_id, "screenplay_production", "episode", "ep-production",
                status, "same-production-fingerprint", stamp - 1, stamp,
            ),
        )
    # Mirrors the old 800-char summary contract: stage/token data survived,
    # but episode_id and production_grant_id were absent.
    truncated_meta = json.dumps({
        "_truncated": True,
        "stage_key": "screenplay_blueprint_patch",
        "requested_max_tokens": 16384,
        "effective_max_tokens": 16384,
    })
    conn.execute(
        """INSERT INTO provider_calls(
               ts,kind,model,status,latency_ms,meta,run_id,operation_id,
               attempt_no,recovery_disposition,request_hash
           ) VALUES(?,?,?,?,?,?,?,?,?,?,NULL)""",
        (
            stamp - 1, "chat", "model", "INTERRUPTED", 303769,
            truncated_meta, "run-61453", "old-run-scoped-op", 1,
            "REQUIRES_EXPLICIT_RETRY",
        ),
    )
    conn.commit()

    budget = stages._BlueprintGenerationBudget.from_durable_calls(
        run_id="run-fresh",
        episode_id="ep-production",
        input_fingerprint="same-production-fingerprint",
    )

    assert budget.provider_calls == 1
    assert budget.unknown_output_tokens == 16384
    assert "screenplay_blueprint_patch" in budget._durable_unknown_stage_calls
    with pytest.raises(stages.StageError, match="RETRY_GRANT_REQUIRED"):
        budget.explicit_retry_call_id("screenplay_blueprint_patch")


def test_blueprint_wall_budget_starts_new_epoch_for_fresh_activation(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "blueprint-wall-lineage.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()
    conn = db.get_conn()
    now_value = 10_000.0
    monkeypatch.setattr(stages.time, "time", lambda: now_value)
    for run_id in ("run-wall-old", "run-wall-fresh"):
        conn.execute(
            """INSERT INTO workflow_runs(
                   id,workflow_type,scope_type,scope_id,status,
                   input_fingerprint,started_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?)""",
            (
                run_id,
                "screenplay_production",
                "episode",
                "ep-wall-lineage",
                "FAILED" if run_id.endswith("old") else "RUNNING",
                "same-wall-fingerprint",
                now_value - (1900 if run_id.endswith("old") else 1),
                now_value,
            ),
        )
    conn.execute(
        """INSERT INTO provider_calls(
               ts,kind,model,status,latency_ms,meta,response_json,run_id,
               operation_id,attempt_no
           ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (
            now_value - 1900,
            "chat",
            "model",
            "OK",
            10,
            json.dumps({
                "stage_key": "screenplay_blueprint_shard",
                "episode_id": "ep-wall-lineage",
                "requested_max_tokens": 10,
                "effective_max_tokens": 10,
            }),
            json.dumps({"usage": {"completion_tokens": 1}}),
            "run-wall-old",
            "wall-operation",
            1,
        ),
    )
    conn.commit()

    budget = stages._BlueprintGenerationBudget.from_durable_calls(
        run_id="run-wall-fresh",
        started_at_epoch=now_value - 1,
        episode_id="ep-wall-lineage",
        input_fingerprint="same-wall-fingerprint",
    )

    reservation = budget.claim(max_tokens=1, operation_id="new-operation")
    assert budget.provider_calls == 1
    assert budget.actual_output_tokens == 0
    budget.settle(reservation, unreported_outcome="not_sent")


def test_expired_old_unknown_keeps_liability_but_new_grant_opens_wall_epoch(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "blueprint-retry-wall.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()
    conn = db.get_conn()
    now_value = 10_000.0
    monkeypatch.setattr(stages.time, "time", lambda: now_value)
    for run_id, status, started in (
        ("run-old", "FAILED", now_value - 1900),
        ("run-fresh", "RUNNING", now_value - 1),
    ):
        conn.execute(
            """INSERT INTO workflow_runs(
                   id,workflow_type,scope_type,scope_id,status,
                   input_fingerprint,started_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?)""",
            (
                run_id, "screenplay", "episode", "ep-retry-wall", status,
                "same-fingerprint", started, now_value,
            ),
        )
    conn.execute(
        """INSERT INTO provider_calls(
               ts,kind,model,status,latency_ms,meta,run_id,operation_id,
               attempt_no,recovery_disposition,production_grant_id
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (
            now_value - 1800, "chat", "model", "INTERRUPTED", 300000,
            json.dumps({
                "stage_key": "screenplay_blueprint_patch",
                "requested_max_tokens": 16384,
                "effective_max_tokens": 8192,
            }),
            "run-old", "stable-op", 1, "REQUIRES_EXPLICIT_RETRY", "grant-old",
        ),
    )
    conn.commit()

    blocked = stages._BlueprintGenerationBudget.from_durable_calls(
        run_id="run-fresh",
        started_at_epoch=now_value - 1,
        episode_id="ep-retry-wall",
        input_fingerprint="same-fingerprint",
        retry_grant_id="grant-old",
    )
    assert blocked.provider_calls == 1
    assert blocked.unknown_output_tokens == 8192
    with pytest.raises(stages.StageError, match="RETRY_GRANT_REQUIRED"):
        blocked.assert_activation_admissible()

    allowed = stages._BlueprintGenerationBudget.from_durable_calls(
        run_id="run-fresh",
        started_at_epoch=now_value - 1,
        episode_id="ep-retry-wall",
        input_fingerprint="same-fingerprint",
        retry_grant_id="grant-old",
    )
    allowed.authorize_unknown_retry("grant-new")
    allowed.assert_activation_admissible()
    reservation = allowed.claim(max_tokens=4096, operation_id="stable-op")
    assert allowed.provider_calls == 2
    assert allowed.unknown_output_tokens == 8192
    allowed.settle(reservation, unreported_outcome="not_sent")


def test_durable_wall_budget_uses_original_run_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(stages, "get_conn", lambda: _DurableBudgetConnection([]))
    monkeypatch.setattr(stages.time, "time", lambda: 2000.0)
    monkeypatch.setattr(stages.time, "monotonic", lambda: 500.0)

    budget = stages._BlueprintGenerationBudget.from_durable_calls(
        run_id="run-old",
        started_at_epoch=(
            2000.0 - stages.BLUEPRINT_GENERATION_MAX_WALL_SECONDS
        ),
    )

    with pytest.raises(stages.StageError, match="TIME_BUDGET"):
        budget.claim(max_tokens=1)


def test_runtime_budget_restores_exact_retry_grant_and_historical_unknown(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.production.grant import issue_production_grant
    from app.production.revision import ensure_production_revision

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "runtime-budget.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()
    conn = db.get_conn()
    stamp = db.now()
    conn.execute(
        "INSERT INTO projects(id,name,status,created_at) VALUES('p','p','created',?)",
        (stamp,),
    )
    conn.execute(
        """INSERT INTO episodes(
               id,project_id,episode_no,title,status,screenplay_status,created_at
           ) VALUES('e','p',1,'e','planned','pending',?)""",
        (stamp,),
    )
    for run_id, status, config in (
        ("old", "FAILED", {}),
        ("fresh", "RUNNING", {
            "blueprint_budget_lineage_fingerprint": "authority",
        }),
    ):
        conn.execute(
            """INSERT INTO workflow_runs(
                   id,workflow_type,scope_type,scope_id,status,input_fingerprint,
                   config_snapshot_json,started_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                run_id, "screenplay", "episode", "e", status, "authority",
                json.dumps(config), stamp - 1, stamp,
            ),
        )
    conn.execute(
        """INSERT INTO provider_calls(
               ts,kind,model,status,latency_ms,meta,run_id,operation_id,
               attempt_no,recovery_disposition
           ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (
            stamp - 1, "chat", "model", "INTERRUPTED", 300000,
            json.dumps({
                "stage_key": "screenplay_blueprint_patch",
                "requested_max_tokens": 16384,
                "effective_max_tokens": 8192,
            }),
            "old", "stable-op", 1, "REQUIRES_EXPLICIT_RETRY",
        ),
    )
    conn.commit()
    liability = stages._BlueprintGenerationBudget.from_durable_calls(
        run_id="fresh", episode_id="e", input_fingerprint="authority",
    )
    revision = ensure_production_revision(
        episode_id="e", kind="screenplay", input_fingerprint="authority",
        resume=False,
    )
    grant, _ = issue_production_grant(
        episode_id="e", project_id="p", production_revision_id=revision.id,
        kind="screenplay", issued_by="user_retry_approval",
        input_artifact_hash=stages.blueprint_retry_receipts_hash(
            liability.unknown_receipts
        ),
    )
    snapshot = {
        "blueprint_budget_lineage_fingerprint": "authority",
        "blueprint_retry_grant_id": grant.grant_id,
        "blueprint_retry_receipts_hash": stages.blueprint_retry_receipts_hash(
            liability.unknown_receipts
        ),
    }
    conn.execute(
        "UPDATE workflow_runs SET config_snapshot_json=? WHERE id='fresh'",
        (json.dumps(snapshot),),
    )
    conn.execute(
        "UPDATE production_grants SET consumed_at=? WHERE id=?",
        (db.now(), grant.grant_id),
    )
    conn.commit()

    budget = stages._blueprint_generation_budget_for_trace(
        SimpleNamespace(run_id="fresh"), episode_id="e",
    )
    assert budget.unknown_output_tokens == 8192
    assert budget.requires_fresh_retry_grant is False

    snapshot["blueprint_retry_receipts_hash"] = "sha256:wrong"
    conn.execute(
        "UPDATE workflow_runs SET config_snapshot_json=? WHERE id='fresh'",
        (json.dumps(snapshot),),
    )
    conn.commit()
    wrong_hash = stages._blueprint_generation_budget_for_trace(
        SimpleNamespace(run_id="fresh"), episode_id="e",
    )
    assert wrong_hash.unknown_output_tokens == 8192
    assert wrong_hash.requires_fresh_retry_grant is True

    snapshot["blueprint_retry_receipts_hash"] = stages.blueprint_retry_receipts_hash(
        liability.unknown_receipts
    )
    conn.execute(
        "UPDATE workflow_runs SET config_snapshot_json=? WHERE id='fresh'",
        (json.dumps(snapshot),),
    )
    conn.execute(
        "UPDATE production_grants SET revoked_at=? WHERE id=?",
        (db.now(), grant.grant_id),
    )
    conn.commit()
    revoked = stages._blueprint_generation_budget_for_trace(
        SimpleNamespace(run_id="fresh"), episode_id="e",
    )
    assert revoked.unknown_output_tokens == 8192
    assert revoked.requires_fresh_retry_grant is True


@pytest.mark.parametrize("snapshot", ["{bad", "{}"])
def test_runtime_budget_rejects_missing_or_corrupt_activation_snapshot(
    snapshot: str,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "runtime-bad-snapshot.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()
    conn = db.get_conn()
    stamp = db.now()
    conn.execute(
        """INSERT INTO workflow_runs(
               id,workflow_type,scope_type,scope_id,status,input_fingerprint,
               config_snapshot_json,started_at,updated_at
           ) VALUES('fresh','screenplay','episode','e','RUNNING','authority',?,?,?)""",
        (snapshot, stamp, stamp),
    )
    conn.commit()

    with pytest.raises(stages.StageError, match="BUDGET_SNAPSHOT_INVALID"):
        stages._blueprint_generation_budget_for_trace(
            SimpleNamespace(run_id="fresh"), episode_id="e",
        )


def test_blueprint_operation_fingerprint_binds_provider_and_exact_request() -> None:
    base = {
        "episode_id": "ep-op",
        "shard_index": 1,
        "attempt": 1,
        "split_depth": 0,
        "source_hash": "source",
        "boundary_hash": "boundary",
        "prompt": "user prompt",
        "provider": "hiagent",
        "model": "model-a",
        "max_tokens": 100,
        "effective_max_tokens": 100,
        "temperature": 0.15,
        "provider_semantic_settings": {"uses_temperature": True},
    }
    original = stages._blueprint_provider_operation_id(**base)

    for key, value in (
        ("provider", "openrouter"),
        ("model", "model-b"),
        ("prompt", "changed prompt"),
        ("max_tokens", 101),
        ("effective_max_tokens", 80),
        ("temperature", 0.2),
        (
            "provider_semantic_settings",
            {"reasoning_effort": "high", "uses_temperature": False},
        ),
    ):
        assert stages._blueprint_provider_operation_id(
            **{**base, key: value},
        ) != original


def test_structured_operation_fingerprint_changes_with_provider_payload_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        stages.hiagent,
        "text_request_token_limits",
        lambda **_kwargs: ("openrouter", "model", 8192),
    )
    monkeypatch.setattr(
        stages.hiagent,
        "text_request_semantic_settings",
        lambda _provider: {"reasoning_effort": "low"},
    )
    base, effective = stages._blueprint_structured_operation_id(
        operation_kind="review",
        episode_id="ep-op",
        semantic_input_hash="content-hash",
        ordinal="1:1:full",
        messages=[{"role": "user", "content": "review"}],
        output_schema={"type": "object"},
        requested_max_tokens=16384,
        temperature=0.1,
    )
    assert effective == 8192

    monkeypatch.setattr(
        stages.hiagent,
        "text_request_semantic_settings",
        lambda _provider: {"reasoning_effort": "high"},
    )
    changed_reasoning, _ = stages._blueprint_structured_operation_id(
        operation_kind="review",
        episode_id="ep-op",
        semantic_input_hash="content-hash",
        ordinal="1:1:full",
        messages=[{"role": "user", "content": "review"}],
        output_schema={"type": "object"},
        requested_max_tokens=16384,
        temperature=0.1,
    )
    assert changed_reasoning != base

    monkeypatch.setattr(
        stages.hiagent,
        "text_request_token_limits",
        lambda **_kwargs: ("openrouter", "model", 4096),
    )
    changed_capability, _ = stages._blueprint_structured_operation_id(
        operation_kind="review",
        episode_id="ep-op",
        semantic_input_hash="content-hash",
        ordinal="1:1:full",
        messages=[{"role": "user", "content": "review"}],
        output_schema={"type": "object"},
        requested_max_tokens=16384,
        temperature=0.1,
    )
    assert changed_capability != changed_reasoning


def test_strict_durable_replay_cache_miss_is_not_sent() -> None:
    with pytest.raises(
        hiagent.ProviderError,
        match="durable provider",
    ) as caught:
        hiagent._require_cached_replay_or_raise(None, {
            "require_cached_successful_operation": True,
        })

    assert caught.value.delivery_state == "not_sent"
    assert caught.value.replay_safe is True


def test_blueprint_disables_hidden_gateway_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    async def not_sent(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise hiagent.ProviderError(
            "not sent",
            retryable=True,
            failure_kind="connection_failed",
            delivery_state="not_sent",
            replay_safe=True,
        )

    monkeypatch.setattr(stages.model_gateway.hiagent, "chat", not_sent)

    with pytest.raises(hiagent.ProviderError, match="not sent"):
        asyncio.run(stages.model_gateway.chat(
            [{"role": "user", "content": "x"}],
            call_meta={"disable_provider_retries": True},
        ))

    assert calls == 1


def test_effective_model_cap_controls_unknown_exposure() -> None:
    budget = stages._BlueprintGenerationBudget()
    reservation = budget.claim(
        max_tokens=8,
        requested_max_tokens=16,
    )
    budget.record_usage(reservation, {
        "completion_tokens": None,
        "reused": False,
    })

    settlement = budget.settle(reservation)

    assert settlement["requested_max_tokens"] == 16
    assert settlement["effective_max_tokens"] == 8
    assert settlement["charged_output_tokens"] == 8
    assert budget.requested_output_tokens == 16
    assert budget.unknown_output_tokens == 8
