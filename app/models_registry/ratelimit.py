"""按凭据的进程内限速：RPM/TPM 令牌桶 + 并发信号量。L1，纯算法零 db 依赖。

**局限（如实标注，不是全局限速）**：状态全部在本进程内存里；多进程部署下
每个进程各自维护一份，总吞吐是"进程数 × 这里配的上限"，不是这里配的绝对值。
B 上目前是单进程部署（见 CLAUDE.md 生产拓扑），这个局限暂时不影响实际效果，
但代码本身不能假装自己在做跨进程限速——这条注释就是 EP-05 §7 要求的"如实
标注"。

**超限排队，不报错**：``acquire()`` 桶里没有余量时协程挂起等补充，不抛异常
（EP-05 §7）。``timeout_s`` 给排队一个上限——超过这个时长仍拿不到才真的抛
``TimeoutError``，这是"配置明显不合理"（比如 RPM=1 却排了 200 个请求）时的
兜底，不是常态路径。

本阶段未接入任何真实调用点：真实调用在 ``app/hiagent.py``，该文件行数基线
已顶满零余量（见 ``app/FILE_CONVENTIONS.toml``），接入需要主会话决定是否为此
新增基线余量——见交付报告"未完成项"，与 ``routing.py::call_with_failover``
同一处境。
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass


@dataclass
class _TokenBucket:
    capacity: float
    refill_per_s: float
    tokens: float
    updated_at: float

    def _refill(self) -> None:
        now = time.monotonic()
        self.tokens = min(self.capacity, self.tokens + max(0.0, now - self.updated_at) * self.refill_per_s)
        self.updated_at = now

    def try_take(self, amount: float) -> bool:
        self._refill()
        if self.tokens >= amount:
            self.tokens -= amount
            return True
        return False

    def wait_s(self, amount: float) -> float:
        self._refill()
        deficit = amount - self.tokens
        if deficit <= 0:
            return 0.0
        return deficit / self.refill_per_s if self.refill_per_s > 0 else float("inf")


@dataclass
class _CredentialLimiter:
    rpm_bucket: _TokenBucket | None
    tpm_bucket: _TokenBucket | None
    semaphore: asyncio.Semaphore


_LIMITERS: dict[str, _CredentialLimiter] = {}
_LOCK = asyncio.Lock()


def _make_bucket(per_minute: int) -> _TokenBucket | None:
    if per_minute <= 0:
        return None
    return _TokenBucket(
        capacity=float(per_minute), refill_per_s=per_minute / 60.0,
        tokens=float(per_minute), updated_at=time.monotonic(),
    )


async def _limiter_for(key: str, *, rpm: int, tpm: int, concurrency: int) -> _CredentialLimiter:
    async with _LOCK:
        existing = _LIMITERS.get(key)
        if existing is not None:
            return existing
        limiter = _CredentialLimiter(
            rpm_bucket=_make_bucket(rpm), tpm_bucket=_make_bucket(tpm),
            semaphore=asyncio.Semaphore(max(1, concurrency)),
        )
        _LIMITERS[key] = limiter
        return limiter


class Acquired:
    """``acquire()`` 的返回值：async 上下文管理器，退出时释放并发槽位。"""

    def __init__(self, semaphore: asyncio.Semaphore) -> None:
        self._semaphore = semaphore

    async def __aenter__(self) -> Acquired:
        return self

    async def __aexit__(self, *exc: object) -> None:
        self._semaphore.release()


async def _wait_for_bucket(bucket: _TokenBucket | None, amount: float, deadline: float) -> None:
    if bucket is None or amount <= 0:
        return
    while not bucket.try_take(amount):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("限速排队超时")
        await asyncio.sleep(min(bucket.wait_s(amount), remaining, 1.0))


async def acquire(
    key: str, *, rpm: int = 0, tpm: int = 0, concurrency: int = 0,
    estimated_tokens: int = 0, timeout_s: float = 300.0,
) -> Acquired:
    """限速 + 并发获取。``rpm``/``tpm``/``concurrency`` 任一为 0 表示该维度
    不限；``concurrency`` 为 0 时仍分配 1 个槽位（信号量不能是 0 容量）。
    """
    key = str(key or "").strip() or "default"
    limiter = await _limiter_for(key, rpm=rpm, tpm=tpm, concurrency=max(1, concurrency or 1))
    deadline = time.monotonic() + max(0.0, timeout_s)
    await _wait_for_bucket(limiter.rpm_bucket, 1.0, deadline)
    await _wait_for_bucket(limiter.tpm_bucket, float(max(0, estimated_tokens)), deadline)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("限速排队超时")
    try:
        await asyncio.wait_for(limiter.semaphore.acquire(), timeout=remaining)
    except TimeoutError as exc:
        raise TimeoutError("限速排队超时") from exc
    return Acquired(limiter.semaphore)


def reset_for_tests() -> None:
    _LIMITERS.clear()
