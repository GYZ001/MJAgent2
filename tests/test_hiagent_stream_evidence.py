"""ERR-20260831-4c9132 regression: content-review refusals must not be told
apart from genuine SSE interruptions by keyword-matching the refusal text.

Real incident: HiAgent rejected "我欲封天" chapter 1616 for content-policy
reasons. The refusal ("抱歉，该问题不符合安全合规要求，暂时无法回答", a fixed
22 characters) arrived as an ordinary ``delta.content`` chunk and the
connection then closed without ``[DONE]`` -- indistinguishable, under the old
code, from a genuine transport interruption. The old code discarded
``state`` (where a terminal ``finish_reason`` would have been captured)
before building evidence, and reported "结果不确定，可点击重试；...再决定是否
调整「修复重试上限」" -- a guaranteed-to-fail retry with an irrelevant lever,
for an outcome that was actually already final.

``app.harness.hiagent_stream_evidence.classify_interrupted_stream`` fixes the
classification with two structural signals (CLAUDE.md「禁止黑白名单与枚举
穷举」-- neither looks at what the refusal text says):

1. ``finish_reason == "content_filter"``, the provider's own terminal signal.
2. Failing that, >=2 attempts of the same business operation each ending
   INTERRUPTED with byte-identical, non-empty evidence -- verified against
   170 real production rows sharing this exact incident's shape (same
   operation_id, escalating attempt_no, identical 22-character content, up
   to 6 attempts; a random transport cut does not reproduce like that).

This file covers the pure judgment function, its DB-query glue, the
end-to-end ``_stream_chat_completion`` behavior for both outcomes, and that
``app/errors.py`` actually lets the quoted provider text reach the user
instead of swallowing it into the generic "可稍后重试" hint.
"""
from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest

from app import db as db_mod
from app import errors as errors_mod
from app import hiagent
from app.harness import hiagent_stream_evidence

REFUSAL_TEXT = "抱歉，该问题不符合安全合规要求，暂时无法回答"


def _conn():
    conn = db_mod.sqlite3.connect(":memory:")
    conn.row_factory = db_mod.sqlite3.Row
    conn.executescript(db_mod.SCHEMA)
    for stmt in db_mod.MIGRATIONS:
        try:
            conn.execute(stmt)
        except db_mod.sqlite3.OperationalError:
            pass
    return conn


def _insert_interrupted_call(conn, *, operation_id: str, received_chars: int, summary: str | None) -> int:
    response_json = (
        json.dumps({"interrupted_stream": {"summary": summary}}, ensure_ascii=False)
        if summary is not None else None
    )
    cur = conn.execute(
        """INSERT INTO provider_calls(
               ts, kind, model, status, http_status, latency_ms,
               operation_id, attempt_no, received_chars, response_json
           ) VALUES(?, 'chat', 'm', 'INTERRUPTED', 200, 100, ?, 1, ?, ?)""",
        (time.time(), operation_id, received_chars, response_json),
    )
    conn.commit()
    return int(cur.lastrowid)


# ---------------------------------------------------------------------------
# classify_interrupted_stream -- pure judgment + its DB-query glue
# ---------------------------------------------------------------------------


def test_content_filter_finish_reason_is_immediately_deterministic():
    """Signal 1 needs no history at all -- it fires on a call_id that has no
    matching provider_calls row, proving the DB is never even queried."""
    conn = _conn()
    assert hiagent_stream_evidence.classify_interrupted_stream(
        conn, 999999, "content_filter", 300,
    ) is True


def test_single_attempt_without_finish_reason_stays_uncertain():
    conn = _conn()
    call_id = _insert_interrupted_call(
        conn, operation_id="op-single", received_chars=22, summary=REFUSAL_TEXT,
    )
    assert hiagent_stream_evidence.classify_interrupted_stream(
        conn, call_id, None, 300,
    ) is False


def test_repeated_byte_identical_short_content_is_deterministic():
    """Reproduces the real shape: same operation_id, second attempt matches
    the first byte-for-byte."""
    conn = _conn()
    _insert_interrupted_call(
        conn, operation_id="op-repeat", received_chars=22, summary=REFUSAL_TEXT,
    )
    second_call_id = _insert_interrupted_call(
        conn, operation_id="op-repeat", received_chars=22, summary=REFUSAL_TEXT,
    )
    assert hiagent_stream_evidence.classify_interrupted_stream(
        conn, second_call_id, None, 300,
    ) is True


