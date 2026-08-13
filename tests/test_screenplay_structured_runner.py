from __future__ import annotations

import asyncio

import pytest
from pydantic import BaseModel, Field

from app import schemas
from app.harness import model_gateway
from app.narrative_blueprint import BlueprintSemanticReview


class _Payload(BaseModel):
    value: int


class _Scene(BaseModel):
    story_function: str = Field(min_length=6)


class _ScenePayload(BaseModel):
    scenes: list[_Scene]


class _PermissiveReviewPayload(BaseModel):
    issues: list[dict] = Field(default_factory=list)


@pytest.mark.parametrize(
    ("raw", "expected_values"),
    (
        ('{"value":1}', [1]),
        (
            '{"value":1}\n明确说明：以下是修正版\n{"value":2}',
            [2, 1],
        ),
        (
            '草稿说明\n明确说明：以下是修正版\n{"value":7}',
            [7],
        ),
        ('{"parent":{"value":7},"unfinished":', []),
        ('{"parent":[{"value":7}],"unfinished":', []),
        ('{"parent":[1 {"value":7}]', []),
        ('{"parent" {"value":7}}', []),
        ('{"value":\nBROKEN\n{"value":7}', []),
        ('"unfinished {"value":7}', []),
    ),
    ids=(
        "direct-root",
        "root-after-explicitly-completed-root",
        "root-after-explanation-with-no-active-root",
        "nested-object",
        "nested-array",
        "nested-object-after-missing-comma",
        "nested-object-after-missing-colon",
        "non-json-token-does-not-reset-active-root",
        "candidate-inside-unclosed-top-level-string",
    ),
)
def test_json_candidate_provenance_acceptance_matrix(
    raw: str,
    expected_values: list[int],
) -> None:
    candidates = model_gateway._json_candidates(raw)

    assert [candidate["value"] for candidate in candidates] == expected_values


@pytest.mark.parametrize(
    "raw",
    (
        '{"parent":[1 {"issues":[]}]}',
        '{"parent" {"issues":[]}}',
    ),
    ids=("missing-comma", "missing-colon"),
)
def test_structured_runner_rejects_nested_permissive_candidate(
    monkeypatch,
    raw: str,
) -> None:
    async def fake_chat(*_args, **_kwargs):
        return raw

    monkeypatch.setattr(model_gateway, "chat", fake_chat)

    with pytest.raises(model_gateway.StructuredFormatError):
        asyncio.run(model_gateway.chat_structured(
            [{"role": "user", "content": "review"}],
            model_type=_PermissiveReviewPayload,
            validate=None,
            operation_id="test.reject-malformed-parent-child:v1:abc",
            max_tokens=128,
            format_retry_limit=0,
            semantic_retry_limit=0,
        ))


@pytest.mark.parametrize(
    "raw",
    (
        "[{}]",
        '[{"issues":[]}]',
        '说明文字：[{"issues":[]}]',
    ),
    ids=("empty-object-child", "valid-object-child", "prose-bracket-child"),
)
def test_structured_runner_rejects_array_or_bracket_child_end_to_end(
    monkeypatch,
    raw: str,
) -> None:
    async def fake_chat(*_args, **_kwargs):
        return raw

    monkeypatch.setattr(model_gateway, "chat", fake_chat)

    with pytest.raises(model_gateway.StructuredFormatError):
        asyncio.run(model_gateway.chat_structured(
            [{"role": "user", "content": "review"}],
            model_type=_PermissiveReviewPayload,
            validate=None,
            operation_id="test.reject-array-child:v1:abc",
            max_tokens=128,
            format_retry_limit=0,
            semantic_retry_limit=0,
        ))


