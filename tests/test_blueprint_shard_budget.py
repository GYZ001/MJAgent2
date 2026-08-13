from __future__ import annotations

import asyncio
import hashlib
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


def test_durable_wall_budget_uses_original_run_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(stages, "get_conn", lambda: _DurableBudgetConnection([]))
    monkeypatch.setattr(stages.time, "time", lambda: 2000.0)
    monotonic_values = iter([500.0, 500.0])
    monkeypatch.setattr(stages.time, "monotonic", lambda: next(monotonic_values))

    budget = stages._BlueprintGenerationBudget.from_durable_calls(
        run_id="run-old",
        started_at_epoch=(
            2000.0 - stages.BLUEPRINT_GENERATION_MAX_WALL_SECONDS
        ),
    )

    with pytest.raises(stages.StageError, match="TIME_BUDGET"):
        budget.claim(max_tokens=1)
