"""过载拒绝波要靠次数熬过去：退避封顶、流中断计入拥塞（2026-09-06 第 5/6 轮映射台整台失败）。"""
from __future__ import annotations

from app import config
from app import generation_concurrency as gc


def test_defaults_give_roughly_a_quarter_hour_of_budget() -> None:
    delays = [min(config.TEXT_PROVIDER_RETRY_BASE_DELAY * 2 ** n, config.TEXT_PROVIDER_RETRY_MAX_DELAY)
              for n in range(config.TEXT_PROVIDER_MAX_RETRIES)]
    assert config.TEXT_PROVIDER_MAX_RETRIES >= 8 and max(delays) <= 120
    assert 600 <= sum(delays) <= 1200


def test_stream_interruption_counts_as_congestion_evidence() -> None:
    class _Exc(Exception):
        retryable = True
        failure_kind = "stream_interrupted"

    assert gc._congestion_reason(_Exc()) == "stream_interrupted"

    class _Business(Exception):
        retryable = False
        failure_kind = "stream_interrupted"

    assert gc._congestion_reason(_Business()) is None  # 不可重试的不算
