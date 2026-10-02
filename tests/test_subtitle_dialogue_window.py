"""字幕闸门：有声时段 + 片尾加密补抽帧（2026-10-01）。

真实故障复现（第四轮逐帧复查 ``ep_a3c61162b4ce`` 第 8 段 ``ver_3b87f72d6806``）：
稀疏抽样（``subtitle_gate.FRAME_INTERVAL_S``=1.5 秒一帧）覆盖不到片长最后约 1.4
秒，画面在 14.3-14.9 秒烧出的字幕整段落在抽样间隙里，首轮判「无字幕」。本文件
覆盖 ``app.media_exec.subtitle_dialogue_window`` 的纯函数（``speech_intervals``/
``dense_sample_times``）与 ``dense_dialogue_frames`` 的三种真实路径（探测成功、
纯静音不补抽、探测/抽帧失败放行留痕），以及接上 ``subtitle_gate.
detect_subtitle_overlay`` 后的端到端行为（首轮漏判、补抽帧命中字幕 → 判定拦截）。

夹具复用 ``tests/test_video_subtitle_gate.py`` 同款 ``evaluate_version`` 桩法
（``enabled``/``write_verdict``/``spoken_lines_for_shot``）；VLM 调用桩到
``app.hiagent.chat`` 这一层，让 ``detect_subtitle_overlay`` 的合并抽帧逻辑真实
运行，才算验到接线本身（同 ``tests/test_character_count_dense_recheck.py`` 的
桩法）。

2026-10-01 代码审查新增两组真实 ffmpeg 回归用例（``_probe_stream_info`` 相关），
复现并锁住两个曾经静默发生的缺陷：容器/音轨时长与视频流可解码时长不一致导致
片尾安全点静默抽不出帧；无音轨视频被当成「持续有声」误触发补抽。两组都用
``ffmpeg lavfi`` 现场生成最小样本，不依赖仓库外的调试产物，与
``tests/test_final_edit.py`` 的 ``_source_clip`` 同一手法。
"""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from app import hiagent
from app.media_exec import subtitle_dialogue_window as dense
from app.media_exec import subtitle_gate

_FFMPEG_AVAILABLE = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
_SKIP_NO_FFMPEG = pytest.mark.skipif(not _FFMPEG_AVAILABLE, reason="ffmpeg/ffprobe unavailable")


# ---------------------------------------------------------------------------
# speech_intervals：纯函数，喂 silencedetect 的事件文本
# ---------------------------------------------------------------------------

def _stderr(*, duration: str, events: list[tuple[str, float]]) -> str:
    lines = [f"  Duration: {duration}, start: 0.000000, bitrate: 1000 kb/s"]
    lines += [f"[silencedetect] silence_{kind}: {t}" for kind, t in events]
    return "\n".join(lines)


def test_speech_intervals_fills_gaps_between_silences() -> None:
    """真实样本形状（s08_6806.mp4 实测）：末段静音结束后到片尾是一段有声区间，
    正是本次漏判的 14.3-14.9 秒所在窗口。"""
    stderr = _stderr(duration="00:00:15.07", events=[
        ("start", 0.0), ("end", 5.84262),
        ("start", 6.03969), ("end", 7.90572),
        ("start", 9.62884), ("end", 14.3496),
    ])
    speech = dense.speech_intervals(15.07, stderr)
    assert speech == [(5.84262, 6.03969), (7.90572, 9.62884), (14.3496, 15.07)]


def test_speech_intervals_fully_silent_video_yields_nothing() -> None:
    stderr = _stderr(duration="00:00:10.00", events=[("start", 0.0)])
    assert dense.speech_intervals(10.0, stderr) == []


def test_speech_intervals_no_silence_events_means_fully_voiced() -> None:
    stderr = _stderr(duration="00:00:05.00", events=[])
    assert dense.speech_intervals(5.0, stderr) == [(0.0, 5.0)]


# ---------------------------------------------------------------------------
# dense_sample_times：纯函数，有声区间 → 补抽时间点
# ---------------------------------------------------------------------------

def test_dense_sample_times_empty_speech_adds_nothing() -> None:
    assert dense.dense_sample_times(15.0, []) == []


def test_dense_sample_times_covers_tail_window_and_caps_at_limit() -> None:
    speech = [(5.84262, 6.03969), (7.90572, 9.62884), (14.3496, 15.07)]
    times = dense.dense_sample_times(15.07, speech)
    assert len(times) <= dense.MAX_DENSE_FRAMES
    assert any(14.3 <= t <= 14.9 for t in times), "必须补到本次漏判的真实窗口"
    assert times == sorted(times)


