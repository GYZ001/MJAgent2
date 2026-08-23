from __future__ import annotations

import inspect

import httpx
import pytest

from app import db, hiagent
from app.harness import model_gateway


def test_chat_accepts_response_format_keyword() -> None:
    parameter = inspect.signature(hiagent.chat).parameters["response_format"]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is None

    gateway_parameter = inspect.signature(model_gateway.chat).parameters[
        "response_format"
    ]
    assert gateway_parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert gateway_parameter.default is None


def test_response_format_unsupported_detection_only_on_explicit_client_rejection() -> None:
    # 明确拒绝 response_format 字段的客户端错误 → 判定不支持。
    explicit = hiagent.ProviderError(
        "请求被拒绝（HTTP 400）：unknown field response_format",
        retryable=False,
        raw='{"error":{"message":"unsupported parameter: response_format"}}',
    )
    assert hiagent._looks_like_response_format_unsupported(explicit) is True

    # 限流/超时/5xx（retryable）绝不能被误判为能力缺失。
    rate_limited = hiagent.ProviderError(
        "网关限流（HTTP 429）：response_format json_schema",
        retryable=True,
    )
    assert hiagent._looks_like_response_format_unsupported(rate_limited) is False

    # 与 response_format 无关的普通拒绝 → 不判定。
    unrelated = hiagent.ProviderError(
        "请求被拒绝（HTTP 400）：invalid model id",
        retryable=False,
    )
    assert hiagent._looks_like_response_format_unsupported(unrelated) is False

    # 网关要求 messages 显式提到 JSON 是请求契约错误，不是能力缺失；
    # 不得记入 unsupported 后悄然去掉结构化约束。
    missing_instruction = hiagent.ProviderError(
        "请求被拒绝（HTTP 400）：'messages' must contain the word 'json' "
        "in some form, to use 'response_format' of type 'json_object'",
        retryable=False,
        raw='{"error":{"code":"InvalidParameter"}}',
    )
    assert hiagent._looks_like_response_format_unsupported(missing_instruction) is False


def test_json_object_adds_json_instruction_without_mutating_messages() -> None:
    messages = [{"role": "user", "content": "只返回一个对象"}]

    normalized = hiagent._messages_for_response_format(
        messages, {"type": "json_object"},
    )

    assert messages == [{"role": "user", "content": "只返回一个对象"}]
    assert normalized is not messages
    assert normalized[0]["role"] == "system"
    assert "json" in normalized[0]["content"].lower()
    assert normalized[1:] == messages


def test_json_object_does_not_duplicate_existing_json_instruction() -> None:
    messages = [
        {"role": "system", "content": "Return valid JSON only."},
        {"role": "user", "content": "生成结果"},
    ]

    normalized = hiagent._messages_for_response_format(
        messages, {"type": "json_object"},
    )

    assert normalized == messages
    assert normalized is not messages
    assert sum(
        "json" in str(message.get("content") or "").lower()
        for message in normalized
    ) == 1


def test_non_json_response_format_does_not_change_messages() -> None:
    messages = [{"role": "user", "content": "写一段自由文本"}]

    assert hiagent._messages_for_response_format(messages, None) is messages
    assert hiagent._messages_for_response_format(
        messages, {"type": "text"},
    ) is messages


def test_response_format_capability_memory_roundtrip() -> None:
    provider, model = "test-provider", "test-model-cap"
    key = hiagent._response_format_capability_key(provider, model)
    hiagent._RESPONSE_FORMAT_UNSUPPORTED.discard(key)
    try:
        assert hiagent._response_format_known_unsupported(provider, model) is False
        hiagent._remember_response_format_unsupported(provider, model)
        assert hiagent._response_format_known_unsupported(provider, model) is True
    finally:
        hiagent._RESPONSE_FORMAT_UNSUPPORTED.discard(key)


@pytest.mark.asyncio
async def test_expected_json_meta_auto_attaches_json_object(monkeypatch) -> None:
    """任何 expected_json 的业务调用（含直接 chat 的蓝图分片）都应在生成阶段约束 JSON。"""
    captured: dict[str, object] = {}

    async def fake_hiagent_chat(messages, **kwargs):
        captured["messages"] = messages
        captured.update(kwargs)
        return '{"ok": true}'

    async def passthrough_slot(fn):
        return await fn()

    monkeypatch.setattr(hiagent, "chat", fake_hiagent_chat)
    monkeypatch.setattr(
        "app.generation_concurrency.run_with_provider_call_slot",
        passthrough_slot,
    )

    original = [{"role": "user", "content": "只返回一个对象"}]
    result = await model_gateway.chat(
        original,
        call_meta={"expected_json": True, "stage_key": "screenplay_blueprint_shard"},
    )
    assert result == '{"ok": true}'
    assert captured.get("response_format") == {"type": "json_object"}
    sent_messages = captured["messages"]
    assert sent_messages is not original
    assert sent_messages[0]["role"] == "system"
    assert "json" in sent_messages[0]["content"].lower()
    assert sent_messages[1:] == original
    assert original == [{"role": "user", "content": "只返回一个对象"}]