@pytest.mark.parametrize(
    "raw",
    (
        '{"value":\nBROKEN\n{"issues":[]}',
        '"unfinished {"issues":[]}',
    ),
    ids=("damaged-active-root", "unclosed-top-level-string"),
)
def test_structured_runner_rejects_polluted_permissive_candidate_end_to_end(
    monkeypatch,
    raw: str,
) -> None:
    async def fake_chat(*_args, **_kwargs):
        return raw

    monkeypatch.setattr(model_gateway, "chat", fake_chat)

    with pytest.raises(model_gateway.StructuredFormatError):
        asyncio.run(model_gateway.chat_structured(
            [{"role": "user", "content": "review"}],
            model_type=_PermissiveReviewPayload,
            validate=None,
            operation_id="test.reject-polluted-permissive:v1:abc",
            max_tokens=128,
            format_retry_limit=0,
            semantic_retry_limit=0,
        ))


@pytest.mark.parametrize(
    "raw",
    (
        '草稿说明\n最终答案: {"value":7}',
        '草稿说明\n最终 {"value":7}, 以上为修正版',
    ),
)
def test_structured_runner_recovers_complete_trailing_json(
    monkeypatch,
    raw: str,
) -> None:
    calls = 0
    attempts: list[dict] = []

    async def fake_chat(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return raw

    def unexpected_root_repair(*_args, **_kwargs):
        raise AssertionError("valid trailing object must win before root repair")

    monkeypatch.setattr(model_gateway, "chat", fake_chat)
    monkeypatch.setattr(schemas, "extract_json", unexpected_root_repair)
    result = asyncio.run(model_gateway.chat_structured(
        [{"role": "user", "content": "return json"}],
        model_type=_Payload,
        validate=None,
        operation_id="test.trailing-json:v1:abc",
        max_tokens=128,
        on_attempt=attempts.append,
    ))
    assert result.value == 7
    assert calls == 1
    assert attempts[0]["outcome"] == "validated"
    assert attempts[0]["local_recovery"] is True


def test_structured_runner_only_validates_latest_complete_root(
    monkeypatch,
) -> None:
    attempts: list[dict] = []
    raw = '{"value":1}\n以下是最新修正版\n{"value":2}'

    async def fake_chat(*_args, **_kwargs):
        return raw

    def unexpected_candidate_scan(*_args, **_kwargs):
        raise AssertionError("chat_structured must not enumerate older roots")

    def unexpected_root_repair(*_args, **_kwargs):
        raise AssertionError("a complete latest root must not use extract_json")

    monkeypatch.setattr(model_gateway, "chat", fake_chat)
    monkeypatch.setattr(model_gateway, "_json_candidates", unexpected_candidate_scan)
    monkeypatch.setattr(schemas, "extract_json", unexpected_root_repair)

    result = asyncio.run(model_gateway.chat_structured(
        [{"role": "user", "content": "return json"}],
        model_type=_Payload,
        validate=None,
        operation_id="test.authoritative-latest-complete:v1:abc",
        max_tokens=128,
        format_retry_limit=0,
        semantic_retry_limit=0,
        on_attempt=attempts.append,
    ))

    assert result.value == 2
    assert attempts[0]["outcome"] == "validated"
    assert attempts[0]["local_recovery"] is True


@pytest.mark.parametrize(
    "raw",
    (
        '{"value":1}\n以下是最新修正版\n{value:2}',
        '{"value":1}\n以下是最新修正版\n{not JSON}',
        "{value:2}",
        "{not JSON}",
    ),
    ids=(
        "old-valid-before-unquoted-key",
        "old-valid-before-non-json-object",
        "standalone-unquoted-key",
        "standalone-non-json-object",
    ),
)
def test_structured_runner_fails_closed_on_latest_unparseable_root(
    monkeypatch,
    raw: str,
) -> None:
    attempts: list[dict] = []

    async def fake_chat(*_args, **_kwargs):
        return raw

    monkeypatch.setattr(model_gateway, "chat", fake_chat)

    with pytest.raises(model_gateway.StructuredFormatError, match="JSON 解析失败"):
        asyncio.run(model_gateway.chat_structured(
            [{"role": "user", "content": "return json"}],
            model_type=_Payload,
            validate=None,
            operation_id="test.fail-closed-latest-root:v1:abc",
            max_tokens=128,
            format_retry_limit=0,
            semantic_retry_limit=0,
            on_attempt=attempts.append,
        ))

    assert attempts[0]["outcome"] == "format_error"
    assert "JSON 解析失败" in attempts[0]["validation_errors"][0]


def test_structured_runner_repairs_root_after_nested_review_candidates_fail(
    monkeypatch,
) -> None:
    calls = 0
    attempts: list[dict] = []
    raw = """{"issues":[
    {
        "code":"state_subject_assignment_conflict",
        "node_keys":["S005-E01-S05-N001"],
        "message":"joint主体与原文冲突",
        "required_resolution":"修正为["孟浩","王有材"]并保持其余字段"
    },
    {
        "code":"timeline_conflict",
        "node_keys":["S004-N001"],
        "message":"时间顺序冲突",
        "required_resolution":"恢复原文顺序"
    }
],"transport_note":"production-29805-redacted"}"""

    async def fake_chat(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return raw

    def normalize(payload):
        normalized = dict(payload)
        normalized.pop("transport_note", None)
        return normalized

    monkeypatch.setattr(model_gateway, "chat", fake_chat)

    result = asyncio.run(model_gateway.chat_structured(
        [{"role": "user", "content": "review current blueprint"}],
        model_type=BlueprintSemanticReview,
        validate=None,
        operation_id="test.review-root-recovery:v1:production-29805-redacted",
        max_tokens=8192,
        format_retry_limit=0,
        semantic_retry_limit=0,
        normalize_payload=normalize,
        on_attempt=attempts.append,
    ))

    assert isinstance(result, BlueprintSemanticReview)
    assert result.issues[0].required_resolution == (
        '修正为["孟浩","王有材"]并保持其余字段'
    )
    assert calls == 1
    assert len(attempts) == 1
    assert attempts[0]["outcome"] == "validated"
    assert attempts[0]["format_attempt"] == 0
    assert attempts[0]["semantic_attempt"] == 0
    assert attempts[0]["local_recovery"] is True


def test_structured_runner_repairs_latest_eligible_review_root(
    monkeypatch,
) -> None:
    calls = 0
    attempts: list[dict] = []
    raw = """{"issues":[]}
以上是旧草稿，以下是最新修正版。
{"issues":[
    {
        "code":"state_subject_assignment_conflict",
        "node_keys":["S005-E01-S05-N001"],
        "message":"joint主体与原文冲突",
        "required_resolution":"修正为["孟浩","王有材"]并保持其余字段"
    },
    {
        "code":"timeline_conflict",
        "node_keys":["S004-N001"],
        "message":"时间顺序冲突",
        "required_resolution":"恢复原文顺序"
    }
]}"""

    async def fake_chat(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return raw

    monkeypatch.setattr(model_gateway, "chat", fake_chat)

    result = asyncio.run(model_gateway.chat_structured(
        [{"role": "user", "content": "review current blueprint"}],
        model_type=BlueprintSemanticReview,
        validate=None,
        operation_id="test.latest-review-root:v1:production-29805-redacted",
        max_tokens=8192,
        format_retry_limit=0,
        semantic_retry_limit=0,
        on_attempt=attempts.append,
    ))

    assert [issue.code for issue in result.issues] == [
        "state_subject_assignment_conflict",
        "timeline_conflict",
    ]
    assert result.issues[0].required_resolution == (
        '修正为["孟浩","王有材"]并保持其余字段'
    )
    assert calls == 1
    assert len(attempts) == 1
    assert attempts[0]["outcome"] == "validated"
    assert attempts[0]["format_attempt"] == 0
    assert attempts[0]["local_recovery"] is True


def test_structured_runner_does_not_accept_nested_candidate_as_root(
    monkeypatch,
) -> None:
    raw = """{"issues":[
    {"code":"first","message":"必须保留的问题一"},
    {"code":"second","message":"必须保留的问题二"}
],"note":"修正为["甲","乙"]"}"""

    async def fake_chat(*_args, **_kwargs):
        return raw

    monkeypatch.setattr(model_gateway, "chat", fake_chat)

    result = asyncio.run(model_gateway.chat_structured(
        [{"role": "user", "content": "review"}],
        model_type=_PermissiveReviewPayload,
        validate=None,
        operation_id="test.reject-nested-root:v1:abc",
        max_tokens=128,
        format_retry_limit=0,
        semantic_retry_limit=0,
    ))

    assert [issue["code"] for issue in result.issues] == ["first", "second"]


def test_structured_runner_rejects_unknown_only_quote_repaired_root(
    monkeypatch,
) -> None:
    async def fake_chat(*_args, **_kwargs):
        return '{"transport_note":"修正为["甲","乙"]"}'

    monkeypatch.setattr(model_gateway, "chat", fake_chat)

    with pytest.raises(model_gateway.StructuredFormatError):
        asyncio.run(model_gateway.chat_structured(
            [{"role": "user", "content": "review"}],
            model_type=_PermissiveReviewPayload,
            validate=None,
            operation_id="test.reject-default-only-recovery:v1:abc",
            max_tokens=128,
            format_retry_limit=0,
            semantic_retry_limit=0,
        ))


def test_structured_runner_preserves_direct_empty_object_behavior(
    monkeypatch,
) -> None:
    async def fake_chat(*_args, **_kwargs):
        return "{}"

    monkeypatch.setattr(model_gateway, "chat", fake_chat)

    result = asyncio.run(model_gateway.chat_structured(
        [{"role": "user", "content": "review"}],
        model_type=_PermissiveReviewPayload,
        validate=None,
        operation_id="test.direct-empty-object:v1:abc",
        max_tokens=128,
        format_retry_limit=0,
        semantic_retry_limit=0,
    ))

    assert result.issues == []
    assert result.model_fields_set == set()


def test_structured_runner_uses_one_format_repair(monkeypatch) -> None:
    prompts: list[str] = []
    metas: list[dict] = []

    async def fake_chat(messages, **kwargs):
        prompts.append(messages[0]["content"])
        metas.append(kwargs["call_meta"])
        return "not-json" if len(prompts) == 1 else '{"value":3}'

    monkeypatch.setattr(model_gateway, "chat", fake_chat)
    result = asyncio.run(model_gateway.chat_structured(
        [{"role": "user", "content": "original large context"}],
        model_type=_Payload,
        validate=None,
        operation_id="test.format-repair:v1:abc",
        max_tokens=128,
        format_retry_limit=1,
    ))
    assert result.value == 3
    assert len(prompts) == 2
    assert "Schema" in prompts[1]
    assert "not-json" in prompts[1]
    assert "original large context" not in prompts[1]
    assert [meta["format_attempt"] for meta in metas] == [0, 1]
    assert [meta["semantic_attempt"] for meta in metas] == [0, 0]
    assert metas[0]["operation_id"] != metas[1]["operation_id"]
    assert metas[0]["base_operation_id"] == metas[1]["base_operation_id"]


def test_format_repair_keeps_outer_candidate_validation_error(
    monkeypatch,
) -> None:
    prompts: list[str] = []

    async def fake_chat(messages, **_kwargs):
        prompts.append(messages[0]["content"])
        if len(prompts) == 1:
            return '{"scenes":[{"story_function":"setup"}]}'
        return '{"scenes":[{"story_function":"建立本场冲突"}]}'

    monkeypatch.setattr(model_gateway, "chat", fake_chat)

    result = asyncio.run(model_gateway.chat_structured(
        [{"role": "user", "content": "original"}],
        model_type=_ScenePayload,
        validate=None,
        operation_id="test.outer-format-error:v1:abc",
        max_tokens=128,
        format_retry_limit=1,
    ))

    assert result.scenes[0].story_function == "建立本场冲突"
    assert "String should have at least 6 characters" in prompts[1]
    assert '"story_function":"setup"' in prompts[1]
    assert "Field required" not in prompts[1]


def test_format_repair_does_not_fall_back_past_latest_complete_root(
    monkeypatch,
) -> None:
    prompts: list[str] = []
    attempts: list[dict] = []
    raw = '{"value":1}\n最终候选 {"other":1}'

    async def fake_chat(messages, **_kwargs):
        prompts.append(messages[0]["content"])
        return raw if len(prompts) == 1 else '{"value":3}'

    def unexpected_root_repair(*_args, **_kwargs):
        raise AssertionError("a complete latest root must not use extract_json")

    monkeypatch.setattr(model_gateway, "chat", fake_chat)
    monkeypatch.setattr(schemas, "extract_json", unexpected_root_repair)

    result = asyncio.run(model_gateway.chat_structured(
        [{"role": "user", "content": "original"}],
        model_type=_Payload,
        validate=None,
        operation_id="test.recovered-root-format-error:v1:abc",
        max_tokens=128,
        format_retry_limit=1,
        on_attempt=attempts.append,
    ))

    assert result.value == 3
    assert "Field required" in prompts[1]
    assert '"other":1' in prompts[1]
    assert '"value":1' not in prompts[1]
    assert "Input should be a valid integer" not in prompts[1]
    assert [attempt["outcome"] for attempt in attempts] == [
        "format_error",
        "validated",
    ]
    assert "Field required" in attempts[0]["validation_errors"][0]
    assert attempts[0]["local_recovery"] is True


def test_format_repair_uses_unrepairable_latest_root_not_older_valid_root(
    monkeypatch,
) -> None:
    prompts: list[str] = []
    attempts: list[dict] = []
    raw = '{"value":1}\n最终候选 {"value":2 "note":"缺少逗号"}'

    async def fake_chat(messages, **_kwargs):
        prompts.append(messages[0]["content"])
        return raw if len(prompts) == 1 else '{"value":3}'

    monkeypatch.setattr(model_gateway, "chat", fake_chat)

    result = asyncio.run(model_gateway.chat_structured(
        [{"role": "user", "content": "original"}],
        model_type=_Payload,
        validate=None,
        operation_id="test.unrepairable-latest-root:v1:abc",
        max_tokens=128,
        format_retry_limit=1,
        on_attempt=attempts.append,
    ))

    assert result.value == 3
    assert "JSON 解析失败" in prompts[1]
    assert '{"value":2 "note":"缺少逗号"}' in prompts[1]
    assert '{"value":1}' not in prompts[1]
    assert [attempt["outcome"] for attempt in attempts] == [
        "format_error",
        "validated",
    ]
    assert "JSON 解析失败" in attempts[0]["validation_errors"][0]
    assert attempts[0]["local_recovery"] is True


def test_structured_runner_applies_local_payload_normalizer(
    monkeypatch,
) -> None:
    calls = 0
    attempts: list[dict] = []

    async def fake_chat(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return '{"value":1}'

    monkeypatch.setattr(model_gateway, "chat", fake_chat)

    result = asyncio.run(model_gateway.chat_structured(
        [{"role": "user", "content": "original"}],
        model_type=_Payload,
        validate=None,
        operation_id="test.local-normalizer:v1:abc",
        max_tokens=128,
        normalize_payload=lambda payload: {**payload, "value": 7},
        on_attempt=attempts.append,
    ))

    assert result.value == 7
    assert calls == 1
    assert attempts[0]["local_recovery"] is True


def test_structured_runner_semantic_retry_has_independent_budget(monkeypatch) -> None:
    prompts: list[str] = []
    metas: list[dict] = []

    async def fake_chat(messages, **kwargs):
        prompts.append(messages[0]["content"])
        metas.append(kwargs["call_meta"])
        return '{"value":-1}' if len(prompts) == 1 else '{"value":2}'

    monkeypatch.setattr(model_gateway, "chat", fake_chat)
    result = asyncio.run(model_gateway.chat_structured(
        [{"role": "user", "content": "original"}],
        model_type=_Payload,
        validate=lambda value: [] if value.value > 0 else ["value 必须为正数"],
        operation_id="test.semantic-retry:v1:abc",
        max_tokens=128,
        format_retry_limit=0,
        semantic_retry_limit=1,
        repair_context="only this context",
    ))
    assert result.value == 2
    assert "value 必须为正数" in prompts[1]
    assert "only this context" in prompts[1]
    assert [meta["format_attempt"] for meta in metas] == [0, 0]
    assert [meta["semantic_attempt"] for meta in metas] == [0, 1]
    assert metas[0]["operation_id"] != metas[1]["operation_id"]
    assert metas[0]["base_operation_id"] == metas[1]["base_operation_id"]


def test_structured_runner_builds_repair_schema_from_failed_candidate(
    monkeypatch,
) -> None:
    prompts: list[str] = []
    repair_candidates: list[int] = []

    async def fake_chat(messages, **_kwargs):
        prompts.append(messages[0]["content"])
        return '{"value":-1}' if len(prompts) == 1 else '{"value":2}'

    def build_repair_schema(value: _Payload) -> dict:
        repair_candidates.append(value.value)
        return {
            "type": "object",
            "x-schema-phase": "semantic-repair",
            "x-failed-value": value.value,
        }

    monkeypatch.setattr(model_gateway, "chat", fake_chat)
    result = asyncio.run(model_gateway.chat_structured(
        [{"role": "user", "content": "original"}],
        model_type=_Payload,
        validate=lambda value: [] if value.value > 0 else ["value 必须为正数"],
        operation_id="test.dynamic-semantic-schema:v1:abc",
        max_tokens=128,
        semantic_retry_limit=1,
        output_schema={
            "type": "object",
            "x-schema-phase": "initial-output",
        },
        repair_schema=build_repair_schema,
    ))

    assert result.value == 2
    assert repair_candidates == [-1]
    assert '"x-schema-phase": "semantic-repair"' in prompts[1]
    assert '"x-failed-value": -1' in prompts[1]
    assert '"x-schema-phase": "initial-output"' not in prompts[1]


def test_structured_runner_does_not_accept_malformed_http_200(monkeypatch) -> None:
    async def fake_chat(*_args, **_kwargs):
        return "HTTP 200 but no task object"

    monkeypatch.setattr(model_gateway, "chat", fake_chat)
    with pytest.raises(model_gateway.StructuredFormatError):
        asyncio.run(model_gateway.chat_structured(
            [{"role": "user", "content": "original"}],
            model_type=_Payload,
            validate=None,
            operation_id="test.malformed:v1:abc",
            max_tokens=128,
            format_retry_limit=0,
        ))


def test_structured_runner_does_not_schema_repair_provider_error_envelope(
    monkeypatch,
) -> None:
    calls = 0
    attempts: list[dict] = []

    async def fake_chat(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return '{"error":"content policy rejected"}'

    monkeypatch.setattr(model_gateway, "chat", fake_chat)

    with pytest.raises(
        model_gateway.StructuredProviderRejection,
        match="content policy rejected",
    ):
        asyncio.run(model_gateway.chat_structured(
            [{"role": "user", "content": "original"}],
            model_type=_Payload,
            validate=None,
            operation_id="test.provider-rejected:v1:abc",
            max_tokens=128,
            format_retry_limit=2,
            on_attempt=attempts.append,
        ))

    assert calls == 1
    assert attempts[0]["outcome"] == "provider_rejected"
