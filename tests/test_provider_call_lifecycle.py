import asyncio
import base64
import json
import sqlite3

import httpx
import pytest

from app import db, hiagent, system_api, video_modes, worker


class _Response:
    status_code = 200
    text = '{"choices": []}'

    def json(self):
        return {"choices": []}


class _Client:
    async def post(self, url, *, json, headers):
        return _Response()


@pytest.mark.parametrize(
    ("delivery_state", "replay_safe", "create_not_accepted", "unknown"),
    [
        pytest.param("responded", False, False, True, id="http-409"),
        pytest.param("responded", False, False, True, id="http-429"),
        pytest.param("responded", False, False, True, id="http-5xx"),
        pytest.param("unknown", False, False, True, id="malformed-2xx"),
        pytest.param("unknown", False, False, True, id="read-timeout"),
        pytest.param("unknown", False, False, True, id="write-timeout"),
        pytest.param("not_sent", True, False, False, id="connect-or-pool-timeout"),
        pytest.param("responded", False, True, False, id="explicit-not-accepted"),
    ],
)
def test_video_create_replay_safety_is_explicit_only(
    delivery_state: str,
    replay_safe: bool,
    create_not_accepted: bool,
    unknown: bool,
) -> None:
    error = hiagent.ProviderError(
        "provider create test",
        delivery_state=delivery_state,
        replay_safe=replay_safe,
        create_not_accepted=create_not_accepted,
    )

    assert worker._provider_create_outcome_unknown(error) is unknown


def test_screenplay_baseline_uses_dedicated_long_read_timeout(monkeypatch) -> None:
    monkeypatch.setattr(hiagent.config, "TIMEOUT_CHAT_READ", 300.0)
    monkeypatch.setattr(hiagent.config, "TIMEOUT_CHAT_BASELINE_READ", 600.0)
    monkeypatch.setattr(hiagent.config, "TIMEOUT_CHAT_VIDEO_PLAN_READ", 660.0)
    monkeypatch.setattr(hiagent.config, "TIMEOUT_CHAT_STORYBOARD_OUTLINE_READ", 720.0)

    assert hiagent._chat_read_timeout_s(None) == 300.0
    assert hiagent._chat_read_timeout_s({"stage": "discover_character_candidates"}) == 300.0
    assert hiagent._chat_read_timeout_s({"stage": "剧本首次整版 Baseline"}) == 600.0
    assert hiagent._chat_read_timeout_s({"stage": "screenplay_narrative_patch"}) == 600.0
    assert hiagent._chat_read_timeout_s({"stage": "episode_video_mode_plan"}) == 660.0
    assert hiagent._chat_read_timeout_s({"stage_key": "narrative_graph_patch"}) == 600.0
    assert hiagent._chat_read_timeout_s({"stage_key": "storyboard_outline"}) == 720.0
    assert hiagent._chat_read_timeout_s({"stage_key": "storyboard"}) == 600.0
    assert hiagent._chat_read_timeout_s({"stage_key": "storyboard_shot_6"}) == 600.0


def test_post_json_writes_running_before_updating_same_ledger_row(monkeypatch) -> None:
    events: list[tuple] = []

    def start(kind, model, *, meta=None, request_json=None):
        events.append(("start", kind, model, meta, request_json))
        return 42

    def finish(call_id, status, http_status, latency_ms, *, error=None, response_json=None):
        events.append(("finish", call_id, status, http_status, response_json))

    monkeypatch.setattr(hiagent, "start_provider_call", start)
    monkeypatch.setattr(hiagent, "finish_provider_call", finish)

    result = asyncio.run(hiagent._post_json(
        _Client(), "https://example.invalid/chat", {"prompt": "hello"},
        kind="chat", model="test-model", headers={"x": "y"}, meta={"stage": "可拍剧本"},
    ))

    assert result == {"choices": []}
    assert events[0][0] == "start"
    assert events[0][3]["http_attempt"] == 1
    assert events[1][:4] == ("finish", 42, "OK", 200)


def test_post_json_write_timeout_is_logged_and_not_retried(monkeypatch) -> None:
    events: list[tuple] = []

    class WriteTimeoutClient:
        async def post(self, url, *, json, headers):
            raise httpx.WriteTimeout("upload stalled")

    monkeypatch.setattr(
        hiagent, "start_provider_call",
        lambda kind, model, *, meta=None, request_json=None: events.append(("start", meta)) or 7,
    )
    monkeypatch.setattr(
        hiagent, "finish_provider_call",
        lambda call_id, status, http_status, latency_ms, *, error=None, response_json=None:
            events.append(("finish", status, error)),
    )

    try:
        asyncio.run(hiagent._post_json(
            WriteTimeoutClient(), "https://example.invalid/image", {"image": "abc"},
            kind="image_edit", model="test-model", headers={"x": "y"}, retries=2,
        ))
    except hiagent.ProviderError as exc:
        assert exc.timeout_phase == "write"
        assert "请求" in str(exc)
    else:
        raise AssertionError("expected ProviderError")

    assert [event[0] for event in events] == ["start", "finish"]
    assert events[0][1]["request_bytes"] > 0
    assert "WriteTimeout" in events[1][2]
    assert "phase=write" in events[1][2]