@pytest.mark.asyncio
async def test_non_json_call_does_not_attach_response_format(monkeypatch) -> None:
    captured: dict[str, object] = {}

    async def fake_hiagent_chat(messages, **kwargs):
        captured["messages"] = messages
        captured.update(kwargs)
        return "plain text answer"

    async def passthrough_slot(fn):
        return await fn()

    monkeypatch.setattr(hiagent, "chat", fake_hiagent_chat)
    monkeypatch.setattr(
        "app.generation_concurrency.run_with_provider_call_slot",
        passthrough_slot,
    )

    original = [{"role": "user", "content": "写一段自由文本"}]
    await model_gateway.chat(
        original,
        call_meta={"stage_key": "free_text"},
    )
    assert "response_format" not in captured
    assert captured["messages"] is original


@pytest.mark.asyncio
async def test_json_object_first_provider_payload_contains_json_instruction(
    monkeypatch,
) -> None:
    """The first provider request is valid; no 400-driven fallback is needed."""
    payloads: list[dict] = []

    monkeypatch.setattr(
        hiagent,
        "text_request_token_limits",
        lambda **_kwargs: ("hiagent", "test-model", 256),
    )
    monkeypatch.setattr(
        hiagent,
        "active_model_token_limits",
        lambda *_args, **_kwargs: {
            "context_window_tokens": 8192,
            "max_output_tokens": 256,
            "token_limits_source": "test",
        },
    )
    monkeypatch.setattr(
        hiagent,
        "_model_connection",
        lambda *_args, **_kwargs: ("https://example.invalid", {"x-test": "1"}),
    )
    monkeypatch.setattr(
        hiagent,
        "_cached_successful_provider_response",
        lambda *_args, **_kwargs: None,
    )

    async def fake_request(_client, _url, payload, **_kwargs):
        payloads.append(payload)
        assert payload["response_format"] == {"type": "json_object"}
        assert any(
            "json" in str(message.get("content") or "").lower()
            for message in payload["messages"]
        )
        return {"choices": [{"message": {"content": '{"ok":true}'}}]}

    monkeypatch.setattr(hiagent, "_plain_chat_request", fake_request)

    original = [{"role": "user", "content": "只返回对象"}]
    result = await hiagent.chat(
        original,
        response_format={"type": "json_object"},
        max_tokens=256,
        call_meta={"operation_id": "op-json-contract"},
    )

    assert result == '{"ok":true}'
    assert original == [{"role": "user", "content": "只返回对象"}]
    assert len(payloads) == 1


def _patch_chat_dispatch_dependencies(monkeypatch, provider: str, model: str) -> None:
    """Stub the plumbing chat() needs before it ever reaches the network.

    Shared by every test below that drives ``hiagent.chat`` end-to-end through
    a fake ``_plain_chat_request`` -- only the transport call itself differs
    per test.
    """
    monkeypatch.setattr(
        hiagent,
        "text_request_token_limits",
        lambda **_kwargs: (provider, model, 256),
    )
    monkeypatch.setattr(
        hiagent,
        "active_model_token_limits",
        lambda *_args, **_kwargs: {
            "context_window_tokens": 8192,
            "max_output_tokens": 256,
            "token_limits_source": "test",
        },
    )
    monkeypatch.setattr(
        hiagent,
        "_model_connection",
        lambda *_args, **_kwargs: (
            "https://example.invalid",
            {"x-test": "1"},
        ),
    )
    monkeypatch.setattr(
        hiagent,
        "_cached_successful_provider_response",
        lambda *_args, **_kwargs: None,
    )


def _unsupported_json_schema_error() -> "hiagent.ProviderError":
    return hiagent.ProviderError(
        "请求被拒绝（HTTP 400）：unsupported parameter json_schema",
        retryable=False,
        raw=(
            '{"error":{"message":"response_format json_schema '
            'is not supported"}}'
        ),
    )


