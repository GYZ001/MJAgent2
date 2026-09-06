"""图片通道的成功/拥塞接进自适应状态机（此前只有文本与视频通道上报，图片通道永远升不上去）。"""
from __future__ import annotations

import asyncio

import pytest

from app import generation_concurrency as gc
from app import hiagent


def test_with_channel_outcome_reports_health_and_congestion(monkeypatch) -> None:
    calls: list[tuple[str, str | None]] = []
    import app.media_pipeline.concurrency as cc
    monkeypatch.setattr(cc, "report_healthy", lambda resource: calls.append((resource, None)))
    monkeypatch.setattr(cc, "report_congestion", lambda resource, *, reason: calls.append((resource, reason)))

    async def ok():
        return {"data": 1}

    async def rate_limited():
        raise hiagent.ProviderError("HTTP 429", retryable=True, failure_kind="rate_limited", delivery_state="responded")

    async def business():
        raise ValueError("结构不合法")

    assert asyncio.run(gc.with_channel_outcome("image_request", ok())) == {"data": 1}
    with pytest.raises(hiagent.ProviderError):
        asyncio.run(gc.with_channel_outcome("image_request", rate_limited()))
    with pytest.raises(ValueError):
        asyncio.run(gc.with_channel_outcome("image_request", business()))
    assert calls == [("image_request", None), ("image_request", "rate_limited")]  # 业务错误不算拥塞也不算健康