def test_harness_chat_read_timeout_is_interrupted_without_replay(
    tmp_path, monkeypatch,
) -> None:
    attempts = 0
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "provider-read-timeout.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()

    class ReadTimeoutClient:
        async def post(self, url, *, json, headers):
            nonlocal attempts
            attempts += 1
            raise httpx.ReadTimeout("generation still running")

    with pytest.raises(hiagent.ProviderError) as caught:
        asyncio.run(hiagent._post_json(
            ReadTimeoutClient(),
            "https://example.invalid/chat",
            {"messages": []},
            kind="chat",
            model="test-model",
            headers={"x": "y"},
            retries=2,
            meta={"gateway": "execution_harness"},
        ))

    assert caught.value.retryable is True
    assert caught.value.timeout_phase == "read"
    assert caught.value.delivery_state == "unknown"
    assert caught.value.replay_safe is False
    assert caught.value.requires_explicit_retry is True
    assert attempts == 1
    row = db.get_conn().execute(
        "SELECT status,recovery_disposition,response_json "
        "FROM provider_calls ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert dict(row) == {
        "status": "INTERRUPTED",
        "recovery_disposition": "REQUIRES_EXPLICIT_RETRY",
        "response_json": None,
    }


@pytest.mark.parametrize("heartbeat", [b"\n", b": keepalive\n\n"], ids=["blank-line", "keepalive"])
def test_stream_total_timeout_covers_keepalive_and_blank_lines(
    heartbeat, monkeypatch,
) -> None:
    fault = {"timeout_scope_active": False}
    events: list[tuple] = []

    class InjectedTotalTimeout:
        async def __aenter__(self):
            fault["timeout_scope_active"] = True

        async def __aexit__(self, *_args):
            fault["timeout_scope_active"] = False

    class HeartbeatThenTimeout(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield heartbeat
            if fault["timeout_scope_active"]:
                raise asyncio.TimeoutError("injected stream total timeout")
            yield b"data: [DONE]\n\n"

        async def aclose(self):
            return None

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            stream=HeartbeatThenTimeout(),
            headers={"content-type": "text/event-stream"},
        )

    monkeypatch.setattr(hiagent.asyncio, "timeout", lambda _seconds: InjectedTotalTimeout())
    monkeypatch.setattr(hiagent, "get_setting", lambda _key: "60")
    monkeypatch.setattr(hiagent, "start_provider_call", lambda *_args, **_kwargs: 17)
    monkeypatch.setattr(
        hiagent,
        "finish_provider_call",
        lambda call_id, status, http_status, latency_ms, *, error=None, response_json=None:
        events.append((call_id, status, http_status, error, response_json)),
    )

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await hiagent._stream_chat_completion(
                client,
                "https://provider.test/chat/completions",
                {"model": "m", "messages": []},
                kind="chat",
                model="m",
                headers={},
            )

    with pytest.raises(hiagent.ProviderError) as caught:
        asyncio.run(run())

    assert caught.value.timeout_phase == "总时长"
    assert caught.value.retryable is True
    assert caught.value.replay_safe is False
    assert caught.value.requires_explicit_retry is True
    assert len(events) == 1
    assert events[0][0:3] == (17, "INTERRUPTED", None)
    assert events[0][4] is None


