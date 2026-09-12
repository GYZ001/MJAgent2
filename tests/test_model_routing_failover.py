"""按 purpose 的选路、健康度熔断、故障转移审计、限速。EP-05 第二阶段。"""
from __future__ import annotations

import asyncio
import json
import time

import pytest

from app.db import get_conn, set_setting
from app.models_registry import bindings, health, ratelimit, routing, store


def _seed_two_priority_chain() -> None:
    set_setting("custom_models", json.dumps([
        {"id": "model_a", "provider": "custom:model_a", "model": "text-a", "kinds": ["text"],
         "builtin": False, "protocol": "openai", "base_url": "https://a.example.test/v1"},
        {"id": "model_b", "provider": "custom:model_b", "model": "text-b", "kinds": ["text"],
         "builtin": False, "protocol": "openai", "base_url": "https://b.example.test/v1"},
    ], ensure_ascii=False))
    store.put_credential("model_a", base_url="https://a.example.test/v1", api_key="sk-a", rotated_by="t")
    store.put_credential("model_b", base_url="https://b.example.test/v1", api_key="sk-b", rotated_by="t")
    bindings.upsert_binding(purpose="text:default", model_id="model_a", priority=0)
    bindings.upsert_binding(purpose="text:default", model_id="model_b", priority=1)


def _open_circuit(model_id: str) -> None:
    """把 model_id 打到 circuit_open：5 次调用、4 次失败（80% > 50% 阈值）。"""
    for _ in range(4):
        health.record_outcome(model_id, "server_error", latency_ms=10)
    health.record_outcome(model_id, "server_error", latency_ms=10)


# ---------------------------------------------------------------------------
# resolve()：优先级 + 熔断过滤
# ---------------------------------------------------------------------------

def test_resolve_returns_priority_zero_when_healthy() -> None:
    _seed_two_priority_chain()
    resolved = routing.resolve("text:default")
    assert resolved is not None
    assert resolved.provider == "custom:model_a"
    assert resolved.priority == 0


def test_resolve_skips_circuit_open_and_falls_to_next_priority() -> None:
    _seed_two_priority_chain()
    _open_circuit("model_a")
    assert health.effective_state("model_a") == "circuit_open"

    resolved = routing.resolve("text:default")
    assert resolved is not None
    assert resolved.provider == "custom:model_b"


def test_resolve_returns_none_when_all_candidates_circuit_open() -> None:
    _seed_two_priority_chain()
    _open_circuit("model_a")
    _open_circuit("model_b")

    assert routing.resolve("text:default") is None


def test_half_open_probe_success_recovers_to_healthy_and_reselected() -> None:
    _seed_two_priority_chain()
    _open_circuit("model_a")
    win = health._window_map()["model_a"]  # noqa: SLF001 测试直接摆弄冷却期，绕开真实等待
    win.opened_at = time.time() - (win.cooldown_s + 1)

    assert health.effective_state("model_a") == "half_open"
    health.record_outcome("model_a", None, latency_ms=5)  # 探针成功
    assert health.effective_state("model_a") == "healthy"

    resolved = routing.resolve("text:default")
    assert resolved is not None
    assert resolved.provider == "custom:model_a"


def test_half_open_probe_failure_reopens_circuit_with_longer_cooldown() -> None:
    _seed_two_priority_chain()
    _open_circuit("model_a")
    win = health._window_map()["model_a"]  # noqa: SLF001
    first_cooldown = win.cooldown_s
    win.opened_at = time.time() - (first_cooldown + 1)

    assert health.effective_state("model_a") == "half_open"
    health.record_outcome("model_a", "server_error", latency_ms=5)  # 探针失败

    assert win.state == "circuit_open"
    assert win.cooldown_s > first_cooldown


def test_resolve_provider_for_kind_falls_back_without_any_binding() -> None:
    set_setting("custom_models", json.dumps([
        {"id": "model_only", "provider": "custom:model_only", "model": "text-only",
         "kinds": ["text"], "builtin": False, "protocol": "openai",
         "base_url": "https://only.example.test/v1"},
    ], ensure_ascii=False))
    assert routing.resolve_provider_for_kind("text") == "custom:model_only"