@pytest.mark.asyncio
async def test_response_format_capability_ladder_degrades_step_by_step(
    monkeypatch,
) -> None:
    """Non-required calls may still ride the ladder all the way to free text."""
    payloads: list[dict] = []
    provider, model = "hiagent", "optional-schema-model"
    capability_key = hiagent._response_format_capability_key(provider, model)
    hiagent._RESPONSE_FORMAT_UNSUPPORTED.discard(capability_key)
    hiagent._JSON_SCHEMA_UNSUPPORTED.discard(capability_key)
    hiagent._JSON_SCHEMA_REJECT_STREAK.pop(capability_key, None)
    schema = {
        "type": "object",
        "properties": {"ok": {"type": "boolean"}},
        "required": ["ok"],
        "additionalProperties": False,
    }
    response_format = {
        "type": "json_schema",
        "json_schema": {
            "name": "optional_schema",
            "strict": True,
            "schema": schema,
        },
    }
    _patch_chat_dispatch_dependencies(monkeypatch, provider, model)

    async def rejected_request(_client, _url, payload, **_kwargs):
        payloads.append(payload)
        raise _unsupported_json_schema_error()

    monkeypatch.setattr(hiagent, "_plain_chat_request", rejected_request)
    try:
        with pytest.raises(hiagent.ProviderError, match="unsupported parameter"):
            await hiagent.chat(
                [{"role": "user", "content": "Return JSON."}],
                response_format=response_format,
                max_tokens=256,
                call_meta={"operation_id": "op-optional-json-schema"},
            )
        # 能力阶梯：json_schema → json_object → 纯文本。每一级只在网关明确拒绝
        # 上一级时下探一次，之后的调用直接从可用的那一级开始，不会每次都重新试探。
        assert [item.get("response_format") for item in payloads] == [
            response_format,
            {"type": "json_object"},
            None,
        ]
        # 单次拒绝只是一次噪声候选，还没连续到拉黑阈值（默认 3），所以
        # json_schema 这一级本身还没被记为不支持——只是这次调用自己往下探了。
        assert hiagent._json_schema_known_unsupported(provider, model) is False
        assert hiagent._JSON_SCHEMA_REJECT_STREAK.get(capability_key) == 1
        # "整个 response_format 都不支持" 是不同的、单次即记的缓存，未受影响。
        assert (
            hiagent._response_format_known_unsupported(provider, model)
            is True
        )
    finally:
        hiagent._RESPONSE_FORMAT_UNSUPPORTED.discard(capability_key)
        hiagent._JSON_SCHEMA_UNSUPPORTED.discard(capability_key)
        hiagent._JSON_SCHEMA_REJECT_STREAK.pop(capability_key, None)


@pytest.mark.asyncio
async def test_required_call_ignores_json_schema_cache_and_never_downgrades(
    monkeypatch,
) -> None:
    """response_format_required=True must not consult the opportunistic cache.

    Even a (provider, model) already blacklisted for json_schema must still
    receive the caller's exact response_format on a required call -- the cache
    is a performance shortcut for opportunistic callers only.
    """
    payloads: list[dict] = []
    provider, model = "hiagent", "required-schema-model"
    capability_key = hiagent._response_format_capability_key(provider, model)
    hiagent._JSON_SCHEMA_UNSUPPORTED.add(capability_key)  # pre-seed as "known unsupported"
    schema = {
        "type": "object",
        "properties": {"ok": {"type": "boolean"}},
        "required": ["ok"],
        "additionalProperties": False,
    }
    response_format = {
        "type": "json_schema",
        "json_schema": {
            "name": "required_schema",
            "strict": True,
            "schema": schema,
        },
    }
    _patch_chat_dispatch_dependencies(monkeypatch, provider, model)

    async def succeeding_request(_client, _url, payload, **_kwargs):
        payloads.append(payload)
        return {"choices": [{"message": {"content": '{"ok":true}'}}]}

    monkeypatch.setattr(hiagent, "_plain_chat_request", succeeding_request)
    try:
        result = await hiagent.chat(
            [{"role": "user", "content": "Return JSON."}],
            response_format=response_format,
            max_tokens=256,
            call_meta={
                "operation_id": "op-required-cache-bypass",
                "response_format_required": True,
            },
        )
        assert result == '{"ok":true}'
        assert len(payloads) == 1
        assert payloads[0]["response_format"] == response_format
    finally:
        hiagent._JSON_SCHEMA_UNSUPPORTED.discard(capability_key)
        hiagent._JSON_SCHEMA_REJECT_STREAK.pop(capability_key, None)