def test_stream_eof_without_done_fails_lifecycle_and_discards_partial_tool_call(
    tmp_path, monkeypatch,
) -> None:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "provider-stream-eof.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()
    partial_text = "部分回复"
    frame = {
        "choices": [{
            "delta": {
                "content": partial_text,
                "tool_calls": [{
                    "index": 0,
                    "id": "call_partial",
                    "function": {
                        "name": "resource.read",
                        "arguments": '{"uri":"manju://pro',
                    },
                }],
            },
            "finish_reason": "tool_calls",
        }],
    }
    body = f"data: {json.dumps(frame, ensure_ascii=False)}\n\n"
    emitted: list[tuple[str, str]] = []

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=body,
            headers={"content-type": "text/event-stream"},
        )

    def forbidden_reconstruction(*_args, **_kwargs):
        raise AssertionError("incomplete stream must not reconstruct truncated tool arguments")

    monkeypatch.setattr(hiagent, "get_setting", lambda _key: "60")
    monkeypatch.setattr(hiagent, "_reconstruct_stream_data", forbidden_reconstruction)

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await hiagent._stream_chat_completion(
                client,
                "https://provider.test/chat/completions",
                {"model": "m", "messages": []},
                kind="chat_tools",
                model="m",
                headers={},
                on_token=lambda kind, text: emitted.append((kind, text)),
            )

    with pytest.raises(hiagent.ProviderError) as caught:
        asyncio.run(run())

    assert caught.value.failure_kind == "stream_interrupted"
    assert caught.value.retryable is True
    assert caught.value.delivery_state == "unknown"
    assert caught.value.replay_safe is False
    assert caught.value.requires_explicit_retry is True
    assert emitted == [("content", partial_text)]
    row = db.get_conn().execute(
        "SELECT status,http_status,error,response_json,received_chars,recovery_disposition "
        "FROM provider_calls WHERE kind='chat_tools' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row["status"] == "INTERRUPTED"
    assert row["http_status"] == 200
    assert row["error"].startswith("stream interrupted before [DONE] (latency_ms=")
    assert row["error"].endswith(f", received_chars={len(partial_text)})")
    assert row["response_json"] is None
    assert row["received_chars"] == len(partial_text)
    assert row["recovery_disposition"] == "REQUIRES_EXPLICIT_RETRY"


def test_harness_zero_token_stream_interruption_does_not_fallback_to_second_request(
    tmp_path, monkeypatch,
) -> None:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "provider-stream-zero-token.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()
    stream_requests = 0
    fallback_requests = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal stream_requests
        stream_requests += 1
        return httpx.Response(
            200,
            text="",
            headers={"content-type": "text/event-stream"},
        )

    async def forbidden_post_json(*_args, **_kwargs):
        nonlocal fallback_requests
        fallback_requests += 1
        raise AssertionError("interrupted stream must not create a second generation")

    monkeypatch.setattr(hiagent, "get_setting", lambda _key: "60")
    monkeypatch.setattr(hiagent, "_post_json", forbidden_post_json)

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await hiagent._stream_or_fallback(
                client,
                "https://provider.test/chat/completions",
                {"model": "m", "messages": []},
                kind="chat",
                model="m",
                headers={},
                key_name="TEST_API_KEY",
                meta={"gateway": "execution_harness"},
                on_token=lambda _kind, _text: None,
            )

    with pytest.raises(hiagent.ProviderError) as caught:
        asyncio.run(run())

    assert caught.value.failure_kind == "stream_interrupted"
    assert caught.value.requires_explicit_retry is True
    assert stream_requests == 1
    assert fallback_requests == 0
    row = db.get_conn().execute(
        "SELECT status,recovery_disposition FROM provider_calls ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert dict(row) == {
        "status": "INTERRUPTED",
        "recovery_disposition": "REQUIRES_EXPLICIT_RETRY",
    }


def test_harness_chat_network_error_has_single_adapter_attempt(monkeypatch) -> None:
    attempts = 0

    class NetworkErrorClient:
        async def post(self, url, *, json, headers):
            nonlocal attempts
            attempts += 1
            raise httpx.ConnectError("connection reset")

    monkeypatch.setattr(hiagent, "start_provider_call", lambda *_args, **_kwargs: 7)
    monkeypatch.setattr(hiagent, "finish_provider_call", lambda *_args, **_kwargs: None)

    with pytest.raises(hiagent.ProviderError, match="网络错误") as caught:
        asyncio.run(hiagent._post_json(
            NetworkErrorClient(),
            "https://example.invalid/chat",
            {"messages": []},
            kind="chat",
            model="test-model",
            headers={"x": "y"},
            retries=2,
            meta={"gateway": "execution_harness"},
        ))

    assert attempts == 1
    assert caught.value.delivery_state == "not_sent"
    assert caught.value.replay_safe is True
    assert caught.value.requires_explicit_retry is False


def test_harness_chat_429_is_not_immediately_replayed(monkeypatch) -> None:
    attempts = 0

    class RateLimitedResponse:
        status_code = 429
        text = '{"error":{"message":"TPM limit exceeded"}}'

    class RateLimitedClient:
        async def post(self, url, *, json, headers):
            nonlocal attempts
            attempts += 1
            return RateLimitedResponse()

    monkeypatch.setattr(hiagent, "start_provider_call", lambda *_args, **_kwargs: 7)
    monkeypatch.setattr(hiagent, "finish_provider_call", lambda *_args, **_kwargs: None)

    with pytest.raises(hiagent.ProviderError) as caught:
        asyncio.run(hiagent._post_json(
            RateLimitedClient(),
            "https://example.invalid/chat",
            {"messages": []},
            kind="chat",
            model="test-model",
            headers={"x": "y"},
            retries=2,
            meta={"gateway": "execution_harness"},
        ))

    assert caught.value.retryable is True
    assert caught.value.delivery_state == "responded"
    assert caught.value.replay_safe is False
    assert attempts == 1


def test_http_400_rejection_is_not_replayed_by_error_text(monkeypatch) -> None:
    attempts = 0

    class OutputSafetyResponse:
        status_code = 400
        text = (
            '{"error":{"code":"OutputImageSensitiveContentDetected",'
            '"message":"output image may contain sensitive information"}}'
        )

    class SuccessResponse:
        status_code = 200
        text = '{"data":[{"url":"https://example.invalid/image.jpg"}]}'

        def json(self):
            return {"data": [{"url": "https://example.invalid/image.jpg"}]}

    class RecoveringImageClient:
        async def post(self, url, *, json, headers):
            nonlocal attempts
            attempts += 1
            return OutputSafetyResponse() if attempts == 1 else SuccessResponse()

    monkeypatch.setattr(hiagent, "start_provider_call", lambda *_args, **_kwargs: attempts + 10)
    monkeypatch.setattr(hiagent, "finish_provider_call", lambda *_args, **_kwargs: None)

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr(hiagent.asyncio, "sleep", no_sleep)

    with pytest.raises(hiagent.ProviderError) as caught:
        asyncio.run(hiagent._post_json(
            RecoveringImageClient(),
            "https://example.invalid/images/generations",
            {"prompt": "ordinary character portrait"},
            kind="image_generate",
            model="test-model",
            headers={"x": "y"},
            retries=2,
        ))

    assert attempts == 1
    assert caught.value.failure_kind == "provider_rejected"


def test_http_400_rejection_is_typed_without_parsing_body_words() -> None:
    error = hiagent._classify_http_error(
        400,
        '{"error":{"code":"InputTextSensitiveContentDetected"}}',
    )

    assert error.retryable is False
    assert error.failure_kind == "provider_rejected"
    assert "请求被拒绝" in str(error)


def test_image_rejection_does_not_rewrite_or_replay_prompt(monkeypatch) -> None:
    calls: list[dict] = []

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    async def fake_post_json(_client, _url, payload, **kwargs):
        calls.append({"payload": payload, **kwargs})
        raise hiagent.ProviderError(
            "供应商拒绝请求（HTTP 400）",
            raw='{"error":{"code":"AnyFutureProviderCode"}}',
            failure_kind="provider_rejected",
        )

    monkeypatch.setattr(hiagent.httpx, "AsyncClient", lambda **_kwargs: Client())
    monkeypatch.setattr(hiagent, "_post_json", fake_post_json)
    monkeypatch.setattr(hiagent, "_model_connection", lambda *_args: ("https://provider", {"x": "y"}))
    monkeypatch.setattr(hiagent, "active_model", lambda *_args: "image-model")

    prompt = "男，深色夹克军靴，高大帅气武警气质"
    with pytest.raises(hiagent.ProviderError):
        asyncio.run(hiagent.generate_image(
            prompt,
            call_meta={"asset_kind": "portrait", "character_name": "钟成"},
        ))

    assert len(calls) == 1
    assert calls[0]["payload"]["prompt"] == prompt


def test_arbitrary_image_provider_rejection_is_not_replayed(monkeypatch) -> None:
    attempts = 0

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    async def rejected(_client, _url, _payload, **_kwargs):
        nonlocal attempts
        attempts += 1
        raise hiagent.ProviderError(
            "任意未来供应商拒绝",
            raw='{"error":{"code":"AnyFutureProviderCode"}}',
            failure_kind="provider_rejected",
        )

    monkeypatch.setattr(hiagent.httpx, "AsyncClient", lambda **_kwargs: Client())
    monkeypatch.setattr(hiagent, "_post_json", rejected)
    monkeypatch.setattr(hiagent, "_model_connection", lambda *_args: ("https://provider", {"x": "y"}))
    monkeypatch.setattr(hiagent, "active_model", lambda *_args: "image-model")

    with pytest.raises(hiagent.ProviderError, match="任意未来供应商拒绝"):
        asyncio.run(hiagent.generate_image("普通角色立绘"))

    assert attempts == 1


def test_interrupted_provider_operation_links_to_successful_retry(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "provider-recovery.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()

    first = db.start_provider_call(
        "video_create", "model", request_json={"prompt": "same"},
        meta={"operation_id": "video-create-v1"},
    )
    # Simulate the next process startup marking the in-flight socket attempt.
    db.get_conn().execute(
        "UPDATE provider_calls SET status='INTERRUPTED', recovery_disposition='AWAITING_RETRY' WHERE id=?",
        (first,),
    )
    db.get_conn().commit()

    second = db.start_provider_call(
        "video_create", "model", request_json={"prompt": "same"},
        meta={"operation_id": "video-create-v1"},
    )
    db.finish_provider_call(second, "OK", 200, 25, response_json={"id": "provider-task"})

    old = db.get_conn().execute(
        "SELECT superseded_by_call_id, recovery_disposition FROM provider_calls WHERE id=?",
        (first,),
    ).fetchone()
    new = db.get_conn().execute(
        "SELECT operation_id, attempt_no, supersedes_call_id FROM provider_calls WHERE id=?",
        (second,),
    ).fetchone()
    assert dict(old) == {
        "superseded_by_call_id": second,
        "recovery_disposition": "RETRIED_SUCCESSFULLY",
    }
    assert dict(new) == {
        "operation_id": "video-create-v1", "attempt_no": 2, "supersedes_call_id": first,
    }


def test_late_response_from_old_process_cannot_overwrite_interrupted_call(
    tmp_path, monkeypatch,
) -> None:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "provider-fence.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()
    call_id = db.start_provider_call(
        "chat", "model", request_json={"prompt": "same"},
    )
    conn = db.get_conn()
    conn.execute(
        "UPDATE provider_calls SET status='INTERRUPTED', "
        "recovery_disposition='AWAITING_RETRY' WHERE id=?",
        (call_id,),
    )
    conn.commit()

    db.finish_provider_call(call_id, "OK", 200, 25, response_json={"late": True})

    row = conn.execute(
        "SELECT status, response_json, recovery_disposition FROM provider_calls WHERE id=?",
        (call_id,),
    ).fetchone()
    assert dict(row) == {
        "status": "INTERRUPTED", "response_json": None,
        "recovery_disposition": "AWAITING_RETRY",
    }


def test_successful_text_operation_can_be_reused_after_local_state_conflict(
    tmp_path, monkeypatch,
) -> None:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "provider-cache.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()
    payload = {
        "model": "text-model",
        "messages": [{"role": "user", "content": "same"}],
        "temperature": 0.7,
        "max_tokens": 1024,
    }
    operation_id = db.provider_operation_id("chat", "text-model", payload)
    call_id = db.start_provider_call(
        "chat",
        "text-model",
        meta={"operation_id": operation_id},
        request_json=payload,
    )
    db.finish_provider_call(
        call_id,
        "OK",
        200,
        25,
        response_json={"choices": [{"message": {"content": "cached result"}}]},
    )

    ignored_without_opt_in = hiagent._cached_successful_provider_response(
        "chat", "text-model", payload, {},
    )
    cached = hiagent._cached_successful_provider_response(
        "chat", "text-model", payload, {
            "reuse_successful_operation": True,
            "operation_id": operation_id,
        },
    )

    assert ignored_without_opt_in is None
    assert cached == {"choices": [{"message": {"content": "cached result"}}]}
    hit = db.get_conn().execute(
        "SELECT status,meta FROM provider_calls WHERE kind='provider_cache_hit' "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert hit["status"] == "REUSED"
    assert json.loads(hit["meta"])["source_provider_call_id"] == call_id


def test_success_cache_requires_matching_contract_version(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "provider-contract-cache.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()
    payload = {
        "model": "text-model",
        "messages": [{"role": "user", "content": "same"}],
        "temperature": 0.7,
        "max_tokens": 1024,
    }
    operation_id = db.provider_operation_id("chat", "text-model", payload)
    call_id = db.start_provider_call(
        "chat",
        "text-model",
        meta={
            "contract_version": "2.1.2",
            "operation_id": operation_id,
        },
        request_json=payload,
    )
    db.finish_provider_call(
        call_id,
        "OK",
        200,
        25,
        response_json={"choices": [{"message": {"content": "old contract result"}}]},
    )

    stale = hiagent._cached_successful_provider_response(
        "chat",
        "text-model",
        payload,
        {
            "reuse_successful_operation": True,
            "contract_version": "2.1.3",
            "operation_id": operation_id,
        },
    )
    matching = hiagent._cached_successful_provider_response(
        "chat",
        "text-model",
        payload,
        {
            "reuse_successful_operation": True,
            "contract_version": "2.1.2",
            "operation_id": operation_id,
        },
    )

    assert stale is None
    assert matching == {"choices": [{"message": {"content": "old contract result"}}]}


def test_provider_metadata_truncation_preserves_valid_json(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "provider-meta.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()

    call_id = db.start_provider_call(
        "chat",
        "text-model",
        meta={
            "stage": "screenplay",
            "generation_contract": "screenplay-generation-ir.v1.1",
            "published_output_contract": "EpisodeScreenplay@4.0.0",
            "prompt_version": "screenplay-compact-ir-5.1.0",
            "latest_errors": ["错误" * 500 for _ in range(20)],
        },
        request_json={"messages": []},
    )
    row = db.get_conn().execute(
        "SELECT meta FROM provider_calls WHERE id=?",
        (call_id,),
    ).fetchone()
    metadata = json.loads(row["meta"])

    assert metadata["_truncated"] is True
    assert metadata["_original_chars"] > 800
    assert metadata["_sha256"]
    assert metadata["generation_contract"] == "screenplay-generation-ir.v1.1"
    assert metadata["published_output_contract"] == "EpisodeScreenplay@4.0.0"
    assert metadata["prompt_version"] == "screenplay-compact-ir-5.1.0"


def test_semantic_attempt_id_separates_new_repair_from_crash_recovery(
    tmp_path, monkeypatch,
) -> None:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "provider-semantic-cache.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()
    payload = {
        "model": "text-model",
        "messages": [{"role": "user", "content": "byte-identical repair prompt"}],
        "temperature": 0.7,
        "max_tokens": 1024,
    }
    first_operation = "op_sem_attempt_one"
    call_id = db.start_provider_call(
        "chat", "text-model",
        meta={"operation_id": first_operation},
        request_json=payload,
    )
    db.finish_provider_call(
        call_id, "OK", 200, 20,
        response_json={"choices": [{"message": {"content": "first result"}}]},
    )

    recovered = hiagent._cached_successful_provider_response(
        "chat", "text-model", payload,
        {"reuse_successful_operation": True, "operation_id": first_operation},
    )
    fresh_attempt = hiagent._cached_successful_provider_response(
        "chat", "text-model", payload,
        {"reuse_successful_operation": True, "operation_id": "op_sem_attempt_two"},
    )

    assert recovered is not None
    assert fresh_attempt is None


def test_custom_text_provider_uses_opt_in_success_cache(monkeypatch) -> None:
    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    async def forbidden_post(*_args, **_kwargs):
        raise AssertionError("cached operation must not call provider again")

    monkeypatch.setattr(hiagent, "active_provider", lambda _kind: "custom:model-x")
    monkeypatch.setattr(hiagent, "active_model", lambda *_args: "text-model")
    monkeypatch.setattr(hiagent, "_model_connection", lambda *_args: ("https://provider", {}))
    monkeypatch.setattr(hiagent.httpx, "AsyncClient", lambda **_kwargs: Client())
    monkeypatch.setattr(hiagent, "_post_json", forbidden_post)
    monkeypatch.setattr(
        hiagent,
        "_cached_successful_provider_response",
        lambda *_args, **_kwargs: {
            "choices": [{"message": {"content": "cached screenplay"}}],
        },
    )

    result = asyncio.run(hiagent.chat(
        [{"role": "user", "content": "same"}],
        call_meta={"reuse_successful_operation": True},
    ))

    assert result == "cached screenplay"


def test_image_provider_uses_opt_in_success_cache(monkeypatch) -> None:
    async def forbidden_post(*_args, **_kwargs):
        raise AssertionError("cached image operation must not call provider again")

    monkeypatch.setattr(hiagent, "active_model", lambda *_args: "image-model")
    monkeypatch.setattr(hiagent, "_model_connection", lambda *_args: ("https://provider", {}))
    monkeypatch.setattr(hiagent, "_post_json", forbidden_post)
    monkeypatch.setattr(
        hiagent,
        "_cached_successful_provider_response",
        lambda *_args, **_kwargs: {"data": [{"url": "https://cdn.example/portrait.jpg"}]},
    )

    result = asyncio.run(hiagent.generate_image(
        "portrait prompt",
        call_meta={
            "reuse_successful_operation": True,
            "operation_id": "op_portrait_batch_character",
        },
    ))

    assert result["url"] == "https://cdn.example/portrait.jpg"


def test_video_create_sends_stable_idempotency_key(monkeypatch) -> None:
    seen_headers: list[dict[str, str]] = []

    class Response:
        status_code = 200
        text = '{"id":"task-1"}'

        def json(self):
            return {"id": "task-1"}

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, _url, *, json, headers):
            seen_headers.append(headers)
            return Response()

    monkeypatch.setattr(hiagent.httpx, "AsyncClient", lambda **_kwargs: Client())
    monkeypatch.setattr(hiagent, "active_provider", lambda _kind: "hiagent")
    monkeypatch.setattr(hiagent, "_model_connection", lambda *_args: ("https://provider", {"x": "y"}))
    monkeypatch.setattr(hiagent, "active_model", lambda *_args: "video-model")
    monkeypatch.setattr(hiagent, "start_provider_call", lambda *_args, **_kwargs: 1)
    monkeypatch.setattr(hiagent, "finish_provider_call", lambda *_args, **_kwargs: None)

    task_id = asyncio.run(hiagent.create_video_task(
        "prompt", call_meta={"version_id": "ver_1"},
    ))
    retry_task_id = asyncio.run(hiagent.create_video_task(
        "prompt",
        call_meta={
            "version_id": "ver_1",
            "operation_id": "video-create-ver_1-safety-1",
        },
    ))

    assert task_id == "task-1"
    assert retry_task_id == "task-1"
    assert seen_headers[0]["Idempotency-Key"] == "video-create-ver_1"
    assert seen_headers[1]["Idempotency-Key"] == "video-create-ver_1-safety-1"


def test_video_create_persists_exact_request_and_rejects_operation_drift(
    tmp_path, monkeypatch,
) -> None:
    posts: list[dict] = []

    class Response:
        status_code = 200
        text = '{"id":"task-1"}'

        def json(self):
            return {"id": "task-1"}

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, _url, *, json, headers):
            posts.append(json)
            return Response()

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "seedance-request.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()
    monkeypatch.setattr(hiagent.httpx, "AsyncClient", lambda **_kwargs: Client())
    monkeypatch.setattr(hiagent, "active_provider", lambda _kind: "hiagent")
    monkeypatch.setattr(
        hiagent,
        "_model_connection",
        lambda *_args: ("https://provider", {"x": "y"}),
    )
    monkeypatch.setattr(hiagent, "active_model", lambda *_args: "video-model")

    operation_id = "video-create-ver-exact"
    data_url = "data:image/png;base64,ZXhhY3QtYnl0ZXM="
    assert asyncio.run(hiagent.create_video_task(
        "prompt-one",
        image_urls=[(data_url, "reference_image")],
        call_meta={"operation_id": operation_id},
    )) == "task-1"

    saved = db.latest_provider_request_json(
        "video_create", "video-model", operation_id,
    )
    assert saved["content"][1]["image_url"]["url"] == data_url

    with pytest.raises(
        hiagent.ProviderError,
        match="同一业务操作的请求内容发生变化",
    ) as caught:
        asyncio.run(hiagent.create_video_task(
            "prompt-two",
            image_urls=[(data_url, "reference_image")],
            call_meta={"operation_id": operation_id},
        ))

    assert caught.value.failure_kind == "idempotency_request_mismatch"
    assert caught.value.requires_explicit_retry is True

    monkeypatch.setattr(hiagent, "active_model", lambda *_args: "video-model-new")
    with pytest.raises(
        hiagent.ProviderError,
        match="同一业务操作的请求内容发生变化",
    ):
        asyncio.run(hiagent.create_video_task(
            "prompt-one",
            image_urls=[(data_url, "reference_image")],
            call_meta={"operation_id": operation_id},
        ))

    assert len(posts) == 1


def test_image_generation_sends_stable_idempotency_key(monkeypatch) -> None:
    seen_headers: list[dict[str, str]] = []

    class Response:
        status_code = 200
        text = '{"data":[{"url":"https://provider/image.jpg"}]}'

        def json(self):
            return {"data": [{"url": "https://provider/image.jpg"}]}

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, _url, *, json, headers):
            seen_headers.append(headers)
            return Response()

    monkeypatch.setattr(hiagent.httpx, "AsyncClient", lambda **_kwargs: Client())
    monkeypatch.setattr(hiagent, "_model_connection", lambda *_args: ("https://provider", {"x": "y"}))
    monkeypatch.setattr(hiagent, "active_model", lambda *_args: "image-model")
    monkeypatch.setattr(hiagent, "start_provider_call", lambda *_args, **_kwargs: 1)
    monkeypatch.setattr(hiagent, "finish_provider_call", lambda *_args, **_kwargs: None)

    first = asyncio.run(hiagent.generate_image("same prompt", call_meta={"shot_no": 2}))
    second = asyncio.run(hiagent.generate_image("same prompt", call_meta={"shot_no": 2}))

    assert first == second
    assert seen_headers[0]["Idempotency-Key"] == seen_headers[1]["Idempotency-Key"]
    assert seen_headers[0]["Idempotency-Key"].startswith("op_")


def test_video_poll_network_error_is_retryable(monkeypatch) -> None:
    calls: list[tuple] = []

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, url, *, headers):
            request = httpx.Request("GET", url, headers=headers)
            raise httpx.ConnectError("temporary TLS failure", request=request)

    monkeypatch.setattr(hiagent.httpx, "AsyncClient", lambda **_kwargs: Client())
    monkeypatch.setattr(
        hiagent, "_model_connection",
        lambda *_args: ("https://provider.invalid", {"x": "y"}),
    )
    monkeypatch.setattr(hiagent, "active_model", lambda *_args: "video-model")
    monkeypatch.setattr(
        hiagent, "log_provider_call",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    try:
        asyncio.run(hiagent.poll_video_task("task-1"))
    except hiagent.ProviderError as exc:
        assert exc.retryable is True
        assert "ConnectError" in str(exc)
    else:
        raise AssertionError("expected ProviderError")

    assert calls[0][0][:3] == ("video_poll", "video-model", "FAILED")


def _install_video_poll_response(monkeypatch, response) -> None:
    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, _url, *, headers):
            return response

    monkeypatch.setattr(hiagent.httpx, "AsyncClient", lambda **_kwargs: Client())
    monkeypatch.setattr(
        hiagent, "_model_connection",
        lambda *_args: ("https://provider.invalid", {"x": "y"}),
    )
    monkeypatch.setattr(hiagent, "active_model", lambda *_args: "video-model")
    monkeypatch.setattr(hiagent, "log_provider_call", lambda *_args, **_kwargs: None)


def test_video_poll_non_200_preserves_structured_model_rejection(monkeypatch) -> None:
    payload = {
        "error": {
            "message": "request rejected",
            "failure": {
                "category": "model_rejection",
                "kind": "provider_rejected",
                "retryable": True,
            },
        },
    }

    class Response:
        status_code = 422
        text = json.dumps(payload)

        def json(self):
            return payload

    _install_video_poll_response(monkeypatch, Response())

    with pytest.raises(hiagent.ProviderError) as caught:
        asyncio.run(hiagent.poll_video_task("task-1"))

    assert caught.value.failure.to_payload() == {
        "category": "model_rejection",
        "kind": "provider_rejected",
        "disposition": "external_terminal",
        "retryable": False,
    }


def test_video_poll_non_200_preserves_structured_technical_failure(monkeypatch) -> None:
    payload = {
        "error": {
            "message": "provider execution unavailable",
            "failure": {
                "category": "technical",
                "kind": "provider_execution_failed",
                "retryable": True,
            },
        },
    }

    class Response:
        status_code = 409
        text = json.dumps(payload)

        def json(self):
            return payload

    _install_video_poll_response(monkeypatch, Response())

    with pytest.raises(hiagent.ProviderError) as caught:
        asyncio.run(hiagent.poll_video_task("task-1"))

    assert caught.value.failure.to_payload() == {
        "category": "technical",
        "kind": "provider_execution_failed",
        "disposition": "automatic_retry",
        "retryable": True,
    }


def test_video_poll_malformed_response_is_typed_technical_failure(monkeypatch) -> None:
    class Response:
        status_code = 200
        text = "<html>temporary gateway response</html>"

        def json(self):
            raise ValueError("invalid JSON")

    _install_video_poll_response(monkeypatch, Response())

    with pytest.raises(hiagent.ProviderError) as caught:
        asyncio.run(hiagent.poll_video_task("task-1"))

    assert caught.value.failure.to_payload() == {
        "category": "technical",
        "kind": "malformed_response",
        "disposition": "automatic_retry",
        "retryable": True,
    }


@pytest.mark.parametrize(
    ("error_payload", "expected_failure"),
    [
        pytest.param(
            {"message": "provider worker exited"},
            {
                "category": "technical",
                "kind": "provider_execution_failed",
                "disposition": "manual_review",
                "retryable": False,
            },
            id="generic-provider-failure",
        ),
        pytest.param(
            {
                "message": "provider explicitly rejected the input",
                "failure": {
                    "category": "model_rejection",
                    "kind": "provider_rejected",
                    "retryable": False,
                },
            },
            {
                "category": "model_rejection",
                "kind": "provider_rejected",
                "disposition": "external_terminal",
                "retryable": False,
            },
            id="explicit-model-rejection",
        ),
    ],
)
def test_video_poll_uses_structured_failure_evidence_only(
    monkeypatch,
    error_payload: dict,
    expected_failure: dict,
) -> None:
    class Response:
        status_code = 200
        text = "provider response"

        def json(self):
            return {"status": "failed", "error": error_payload}

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, _url, *, headers):
            return Response()

    monkeypatch.setattr(hiagent.httpx, "AsyncClient", lambda **_kwargs: Client())
    monkeypatch.setattr(
        hiagent, "_model_connection",
        lambda *_args: ("https://provider.invalid", {"x": "y"}),
    )
    monkeypatch.setattr(hiagent, "active_model", lambda *_args: "video-model")
    monkeypatch.setattr(hiagent, "log_provider_call", lambda *_args, **_kwargs: None)

    result = asyncio.run(hiagent.poll_video_task("task-1"))

    assert result["status"] == "failed"
    assert result["failure"] == expected_failure