# ---------------------------------------------------------------------------
# health.classify_call_status：结构化判据，纯函数
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status,http_status,expected", [
    ("OK", 200, None),
    ("RUNNING", None, None),
    ("TIMEOUT", None, "timeout"),
    ("INTERRUPTED", None, "timeout"),
    ("NETWORK_ERROR", None, "timeout"),
    ("FAILED", 429, "rate_limited"),
    ("FAILED", 500, "server_error"),
    ("FAILED", 503, "server_error"),
    ("FAILED", 401, "server_error"),
    ("FAILED", 403, "server_error"),
    ("FAILED", 404, "content_rejected"),
    ("FAILED", 422, "content_rejected"),
    ("FAILED", None, "server_error"),
])
def test_classify_call_status(status: str, http_status: int | None, expected: str | None) -> None:
    assert health.classify_call_status(status, http_status) == expected


def test_health_refresh_pulls_from_provider_calls_and_flushes_to_table() -> None:
    _seed_two_priority_chain()
    conn = get_conn()
    for _ in range(4):
        conn.execute(
            "INSERT INTO provider_calls(ts, kind, model, status, http_status, latency_ms) "
            "VALUES(?,?,?,?,?,?)",
            (time.time(), "chat", "text-a", "FAILED", 500, 20),
        )
    conn.execute(
        "INSERT INTO provider_calls(ts, kind, model, status, http_status, latency_ms) "
        "VALUES(?,?,?,?,?,?)",
        (time.time(), "chat", "text-a", "OK", 200, 20),
    )
    conn.commit()

    health.refresh(force=True)

    assert health.effective_state("model_a") == "circuit_open"
    row = conn.execute("SELECT * FROM model_health WHERE model_id='model_a'").fetchone()
    assert row is not None
    assert row["state"] == "circuit_open"
    assert row["window_calls"] == 5
    assert row["window_failures"] == 4


# ---------------------------------------------------------------------------
# call_with_failover：换路分类 + 审计 + 视频二次确认
# ---------------------------------------------------------------------------

class _FakeProviderError(Exception):
    def __init__(self, failure_kind: str = "", failure_category: str = "", timeout_phase: str | None = None) -> None:
        super().__init__(failure_kind or "fake")
        self.failure_kind = failure_kind
        self.failure_category = failure_category
        self.timeout_phase = timeout_phase


def _audit_failover_rows() -> list:
    return get_conn().execute(
        "SELECT * FROM operation_audit WHERE event='models_registry.route_failover' ORDER BY ts"
    ).fetchall()


async def test_call_with_failover_switches_on_server_error_and_audits() -> None:
    _seed_two_priority_chain()
    calls: list[str] = []

    async def fn(candidate: routing.ResolvedModel) -> str:
        calls.append(candidate.model_id)
        if candidate.model_id == "model_a":
            raise _FakeProviderError(failure_kind="upstream_unavailable")
        return "ok-from-" + candidate.model_id

    result = await routing.call_with_failover("text:default", fn, request_id="req-1")

    assert result == "ok-from-model_b"
    assert calls == ["model_a", "model_b"]
    audit_rows = _audit_failover_rows()
    assert len(audit_rows) == 1
    assert audit_rows[0]["target"] == "model_a"
    assert audit_rows[0]["error_code"] == "server_error"


async def test_call_with_failover_does_not_switch_on_unclassified_exception() -> None:
    """没有 failure_kind 字段形状的普通异常（对应 contract_invalid 的落地
    形态：model_gateway 的 StructuredOutputError 不带这套字段）不换路，原样
    抛出——不产生第二次调用，不落换路审计。"""
    _seed_two_priority_chain()
    calls: list[str] = []

    async def fn(candidate: routing.ResolvedModel) -> str:
        calls.append(candidate.model_id)
        raise ValueError("看起来像契约校验失败，不是传输失败")

    with pytest.raises(ValueError):
        await routing.call_with_failover("text:default", fn, request_id="req-2")

    assert calls == ["model_a"]
    assert _audit_failover_rows() == []


async def test_call_with_failover_video_purpose_requires_confirmation_before_switching_on_timeout() -> None:
    """视频计费：客户端侧 timeout 不等于供应商侧失败，未提供确认回调时不换路、
    不重发（断言没有产生第二次调用）。"""
    set_setting("custom_models", json.dumps([
        {"id": "vid_a", "provider": "custom:vid_a", "model": "video-a", "kinds": ["video"],
         "builtin": False, "protocol": "seedance", "base_url": "https://va.example.test/v1"},
        {"id": "vid_b", "provider": "custom:vid_b", "model": "video-b", "kinds": ["video"],
         "builtin": False, "protocol": "seedance", "base_url": "https://vb.example.test/v1"},
    ], ensure_ascii=False))
    bindings.upsert_binding(purpose="video:shot", model_id="vid_a", priority=0)
    bindings.upsert_binding(purpose="video:shot", model_id="vid_b", priority=1)
    calls: list[str] = []

    async def fn(candidate: routing.ResolvedModel) -> str:
        calls.append(candidate.model_id)
        raise _FakeProviderError(timeout_phase="read")

    with pytest.raises(_FakeProviderError):
        await routing.call_with_failover("video:shot", fn, request_id="req-3")

    assert calls == ["vid_a"]  # 没有第二次供应商调用
    assert _audit_failover_rows() == []