def test_dense_sample_times_prefers_points_closest_to_tail_when_over_cap() -> None:
    """有声区间几乎贯穿全程时候选点远超上限，必须优先保留离片尾最近的——与本次
    失败样本「结尾戛然而止的台词」同一类故障，排在前面最先保留。"""
    times = dense.dense_sample_times(15.0, [(0.0, 15.0)])
    assert len(times) == dense.MAX_DENSE_FRAMES
    assert max(times) > 14.0


# ---------------------------------------------------------------------------
# dense_dialogue_frames：探测成功 / 纯静音不补抽 / 失败放行留痕
# ---------------------------------------------------------------------------

_STREAM_INFO_VOICED = {"video_duration_s": 15.07, "has_audio": True}


def test_dense_dialogue_frames_extracts_at_computed_times(monkeypatch) -> None:
    monkeypatch.setattr(dense, "_probe_stream_info", lambda path: dict(_STREAM_INFO_VOICED))
    monkeypatch.setattr(dense, "_probe_duration_and_silence", lambda path: (15.07, "stub"))
    monkeypatch.setattr(dense, "speech_intervals", lambda duration, stderr: [(14.3496, 15.07)])
    extracted: list[list[float]] = []
    monkeypatch.setattr(dense, "_extract_frames_at", lambda path, times: extracted.append(times) or [b"f"] * len(times))
    frames, trace = dense.dense_dialogue_frames("/tmp/v.mp4")
    assert len(frames) == len(extracted[0]) > 0
    assert trace["frames_added"] == len(frames)
    assert trace["times_requested"] == len(extracted[0])
    assert "error" not in trace


def test_dense_dialogue_frames_silent_video_does_not_extract(monkeypatch) -> None:
    monkeypatch.setattr(dense, "_probe_stream_info", lambda path: dict(_STREAM_INFO_VOICED))
    monkeypatch.setattr(dense, "_probe_duration_and_silence", lambda path: (10.0, "stub"))
    monkeypatch.setattr(dense, "speech_intervals", lambda duration, stderr: [])

    def _must_not_call(path, times):
        raise AssertionError("纯静音视频不该补抽帧")

    monkeypatch.setattr(dense, "_extract_frames_at", _must_not_call)
    frames, trace = dense.dense_dialogue_frames("/tmp/v.mp4")
    assert frames == [] and trace["frames_added"] == 0 and "error" not in trace


def test_dense_dialogue_frames_probe_failure_passes_through_with_trace(monkeypatch) -> None:
    monkeypatch.setattr(dense, "_probe_stream_info", lambda path: dict(_STREAM_INFO_VOICED))

    def _boom(path):
        raise TimeoutError("ffmpeg 探测超时")

    monkeypatch.setattr(dense, "_probe_duration_and_silence", _boom)
    frames, trace = dense.dense_dialogue_frames("/tmp/v.mp4")
    assert frames == []
    assert "TimeoutError" in trace["error"]


def test_dense_dialogue_frames_probe_returns_none_passes_through(monkeypatch) -> None:
    monkeypatch.setattr(dense, "_probe_stream_info", lambda path: dict(_STREAM_INFO_VOICED))
    monkeypatch.setattr(dense, "_probe_duration_and_silence", lambda path: None)
    frames, trace = dense.dense_dialogue_frames("/tmp/v.mp4")
    assert frames == [] and trace["frames_added"] == 0 and "error" not in trace


def test_dense_dialogue_frames_stream_probe_failure_skips_entirely(monkeypatch) -> None:
    """#0 复核：视频流探测本身失败（ffprobe 缺失/超时）也要按「无法补抽」放行，
    不该继续往下跑到 silencedetect 那一步。"""
    monkeypatch.setattr(dense, "_probe_stream_info", lambda path: None)

    def _must_not_call(path):
        raise AssertionError("视频流探测失败时不该再探测音轨")

    monkeypatch.setattr(dense, "_probe_duration_and_silence", _must_not_call)
    frames, trace = dense.dense_dialogue_frames("/tmp/v.mp4")
    assert frames == [] and trace["frames_added"] == 0 and "error" not in trace


def test_dense_dialogue_frames_no_audio_skips_silencedetect_entirely(monkeypatch) -> None:
    """#1 复核：无音轨直接按 has_audio 短路跳过，不依赖 speech_intervals 的
    「无事件→持续有声」这个在无音轨场景下会说谎的行为。"""
    monkeypatch.setattr(dense, "_probe_stream_info", lambda path: {"video_duration_s": 2.0, "has_audio": False})

    def _must_not_call(path):
        raise AssertionError("无音轨不该再探测静音事件")

    monkeypatch.setattr(dense, "_probe_duration_and_silence", _must_not_call)
    frames, trace = dense.dense_dialogue_frames("/tmp/v.mp4")
    assert frames == [] and trace["frames_added"] == 0 and "error" not in trace
    assert "音轨" in trace["reason"]


