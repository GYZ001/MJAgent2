from __future__ import annotations

import asyncio

import pytest

from app import config, db, hiagent
from app.evidence import repository
from app.harness import model_gateway
from app.orchestration.engine import WorkflowRecorder


def _fresh_database(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "text-retry.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()
    return db.get_conn()


def test_not_sent_text_failure_waits_and_replays_same_harness_step(tmp_path, monkeypatch) -> None:
    _fresh_database(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "TEXT_PROVIDER_MAX_RETRIES", 3)
    monkeypatch.setattr(config, "TEXT_PROVIDER_RETRY_BASE_DELAY", 30.0)
    attempts: list[list[dict[str, str]]] = []
    waits: list[float] = []
    states_while_waiting: list[str] = []
    reasons_while_waiting: list[str | None] = []

    recorder = WorkflowRecorder.create(
        workflow_type="storyboard",
        scope_type="episode",
        scope_id="e1",
        input_fingerprint="same-request",
    )
    recorder.start()
    original_started_at = repository.get_run(recorder.run_id)["started_at"]

    async def fake_chat(messages, **_kwargs):
        attempts.append(messages)
        if len(attempts) < 3:
            raise hiagent.ProviderError(
                "connect failed before delivery",
                retryable=True,
                failure_kind="connection_failed",
                delivery_state="not_sent",
                replay_safe=True,
            )
        return '{"ok":true}'

    async def fake_sleep(delay: float) -> None:
        waits.append(delay)
        run = repository.get_run(recorder.run_id)
        states_while_waiting.append(run["status"])
        reasons_while_waiting.append(run["failure_message"])

    monkeypatch.setattr(model_gateway.hiagent, "chat", fake_chat)
    monkeypatch.setattr(model_gateway.asyncio, "sleep", fake_sleep)
    messages = [{"role": "user", "content": "shot 14"}]

    async def operation() -> str:
        return await model_gateway.chat(
            messages,
            call_meta={"stage_key": "storyboard", "call_role": "stage_repair"},
        )

    _, result = asyncio.run(
        recorder.step("storyboard", operation, contract_key="storyboard", agent_name="storyboard")
    )
    resumed_started_at = repository.get_run(recorder.run_id)["started_at"]
    recorder.succeed()

    assert result == '{"ok":true}'
    assert attempts == [messages, messages, messages]
    assert waits == [30.0, 60.0]
    assert states_while_waiting == ["WAITING_RETRY", "WAITING_RETRY"]
    assert all(reason and "自动执行" in reason for reason in reasons_while_waiting)
    assert resumed_started_at == original_started_at
    events = repository.get_events(recorder.run_id, limit=100)
    assert [event["event_type"] for event in events].count("PROVIDER_RETRY_SCHEDULED") == 2
    assert [event["event_type"] for event in events].count("PROVIDER_RETRY_RESUMED") == 2
    scheduled = next(event for event in events if event["event_type"] == "PROVIDER_RETRY_SCHEDULED")
    assert scheduled["payload"]["stage_key"] == "storyboard"
    assert scheduled["payload"]["call_role"] == "stage_repair"


def test_not_sent_text_retry_budget_is_bounded(monkeypatch) -> None:
    monkeypatch.setattr(config, "TEXT_PROVIDER_MAX_RETRIES", 2)
    monkeypatch.setattr(config, "TEXT_PROVIDER_RETRY_BASE_DELAY", 0.0)
    attempts = 0

    async def always_not_sent(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        raise hiagent.ProviderError(
            "connect failed",
            retryable=True,
            failure_kind="connection_failed",
            delivery_state="not_sent",
            replay_safe=True,
        )

    async def no_wait(_delay: float) -> None:
        return None

    monkeypatch.setattr(model_gateway.hiagent, "chat", always_not_sent)
    monkeypatch.setattr(model_gateway.asyncio, "sleep", no_wait)

    with pytest.raises(hiagent.ProviderError, match="connect failed"):
        asyncio.run(model_gateway.chat([{"role": "user", "content": "x"}]))
    assert attempts == 3  # one initial call plus two configured retries


def test_ambiguous_text_result_is_not_replayed_and_requires_page_retry(
    tmp_path, monkeypatch,
) -> None:
    _fresh_database(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "TEXT_PROVIDER_MAX_RETRIES", 3)
    attempts = 0
    sleeps = 0
    recorder = WorkflowRecorder.create(
        workflow_type="storyboard",
        scope_type="episode",
        scope_id="e1",
        input_fingerprint="ambiguous-read-timeout",
    )
    recorder.start()

    async def read_timeout_after_send(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        raise hiagent.ProviderError(
            "read timeout after request delivery",
            retryable=True,
            failure_kind="request_outcome_unknown",
            delivery_state="unknown",
            requires_explicit_retry=True,
        )

    async def forbidden_sleep(_delay: float) -> None:
        nonlocal sleeps
        sleeps += 1

    monkeypatch.setattr(model_gateway.hiagent, "chat", read_timeout_after_send)
    monkeypatch.setattr(model_gateway.asyncio, "sleep", forbidden_sleep)

    async def operation() -> str:
        return await model_gateway.chat(
            [{"role": "user", "content": "same paid generation"}],
            call_meta={"stage_key": "storyboard", "call_role": "stage_generate"},
        )

    with pytest.raises(hiagent.ProviderError) as caught:
        asyncio.run(recorder.step("storyboard", operation))
    recorder.fail(caught.value)

    assert attempts == 1
    assert sleeps == 0
    assert repository.get_run(recorder.run_id)["status"] == "FAILED"
    events = repository.get_events(recorder.run_id, limit=100)
    interrupted = [
        event for event in events
        if event["event_type"] == "PROVIDER_RESULT_INTERRUPTED"
    ]
    assert len(interrupted) == 1
    assert interrupted[0]["payload"] == {
        "delivery_state": "unknown",
        "failure_kind": "request_outcome_unknown",
        "requires_explicit_retry": True,
        "stage_key": "storyboard",
        "call_role": "stage_generate",
    }
    assert not any(
        event["event_type"] == "PROVIDER_RETRY_SCHEDULED"
        for event in events
    )


def test_retryable_http_response_is_not_treated_as_not_sent(monkeypatch) -> None:
    monkeypatch.setattr(config, "TEXT_PROVIDER_MAX_RETRIES", 3)
    attempts = 0

    async def rate_limited(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        raise hiagent.ProviderError(
            "HTTP 429",
            retryable=True,
            failure_kind="rate_limited",
            delivery_state="responded",
        )

    monkeypatch.setattr(model_gateway.hiagent, "chat", rate_limited)

    with pytest.raises(hiagent.ProviderError, match="429"):
        asyncio.run(model_gateway.chat([{"role": "user", "content": "x"}]))
    assert attempts == 1


def test_shared_auto_run_records_retry_without_pausing_siblings(tmp_path, monkeypatch) -> None:
    _fresh_database(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "TEXT_PROVIDER_MAX_RETRIES", 1)
    monkeypatch.setattr(config, "TEXT_PROVIDER_RETRY_BASE_DELAY", 0.0)
    attempts = 0
    states_while_waiting: list[str] = []

    recorder = WorkflowRecorder.create(
        workflow_type="storyboard",
        scope_type="project",
        scope_id="p1",
        input_fingerprint="concurrent-episodes",
    )
    recorder.start()

    async def once_not_sent(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise hiagent.ProviderError(
                "connect failed before delivery",
                retryable=True,
                failure_kind="connection_failed",
                delivery_state="not_sent",
                replay_safe=True,
            )
        return "ok"

    async def inspect_shared_run(_delay: float) -> None:
        states_while_waiting.append(repository.get_run(recorder.run_id)["status"])

    monkeypatch.setattr(model_gateway.hiagent, "chat", once_not_sent)
    monkeypatch.setattr(model_gateway.asyncio, "sleep", inspect_shared_run)

    async def operation() -> str:
        return await model_gateway.chat(
            [{"role": "user", "content": "episode child"}],
            call_meta={"stage_key": "storyboard"},
        )

    _, result = asyncio.run(recorder.step("episode_pipelines", operation))
    recorder.succeed()

    assert result == "ok"
    assert states_while_waiting == ["RUNNING"]
    assert any(
        event["event_type"] == "PROVIDER_RETRY_SCHEDULED"
        for event in repository.get_events(recorder.run_id, limit=100)
    )


def test_zero_byte_stream_read_timeout_does_not_schedule_retry(tmp_path, monkeypatch) -> None:
    """A zero-byte read timeout is outcome-unknown and must not auto-replay."""
    _fresh_database(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "TEXT_PROVIDER_MAX_RETRIES", 3)
    monkeypatch.setattr(config, "TEXT_PROVIDER_RETRY_BASE_DELAY", 0.0)
    attempts = 0
    sleeps = 0

    recorder = WorkflowRecorder.create(
        workflow_type="storyboard",
        scope_type="episode",
        scope_id="e1",
        input_fingerprint="zero-byte-read-timeout",
    )
    recorder.start()

    async def zero_byte_read_timeout(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        raise hiagent.ProviderError(
            "流式调用read阶段超时（303274ms）；请求结果不确定，已禁止自动重试",
            retryable=True,
            failure_kind="request_outcome_unknown",
            delivery_state="unknown",
            replay_safe=False,
            requires_explicit_retry=True,
            received_chars=0,
        )

    async def forbidden_sleep(_delay: float) -> None:
        nonlocal sleeps
        sleeps += 1

    monkeypatch.setattr(model_gateway.hiagent, "chat", zero_byte_read_timeout)
    monkeypatch.setattr(model_gateway.asyncio, "sleep", forbidden_sleep)

    async def operation() -> str:
        return await model_gateway.chat(
            [{"role": "user", "content": "same one-shot generation"}],
            call_meta={"stage_key": "storyboard", "call_role": "stage_generate"},
        )

    with pytest.raises(hiagent.ProviderError) as caught:
        asyncio.run(recorder.step("storyboard", operation))
    recorder.fail(caught.value)

    assert attempts == 1
    assert sleeps == 0
    assert caught.value.requires_explicit_retry is True
    events = repository.get_events(recorder.run_id, limit=100)
    assert not any(
        event["event_type"] == "PROVIDER_RETRY_SCHEDULED" for event in events
    )


def test_partial_byte_stream_read_timeout_does_not_schedule_retry(tmp_path, monkeypatch) -> None:
    """End-to-end: a partial-byte streaming read timeout stays not-replay-safe,
    so model_gateway.chat must NOT schedule an automatic retry."""
    _fresh_database(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "TEXT_PROVIDER_MAX_RETRIES", 3)
    monkeypatch.setattr(config, "TEXT_PROVIDER_RETRY_BASE_DELAY", 0.0)
    attempts = 0
    sleeps = 0

    recorder = WorkflowRecorder.create(
        workflow_type="storyboard",
        scope_type="episode",
        scope_id="e1",
        input_fingerprint="partial-byte-read-timeout",
    )
    recorder.start()

    async def partial_byte_read_timeout(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        raise hiagent.ProviderError(
            "流式调用read阶段超时（303274ms）；请求结果不确定，已禁止自动重试，请在页面确认后重试",
            retryable=True,
            failure_kind="request_outcome_unknown",
            delivery_state="unknown",
            requires_explicit_retry=True,
            received_chars=123,
        )

    async def forbidden_sleep(_delay: float) -> None:
        nonlocal sleeps
        sleeps += 1

    monkeypatch.setattr(model_gateway.hiagent, "chat", partial_byte_read_timeout)
    monkeypatch.setattr(model_gateway.asyncio, "sleep", forbidden_sleep)

    async def operation() -> str:
        return await model_gateway.chat(
            [{"role": "user", "content": "same paid generation"}],
            call_meta={"stage_key": "storyboard", "call_role": "stage_generate"},
        )

    with pytest.raises(hiagent.ProviderError) as caught:
        asyncio.run(recorder.step("storyboard", operation))
    recorder.fail(caught.value)

    assert attempts == 1
    assert sleeps == 0
    events = repository.get_events(recorder.run_id, limit=100)
    assert not any(
        event["event_type"] == "PROVIDER_RETRY_SCHEDULED" for event in events
    )