async def test_call_with_failover_video_purpose_switches_after_confirmed_terminal_failure() -> None:
    set_setting("custom_models", json.dumps([
        {"id": "vid_a", "provider": "custom:vid_a", "model": "video-a", "kinds": ["video"],
         "builtin": False, "protocol": "seedance", "base_url": "https://va.example.test/v1"},
        {"id": "vid_b", "provider": "custom:vid_b", "model": "video-b", "kinds": ["video"],
         "builtin": False, "protocol": "seedance", "base_url": "https://vb.example.test/v1"},
    ], ensure_ascii=False))
    bindings.upsert_binding(purpose="video:shot", model_id="vid_a", priority=0)
    bindings.upsert_binding(purpose="video:shot", model_id="vid_b", priority=1)
    calls: list[str] = []

    async def fn(candidate: routing.ResolvedModel) -> str:
        calls.append(candidate.model_id)
        if candidate.model_id == "vid_a":
            raise _FakeProviderError(timeout_phase="read")
        return "done"

    result = await routing.call_with_failover(
        "video:shot", fn, request_id="req-4", confirm_terminal_failure=lambda: True,
    )

    assert result == "done"
    assert calls == ["vid_a", "vid_b"]
    assert len(_audit_failover_rows()) == 1


# ---------------------------------------------------------------------------
# sync_legacy_binding：旧监制房下拉框兼容桥
# ---------------------------------------------------------------------------

def test_sync_legacy_binding_writes_priority_zero_when_provider_supports_kind() -> None:
    set_setting("custom_models", json.dumps([
        {"id": "model_x", "provider": "custom:model_x", "model": "text-x", "kinds": ["text"],
         "builtin": False, "protocol": "openai", "base_url": "https://x.example.test/v1"},
    ], ensure_ascii=False))

    routing.sync_legacy_binding("text", "custom:model_x")

    assert bindings.get_priority_zero("text:default")["model_id"] == "model_x"


def test_sync_legacy_binding_skips_silently_when_provider_lacks_kind() -> None:
    set_setting("custom_models", json.dumps([
        {"id": "model_x", "provider": "custom:model_x", "model": "video-x", "kinds": ["video"],
         "builtin": False, "protocol": "seedance", "base_url": "https://x.example.test/v1"},
    ], ensure_ascii=False))

    routing.sync_legacy_binding("text", "custom:model_x")  # model_x 不支持 text

    assert bindings.get_priority_zero("text:default") is None


# ---------------------------------------------------------------------------
# ratelimit：令牌桶 + 信号量，排队而非报错
# ---------------------------------------------------------------------------

async def test_ratelimit_concurrency_serializes_beyond_limit() -> None:
    ratelimit.reset_for_tests()
    order: list[str] = []

    async def worker(name: str) -> None:
        async with await ratelimit.acquire("cred-x", concurrency=1, timeout_s=5):
            order.append(f"{name}-start")
            await asyncio.sleep(0.05)
            order.append(f"{name}-end")

    await asyncio.gather(worker("a"), worker("b"))

    # concurrency=1：不能出现 a-start, b-start, a-end, b-end 交叉执行的痕迹。
    assert order in (["a-start", "a-end", "b-start", "b-end"], ["b-start", "b-end", "a-start", "a-end"])


async def test_ratelimit_rpm_queues_instead_of_raising() -> None:
    ratelimit.reset_for_tests()
    started = time.monotonic()
    for _ in range(3):
        async with await ratelimit.acquire("cred-y", rpm=60, concurrency=8, timeout_s=5):
            pass
    # rpm=60 → 1 令牌/秒补充，桶容量 60；3 次瞬间都在初始余量内，几乎不排队。
    assert time.monotonic() - started < 2.0


async def test_ratelimit_timeout_raises_instead_of_hanging_forever() -> None:
    ratelimit.reset_for_tests()
    async with await ratelimit.acquire("cred-z", rpm=1, concurrency=8, timeout_s=5):
        pass  # 消耗掉唯一的初始令牌
    with pytest.raises(TimeoutError):
        await ratelimit.acquire("cred-z", rpm=1, concurrency=8, timeout_s=0.05)