# ---------------------------------------------------------------------------
# 真实 ffmpeg 回归：容器/音轨时长 ≠ 视频流可解码时长；无音轨视频
# ---------------------------------------------------------------------------

def _video_only_clip(path, *, duration_s: float) -> None:
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
         "-i", f"color=c=blue:s=64x64:r=24:d={duration_s}",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)],
        check=True, capture_output=True, timeout=30,
    )


def _mux_with_overlong_audio(video_path, out_path, *, audio_duration_s: float, voiced: bool) -> None:
    """按真实漏判样本的形状构造一个「音轨比视频流长」的文件：视频轨只有
    ``_video_only_clip`` 那么长，音轨单独生成到 ``audio_duration_s`` 再原样封装，
    不加 ``-shortest``——这正是本仓实测过的编码现象（见模块文档「修复记录」），
    不是臆造的边界输入。``voiced=True`` 用正弦波而非静音，使 silencedetect 判定
    全程有声，走到补抽这条路径上。
    """
    with tempfile.NamedTemporaryFile(suffix=".aac", delete=False) as tmp:
        audio_path = tmp.name
    try:
        source = f"sine=frequency=440:sample_rate=48000" if voiced else "anullsrc=r=48000:cl=stereo"
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", source,
             "-t", str(audio_duration_s), "-c:a", "aac", audio_path],
            check=True, capture_output=True, timeout=30,
        )
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", str(video_path), "-i", audio_path,
             "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "copy", str(out_path)],
            check=True, capture_output=True, timeout=30,
        )
    finally:
        Path(audio_path).unlink(missing_ok=True)


@_SKIP_NO_FFMPEG
def test_probe_stream_info_uses_video_stream_duration_not_container(tmp_path) -> None:
    """#0 复核：ffprobe 视频流 duration 与容器/音轨 duration 实测可不等
    （真实漏判样本两者差约 0.03 秒），``_probe_stream_info`` 必须报视频流那个，
    不是容器那个——否则片尾安全点算出来的基准本身就是错的。"""
    video_only = tmp_path / "video_only.mp4"
    mismatched = tmp_path / "mismatched.mp4"
    _video_only_clip(video_only, duration_s=1.0)
    _mux_with_overlong_audio(video_only, mismatched, audio_duration_s=1.3, voiced=False)
    info = dense._probe_stream_info(str(mismatched))
    assert info is not None
    assert info["has_audio"] is True
    assert info["video_duration_s"] < 1.1, "必须报视频流时长（约 1.0s），不是容器时长（约 1.3s）"


@_SKIP_NO_FFMPEG
def test_probe_stream_info_detects_missing_audio_track(tmp_path) -> None:
    """#1 复核：真的没有音轨（不是静音），``_probe_stream_info`` 必须报
    ``has_audio=False``——这是本函数存在的意义，``speech_intervals`` 单凭
    stderr 文本做不到。"""
    video_only = tmp_path / "video_only.mp4"
    _video_only_clip(video_only, duration_s=1.0)
    info = dense._probe_stream_info(str(video_only))
    assert info is not None
    assert info["has_audio"] is False


@_SKIP_NO_FFMPEG
def test_dense_dialogue_frames_no_audio_track_does_not_dense_sample(tmp_path) -> None:
    """#1 复核端到端：真实无音轨文件喂给唯一对外入口，不触发任何补抽。"""
    video_only = tmp_path / "video_only.mp4"
    _video_only_clip(video_only, duration_s=1.0)
    frames, trace = dense.dense_dialogue_frames(str(video_only))
    assert frames == [] and trace["frames_added"] == 0
    assert "音轨" in trace["reason"]


@_SKIP_NO_FFMPEG
def test_dense_dialogue_frames_container_audio_overrun_does_not_silently_drop_frames(tmp_path) -> None:
    """#0 复核端到端：音轨比视频流长且全程有声（真实漏判样本同一形状）时，补抽
    请求的每个时间点都必须真的抽出帧——修复前，片尾安全点算在容器/音轨时长上会
    落进视频流已经解码不出帧的区间，``frames_added`` 会悄悄小于请求的点数。
    """
    video_only = tmp_path / "video_only.mp4"
    mismatched = tmp_path / "mismatched_voiced.mp4"
    _video_only_clip(video_only, duration_s=1.0)
    _mux_with_overlong_audio(video_only, mismatched, audio_duration_s=1.3, voiced=True)
    frames, trace = dense.dense_dialogue_frames(str(mismatched))
    assert trace["frames_added"] == trace["times_requested"] > 0, (
        "请求的补抽时间点必须全部真的抽出帧，不能静默丢帧"
    )
    assert all(t <= 1.0 for t in trace["times"]), "补抽点不能落进视频流已经没有帧的容器尾部"