def test_media_download_retries_transient_connect_timeout(monkeypatch) -> None:
    calls = 0

    async def fake_download_once(url: str, dest_path: str) -> None:
        nonlocal calls
        calls += 1
        if calls < 3:
            request = httpx.Request("GET", url)
            raise httpx.ConnectTimeout("temporary CDN connect timeout", request=request)

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(hiagent, "_download_once", fake_download_once)
    monkeypatch.setattr(hiagent.asyncio, "sleep", no_sleep)

    asyncio.run(hiagent.download("https://cdn.invalid/video.mp4", "unused.mp4"))

    assert calls == 3


def test_prepare_image_data_urls_records_compression_stats(monkeypatch) -> None:
    raw = b"large-image-payload"
    monkeypatch.setattr(hiagent, "_compress_image_bytes", lambda value: b"small" if value == raw else value)

    prepared, stats = asyncio.run(hiagent._prepare_image_data_urls([
        "data:image/png;base64," + base64.b64encode(raw).decode("ascii"),
    ]))

    assert prepared == ["data:image/jpeg;base64," + base64.b64encode(b"small").decode("ascii")]
    assert stats["media_input_bytes_original"] == len(raw)
    assert stats["media_input_bytes_sent"] == len(b"small")
    assert stats["media_input_compressed_count"] == 1


