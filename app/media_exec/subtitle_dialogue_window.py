"""字幕闸门：有声时段 + 片尾加密补抽帧（2026-10-01）。

L5，与 ``app.media_exec.subtitle_gate`` 同层、同包，不单独声明层号（遵循
``app.media_exec`` 包级声明，见该模块与 ``character_count_dense_recheck.py``
的既有先例）。

背景（第四轮逐帧复查，``ep_a3c61162b4ce`` 第 8 段 ``ver_3b87f72d6806``，A 上
``/tmp/ep1audit5/r4/s08_6806.mp4``）：画面最后约 0.6 秒（14.3-14.9 秒，片长
15.07 秒）温念说「还给我」时烧出白色描边字幕，字幕闸门没拦住。根因是
``subtitle_gate.sample_frames`` 按 ``FRAME_INTERVAL_S``（1.5 秒）均匀抽样，
10 帧覆盖到约 13.5 秒就因 ``MAX_FRAMES=12`` 截止，最后 1.4 秒整段都没抽到
帧——字幕只在说话时才出现，稀疏抽样的「抽样间隙」与「台词窗口」一旦错开就
是结构性漏判，不是运气问题。``character_count_gate`` 的「单帧疑点→加密复核」
两阶段模式在这里不适用：那套机制靠首轮至少命中一帧疑点才触发第二轮加密抽帧
（见 ``character_count_dense_recheck`` 模块文档），而这里首轮可能一帧都没碰到
台词窗口，没有疑点信号可供触发。

判据从数据推导：不猜「台词大概在哪」，而是用 ffmpeg ``silencedetect`` 从成片
自己的音轨量出有声（非静音）区间，这正是字幕唯一可能出现的时段。在每个有声
区间内按 ``DENSE_INTERVAL_S`` 补抽帧，并额外保证片尾 ``TAIL_COVERAGE_S`` 秒内
至少有一帧——独立于某个具体有声区间，因为 ``silencedetect`` 对渐弱收尾的音频
判停顿可能略早于画面字幕实际消失的时刻（压缩降噪、淡出）。

阈值依据：
    - ``SILENCE_NOISE_DB``/``SILENCE_MIN_D``：复用 ``app.voice.clipping`` 同一套
      生产已验证阈值（-35dB、0.3 秒），不另造一套经验值。
    - ``DENSE_INTERVAL_S=0.3``：本次漏判样本「还给我」持续约 0.6 秒，0.3 秒一帧
      保证任何 ≥0.3 秒的台词窗口至少命中 1 帧；是稀疏抽样 1.5 秒间隔的 1/5。
    - ``MAX_DENSE_FRAMES=8``：段固定 15 秒（``storyboard_beat_sheet_schemas.
      SEGMENT_DURATION_S``），最坏情形（整段近乎连续说话）按 0.3 秒间隔可测出
      40+ 候选点，必须封顶避免把单次 VLM 调用的图片数撑爆；8 帧令补抽后合计
      最多 20 帧（12+8），仍在字幕闸门已标定可用的单次调用范围量级内（见
      ``subtitle_gate`` 模块文档「标定」一节，原 12 帧单次调用已验证）。候选点
      超过上限时优先保留离片尾最近的时间点——本次失败样本与「结尾戛然而止的
      台词」是同一类故障，排在前面最先被保留。

取舍：探测/抽帧任一步失败都不影响首轮稀疏帧已经抽到的判定能力——本模块只是
「锦上添花」的补充信号，不是闸门唯一的输入来源。与
``character_count_dense_recheck``「加密复核失败按未判定不拦、原判保留」同一
容错取舍：异常在本模块内部吞掉，不向上抛，只把 ``frames_added``/``error``/
``reason`` 写进返回的 trace 字典供人工核对（调用方把它并进 ``qa_json`` 的
``dialogue_dense_sampling`` 键，见 ``subtitle_gate.detect_subtitle_overlay``）。
纯静音视频（探测不到任何有声区间）不补抽任何帧，不多发 ffmpeg 调用。

修复记录（2026-10-01 代码审查发现，均用 ``/tmp/ep1audit5/r4/s08_6806.mp4`` 与
构造样本实测复现后修复，非假设）：
    - **容器时长陷阱**：最初版本把片尾安全点算在 ``silencedetect`` 报出的容器/
      音轨 ``Duration``（该样本 15.072s）上，而视频流自身只能解码到约 15.00s
      （ffprobe ``stream=duration`` 为 15.041667s）——这是本仓已知的编码现象
      （``app.final_edit._probe_media`` 同一问题的注释）。落在两者之间的时间点
      用 ``ffmpeg -ss`` 抽帧会退出码 0 但不产生任何文件，``_extract_frames_at``
      的 try/except 抓不到任何异常，只是静默少一帧；更糟的是这类点离片尾最近，
      会被「优先保留离片尾最近」的截断逻辑优先留下、挤掉真正能解出帧的中段候选
      点。现改为 ``_probe_stream_info`` 用 ffprobe 单独探测视频流自身时长，
      ``dense_sample_times`` 的安全上界基于这个时长而不是容器时长，并把安全余量
      从原来的 0.01/0.05 秒放宽到 ``TAIL_FRAME_SAFETY_MARGIN_S``（0.15 秒，本样本
      实测两者只差约 0.03-0.04 秒，取约 4-5 倍余量）。
    - **无音轨场景**：完全没有音轨的视频，ffmpeg 对 ``-af silencedetect`` 直接
      忽略、不产生任何 ``silence_start``/``silence_end`` 事件，但 ``Duration`` 行
      仍正常输出——``speech_intervals`` 单凭这段文本无法区分「真的全程有声」与
      「压根没有音轨」，会把整段时长当成持续有声，白白触发最多
      ``MAX_DENSE_FRAMES`` 次补抽。「无音轨」在本仓是已有专门处理的一等场景
      （``has_audio`` 贯穿 ``app.final_edit``/``app.media_exec.concat``/
      ``app.subtitles.episode``），不是边界情况。现在 ``dense_dialogue_frames``
      先用 ``_probe_stream_info`` 探测是否存在音轨，无音轨直接跳过补抽，不再
      依赖 ``speech_intervals`` 的返回值做这个判断。
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from . import subtitle_gate

_LOGGER = logging.getLogger(__name__)

#: 与 app.voice.clipping 同一套生产已验证阈值，不另造一套经验值。
SILENCE_NOISE_DB = "-35dB"
SILENCE_MIN_D = 0.3

DENSE_INTERVAL_S = 0.3
TAIL_COVERAGE_S = 1.0
MAX_DENSE_FRAMES = 8
FFMPEG_TIMEOUT_S = 30.0
#: 视频流可解码时长与容器/音轨时长实测可相差 0.03-0.04 秒（见模块文档「修复
#: 记录」），片尾安全点必须退够这个余量，否则 ffmpeg -ss 落进已经没有可解码帧
#: 的区间，退出码 0 但静默不出帧。
TAIL_FRAME_SAFETY_MARGIN_S = 0.15

_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_SILENCE_EVENT_RE = re.compile(r"silence_(start|end):\s*([0-9]+(?:\.[0-9]+)?)")


def _probe_duration_and_silence(video_path: str) -> tuple[float, str] | None:
    """跑一次 ``silencedetect`` 拿到时长与静音事件文本；探测失败（ffmpeg 缺失/
    超时/读不到 ``Duration``）返回 ``None``，调用方据此按「无法补抽」处理，不
    是报错。"""
    try:
        proc = subprocess.run(
            ["ffmpeg", "-nostdin", "-i", video_path,
             "-af", f"silencedetect=noise={SILENCE_NOISE_DB}:d={SILENCE_MIN_D}", "-f", "null", "-"],
            capture_output=True, text=True, timeout=FFMPEG_TIMEOUT_S, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        _LOGGER.warning("[VIDEO_SUBTITLE_GATE][有声时段探测失败] %s：%s", video_path, exc)
        return None
    stderr = proc.stderr or ""
    match = _DURATION_RE.search(stderr)
    if not match:
        return None
    hours, minutes, seconds = match.groups()
    duration = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    return duration, stderr


def _probe_stream_info(video_path: str) -> dict[str, Any] | None:
    """ffprobe 探测视频流自身可解码时长与是否存在音轨。独立于
    ``_probe_duration_and_silence``（那个读到的是容器/音轨 ``Duration``，两者
    可能不等，见模块文档「修复记录」）；与 ``app.final_edit._probe_media`` 同
    一技术手法，这里不跨层导入那个私有函数、独立实现一份。探测失败（ffmpeg
    缺失/超时/非法输出）返回 ``None``，调用方按「无法补抽」处理，不是报错。
    """
    try:
        raw = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "format=duration:stream=codec_type,duration", "-of", "json", video_path],
            capture_output=True, text=True, timeout=FFMPEG_TIMEOUT_S, check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        _LOGGER.warning("[VIDEO_SUBTITLE_GATE][视频流探测失败] %s：%s", video_path, exc)
        return None
    try:
        payload = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        _LOGGER.warning("[VIDEO_SUBTITLE_GATE][视频流探测输出非法] %s：%s", video_path, exc)
        return None
    streams = payload.get("streams") or []
    try:
        container_duration = float((payload.get("format") or {}).get("duration") or 0)
    except (TypeError, ValueError):
        container_duration = 0.0
    video_duration = 0.0
    for stream in streams:
        if isinstance(stream, dict) and stream.get("codec_type") == "video":
            try:
                video_duration = float(stream.get("duration") or 0)
            except (TypeError, ValueError):
                video_duration = 0.0
            break
    has_audio = any(isinstance(s, dict) and s.get("codec_type") == "audio" for s in streams)
    return {"video_duration_s": video_duration or container_duration, "has_audio": has_audio}


def speech_intervals(duration: float, stderr: str) -> list[tuple[float, float]]:
    """从 ``silencedetect`` 的事件文本反推有声（非静音）区间：相邻静音段之间的
    间隙，加上首段静音前（若静音不是从 0 秒开始）与末段静音后（若视频在静音前
    结束）。事件按文本出现顺序即时间顺序，不需要额外排序。全程静音（从未出现
    ``silence_end``）返回空列表——纯静音视频没有台词，调用方据此不补抽，见模块
    文档「取舍」。

    完全没有任何 ``silence_start``/``silence_end`` 事件时返回 ``[(0, duration)]``
    （视为持续有声），这包含两种本函数单凭这段文本无法区分的情况：「真的全程都
    有声」与「压根没有音轨」（ffmpeg 对无音轨输入直接忽略 ``-af silencedetect``
    滤镜，不报错也不产生任何事件，见模块文档「修复记录」）。调用方
    （``dense_dialogue_frames``）必须在调用本函数之前用 ``_probe_stream_info``
    单独判断是否存在音轨，不要依赖本函数的返回值做这个判断。
    """
    cursor = 0.0
    silence_open = False
    speech: list[tuple[float, float]] = []
    for kind, raw in _SILENCE_EVENT_RE.findall(stderr):
        t = float(raw)
        if kind == "start":
            if t > cursor:
                speech.append((cursor, t))
            silence_open = True
        else:
            cursor = t
            silence_open = False
    if not silence_open and cursor < duration:
        speech.append((cursor, duration))
    return speech


def dense_sample_times(duration: float, speech: list[tuple[float, float]]) -> list[float]:
    """有声区间内按 ``DENSE_INTERVAL_S`` 补抽的时间点，外加片尾
    ``TAIL_COVERAGE_S`` 秒内至少一个点；候选点超过 ``MAX_DENSE_FRAMES`` 时优先
    保留离片尾最近的，理由见模块文档。``speech`` 为空（纯静音）直接返回空列表。

    ``duration`` 必须是对 ``ffmpeg -ss`` 精确抽帧安全的时长上界——调用方须传入
    视频流自身可解码时长（``_probe_stream_info`` 的 ``video_duration_s``），
    **不要**传容器/音轨 duration：两者可能不等（见模块文档「修复记录」），用
    容器 duration 算出的片尾点会落进视频流已经没有可解码帧的区间，静默抽不出
    帧。返回的所有时间点都退够 ``TAIL_FRAME_SAFETY_MARGIN_S`` 安全余量。
    """
    if not speech:
        return []
    cap = max(0.0, duration - TAIL_FRAME_SAFETY_MARGIN_S)
    times: set[float] = set()
    for start, end in speech:
        if start > cap:
            continue
        clamped_end = min(end, cap)
        t = start
        while t < clamped_end:
            times.add(round(t, 3))
            t += DENSE_INTERVAL_S
        times.add(round(clamped_end, 3))
    times.add(round(cap, 3))
    ordered = sorted(t for t in times if 0.0 <= t <= cap)
    if len(ordered) > MAX_DENSE_FRAMES:
        ordered = sorted(sorted(ordered, key=lambda t: cap - t)[:MAX_DENSE_FRAMES])
    return ordered


def _extract_frames_at(video_path: str, times: list[float]) -> list[bytes]:
    """逐个时间点各跑一次 ffmpeg 精确寻址抽 1 帧（``-ss`` 放在 ``-i`` 之后，
    与 ``character_count_dense_recheck`` 同一取舍：这个粒度下快速寻址不够准，
    且补抽只在探测到有声区间时触发，不频繁，可以牺牲一点速度换准确）。单个
    时间点抽取失败只丢弃这一帧，不影响其它时间点。
    """
    frames: list[bytes] = []
    for at_s in times:
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "f.jpg"
            try:
                subprocess.run(
                    ["ffmpeg", "-y", "-loglevel", "error", "-i", video_path, "-ss", f"{at_s:.3f}",
                     "-vf", f"scale={subtitle_gate.FRAME_WIDTH}:-2", "-frames:v", "1", "-q:v", "4", str(out)],
                    check=True, capture_output=True, timeout=FFMPEG_TIMEOUT_S,
                )
            except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
                _LOGGER.warning("[VIDEO_SUBTITLE_GATE][补抽单帧失败] %.3fs：%s", at_s, exc)
                continue
            if out.is_file() and out.stat().st_size > 0:
                frames.append(out.read_bytes())
    return frames


def dense_dialogue_frames(video_path: str) -> tuple[list[bytes], dict[str, Any]]:
    """有声时段 + 片尾补抽帧的唯一对外入口。返回 ``(frames, trace)``：``trace``
    不论成败都存在，写进调用方 verdict 的 ``dialogue_dense_sampling`` 键供人工
    核对；任何一步异常都在本函数内吞掉返回空帧列表，不向上抛，见模块文档
    「取舍」。无音轨视频直接不补抽；补抽安全上界用视频流自身可解码时长，不用
    容器/音轨时长（两个修复点见模块文档「修复记录」）。
    """
    try:
        stream_info = _probe_stream_info(video_path)
        if stream_info is None:
            return [], {"frames_added": 0, "reason": "无法探测视频流信息，跳过补抽"}
        if not stream_info["has_audio"]:
            return [], {"frames_added": 0, "reason": "视频无音轨，不补抽"}
        probed = _probe_duration_and_silence(video_path)
        if probed is None:
            return [], {"frames_added": 0, "reason": "无法探测音轨或时长，跳过补抽"}
        duration, stderr = probed
        times = dense_sample_times(stream_info["video_duration_s"], speech_intervals(duration, stderr))
        if not times:
            return [], {"frames_added": 0, "reason": "全程无有声区间，未补抽"}
        frames = _extract_frames_at(video_path, times)
        trace: dict[str, Any] = {"frames_added": len(frames), "times_requested": len(times), "times": times}
        if len(frames) < len(times):
            _LOGGER.warning(
                "[VIDEO_SUBTITLE_GATE][补抽帧部分丢失] %s：请求 %d 帧，实际抽到 %d 帧",
                video_path, len(times), len(frames),
            )
        return frames, trace
    except Exception as exc:  # noqa: BLE001 补抽失败不影响首轮稀疏帧的判定能力
        _LOGGER.warning("[VIDEO_SUBTITLE_GATE][补抽帧失败] %s：%s", video_path, exc)
        return [], {"frames_added": 0, "error": f"{type(exc).__name__}: {exc}"[:300]}
