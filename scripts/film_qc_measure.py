#!/usr/bin/env python3
"""成片客观质检：可重复、可跨批次对比的量化测量工具。

背景：2026-09-23 只读子任务手工量了 5 部成片（脚本日志在 A 机 /tmp/mjscan_a2/），
发现接缝音量跳变 12~28dB、开场静止；同日主会话用真实成片验收又发现接缝电平差
受内容影响不能单独判音量。本脚本固化口径，供后续每批迭代对同一集重跑、逐项对比。

口径：freezedetect 默认 n=-50dB:d=1.0（ffmpeg 内置 -60dB 几乎测不出静止，对比见
/tmp/mjscan_a2/*_freeze50.log vs *_freeze60.log）；silencedetect 默认-35dB/0.8s；
接缝窗口前后各0.4s、跳变阈值6dB（书面约定，volumedetect mean_volume dBFS 差值，
非 LUFS）；分段音量一致性以 loudness_range_stats() 的极差/标准差为主指标，接缝
跳变只作受内容影响的辅助信号，见 SEAM_JUMP_NOTE。

分段重建（需 edit-report.json）：video_delivery_manifest.items 不落盘每镜真实
渲染时长（查源码 app/downstream_authority.py 确认），无法精确复原，改用「探测
总时长 + Σ接缝 transitions[].duration_s(xfade重叠)」反解等长估计值+scdet 就近
纠正（见 estimate_segment_bounds/refine_bounds_with_cuts）；无 edit-report.json
时分段类指标全部 None，不猜测。

用法示例：
    .venv/bin/python scripts/film_qc_measure.py /tmp/mjfix_w10/lm4.mp4 \\
        --edit-report /tmp/mjfix_w10/lm4.edit-report.json --srt /tmp/mjfix_w10/lm4.srt \\
        --json /tmp/mjfix_w10/lm4.qc.json
    # 生产机经 ssh mjb，加 --nice 避免抢占在途生成任务 CPU：
    ssh mjb "cd /root/MJAgent2 && .venv/bin/python scripts/film_qc_measure.py \\
        projects/proj_f4c8ca4a5775/episodes/4/final/episode.mp4 --nice --json /tmp/lm4.qc.json"

退出码：0=正常完成（个别子项可能仍带 error 字段）；2=输入不可读或 ffprobe 本身
失败（连规格都拿不到，视为致命）。
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import subprocess
import sys
from pathlib import Path
from typing import Any

DEFAULT_FREEZE_NOISE_DB = "-50dB"
DEFAULT_FREEZE_DURATION_S = 1.0
DEFAULT_SILENCE_NOISE_DB = -35.0
DEFAULT_SILENCE_DURATION_S = 0.8
SEAM_WINDOW_S = 0.4
SEAM_JUMP_THRESHOLD_DB = 6.0
OPENING_WINDOW_S = 3.0
BOUNDARY_SNAP_TOLERANCE_S = 1.0
DEFAULT_TIMEOUT_S = 600.0
# 2026-09-23 真实成片验收：接缝电平差受内容影响(静默→对白天然大)，不能单独判音量；
# 主指标见 loudness_range_stats()。
SEAM_JUMP_NOTE = "接缝前后0.4秒电平差（受内容影响：静默→对白也会很大，只作辅助，不单独判音量不一致）"

EXIT_OK = 0
EXIT_INPUT_ERROR = 2

_SCDET_RE = re.compile(r"lavfi\.scd\.score:\s*([\d.]+),\s*lavfi\.scd\.time:\s*([\d.]+)")
_FREEZE_START_RE = re.compile(r"lavfi\.freezedetect\.freeze_start:\s*([\d.]+)")
_FREEZE_DUR_RE = re.compile(r"lavfi\.freezedetect\.freeze_duration:\s*([\d.]+)")
_FREEZE_END_RE = re.compile(r"lavfi\.freezedetect\.freeze_end:\s*([\d.]+)")
_BLACK_RE = re.compile(r"black_start:([\d.]+)\s+black_end:([\d.]+)\s+black_duration:([\d.]+)")
_SILENCE_START_RE = re.compile(r"silence_start:\s*(-?[\d.]+)")
_SILENCE_END_RE = re.compile(r"silence_end:\s*([\d.]+)\s*\|\s*silence_duration:\s*([\d.]+)")
_LOUD_I_RE = re.compile(r"I:\s*(-?[\d.]+) LUFS")
_LOUD_LRA_RE = re.compile(r"LRA:\s*([\d.]+) LU")
_LOUD_PEAK_RE = re.compile(r"Peak:\s*(-?[\d.]+) dBFS")
_MEAN_VOL_RE = re.compile(r"mean_volume:\s*(-?[\d.]+) dB")
_SRT_CUE_RE = re.compile(r"(\d{2}):(\d{2}):(\d{2}),(\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2}),(\d{3})")

class QcError(RuntimeError):
    """输入不可读或 ffprobe 本身失败时抛出；main() 捕获后落成退出码 2。"""

# subprocess 基础设施

def _cmd(args: list[str], nice: bool) -> list[str]:
    """按需在命令前加 nice -n 19——生产机上跑质检时不与在途生成任务抢 CPU。"""
    return ["nice", "-n", "19", *args] if nice else args

def _run_text(args: list[str], *, nice: bool, timeout: float) -> str:
    """跑一个 ffmpeg 子进程，合并 stdout+stderr 为文本；调用方自行正则解析。"""
    proc = subprocess.run(
        _cmd(args, nice), capture_output=True, text=True, timeout=timeout, check=False,
    )
    return (proc.stdout or "") + (proc.stderr or "")

def ffprobe_json(path: Path, *, nice: bool, timeout: float) -> dict[str, Any]:
    """ffprobe -show_format -show_streams 的 JSON 结果；失败抛 QcError（退出码 2）。"""
    args = ["ffprobe", "-v", "error", "-print_format", "json",
            "-show_format", "-show_streams", str(path)]
    try:
        proc = subprocess.run(_cmd(args, nice), capture_output=True, text=True,
                               timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise QcError(f"ffprobe 无法执行：{exc}") from exc
    if proc.returncode != 0 or not proc.stdout.strip():
        raise QcError(f"ffprobe 失败（returncode={proc.returncode}）：{proc.stderr[:500]}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise QcError(f"ffprobe 输出不是合法 JSON：{exc}") from exc

def _try(label: str, fn: Any, *args: Any, **kwargs: Any) -> Any:
    """跑一项测量；某一项失败不让整体崩，返回值里塞 error 字段，其它项照常输出。"""
    try:
        return fn(*args, **kwargs)
    except (OSError, subprocess.TimeoutExpired, QcError, ValueError, KeyError) as exc:
        return {"error": f"{label}失败：{exc}"}

# 规格（ffprobe）

def build_spec(probe: dict[str, Any]) -> dict[str, Any]:
    """规格口径：时长取format.duration；分辨率/帧率/码率取首条视频流，采样率/声道取首条音频流；缺失保留None不猜测。"""
    fmt = probe.get("format") or {}
    streams = probe.get("streams") or []
    vs = next((s for s in streams if s.get("codec_type") == "video"), None)
    aus = next((s for s in streams if s.get("codec_type") == "audio"), None)
    fps = None
    rate = str((vs or {}).get("r_frame_rate") or "")
    if vs and "/" in rate:
        num, den = rate.split("/")
        fps = round(float(num) / float(den), 3) if float(den) else None
    return {
        "duration_s": round(float(fmt.get("duration") or 0.0), 3),
        "width": vs.get("width") if vs else None,
        "height": vs.get("height") if vs else None,
        "fps": fps,
        "video_bitrate_kbps": (round(int(vs["bit_rate"]) / 1000, 1)
                                if vs and vs.get("bit_rate") else None),
        "audio_sample_rate_hz": int(aus["sample_rate"]) if aus and aus.get("sample_rate") else None,
        "audio_channels": aus.get("channels") if aus else None,
    }

# 视频类：scdet + freezedetect + blackdetect 合并一次解码

def run_video_filters(path: Path, *, freeze_noise: str, freeze_duration: float, nice: bool, timeout: float) -> str:
    """一次解码测剪切/静止/黑场，避免三项各自解码一遍整片。"""
    vf = f"scdet,freezedetect=n={freeze_noise}:d={freeze_duration},blackdetect"
    args = ["ffmpeg", "-hide_banner", "-i", str(path), "-vf", vf, "-f", "null", "-"]
    return _run_text(args, nice=nice, timeout=timeout)

def parse_scdet_cuts(text: str) -> list[float]:
    """场景切点时间戳（秒），来自 scdet 滤镜的逐帧 metadata 日志行。"""
    return [round(float(t), 3) for _, t in _SCDET_RE.findall(text)]

def parse_freeze_events(text: str) -> list[dict[str, float]]:
    """freezedetect 冻结事件：start/duration/end 三个 tag 各自按序配对——ffmpeg 总
    是为同一事件连续吐出这三行，即使与 scdet 日志交替打印也不影响（对照真实日志验证过）。"""
    starts = [float(x) for x in _FREEZE_START_RE.findall(text)]
    durs = [float(x) for x in _FREEZE_DUR_RE.findall(text)]
    ends = [float(x) for x in _FREEZE_END_RE.findall(text)]
    n = min(len(starts), len(durs), len(ends))
    return [{"start": starts[i], "duration": durs[i], "end": ends[i]} for i in range(n)]

def parse_black_events(text: str) -> list[dict[str, float]]:
    """blackdetect 黑场事件（ffmpeg 默认阈值：pic_th=0.98, pix_th=0.10, d≥2.0s）。"""
    return [{"start": float(a), "end": float(b), "duration": float(c)}
            for a, b, c in _BLACK_RE.findall(text)]

def merge_contiguous_freeze(events: list[dict[str, float]], gap_s: float = 0.05) -> list[dict[str, float]]:
    """首尾相接（间隙 ≤gap_s）的冻结事件合并成一段——不合并会系统性低估「最长一段」
    （真实样本：连续 4.08s 静止被 ffmpeg 吐成 3 条，不合并「最长」读成 1.6s）。"""
    if not events:
        return []
    ev = sorted(events, key=lambda e: e["start"])
    out = [dict(ev[0])]
    for e in ev[1:]:
        if e["start"] - out[-1]["end"] <= gap_s:
            out[-1]["end"] = e["end"]
            out[-1]["duration"] = round(out[-1]["end"] - out[-1]["start"], 3)
        else:
            out.append(dict(e))
    return out

def freeze_summary(events: list[dict[str, float]], total_duration_s: float) -> dict[str, Any]:
    """静止汇总：总秒数/最长单段/占比，以及开场 0~3 秒是否命中冻结及其时长。"""
    total = round(sum(e["duration"] for e in events), 3)
    longest = round(max((e["duration"] for e in events), default=0.0), 3)
    ratio = round(total / total_duration_s, 4) if total_duration_s > 0 else None
    opening = sum(max(0.0, min(e["end"], OPENING_WINDOW_S) - max(e["start"], 0.0)) for e in events)
    return {
        "events": events, "total_s": total, "longest_s": longest, "ratio": ratio,
        "opening_frozen": opening > 0, "opening_frozen_s": round(opening, 3),
    }

def _measure_cuts_freeze_black(path: Path, duration_s: float, freeze_noise: str, freeze_duration: float, nice: bool, timeout: float) -> dict[str, Any]:
    text = run_video_filters(path, freeze_noise=freeze_noise, freeze_duration=freeze_duration,
                              nice=nice, timeout=timeout)
    cuts = parse_scdet_cuts(text)
    black_events = parse_black_events(text)
    asl = round(duration_s / (len(cuts) + 1), 3) if duration_s else None
    return {
        "scdet_cuts_total": len(cuts), "scdet_cuts": cuts, "asl_s": asl,
        "freeze": freeze_summary(merge_contiguous_freeze(parse_freeze_events(text)), duration_s),
        "blackdetect": {"events": black_events,
                         "total_s": round(sum(e["duration"] for e in black_events), 3)},
    }

# 音频类：silencedetect + ebur128 合并一次解码

def run_audio_filters(path: Path, *, silence_db: float, silence_dur: float, nice: bool, timeout: float) -> str:
    """一次解码测静音/响度，避免两项各自解码一遍整片。"""
    af = f"silencedetect=n={silence_db}dB:d={silence_dur},ebur128=peak=true"
    args = ["ffmpeg", "-hide_banner", "-i", str(path), "-af", af, "-f", "null", "-"]
    return _run_text(args, nice=nice, timeout=timeout)

def parse_silence_events(text: str) -> list[dict[str, float]]:
    """静音段列表；片尾未闭合的静音（到 EOF 都没打印 silence_end）按漏报丢弃，
    不拿片尾时间拼一个假 end。"""
    starts = [float(x) for x in _SILENCE_START_RE.findall(text)]
    pairs = [(float(a), float(b)) for a, b in _SILENCE_END_RE.findall(text)]
    n = min(len(starts), len(pairs))
    return [{"start": round(starts[i], 3), "end": round(pairs[i][0], 3),
              "duration": round(pairs[i][1], 3)} for i in range(n)]

def parse_loudness(text: str) -> dict[str, float | None]:
    """整段响度；ebur128 每帧都会重复打印同名字段，取最后一次匹配即 Summary 块的最终值。"""
    def last(pattern: re.Pattern[str]) -> float | None:
        found = pattern.findall(text)
        return float(found[-1]) if found else None
    return {"integrated_lufs": last(_LOUD_I_RE), "lra_lu": last(_LOUD_LRA_RE),
            "true_peak_dbfs": last(_LOUD_PEAK_RE)}

def _measure_audio(path: Path, duration_s: float, silence_db: float, silence_dur: float, nice: bool, timeout: float) -> dict[str, Any]:
    text = run_audio_filters(path, silence_db=silence_db, silence_dur=silence_dur,
                              nice=nice, timeout=timeout)
    silence = parse_silence_events(text)
    silence_total = round(sum(e["duration"] for e in silence), 3)
    return {
        "silence": {"events": silence, "total_s": silence_total, "count": len(silence)},
        "loudness": parse_loudness(text),
        "active_audio_s": round(max(0.0, duration_s - silence_total), 3),
    }

# 分段重建：edit-report 代数估计 + scdet 纠正

def load_edit_report(path: Path) -> dict[str, Any] | None:
    """读取 episode.edit-report.json；缺失或损坏返回 None（调用方据此关闭分段
    指标，不得猜测均分——契约见本文件头 docstring）。"""
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

def estimate_segment_bounds(edit_report: dict[str, Any], probed_duration_s: float) -> list[dict[str, Any]] | None:
    """从 edit-report 反推每段起止估计值，算法见文件头 docstring「分段重建」节；
    结构不自洽（接缝数应等于镜头数-1）时返回 None，不强行猜测。"""
    items = ((edit_report.get("video_delivery_manifest") or {}).get("items")) or []
    transitions = edit_report.get("transitions") or []
    n = len(items)
    if n == 0:
        return None
    overlaps = [float(t.get("duration_s") or 0.0) for t in transitions]
    if len(overlaps) != n - 1:
        return None
    nominal = (probed_duration_s + sum(overlaps)) / n
    bounds: list[dict[str, Any]] = []
    end = 0.0
    for i, item in enumerate(items):
        start = end
        end = start + nominal - (overlaps[i - 1] if i > 0 else 0.0)
        bounds.append({"shot_no": item.get("shot_no"), "start": round(start, 3),
                        "end": round(end, 3), "basis": "equal_duration_estimate"})
    bounds[-1]["end"] = round(probed_duration_s, 3)
    return bounds

def refine_bounds_with_cuts(bounds: list[dict[str, Any]], cuts: list[float], tolerance_s: float) -> None:
    """原地用 scdet 实测切点校正每个内部接缝：独立找最近切点，容差内才采纳（避免
    镜头内部剪辑点被误当整镜边界），找不到就保留代数估计值。"""
    if not cuts:
        return
    for i in range(len(bounds) - 1):
        target = bounds[i]["end"]
        nearest = min(cuts, key=lambda c: abs(c - target))
        if abs(nearest - target) <= tolerance_s:
            bounds[i]["end"] = nearest
            bounds[i + 1]["start"] = nearest
            bounds[i]["basis"] = "scdet_snap"

def assign_cuts_to_segments(cuts: list[float], bounds: list[dict[str, Any]]) -> dict[Any, int]:
    """把切点计数归属到所在段（落在段边界上的切点算接缝，不算段内切点）。"""
    counts: dict[Any, int] = {}
    for t in cuts:
        for seg in bounds:
            if seg["start"] < t < seg["end"]:
                counts[seg["shot_no"]] = counts.get(seg["shot_no"], 0) + 1
                break
    return counts

def segment_integrated_loudness(path: Path, start: float, duration: float, *, nice: bool, timeout: float) -> float | None:
    """单段 ebur128 积分响度——独立小窗口解码，不计入「尽量少遍历」的整片扫描约束。"""
    dur = max(0.3, duration)
    args = ["ffmpeg", "-hide_banner", "-ss", f"{start:.3f}", "-i", str(path),
            "-t", f"{dur:.3f}", "-af", "ebur128=peak=true", "-f", "null", "-"]
    text = _run_text(args, nice=nice, timeout=timeout)
    return parse_loudness(text)["integrated_lufs"]

def window_mean_volume(path: Path, start: float, duration: float, *, nice: bool, timeout: float) -> float | None:
    """[start, start+duration) 窗口内 volumedetect 的均值电平（dBFS）。"""
    args = ["ffmpeg", "-hide_banner", "-ss", f"{max(0.0, start):.3f}", "-i", str(path),
            "-t", f"{duration:.3f}", "-af", "volumedetect", "-f", "null", "-"]
    text = _run_text(args, nice=nice, timeout=timeout)
    m = _MEAN_VOL_RE.search(text)
    return float(m.group(1)) if m else None

def seam_jumps(path: Path, bounds: list[dict[str, Any]], *, window_s: float, nice: bool, timeout: float) -> list[dict[str, Any]]:
    """逐接缝测「前 window_s 秒」与「后 window_s 秒」的电平差（dB），口径见文件头。"""
    out = []
    for i in range(len(bounds) - 1):
        t = bounds[i]["end"]
        before = window_mean_volume(path, t - window_s, window_s, nice=nice, timeout=timeout)
        after = window_mean_volume(path, t, window_s, nice=nice, timeout=timeout)
        jump = round(after - before, 1) if before is not None and after is not None else None
        out.append({"from_shot_no": bounds[i]["shot_no"], "to_shot_no": bounds[i + 1]["shot_no"],
                     "boundary_t": round(t, 3), "before_db": before, "after_db": after,
                     "jump_db": jump})
    return out

def loudness_range_stats(bounds: list[dict[str, Any]]) -> tuple[float | None, float | None]:
    """各段 integrated LUFS 的极差(max-min)与标准差——判"音量是否一致"的主指标，比
    接缝 0.4s 电平差更可信（那个受内容影响，见 SEAM_JUMP_NOTE）；无有效值给 None 不给 0。"""
    values = [b["integrated_lufs"] for b in bounds if b.get("integrated_lufs") is not None]
    if not values:
        return None, None
    return round(max(values) - min(values), 2), round(statistics.pstdev(values), 2)

def _measure_segments(path: Path, report: dict[str, Any], cuts_list: list[float], duration_s: float, nice: bool, timeout: float) -> dict[str, Any] | None:
    bounds = estimate_segment_bounds(report, duration_s)
    if not bounds:
        return None
    refine_bounds_with_cuts(bounds, cuts_list, BOUNDARY_SNAP_TOLERANCE_S)
    cut_counts = assign_cuts_to_segments(cuts_list, bounds)
    for seg in bounds:
        seg["n_cuts_inside"] = cut_counts.get(seg["shot_no"], 0)
        seg["integrated_lufs"] = segment_integrated_loudness(
            path, seg["start"], seg["end"] - seg["start"], nice=nice, timeout=timeout)
    loudness_range_lu, loudness_stdev_lu = loudness_range_stats(bounds)
    jumps = seam_jumps(path, bounds, window_s=SEAM_WINDOW_S, nice=nice, timeout=timeout)
    big = [j for j in jumps if j["jump_db"] is not None and abs(j["jump_db"]) >= SEAM_JUMP_THRESHOLD_DB]
    max_abs = max((abs(j["jump_db"]) for j in jumps if j["jump_db"] is not None), default=0.0)
    return {
        "n_segments": len(bounds), "bounds": bounds,
        "loudness_range_lu": loudness_range_lu, "loudness_stdev_lu": loudness_stdev_lu,
        "seam_jumps": jumps, "seam_jump_note": SEAM_JUMP_NOTE,
        "seam_jump_count_ge_threshold": len(big), "seam_jump_max_abs_db": round(max_abs, 1),
    }

# 字幕

def parse_srt(path: Path) -> dict[str, Any] | None:
    """srt 条数与字幕总时长；文件不存在返回 None（区别于「有文件但 0 条」）。"""
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    cues = _SRT_CUE_RE.findall(text)
    total = sum(
        max(0.0,
            (int(h2) * 3600 + int(m2) * 60 + int(s2) + int(ms2) / 1000)
            - (int(h1) * 3600 + int(m1) * 60 + int(s1) + int(ms1) / 1000))
        for h1, m1, s1, ms1, h2, m2, s2, ms2 in cues
    )
    return {"n_cues": len(cues), "total_duration_s": round(total, 3)}

def _measure_subtitles(srt_path: Path, duration_s: float, silence_total_s: float) -> dict[str, Any] | None:
    srt = parse_srt(srt_path)
    if srt is None:
        return None
    active = max(0.0, duration_s - silence_total_s)
    coverage = round(srt["total_duration_s"] / active, 4) if active > 0 else None
    return {**srt, "active_audio_s": round(active, 3), "coverage_ratio": coverage}

# 编排 + CLI

def measure_episode(
    video_path: Path, *, edit_report_path: Path | None = None, srt_path: Path | None = None,
    freeze_noise: str = DEFAULT_FREEZE_NOISE_DB, freeze_duration: float = DEFAULT_FREEZE_DURATION_S,
    silence_db: float = DEFAULT_SILENCE_NOISE_DB, silence_duration: float = DEFAULT_SILENCE_DURATION_S,
    nice: bool = False, timeout: float = DEFAULT_TIMEOUT_S,
) -> dict[str, Any]:
    """跑全部量测；单项失败记 error 字段不影响其它项（ffprobe 失败除外，抛 QcError）。"""
    probe = ffprobe_json(video_path, nice=nice, timeout=timeout)
    spec = build_spec(probe)
    duration_s = spec["duration_s"]
    result: dict[str, Any] = {"path": str(video_path), "spec": spec}

    result["cuts_freeze_black"] = _try(
        "剪切/静止/黑场", _measure_cuts_freeze_black, video_path, duration_s,
        freeze_noise, freeze_duration, nice, timeout)
    result["audio"] = _try(
        "静音/响度", _measure_audio, video_path, duration_s, silence_db, silence_duration, nice, timeout)

    report = load_edit_report(edit_report_path) if edit_report_path else None
    cfb = result["cuts_freeze_black"]
    cuts_list = cfb.get("scdet_cuts") if isinstance(cfb, dict) else []
    result["segments"] = (
        _try("分段", _measure_segments, video_path, report, cuts_list or [], duration_s, nice, timeout)
        if report is not None else None
    )

    audio = result["audio"]
    silence_total = audio.get("silence", {}).get("total_s", 0.0) if isinstance(audio, dict) else 0.0
    result["subtitles"] = (
        _try("字幕", _measure_subtitles, srt_path, duration_s, silence_total) if srt_path else None
    )
    return result

def _print_summary_cn(result: dict[str, Any]) -> None:
    """终端中文摘要；完整数据看 --json 输出。"""
    spec = result["spec"]
    print(f"时长 {spec['duration_s']}s  {spec['width']}x{spec['height']}@{spec['fps']}fps  "
          f"视频码率 {spec['video_bitrate_kbps']}kbps  音频 {spec['audio_sample_rate_hz']}Hz/"
          f"{spec['audio_channels']}ch")
    cfb = result.get("cuts_freeze_black") or {}
    if "error" in cfb:
        print(f"剪切/静止/黑场：测量失败——{cfb['error']}")
    else:
        fr = cfb["freeze"]
        opening = f"冻结 {fr['opening_frozen_s']}s" if fr["opening_frozen"] else "正常"
        print(f"切点 {cfb['scdet_cuts_total']} 个，ASL {cfb['asl_s']}s；"
              f"冻结共 {fr['total_s']}s（最长 {fr['longest_s']}s，占比 {fr['ratio']}），开场3秒{opening}；"
              f"黑场 {cfb['blackdetect']['total_s']}s")
    segs = result.get("segments")
    if segs and "error" not in segs and segs.get("loudness_range_lu") is not None:
        range_txt = f"{segs['loudness_range_lu']}LU(标准差{segs['loudness_stdev_lu']}LU)"
    else:
        range_txt = "N/A"
    audio = result.get("audio") or {}
    if "error" in audio:
        print(f"静音/响度：测量失败——{audio['error']}")
    else:
        sil, loud = audio["silence"], audio["loudness"]
        print(f"响度极差(分段,主指标) {range_txt}；整体 I={loud['integrated_lufs']}LUFS "
              f"LRA={loud['lra_lu']} 真峰={loud['true_peak_dbfs']}dBFS；"
              f"静音共 {sil['total_s']}s（{sil['count']} 段）")
    if segs is None:
        print("分段指标：无 edit-report，不给分段指标")
    elif "error" in segs:
        print(f"分段指标：测量失败——{segs['error']}")
    else:
        print(f"分段 {segs['n_segments']} 段；{SEAM_JUMP_NOTE}，"
              f"≥{SEAM_JUMP_THRESHOLD_DB}dB 的有 {segs['seam_jump_count_ge_threshold']} 处，"
              f"最大 {segs['seam_jump_max_abs_db']}dB")
    subs = result.get("subtitles")
    if subs is None:
        print("字幕：无 srt")
    elif "error" in subs:
        print(f"字幕：测量失败——{subs['error']}")
    else:
        print(f"字幕 {subs['n_cues']} 条，时长 {subs['total_duration_s']}s，"
              f"覆盖有声时长比 {subs['coverage_ratio']}")

def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="成片客观质检：可重复、可跨批次对比的量化测量。")
    parser.add_argument("video", type=Path, help="episode.mp4 路径")
    parser.add_argument("--edit-report", type=Path, default=None, help="episode.edit-report.json 路径；缺省探测同目录同名文件")
    parser.add_argument("--srt", type=Path, default=None, help="episode.srt 路径；缺省探测同目录同名文件")
    parser.add_argument("--json", type=Path, default=None, help="写出 JSON 结果到此路径")
    parser.add_argument("--nice", action="store_true", help="子进程加 nice -n 19（生产机上用）")
    parser.add_argument("--freeze-noise-db", default=DEFAULT_FREEZE_NOISE_DB)
    parser.add_argument("--freeze-duration-s", type=float, default=DEFAULT_FREEZE_DURATION_S)
    parser.add_argument("--silence-db", type=float, default=DEFAULT_SILENCE_NOISE_DB)
    parser.add_argument("--silence-duration-s", type=float, default=DEFAULT_SILENCE_DURATION_S)
    parser.add_argument("--timeout-s", type=float, default=DEFAULT_TIMEOUT_S)
    return parser

def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    video_path: Path = args.video
    if not video_path.is_file():
        print(f"输入不可读：{video_path}", file=sys.stderr)
        return EXIT_INPUT_ERROR
    edit_report_path = args.edit_report or video_path.with_name(video_path.stem + ".edit-report.json")
    srt_path = args.srt or video_path.with_suffix(".srt")
    try:
        result = measure_episode(
            video_path,
            edit_report_path=edit_report_path if edit_report_path.is_file() else None,
            srt_path=srt_path if srt_path.is_file() else None,
            freeze_noise=args.freeze_noise_db, freeze_duration=args.freeze_duration_s,
            silence_db=args.silence_db, silence_duration=args.silence_duration_s,
            nice=args.nice, timeout=args.timeout_s,
        )
    except QcError as exc:
        print(f"测量失败：{exc}", file=sys.stderr)
        return EXIT_INPUT_ERROR
    _print_summary_cn(result)
    if args.json:
        args.json.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"已写出 {args.json}")
    return EXIT_OK

if __name__ == "__main__":
    sys.exit(main())
