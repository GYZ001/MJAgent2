"""供应商通道并发 0/空=自动：安全阀只是上限，真实并发靠慢启动 AIMD 从供应商信号探出来（用户 2026-09-06 拍板：所有并发不设上限）。"""
from __future__ import annotations

import pytest

from app.media_pipeline import concurrency as cc
from app.media_pipeline import stages as S


@pytest.fixture(autouse=True)
def _fresh_channels(monkeypatch):
    monkeypatch.setattr(cc, "_channels", {})
    monkeypatch.setattr(cc, "_loop_semaphores", {})
    values: dict[str, str] = {}
    monkeypatch.setattr(cc, "get_setting", lambda key: values.get(key))
    monkeypatch.setattr(cc, "set_setting", lambda key, value: values.__setitem__(key, value))
    return values


def test_zero_or_blank_means_auto_with_warm_start_and_safety_ceiling(_fresh_channels) -> None:
    _fresh_channels["image_request_concurrency"] = "0"
    state = cc.ensure_channel(S.RESOURCE_IMAGE)
    assert state.auto and state.hard_limit == cc.AUTO_CEILINGS[S.RESOURCE_IMAGE] == 64
    assert state.current == cc.CHANNEL_DEFAULTS[S.RESOURCE_IMAGE] == 4  # 热启动，不是一上来 64
    assert cc.channel_limit(S.RESOURCE_IMAGE) == 4
    text = cc.ensure_channel(cc.RESOURCE_TEXT_PROVIDER)  # 空值同样是自动
    assert text.auto and text.hard_limit == 32 and text.current == 6
    _fresh_channels["vlm_request_concurrency"] = "12"
    fixed = cc.ensure_channel(S.RESOURCE_VLM)
    assert not fixed.auto and fixed.hard_limit == fixed.current == 12


def test_auto_slow_start_doubles_per_healthy_minute_then_adds_one_after_congestion(monkeypatch, _fresh_channels) -> None:
    _fresh_channels["image_request_concurrency"] = "0"
    now = [1000.0]
    monkeypatch.setattr(cc.time, "time", lambda: now[0])
    cc.ensure_channel(S.RESOURCE_IMAGE)
    cc.report_healthy(S.RESOURCE_IMAGE)  # 起计时
    now[0] += 61
    cc.report_healthy(S.RESOURCE_IMAGE)
    assert cc.channel_limit(S.RESOURCE_IMAGE) == 8
    now[0] += 61
    cc.report_healthy(S.RESOURCE_IMAGE)
    assert cc.channel_limit(S.RESOURCE_IMAGE) == 16
    cc.report_congestion(S.RESOURCE_IMAGE, reason="http_429")
    cc.report_congestion(S.RESOURCE_IMAGE, reason="http_429")  # 连续两次才减半
    assert cc.channel_limit(S.RESOURCE_IMAGE) == 8 and cc.ensure_channel(S.RESOURCE_IMAGE).slow_start is False
    now[0] += 61  # 冷却 60s
    cc.report_healthy(S.RESOURCE_IMAGE)  # 冷却后重新起计时
    now[0] += 61
    cc.report_healthy(S.RESOURCE_IMAGE)
    assert cc.channel_limit(S.RESOURCE_IMAGE) == 9  # 见过拥塞：加法
    for _ in range(200):
        now[0] += 61
        cc.report_healthy(S.RESOURCE_IMAGE)
    assert cc.channel_limit(S.RESOURCE_IMAGE) == 64  # 封顶在安全阀


def test_fixed_mode_keeps_ten_minute_step_and_reload_jumps_to_new_limit(monkeypatch, _fresh_channels) -> None:
    _fresh_channels["image_request_concurrency"] = "4"
    now = [1000.0]
    monkeypatch.setattr(cc.time, "time", lambda: now[0])
    cc.ensure_channel(S.RESOURCE_IMAGE)
    cc.report_healthy(S.RESOURCE_IMAGE)
    now[0] += 120
    cc.report_healthy(S.RESOURCE_IMAGE)
    assert cc.channel_limit(S.RESOURCE_IMAGE) == 4  # 固定模式 2 分钟不升
    _fresh_channels["image_request_concurrency"] = "16"
    cc.reload_limits_from_settings()
    assert cc.channel_limit(S.RESOURCE_IMAGE) == 16  # 固定模式：提高上限立刻放开


def test_switching_to_auto_keeps_current_and_only_raises_the_ceiling(_fresh_channels) -> None:
    _fresh_channels["image_request_concurrency"] = "6"
    cc.ensure_channel(S.RESOURCE_IMAGE)
    _fresh_channels["image_request_concurrency"] = "0"
    cc.reload_limits_from_settings()
    state = cc.ensure_channel(S.RESOURCE_IMAGE)
    assert state.auto and state.hard_limit == 64 and state.current == 6  # 不跳到安全阀砸供应商


def test_migration_seeds_blank_keys_with_auto(_fresh_channels) -> None:
    cc.migrate_legacy_settings()
    assert _fresh_channels["image_request_concurrency"] == "0"
    assert _fresh_channels["text_generation_concurrency"] == "0"


def test_congestion_halves_at_most_once_per_cooldown_window(monkeypatch, _fresh_channels) -> None:
    """一波同时失败（重启掐流、批量取消）只算一次拥塞证据：冷却期内不再连续减半（实测 10→5→2→1 在同一秒内）。"""
    _fresh_channels["text_generation_concurrency"] = "0"
    now = [1000.0]
    monkeypatch.setattr(cc.time, "time", lambda: now[0])
    state = cc.ensure_channel(cc.RESOURCE_TEXT_PROVIDER)
    state.current = 16
    for _ in range(6):
        cc.report_congestion(cc.RESOURCE_TEXT_PROVIDER, reason="stream_interrupted")
    assert cc.channel_limit(cc.RESOURCE_TEXT_PROVIDER) == 8  # 六次连击只减半一次
    now[0] += 61
    cc.report_congestion(cc.RESOURCE_TEXT_PROVIDER, reason="stream_interrupted")
    cc.report_congestion(cc.RESOURCE_TEXT_PROVIDER, reason="stream_interrupted")
    assert cc.channel_limit(cc.RESOURCE_TEXT_PROVIDER) == 4  # 冷却过了才允许再减半
