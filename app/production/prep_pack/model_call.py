"""Step/telemetry bookkeeping (_begin_step/_finish_step/_run_sync_step/
_run_async_step) and the structured model-call wrapper (_call_structured).

Split out of app/production/prep_pack/chunk_extraction.py (架构复测
2026-09-23)：``scene_recheck.py`` 需要 ``_call_structured``，若继续留在
``chunk_extraction.py`` 里，``scene_recheck`` 模块级 import 它、
``chunk_extraction`` 又要在 ``_extract_chunk`` 里调用 ``scene_recheck.
attach_scene_recheck`` 补场景漏报，两边就成环。本文件只依赖 app.db /
app.evidence / app.harness / app.observability / app.orchestration 与同包
``schemas`` 叶子，不反过来依赖 chunk_extraction/scene_recheck 任何一个，
两边都能放心在模块级 import 它。逐字搬移，不改变任何行为。
"""
from __future__ import annotations

from app.db import get_setting
from app.evidence import repository as evidence_repository
from app.harness import model_gateway
from app.harness.contracts import get_contract
from app.observability.tracing import (
    bind_trace,
    current_trace,
)
from app.orchestration.state_machine import transition_step
from contextlib import nullcontext
from pydantic import BaseModel
from typing import Any

from .schemas import _response_format


def _begin_step(run_id: str | None, step_key: str, *, iteration_no: int = 1) -> str | None:
    if not run_id:
        return None
    step_id = evidence_repository.create_step(
        run_id, step_key,
        iteration_no=iteration_no,
        agent_name="episode_prep_pack",
        contract_version=get_contract("screenplay").version,
    )
    transition_step(step_id, "PENDING", "READY", "输入已就绪", conn=None)
    transition_step(step_id, "READY", "RUNNING", "步骤开始", conn=None)
    return step_id


def _finish_step(step_id: str | None, exc: BaseException | None) -> None:
    if not step_id:
        return
    if exc is not None:
        transition_step(
            step_id, "RUNNING", "FAILED", str(exc)[:1000],
            decision="escalate", error_code=type(exc).__name__.upper(), conn=None,
        )
        return
    transition_step(step_id, "RUNNING", "SUCCEEDED", "步骤完成", decision="accept", conn=None)


def _run_sync_step(run_id: str | None, step_key: str, fn):
    """Wrap one deterministic (non-model-call) unit of work as an observable
    step, reusing the same create_step/transition_step machinery as the
    model-calling steps below -- so it shows up in the same observability
    trace with a registered business name (see
    app.orchestration.engine._STEP_PRESENTATIONS)."""
    step_id = _begin_step(run_id, step_key)
    try:
        result = fn()
    except BaseException as exc:
        _finish_step(step_id, exc)
        raise
    _finish_step(step_id, None)
    return result


async def _run_async_step(run_id: str | None, step_key: str, fn):
    """Async twin of ``_run_sync_step`` for one observable awaited unit of
    work (e.g. an app.portraits/app.scenes discovery call) that is not itself
    a structured model call through ``_call_structured``."""
    step_id = _begin_step(run_id, step_key)
    try:
        result = await fn()
    except BaseException as exc:
        _finish_step(step_id, exc)
        raise
    _finish_step(step_id, None)
    return result


async def _call_structured(
    *,
    run_id: str | None,
    step_key: str,
    prompt: str,
    model_type: type[BaseModel],
    schema_name: str,
    operation_id: str,
    max_tokens: int,
    call_meta: dict[str, Any],
    iteration_no: int = 1,
    output_schema: dict[str, Any] | None = None,
    temperature: float = 0.2,
) -> Any:
    """``output_schema`` (1.10.0, 缺陷 A 修复引入)：可选的手写 JSON Schema
    覆盖——真名裁决候选判别需要把 selected_candidate/supporting_entry_index
    收紧到本次卷宗实际算出的 enum（参照 _prep_pack_functional_candidate_call
    对 model_gateway.chat_structured 的直接调用写法），而不是走
    ``_response_format``/``require_response_format`` 这条固定 schema 路径。
    传入时用 ``output_schema`` 直接驱动 provider 调用；不传（默认）时行为与
    改动前逐字节一致。这是同一个 step 封装（_begin_step/_finish_step 观测
    埋点）下的一个可选分支，不是新建一条调用路径。"""
    step_id = _begin_step(run_id, step_key, iteration_no=iteration_no)
    trace = current_trace()
    ctx = bind_trace(run_id, step_id, trace.trace_id) if run_id else nullcontext()
    # 与 blueprint_repair 同源的旋钮（ERR-20260901-037d7b：早停截断与口吃 JSON
    # 都是随机采样失败，写死 1 次修复机会抽到坏样本即整步失败）。
    format_retry_limit = int(get_setting("screenplay_format_retry_limit") or 1)
    try:
        with ctx:
            if output_schema is not None:
                result = await model_gateway.chat_structured(
                    [{"role": "user", "content": prompt}],
                    model_type=model_type,
                    validate=None,
                    operation_id=operation_id,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    format_retry_limit=format_retry_limit,
                    semantic_retry_limit=1,
                    call_meta=call_meta,
                    output_schema=output_schema,
                )
            else:
                result = await model_gateway.chat_structured(
                    [{"role": "user", "content": prompt}],
                    model_type=model_type,
                    validate=None,
                    operation_id=operation_id,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    format_retry_limit=format_retry_limit,
                    semantic_retry_limit=1,
                    call_meta=call_meta,
                    response_format=_response_format(model_type, schema_name),
                    require_response_format=True,
                )
    except BaseException as exc:
        _finish_step(step_id, exc)
        raise
    _finish_step(step_id, None)
    return result