@pytest.mark.asyncio
async def test_required_call_retries_same_request_on_400_then_succeeds(
    monkeypatch,
) -> None:
    """A required call must retry the identical json_schema request in place.

    Real-world measurement: this class of 400 is ~3.8% independent background
    noise; replaying the exact same request usually succeeds. So a required
    call must never weaken the contract to recover -- it retries.
    """
    payloads: list[dict] = []
    provider, model = "hiagent", "required-retry-model"
    capability_key = hiagent._response_format_capability_key(provider, model)
    hiagent._JSON_SCHEMA_UNSUPPORTED.discard(capability_key)
    hiagent._JSON_SCHEMA_REJECT_STREAK.pop(capability_key, None)
    schema = {
        "type": "object",
        "properties": {"ok": {"type": "boolean"}},
        "required": ["ok"],
        "additionalProperties": False,
    }
    response_format = {
        "type": "json_schema",
        "json_schema": {
            "name": "required_schema",
            "strict": True,
            "schema": schema,
        },
    }
    _patch_chat_dispatch_dependencies(monkeypatch, provider, model)
    sleeps: list[float] = []

    async def no_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(hiagent.asyncio, "sleep", no_sleep)

    async def flaky_then_ok(_client, _url, payload, **_kwargs):
        payloads.append(payload)
        if len(payloads) == 1:
            raise _unsupported_json_schema_error()
        return {"choices": [{"message": {"content": '{"ok":true}'}}]}

    monkeypatch.setattr(hiagent, "_plain_chat_request", flaky_then_ok)
    try:
        result = await hiagent.chat(
            [{"role": "user", "content": "Return JSON."}],
            response_format=response_format,
            max_tokens=256,
            call_meta={
                "operation_id": "op-required-retry-success",
                "response_format_required": True,
            },
        )
        assert result == '{"ok":true}'
        # 两次请求都必须是原样的 json_schema，重试不允许降级。
        assert [item.get("response_format") for item in payloads] == [
            response_format,
            response_format,
        ]
        assert len(sleeps) == 1  # 退避了一次就成功了，不需要第二次重试
        # 中途的一次成功证明该 (provider, model) 支持 json_schema，连续拒绝计数
        # 必须清零，不能被后续互不相关的噪声接着累加。
        assert hiagent._JSON_SCHEMA_REJECT_STREAK.get(capability_key) is None
        assert hiagent._json_schema_known_unsupported(provider, model) is False
    finally:
        hiagent._JSON_SCHEMA_UNSUPPORTED.discard(capability_key)
        hiagent._JSON_SCHEMA_REJECT_STREAK.pop(capability_key, None)


