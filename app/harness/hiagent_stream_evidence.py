"""Pure evidence bookkeeping for interrupted (no-``[DONE]``) SSE chat streams.

Split out of app/hiagent.py purely for file-size budget: that file is pinned
exactly at its FILE_CONVENTIONS.toml line-count baseline and CLAUDE.md's
ratchet forbids raising it ("红线只降不升"). Nothing here touches
``app.hiagent.ProviderError``/``ProviderFailure`` -- callers turn the plain
values returned here into those exceptions themselves -- so this module has
no dependency on app.hiagent and cannot form an import cycle with it. Placed
under app/harness/ to inherit the existing ``"app.harness" = 3`` layer
prefix in app/LAYERS.toml (same layer as app.hiagent) without adding a line
to that file.

Real production incident (ERR-20260831-4c9132): a content-policy refusal
from HiAgent arrived as a normal 22-character ``delta.content`` chunk and
then the connection closed without ``[DONE]`` and without a terminal
``finish_reason`` chunk. The old code discarded ``state`` (where a
``finish_reason`` would have been captured, see ``_accumulate_stream_chunk``
in app/hiagent.py) before building evidence, so a deterministic provider
rejection was indistinguishable from a genuine transient interruption and
was reported to the user as "结果不确定，可点击重试" -- retryable, when
retrying could never succeed. ``classify_interrupted_stream`` below is the
fix: it checks the provider's own terminal signal first, and falls back to
a structural repeated-attempt signal that was verified against 170 real
INTERRUPTED rows sharing this exact 22-character payload (same
``operation_id``, escalating ``attempt_no``, byte-identical content, up to
6 attempts) before landing.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

INTERRUPTED_STREAM_FRAME_CHARS = 300
INTERRUPTED_STREAM_MAX_FRAMES = 4
INTERRUPTED_STREAM_TEXT_CHARS = 300


def remember_unconsumed_stream_frame(frames: list[str], line: str) -> None:
    """Keep a bounded record of SSE frames the reader could not consume."""
    text = str(line or "").strip()
    if not text or len(frames) >= INTERRUPTED_STREAM_MAX_FRAMES:
        return
    frames.append(text[:INTERRUPTED_STREAM_FRAME_CHARS])


def interrupted_stream_evidence(
    *,
    content_parts: list[str],
    reasoning_parts: list[str],
    unconsumed_frames: list[str],
    state: dict[str, Any],
) -> dict[str, Any]:
    """Bounded evidence for a stream that ended without ``[DONE]``.

    The reconstructed answer is still discarded -- an incomplete stream is
    not an answer. But discarding the evidence too left a repeated
    interruption undiagnosable (see module docstring). ``finish_reason`` is
    carried through from ``state`` for the same reason: a provider that did
    send a terminal ``finish_reason`` (e.g. ``content_filter``) before
    dropping the connection without ``[DONE]`` was previously
    indistinguishable from one that sent nothing at all.
    """
    content = "".join(content_parts)[:INTERRUPTED_STREAM_TEXT_CHARS]
    reasoning = "".join(reasoning_parts)[:INTERRUPTED_STREAM_TEXT_CHARS]
    summary = content or reasoning or (
        unconsumed_frames[0] if unconsumed_frames else ""
    )
    return {
        "content_prefix": content,
        "reasoning_prefix": reasoning,
        "unconsumed_frames": list(unconsumed_frames),
        "finish_reason": state.get("finish_reason"),
        "summary": summary[:INTERRUPTED_STREAM_TEXT_CHARS],
    }


def classify_interrupted_stream(
    conn: sqlite3.Connection,
    call_id: int,
    finish_reason: str | None,
    max_signature_chars: int,
) -> bool:
    """True iff this INTERRUPTED stream attempt is a deterministic provider
    rejection rather than a genuine, possibly-transient interruption.

    Two structural signals, no keyword/content matching (CLAUDE.md「禁止
    黑白名单与枚举穷举」-- legal outcomes are derived from what actually
    happened on the wire, not from what the refusal text says):

    1. ``finish_reason == "content_filter"``: the provider's own terminal
       signal for this turn, sufficient on its own.
    2. Absent that: >=2 attempts of the *same* business operation (same
       kind/model/request payload -- ``operation_id`` groups retries this
       way, see ``app.db.provider_operation_id``) each ended INTERRUPTED
       with byte-identical, non-empty evidence. A random transport cut does
       not reproduce the same content twice. A row whose full output
       reached ``max_signature_chars`` is excluded rather than risked as a
       false match: its stored ``summary`` is capped by
       ``interrupted_stream_evidence`` and so cannot prove the provider's
       full output was actually that short -- comparing it could paper over
       a genuine long interruption that happened to be cut at a similar
       point twice.

    The caller is responsible for having already committed *this* attempt's
    INTERRUPTED row before calling (mirrors the existing
    ``app/media_exec/job_state.py:_prior_task_poll_failure_messages``
    contract), so the query below naturally includes it.
    """
    if finish_reason == "content_filter":
        return True
    row = conn.execute(
        "SELECT operation_id FROM provider_calls WHERE id=?", (call_id,),
    ).fetchone()
    operation_id = str((row["operation_id"] if row else "") or "")
    if not operation_id:
        return False
    rows = conn.execute(
        """SELECT response_json, received_chars FROM provider_calls
           WHERE operation_id=? AND status='INTERRUPTED' ORDER BY id""",
        (operation_id,),
    ).fetchall()
    signatures: list[str] = []
    for attempt in rows:
        if int(attempt["received_chars"] or 0) >= max_signature_chars:
            signatures.append("")
            continue
        try:
            payload = json.loads(attempt["response_json"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            signatures.append("")
            continue
        interrupted = payload.get("interrupted_stream") or {}
        signatures.append(str(interrupted.get("summary") or ""))
    if len(signatures) < 2:
        return False
    tail = signatures[-2:]
    return bool(tail[0]) and tail[0] == tail[1]
