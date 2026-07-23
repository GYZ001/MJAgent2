"""内存滑动窗口限流：保护本机 `/mcp` 端点，防止单 token/IP 短时间内洪泛调用。

单进程内存足够，因为本方案约束 `/mcp` 只本地绑定、单进程运行（PRD §12.2）。
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque


class RateLimitExceeded(Exception):
    pass


class SlidingWindowRateLimiter:
    def __init__(self, *, limit: int = 120, window_s: float = 60.0) -> None:
        self.limit = limit
        self.window_s = window_s
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key: str) -> None:
        now = time.monotonic()
        with self._lock:
            bucket = self._hits[key]
            cutoff = now - self.window_s
            while bucket and bucket[0] < cutoff:
                bucket.popleft()
            if len(bucket) >= self.limit:
                raise RateLimitExceeded(
                    f"rate limit exceeded: {self.limit} requests / {self.window_s:.0f}s"
                )
            bucket.append(now)

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


_LIMITER = SlidingWindowRateLimiter()


def get_rate_limiter() -> SlidingWindowRateLimiter:
    return _LIMITER


def reset_rate_limiter_for_tests() -> None:
    _LIMITER.reset()
