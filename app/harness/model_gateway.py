from __future__ import annotations

from typing import Any

from app import hiagent
from app.observability.tracing import current_trace


async def chat(
    messages: list[dict[str, str]],
    *,
    temperature: float = 0.7,
    max_tokens: int = 8192,
    call_meta: dict[str, Any] | None = None,
) -> str:
    """The only text-model entry point for business stages.

    It enforces trace metadata at the harness boundary while retaining the
    provider adapter's retry, redaction and lifecycle recording.
    """
    trace = current_trace()
    meta = {
        "gateway": "execution_harness",
        "run_id": trace.run_id,
        "step_run_id": trace.step_run_id,
        "trace_id": trace.trace_id,
        **(call_meta or {}),
    }
    return await hiagent.chat(
        messages,
        temperature=temperature,
        max_tokens=max_tokens,
        call_meta=meta,
    )
