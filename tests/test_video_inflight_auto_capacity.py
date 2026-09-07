"""视频在途水位：通道要能靠交付升档，项目/本集上限默认不再手填（2026-09-07 用户实测「任务卡三小时」）。

根因是我们自己的并发，不是供应商：B 库 395 个已完成任务提交→完成 p50 7.8 分钟、p99 20.0、最长
21.1，而 ``video_inflight`` 通道只被 ``channel_limit`` 读取、没有任何调用点上报成败，慢启动永远
停在热启动值 15；项目/本集上限又是手填的 24/15。550 个镜头因此排了三个多小时。
"""
from __future__ import annotations

from app.media_pipeline import concurrency, stages as S


def _fresh(monkeypatch, *, setting: str = "0") -> None:
    monkeypatch.setattr(concurrency, "_channels", {})
    monkeypatch.setattr(concurrency, "get_setting", lambda _key: setting)


def test_delivery_reports_let_the_inflight_channel_grow(monkeypatch) -> None:
    _fresh(monkeypatch)
    state = concurrency.ensure_channel(S.RESOURCE_VIDEO_INFLIGHT)
    assert state.auto and state.current == concurrency.CHANNEL_DEFAULTS[S.RESOURCE_VIDEO_INFLIGHT]
    start = state.current
    now = [1_000.0]
    monkeypatch.setattr(concurrency.time, "time", lambda: now[0])
    concurrency.report_video_delivered()  # 第一次只记起点
    assert concurrency.channel_limit(S.RESOURCE_VIDEO_INFLIGHT) == start
    now[0] += concurrency.AUTO_HEALTHY_INTERVAL_S + 1
    concurrency.report_video_delivered()
    assert concurrency.channel_limit(S.RESOURCE_VIDEO_INFLIGHT) == start * 2  # 慢启动翻倍


def test_submit_congestion_downgrades_both_channels(monkeypatch) -> None:
    _fresh(monkeypatch)
    before = {r: concurrency.channel_limit(r) for r in (S.RESOURCE_VIDEO_SUBMIT, S.RESOURCE_VIDEO_INFLIGHT)}
    concurrency.report_video_submit_congestion(reason="submit")  # 一次拥塞只是证据，两次才降档
    for resource, was in before.items():
        assert concurrency.channel_limit(resource) == was, resource
    concurrency.report_video_submit_congestion(reason="submit")
    for resource, was in before.items():
        assert concurrency.channel_limit(resource) < was, resource


def test_zero_setting_means_the_global_channel_governs(monkeypatch) -> None:
    from app.media_pipeline import retry_policy

    _fresh(monkeypatch)
    monkeypatch.setattr(retry_policy, "get_setting", lambda _key: "0")
    expected = concurrency.channel_limit(S.RESOURCE_VIDEO_INFLIGHT)
    assert retry_policy.episode_inflight_cap() == expected
    assert retry_policy.project_inflight_cap() == expected


def test_explicit_number_is_still_honoured(monkeypatch) -> None:
    from app.media_pipeline import retry_policy

    _fresh(monkeypatch)
    monkeypatch.setattr(retry_policy, "get_setting", lambda _key: "7")
    assert retry_policy.episode_inflight_cap() == 7 and retry_policy.project_inflight_cap() == 7


def test_auto_side_is_not_rejected_by_the_cross_field_check() -> None:
    """一侧自动（0）时不该再比大小：本集手填 20、项目自动，曾被误判成「项目上限低于单集上限」。"""
    from app import monitoring

    current = {"episode_video_inflight_limit": "0", "project_video_inflight_limit": "0"}
    assert monitoring.validate_settings_patch({"episode_video_inflight_limit": "20"}, current)
    assert monitoring.validate_settings_patch({"project_video_inflight_limit": "0"}, current)
