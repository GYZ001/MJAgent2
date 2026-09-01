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
fix: a structural repeated-attempt signal, verified against 170 real
INTERRUPTED rows sharing this exact 22-character payload (same
``operation_id``, escalating ``attempt_no``, byte-identical content, up to
6 attempts) before landing.

2026-09-01（ERR-20260901-6d6a87 及同批 11 条）：原来还有第二条判据——供应商自己
的 ``finish_reason == "content_filter"``，单次即判"确定性拒绝"。这条判据被实测
证伪，已删除：``episode_prep_pack:ep_a3d88b44a6cd:chunk:1`` 这个 operation_id
（同一份请求）在 08-31 10:23、08-31 16:46、09-01 04:28 三次 OK，04:29:47 第四次
带着 ``finish_reason=content_filter`` 被拒；把那一次的请求原样重放三次，**三次
全部成功**。拒绝集中出现在用户并行跑 7 集的那两个时间窗（04:29 与 04:56 各一批），
说明 HiAgent 在突发负载下也会用这套"内容审核"话术+``content_filter`` 回绝，
供应商的终止信号**在单次尝试上不可信**。而"重试不会改变结果，请调整内容后重新
生成"这句承诺一旦判错，就是把用户推去改小说原文或换模型——CLAUDE.md「界面承诺
必须与实际行为一致」。现在一律要求复现：同一 operation_id 两次拿到字节相同的
拒绝证据才算确定（真拒绝稳定复现，抖动不会），单次拒绝退回既有的"结果不确定，
请确认后重试"契约。
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

    判据只有一条结构信号，不看拒绝文案说了什么（CLAUDE.md「禁止黑白名单与
    枚举穷举」）：同一个业务操作（``operation_id`` 按 kind/model/请求体分组，
    见 ``app.db.provider_operation_id``）**≥2 次**尝试都以 INTERRUPTED 结束，
    且证据字节完全相同、非空。真拒绝稳定复现，随机传输中断不会两次给出同一段
    内容。整段输出长度触到 ``max_signature_chars`` 的行不参与比对而直接排除：
    它存下来的 ``summary`` 被 ``interrupted_stream_evidence`` 截断过，证明不了
    供应商当时真的只输出了这么短，拿它比对可能把两次"恰好断在相似位置"的长
    中断误判成拒绝。

    ``finish_reason`` 只进证据、不再单独定性：供应商在突发负载下同样会发
    ``content_filter``（见模块 docstring 的三次重放实测），单次信号不足以支撑
    "重试不会改变结果"这句话。

    The caller is responsible for having already committed *this* attempt's
    INTERRUPTED row before calling (mirrors the existing
    ``app/media_exec/job_state.py:_prior_task_poll_failure_messages``
    contract), so the query below naturally includes it.
    """
    del finish_reason  # 只进证据，不再单独定性——理由见上方 docstring
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