def test_different_content_across_attempts_stays_uncertain():
    """A random transport cut does not reproduce the same content twice."""
    conn = _conn()
    _insert_interrupted_call(
        conn, operation_id="op-diff", received_chars=10, summary="部分回复一",
    )
    second_call_id = _insert_interrupted_call(
        conn, operation_id="op-diff", received_chars=12, summary="部分回复二",
    )
    assert hiagent_stream_evidence.classify_interrupted_stream(
        conn, second_call_id, None, 300,
    ) is False


def test_capped_rows_are_excluded_even_when_coincidentally_identical():
    """A long, genuinely-truncated stream cannot prove its full output was
    short just because two attempts happened to share the same first 300
    characters -- max_signature_chars guards exactly this false-positive."""
    conn = _conn()
    capped_prefix = "x" * 300
    _insert_interrupted_call(
        conn, operation_id="op-capped", received_chars=50000, summary=capped_prefix,
    )
    second_call_id = _insert_interrupted_call(
        conn, operation_id="op-capped", received_chars=50000, summary=capped_prefix,
    )
    assert hiagent_stream_evidence.classify_interrupted_stream(
        conn, second_call_id, None, 300,
    ) is False


def test_empty_summary_never_counts_as_a_match():
    conn = _conn()
    _insert_interrupted_call(conn, operation_id="op-empty", received_chars=0, summary="")
    second_call_id = _insert_interrupted_call(
        conn, operation_id="op-empty", received_chars=0, summary="",
    )
    assert hiagent_stream_evidence.classify_interrupted_stream(
        conn, second_call_id, None, 300,
    ) is False


def test_missing_operation_id_stays_uncertain():
    conn = _conn()
    call_id = _insert_interrupted_call(
        conn, operation_id="", received_chars=22, summary=REFUSAL_TEXT,
    )
    assert hiagent_stream_evidence.classify_interrupted_stream(
        conn, call_id, None, 300,
    ) is False


# ---------------------------------------------------------------------------
# interrupted_stream_evidence -- observability: finish_reason must survive
# ---------------------------------------------------------------------------


def test_evidence_carries_finish_reason_through_from_state():
    evidence = hiagent_stream_evidence.interrupted_stream_evidence(
        content_parts=[REFUSAL_TEXT],
        reasoning_parts=[],
        unconsumed_frames=[],
        state={"finish_reason": "content_filter"},
    )
    assert evidence["finish_reason"] == "content_filter"
    assert evidence["summary"] == REFUSAL_TEXT


def test_evidence_finish_reason_is_none_when_state_has_none():
    evidence = hiagent_stream_evidence.interrupted_stream_evidence(
        content_parts=["部分回复"],
        reasoning_parts=[],
        unconsumed_frames=[],
        state={},
    )
    assert evidence["finish_reason"] is None


# ---------------------------------------------------------------------------
# End-to-end through _stream_chat_completion
# ---------------------------------------------------------------------------


def _sse_body(*, content: str, finish_reason: str | None = None) -> str:
    delta: dict = {"content": content}
    choice: dict = {"delta": delta}
    if finish_reason is not None:
        choice["finish_reason"] = finish_reason
    frame = {"choices": [choice]}
    return f"data: {json.dumps(frame, ensure_ascii=False)}\n\n"


def _run_stream(monkeypatch, tmp_path, db_name: str, body: str, *, payload=None):
    monkeypatch.setattr(db_mod, "DB_PATH", tmp_path / db_name)
    monkeypatch.setattr(db_mod._local, "conn", None, raising=False)
    db_mod.init_db()
    monkeypatch.setattr(hiagent, "get_setting", lambda _key: "60")

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(hiagent.ProviderError) as caught:
                await hiagent._stream_chat_completion(
                    client, "https://provider.test/chat/completions",
                    payload or {"model": "m", "messages": []},
                    kind="chat", model="m", headers={},
                )
            return caught.value

    return asyncio.run(run())


