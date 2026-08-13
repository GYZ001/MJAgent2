from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from app import hiagent, stages
from app.source_excerpt import index_source_segments


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


class _EmptyRows:
    @staticmethod
    def fetchall() -> list[dict]:
        return []


class _NoCacheConnection:
    @staticmethod
    def execute(_sql: str, _params=()) -> _EmptyRows:
        return _EmptyRows()


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
    budget.requested_output_tokens = stages.BLUEPRINT_GENERATION_MAX_OUTPUT_TOKENS
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