def test_seeded_image_write_timeout_never_drops_identity_seed(monkeypatch) -> None:
    seen_inputs: list[list[str] | None] = []

    async def fake_generate_image(prompt, *, size, image_inputs=None, call_meta=None):
        seen_inputs.append(image_inputs)
        raise hiagent.ProviderError(
            "上传超时",
            retryable=True,
            timeout_phase="write",
        )

    monkeypatch.setattr(hiagent, "generate_image", fake_generate_image)

    with pytest.raises(hiagent.ProviderError, match="上传超时"):
        asyncio.run(video_modes._generate_image_with_seed_fallback(
            "prompt",
            ["data:image/jpeg;base64,abc"],
            call_meta={"shot_no": 1},
        ))

    assert seen_inputs == [["data:image/jpeg;base64,abc"]]


def test_seeded_image_transient_provider_error_never_drops_identity_seed(monkeypatch) -> None:
    seen_inputs: list[list[str] | None] = []

    async def fake_generate_image(prompt, *, size, image_inputs=None, call_meta=None):
        seen_inputs.append(image_inputs)
        raise hiagent.ProviderError("HTTP 429 rate limited", retryable=True, raw="status=429")

    monkeypatch.setattr(hiagent, "generate_image", fake_generate_image)

    with pytest.raises(hiagent.ProviderError, match="429"):
        asyncio.run(video_modes._generate_image_with_seed_fallback(
            "prompt", ["data:image/jpeg;base64,identity"], call_meta={"shot_no": 1},
        ))

    assert seen_inputs == [["data:image/jpeg;base64,identity"]]