# ---------------------------------------------------------------------------
# 端到端：subtitle_gate.evaluate_version 接上补抽帧之后
# ---------------------------------------------------------------------------

_SPARSE_COUNT = 5


def _wire(monkeypatch, *, dense_frames, dense_trace, chat):
    monkeypatch.setattr(subtitle_gate, "enabled", lambda: True)
    monkeypatch.setattr(subtitle_gate, "sample_frames", lambda video_path: [b"s"] * _SPARSE_COUNT)
    monkeypatch.setattr(subtitle_gate, "spoken_lines_for_shot", lambda shot_id: None)
    monkeypatch.setattr(dense, "dense_dialogue_frames", lambda video_path: (dense_frames, dense_trace))
    monkeypatch.setattr(hiagent, "chat", chat)
    written: list[tuple[str, dict]] = []
    monkeypatch.setattr(subtitle_gate, "write_verdict", lambda vid, verdict: written.append((vid, verdict)))
    return written


def _run() -> dict:
    job = {"project_id": "p", "shot_id": "s08"}
    return asyncio.run(subtitle_gate.evaluate_version(job, {"id": "ver_3b87f72d6806"}, "/tmp/s08_6806.mp4"))


def _chat_hit_last_frame(messages, *, call_meta=None, **_kwargs):
    frame_count = len(messages[1]["content"]) - 1
    frames = [
        {"index": i, "overlay_text": i == frame_count, "text_seen": "还给我" if i == frame_count else "", "where": "画面下方"}
        for i in range(1, frame_count + 1)
    ]
    return json.dumps({"frames": frames}, ensure_ascii=False)


async def _chat_hit_last_frame_async(messages, *, call_meta=None, **_kwargs):
    return _chat_hit_last_frame(messages, call_meta=call_meta, **_kwargs)


def test_dense_frame_catches_subtitle_missed_by_sparse_pass(monkeypatch) -> None:
    """首轮稀疏帧干净，补抽的最后一帧命中字幕 → 整体判定拦截，frames_checked
    必须计入补抽帧，dialogue_dense_sampling 留痕补了几帧。"""
    written = _wire(
        monkeypatch, dense_frames=[b"dense1"], dense_trace={"frames_added": 1, "times": [14.65]},
        chat=_chat_hit_last_frame_async,
    )
    result = _run()
    assert result["subtitle_overlay"] is True
    assert result["frames_checked"] == _SPARSE_COUNT + 1
    assert result["overlay_frames"][0]["text_seen"] == "还给我"
    assert result["dialogue_dense_sampling"] == {"frames_added": 1, "times": [14.65]}
    assert written == [("ver_3b87f72d6806", result)]


async def _chat_clean(messages, *, call_meta=None, **_kwargs):
    frame_count = len(messages[1]["content"]) - 1
    frames = [{"index": i, "overlay_text": False} for i in range(1, frame_count + 1)]
    return json.dumps({"frames": frames})


def test_silent_video_sends_only_sparse_frames_to_vlm(monkeypatch) -> None:
    calls: list[int] = []

    async def chat(messages, *, call_meta=None, **_kwargs):
        calls.append(len(messages[1]["content"]) - 1)
        return await _chat_clean(messages, call_meta=call_meta, **_kwargs)

    written = _wire(
        monkeypatch, dense_frames=[], dense_trace={"frames_added": 0, "reason": "全程无有声区间，未补抽"},
        chat=chat,
    )
    result = _run()
    assert calls == [_SPARSE_COUNT]
    assert result["frames_checked"] == _SPARSE_COUNT
    assert result["subtitle_overlay"] is False
    assert result["dialogue_dense_sampling"]["frames_added"] == 0
    assert written == [("ver_3b87f72d6806", result)]


def test_dense_sampling_failure_still_yields_judged_verdict_with_trace(monkeypatch) -> None:
    """补抽这一步失败（ffmpeg 探测挂了）不该把整条字幕闸门拖成「未判定」——
    首轮稀疏帧仍然正常送检判定，失败只在 dialogue_dense_sampling 里留痕。"""
    written = _wire(
        monkeypatch, dense_frames=[], dense_trace={"frames_added": 0, "error": "TimeoutError: ffmpeg 探测超时"},
        chat=_chat_clean,
    )
    result = _run()
    assert result["checked"] is True
    assert result["frames_checked"] == _SPARSE_COUNT
    assert result["subtitle_overlay"] is False
    assert "TimeoutError" in result["dialogue_dense_sampling"]["error"]
    assert written == [("ver_3b87f72d6806", result)]
