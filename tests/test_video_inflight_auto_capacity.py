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
    """攒够样本、确认交付没变慢之后，慢启动才开始翻倍。"""
    _fresh(monkeypatch)
    _reset_latency(monkeypatch)
    state = concurrency.ensure_channel(S.RESOURCE_VIDEO_INFLIGHT)
    assert state.auto and state.current == concurrency.CHANNEL_DEFAULTS[S.RESOURCE_VIDEO_INFLIGHT]
    start = state.current
    now = [1_000.0]
    monkeypatch.setattr(concurrency.time, "time", lambda: now[0])
    for _ in range(concurrency.LATENCY_SAMPLE_WINDOW // 2):
        concurrency.report_video_delivered(480.0)  # 攒基线期间按兵不动
    assert concurrency.channel_limit(S.RESOURCE_VIDEO_INFLIGHT) == start
    concurrency.report_video_delivered(480.0)  # 有基线后第一次只记健康起点
    now[0] += concurrency.AUTO_HEALTHY_INTERVAL_S + 1
    concurrency.report_video_delivered(480.0)
    assert concurrency.channel_limit(S.RESOURCE_VIDEO_INFLIGHT) == start * 2


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


def _reset_latency(monkeypatch) -> None:
    monkeypatch.setattr(concurrency, "_delivery_latencies", type(concurrency._delivery_latencies)(maxlen=concurrency.LATENCY_SAMPLE_WINDOW))
    monkeypatch.setattr(concurrency, "_best_delivery_median", None)


def test_latency_inflation_is_the_congestion_signal(monkeypatch) -> None:
    """供应商对超发不报错、只变慢：只认错误的 AIMD 会一路顶到安全阀（2026-09-07 实测 p50 7.8→24.8 分钟）。"""
    _reset_latency(monkeypatch)
    half = concurrency.LATENCY_SAMPLE_WINDOW // 2
    for _ in range(half - 1):
        assert concurrency.delivery_latency_verdict(480.0) == "unknown"  # 样本不足不表态
    assert concurrency.delivery_latency_verdict(480.0) == "healthy"  # 首次成样，记为最好基线
    for _ in range(concurrency.LATENCY_SAMPLE_WINDOW):
        verdict = concurrency.delivery_latency_verdict(1500.0)  # 中位数抬到 3 倍
    assert verdict == "congested"


def test_latency_back_at_baseline_is_healthy_again(monkeypatch) -> None:
    _reset_latency(monkeypatch)
    for _ in range(concurrency.LATENCY_SAMPLE_WINDOW):
        concurrency.delivery_latency_verdict(480.0)
    for _ in range(concurrency.LATENCY_SAMPLE_WINDOW):
        verdict = concurrency.delivery_latency_verdict(600.0)  # 1.25 倍，未越阈
    assert verdict == "healthy"


def test_delivery_report_downgrades_when_saturated(monkeypatch) -> None:
    _fresh(monkeypatch)
    _reset_latency(monkeypatch)
    monkeypatch.setattr(concurrency, "delivery_latency_verdict", lambda _d: "congested")
    before = concurrency.channel_limit(S.RESOURCE_VIDEO_INFLIGHT)
    concurrency.report_video_delivered(9999.0)
    concurrency.report_video_delivered(9999.0)  # 两次拥塞才降档
    assert concurrency.channel_limit(S.RESOURCE_VIDEO_INFLIGHT) < before


def test_channel_holds_still_until_a_real_baseline_exists(monkeypatch) -> None:
    """样本不足时既不升也不降：把「未知」当健康会让通道在基线成型前冲到安全阀，
    基线随之被记成饱和值，闭环永远收不紧（2026-09-07 实测顶到 128、单任务 7.8→19.9 分钟）。"""
    _fresh(monkeypatch)
    _reset_latency(monkeypatch)
    start = concurrency.channel_limit(S.RESOURCE_VIDEO_INFLIGHT)
    now = [1_000.0]
    monkeypatch.setattr(concurrency.time, "time", lambda: now[0])
    for _ in range(concurrency.LATENCY_SAMPLE_WINDOW // 2 - 1):
        now[0] += concurrency.AUTO_HEALTHY_INTERVAL_S + 1  # 就算时间足够升档也不许升
        concurrency.report_video_delivered(480.0)
    assert concurrency.channel_limit(S.RESOURCE_VIDEO_INFLIGHT) == start


def test_congestion_clears_the_window_so_it_does_not_cascade(monkeypatch) -> None:
    """降档后旧样本描述的是降档前的水位；不清空会连环降档一路砍到 1。"""
    _fresh(monkeypatch)
    _reset_latency(monkeypatch)
    monkeypatch.setattr(concurrency, "delivery_latency_verdict", lambda _d: "congested")
    concurrency._delivery_latencies.extend([1500.0] * concurrency.LATENCY_SAMPLE_WINDOW)
    concurrency.report_video_delivered(1500.0)
    assert not concurrency._delivery_latencies