def test_multiview_seed_rejection_never_retries_without_identity_seed(monkeypatch) -> None:
    from app import multiview

    seen_inputs: list[list[str] | None] = []

    async def fake_generate_image(prompt, *, size, image_inputs=None, call_meta=None):
        seen_inputs.append(image_inputs)
        raise hiagent.ProviderError("InputImageSensitiveContentDetected", raw="HTTP 400")

    monkeypatch.setattr(hiagent, "generate_image", fake_generate_image)

    with pytest.raises(hiagent.ProviderError, match="InputImageSensitiveContentDetected"):
        asyncio.run(multiview._generate_image(
            "prompt",
            seed_inputs=["data:image/jpeg;base64,identity"],
            call_meta={"asset_kind": "character_view"},
        ))

    assert seen_inputs == [["data:image/jpeg;base64,identity"]]


def test_jobs_overview_includes_running_screenplay(monkeypatch) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE jobs(
            id TEXT, kind TEXT, shot_id TEXT, version_id TEXT, episode_id TEXT,
            project_id TEXT, status TEXT, error TEXT, created_at REAL, updated_at REAL,
            after_shot_id TEXT, after_version_id TEXT, scene_kinds TEXT, run_id TEXT
        );
        CREATE TABLE shots(id TEXT, episode_id TEXT, shot_no INTEGER);
        CREATE TABLE projects(id TEXT, name TEXT);
        CREATE TABLE episodes(
            id TEXT, project_id TEXT, episode_no INTEGER, title TEXT,
            screenplay_status TEXT, screenplay_error TEXT, screenplay_started_at REAL,
            screenplay_updated_at REAL, created_at REAL
        );
        CREATE TABLE workflow_runs(
            id TEXT, workflow_type TEXT, scope_type TEXT, scope_id TEXT,
            status TEXT, current_step_key TEXT, updated_at REAL,
            failure_message TEXT, recovered_by_run_id TEXT
        );
        INSERT INTO projects VALUES('p1', '测试项目');
        INSERT INTO episodes VALUES('e1', 'p1', 1, '第一集', 'running', NULL, 100, 120, 10);
    """)
    monkeypatch.setattr(system_api, "get_conn", lambda: conn)

    result = system_api.jobs_overview()

    assert result["recent"][0]["kind"] == "screenplay"
    assert result["recent"][0]["status"] == "running"
    assert result["recent"][0]["episode_no"] == 1
    assert result["counts"]["running"] == 1


def test_jobs_overview_includes_harness_runs_and_deduplicates_linked_work(monkeypatch) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE jobs(
            id TEXT, kind TEXT, shot_id TEXT, version_id TEXT, episode_id TEXT,
            project_id TEXT, status TEXT, error TEXT, created_at REAL, updated_at REAL,
            run_id TEXT
        );
        CREATE TABLE shots(id TEXT, episode_id TEXT, shot_no INTEGER);
        CREATE TABLE projects(id TEXT, name TEXT);
        CREATE TABLE episodes(
            id TEXT, project_id TEXT, episode_no INTEGER, title TEXT,
            screenplay_status TEXT, screenplay_error TEXT, screenplay_started_at REAL,
            screenplay_updated_at REAL, created_at REAL
        );
        CREATE TABLE workflow_runs(
            id TEXT, workflow_type TEXT, scope_type TEXT, scope_id TEXT,
            status TEXT, current_step_key TEXT, updated_at REAL,
            failure_message TEXT, recovered_by_run_id TEXT
        );
        INSERT INTO projects VALUES('p1', 'Project');
        INSERT INTO episodes VALUES('e1', 'p1', 1, 'Episode', 'ready', NULL, 100, 120, 10);
        INSERT INTO workflow_runs VALUES(
            'run_refs', 'character_references', 'project', 'p1',
            'RUNNING', 'character_references', 200, NULL, NULL
        );
        INSERT INTO workflow_runs VALUES(
            'run_script', 'screenplay', 'episode', 'e1',
            'SUCCEEDED', 'screenplay', 190, NULL, NULL
        );
        INSERT INTO jobs(
            id, kind, episode_id, project_id, status, created_at, updated_at, run_id
        ) VALUES('job_linked', 'video', 'e1', 'p1', 'running', 180, 180, 'run_refs');
    """)
    monkeypatch.setattr(system_api, "get_conn", lambda: conn)

    result = system_api.jobs_overview()

    assert result["counts"] == {"running": 1, "succeeded": 1}
    assert [row["id"] for row in result["recent"]] == ["run_refs", "run_script"]
    assert result["recent"][0]["kind"] == "character_references"
    assert result["recent"][0]["project_name"] == "Project"
    assert all(row["id"] not in {"job_linked", "screenplay_e1"} for row in result["recent"])


def test_provider_stream_progress_persists_heartbeat(
    tmp_path, monkeypatch,
) -> None:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "provider-progress.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()
    call_id = db.start_provider_call(
        "chat",
        "text-model",
        meta={"stage": "screenplay"},
        request_json={"messages": []},
    )

    db.update_provider_call_progress(
        call_id,
        received_chars=8192,
        chunk_at=1234.5,
    )
    row = db.get_conn().execute(
        "SELECT first_chunk_at,last_chunk_at,received_chars "
        "FROM provider_calls WHERE id=?",
        (call_id,),
    ).fetchone()

    assert row["first_chunk_at"] == 1234.5
    assert row["last_chunk_at"] == 1234.5
    assert row["received_chars"] == 8192