@pytest.mark.asyncio
async def test_required_call_retry_links_provider_call_audit_chain(
    tmp_path, monkeypatch,
) -> None:
    """The required-format 400 retry must leave a traceable chain in provider_calls.

    Unlike the tests above (which stub ``hiagent._plain_chat_request`` and
    therefore never reach ``_post_json``'s ``start_provider_call`` /
    ``finish_provider_call`` calls), this test only stubs the HTTP transport
    (``httpx.AsyncClient.post``) so the *real* observability write path runs
    against a real (temp) sqlite db. It asserts the two rows share one
    operation_id -- derived purely from kind/model/payload, unaffected by the
    call_meta bookkeeping the fix adds -- and that attempt_no/supersedes_call_id
    correctly link the retry instead of looking like two unrelated calls.
    """
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "provider-required-retry.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()

    provider, model = "hiagent", "required-retry-audit-model"
    capability_key = hiagent._response_format_capability_key(provider, model)
    hiagent._JSON_SCHEMA_UNSUPPORTED.discard(capability_key)
    hiagent._JSON_SCHEMA_REJECT_STREAK.pop(capability_key, None)
    schema = {
        "type": "object",
        "properties": {"ok": {"type": "boolean"}},
        "required": ["ok"],
        "additionalProperties": False,
    }
    response_format = {
        "type": "json_schema",
        "json_schema": {
            "name": "required_schema",
            "strict": True,
            "schema": schema,
        },
    }
    _patch_chat_dispatch_dependencies(monkeypatch, provider, model)

    async def no_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(hiagent.asyncio, "sleep", no_sleep)

    calls = {"n": 0}

    class _FakeResponse:
        def __init__(self, status_code: int, *, text: str = "", json_body=None):
            self.status_code = status_code
            self.text = text
            self._json_body = json_body

        def json(self):
            return self._json_body

    async def fake_post(_self, _url, *, json, headers):  # noqa: ARG001
        calls["n"] += 1
        if calls["n"] == 1:
            return _FakeResponse(
                400,
                text=(
                    '{"error":{"message":"response_format json_schema '
                    'is not supported"}}'
                ),
            )
        return _FakeResponse(
            200,
            json_body={"choices": [{"message": {"content": '{"ok":true}'}}]},
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    try:
        # Deliberately no explicit call_meta["operation_id"] -- this exercises
        # the payload-hash path (app.db.provider_operation_id) directly, which
        # is the one the fix must not perturb.
        result = await hiagent.chat(
            [{"role": "user", "content": "Return JSON."}],
            response_format=response_format,
            max_tokens=256,
            call_meta={"response_format_required": True},
        )
        assert result == '{"ok":true}'
        assert calls["n"] == 2

        rows = db.get_conn().execute(
            "SELECT id, operation_id, attempt_no, status, supersedes_call_id, "
            "superseded_by_call_id, recovery_disposition "
            "FROM provider_calls ORDER BY id"
        ).fetchall()
        assert len(rows) == 2
        failed_row, ok_row = rows

        assert failed_row["status"] == "FAILED"
        assert ok_row["status"] == "OK"
        # Same business operation -- the hard constraint from the review: the
        # required-format retry must not make operation_id fork per attempt.
        assert failed_row["operation_id"] == ok_row["operation_id"]
        assert failed_row["operation_id"]
        # This is the actual defect: before the fix both rows land at
        # attempt_no=1 with no supersedes/superseded link, indistinguishable
        # from two unrelated duplicate calls in /api/system/calls.
        assert failed_row["attempt_no"] == 1
        assert ok_row["attempt_no"] == 2
        assert ok_row["supersedes_call_id"] == failed_row["id"]
        assert failed_row["superseded_by_call_id"] == ok_row["id"]
        assert failed_row["recovery_disposition"] == "RETRIED_SUCCESSFULLY"
    finally:
        hiagent._JSON_SCHEMA_UNSUPPORTED.discard(capability_key)
        hiagent._JSON_SCHEMA_REJECT_STREAK.pop(capability_key, None)


@pytest.mark.asyncio
async def test_required_call_raises_after_exhausting_retries_without_downgrading(
    monkeypatch,
) -> None:
    """When every retry also 400s, a required call fails loudly -- never mute."""
    payloads: list[dict] = []
    provider, model = "hiagent", "required-exhausted-model"
    capability_key = hiagent._response_format_capability_key(provider, model)
    hiagent._JSON_SCHEMA_UNSUPPORTED.discard(capability_key)
    hiagent._JSON_SCHEMA_REJECT_STREAK.pop(capability_key, None)
    schema = {
        "type": "object",
        "properties": {"ok": {"type": "boolean"}},
        "required": ["ok"],
        "additionalProperties": False,
    }
    response_format = {
        "type": "json_schema",
        "json_schema": {
            "name": "required_schema",
            "strict": True,
            "schema": schema,
        },
    }
    _patch_chat_dispatch_dependencies(monkeypatch, provider, model)

    async def no_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(hiagent.asyncio, "sleep", no_sleep)

    async def always_rejected(_client, _url, payload, **_kwargs):
        payloads.append(payload)
        raise _unsupported_json_schema_error()

    monkeypatch.setattr(hiagent, "_plain_chat_request", always_rejected)
    try:
        with pytest.raises(hiagent.ProviderError, match="unsupported parameter"):
            await hiagent.chat(
                [{"role": "user", "content": "Return JSON."}],
                response_format=response_format,
                max_tokens=256,
                call_meta={
                    "operation_id": "op-required-exhausted",
                    "response_format_required": True,
                },
            )
        expected_attempts = 1 + hiagent._RESPONSE_FORMAT_REQUIRED_JSON_SCHEMA_RETRIES
        assert len(payloads) == expected_attempts
        # 每一次都必须是原样的 json_schema，一次都不能降级。
        assert [item.get("response_format") for item in payloads] == (
            [response_format] * expected_attempts
        )
        # 三次都是拒绝，达到了拉黑阈值——这条信息对*其它非 required 调用*仍然
        # 有意义（省下一次大概率会 400 的往返），即便这条 required 调用自己
        # 从未读过这份缓存。
        assert expected_attempts >= hiagent._JSON_SCHEMA_UNSUPPORTED_STREAK_THRESHOLD
        assert hiagent._json_schema_known_unsupported(provider, model) is True
    finally:
        hiagent._JSON_SCHEMA_UNSUPPORTED.discard(capability_key)
        hiagent._JSON_SCHEMA_REJECT_STREAK.pop(capability_key, None)