def test_content_filter_finish_reason_becomes_terminal_rejection(tmp_path, monkeypatch):
    """Requirement #1: finish_reason=="content_filter" must be treated as a
    determined terminal outcome -- no retry offered, provider text quoted."""
    exc = _run_stream(
        monkeypatch, tmp_path, "cf.db",
        _sse_body(content=REFUSAL_TEXT, finish_reason="content_filter"),
    )
    assert exc.failure_category == "model_rejection"
    assert exc.retryable is False
    assert exc.delivery_state == "responded"
    assert REFUSAL_TEXT in str(exc)
    assert "请在页面确认后重试" not in str(exc)
    assert "结果不确定" not in str(exc)


def test_genuine_long_interruption_without_finish_reason_stays_retryable(tmp_path, monkeypatch):
    """Requirement #2: a real mid-sentence cut (long content, no
    finish_reason, single attempt) must not be swept into the deterministic
    path -- classify_interrupted_stream now runs on every interrupted
    stream, so this guards against an over-broad signal."""
    long_prefix = "从前有座山，山里有座庙，庙里有个老和尚在讲故事：" * 20
    exc = _run_stream(monkeypatch, tmp_path, "long-cut.db", _sse_body(content=long_prefix))
    assert exc.failure_kind == "stream_interrupted"
    assert exc.retryable is True
    assert exc.delivery_state == "unknown"
    assert exc.requires_explicit_retry is True


def test_repeated_identical_short_content_across_real_attempts_becomes_deterministic(tmp_path, monkeypatch):
    """Signal 2 end-to-end: no finish_reason at all (HiAgent's actual
    behavior for this refusal is unconfirmed -- see report), but the same
    request retried gets the exact same 22-character content every time.
    The first attempt has nothing to compare against; the second,
    byte-identical attempt of the same operation must flip."""
    monkeypatch.setattr(db_mod, "DB_PATH", tmp_path / "repeat.db")
    monkeypatch.setattr(db_mod._local, "conn", None, raising=False)
    db_mod.init_db()
    monkeypatch.setattr(hiagent, "get_setting", lambda _key: "60")
    body = _sse_body(content=REFUSAL_TEXT)
    payload = {"model": "m", "messages": [{"role": "user", "content": "第1616章"}]}

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    async def attempt():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(hiagent.ProviderError) as caught:
                await hiagent._stream_chat_completion(
                    client, "https://provider.test/chat/completions",
                    payload, kind="chat", model="m", headers={},
                )
            return caught.value

    first = asyncio.run(attempt())
    assert first.failure_kind == "stream_interrupted"
    assert first.retryable is True

    second = asyncio.run(attempt())
    assert second.failure_category == "model_rejection"
    assert second.retryable is False
    assert REFUSAL_TEXT in str(second)


# ---------------------------------------------------------------------------
# app/errors.py -- the quote must actually reach the user, not just exist
# ---------------------------------------------------------------------------


def test_classify_routes_model_rejection_to_its_own_non_technical_category():
    exc = hiagent.ProviderError(
        f"供应商内容审核已明确拒绝本次请求，原文：{REFUSAL_TEXT}；"
        "重试不会改变结果，请调整内容后重新生成，或更换供应商模型",
        raw=f"stream interrupted before [DONE]: {REFUSAL_TEXT}",
        delivery_state="responded",
        failure=hiagent.ProviderFailure.model_rejection(),
    )
    assert errors_mod.classify(exc) == ("provider_rejected", "LLM-REJECTED")
    assert errors_mod.CATEGORIES["provider_rejected"]["technical"] is False


def test_classify_keeps_generic_provider_category_for_other_provider_errors():
    """Must not over-widen: an ordinary transport-timeout ProviderError has
    no model_rejection failure and keeps the existing sanitized category."""
    exc = hiagent.ProviderError("流式调用超时", failure_kind="request_outcome_unknown")
    assert errors_mod.classify(exc) == ("provider", "LLM")


def test_public_error_text_quotes_the_provider_and_drops_the_retry_lie():
    exc = hiagent.ProviderError(
        f"供应商内容审核已明确拒绝本次请求，原文：{REFUSAL_TEXT}；"
        "重试不会改变结果，请调整内容后重新生成，或更换供应商模型",
        raw=f"stream interrupted before [DONE]: {REFUSAL_TEXT}",
        delivery_state="responded",
        failure=hiagent.ProviderFailure.model_rejection(),
    )
    record = errors_mod.log_error(exc)
    assert REFUSAL_TEXT in record.public
    assert "可稍后重试" not in record.public
    assert "修复重试上限" not in record.public
