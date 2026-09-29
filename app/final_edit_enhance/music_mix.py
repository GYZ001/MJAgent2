"""统一配乐：把 ``music_runs`` 算出的时间轴渲染成一条贯穿全集的配乐音轨
（ffmpeg 拼接 + 交叉淡化），再与原片音轨（对白+环境音，做台词侧链闪避）、
独白语音轨（若有）混音、整体 ``loudnorm`` 归一。

混音电平是初值（见函数内常量），未经真实人工试听微调——``app.final_edit_enhance
.apply`` 的调用方文档与成片报告都要如实说明这一点，不能包装成"已校准"。
"""
from __future__ import annotations

from pathlib import Path

from app.final_edit_enhance.ffutil import run_ffmpeg
from app.final_edit_enhance.music_library import MusicLibrary
from app.final_edit_enhance.music_runs import MusicRun, edge_fades, is_crossfade_boundary, tail_extend_s
from app.final_edit_enhance.silence import Interval
from app.media_pipeline.delivery_encode import encode_timeout_s
from app.media_pipeline.loudness import FINAL_AUDIO_RATE

CROSSFADE_S = 2.5
EDGE_FADE_S = 2.5
# 相对原始对白 0dB 基准的初值（见 pilot 设计冻结常量：有台词段配乐更低、独白
# 播放时配乐再压低）；真实闪避强度另由 sidechaincompress 动态决定，这两个
# 只是 sidechaincompress 之外的静态兜底电平。
MUSIC_MAKEUP_DB = -6.0
MONOLOGUE_DUCK_EXTRA_DB = -6.0
MONOLOGUE_TRACK_DB = -3.0


def _fade_filter(label: str, run: MusicRun, fade_in: bool, fade_out: bool) -> str | None:
    parts = []
    if fade_in:
        parts.append(f"afade=t=in:d={EDGE_FADE_S:.2f}")
    if fade_out:
        start = max(0.0, run.duration_s - EDGE_FADE_S)
        parts.append(f"afade=t=out:st={start:.6f}:d={EDGE_FADE_S:.2f}")
    if not parts:
        return None
    return f"[{label}]{','.join(parts)}[{label}f]"


def _fold_chain(runs: list[MusicRun], labels: list[str]) -> tuple[list[str], str]:
    """按交叉淡化/硬接依次折叠 ``labels``，返回中间滤镜语句列表与最终标签。"""
    statements: list[str] = []
    acc = labels[0]
    for i in range(1, len(runs)):
        nxt = labels[i]
        out_label = f"acc{i}"
        if is_crossfade_boundary(runs[i - 1], runs[i]):
            statements.append(f"[{acc}][{nxt}]acrossfade=d={CROSSFADE_S:.2f}:c1=tri:c2=tri[{out_label}]")
        else:
            statements.append(f"[{acc}][{nxt}]concat=n=2:v=0:a=1[{out_label}]")
        acc = out_label
    return statements, acc


def build_music_bed(runs: list[MusicRun], library: MusicLibrary, work_dir: Path) -> Path:
    """产出一条 wav，总时长精确等于 ``sum(run.duration_s for run in runs)``。"""
    if not runs:
        raise ValueError("配乐时间轴为空")
    unique_track_ids = sorted({r.track_id for r in runs if r.track_id is not None})
    input_index_of = {tid: i for i, tid in enumerate(unique_track_ids)}
    cmd = ["ffmpeg", "-y", "-loglevel", "error"]
    for tid in unique_track_ids:
        cmd += ["-i", str(library.by_id(tid).audio_path)]
    silence_idx = len(unique_track_ids)
    has_silence = any(r.track_id is None for r in runs)
    if has_silence:
        cmd += ["-f", "lavfi", "-i", f"anullsrc=channel_layout=stereo:sample_rate={FINAL_AUDIO_RATE}"]

    labels: list[str] = []
    filters: list[str] = []
    for i, run in enumerate(runs):
        base_label = f"run{i}"
        # 只延伸"会与下一个 run 交叉淡化"的尾部（见 app.final_edit_enhance.music_runs
        # 模块 docstring 的推导），折叠后总长精确等于各 run 时长之和。
        feed_s = run.duration_s + tail_extend_s(runs, i, CROSSFADE_S)
        if run.track_id is None:
            filters.append(f"[{silence_idx}:a]atrim=duration={feed_s:.6f}[{base_label}]")
        else:
            track = library.by_id(run.track_id)
            size = int(track.duration_s * FINAL_AUDIO_RATE) + FINAL_AUDIO_RATE
            idx = input_index_of[run.track_id]
            filters.append(
                f"[{idx}:a]aloop=loop=-1:size={size},atrim=duration={feed_s:.6f},asetpts=PTS-STARTPTS[{base_label}]",
            )
        fade_in, fade_out = edge_fades(runs, i)
        faded = _fade_filter(base_label, run, fade_in, fade_out)
        labels.append(f"{base_label}f" if faded else base_label)
        if faded:
            filters.append(faded)

    fold_statements, final_label = _fold_chain(runs, labels) if len(runs) > 1 else ([], labels[0])
    filters.extend(fold_statements)
    filter_complex = ";".join(filters) + f";[{final_label}]aresample={FINAL_AUDIO_RATE}[out]"
    out_path = work_dir / "music_bed.wav"
    cmd += ["-filter_complex", filter_complex, "-map", "[out]", str(out_path)]
    total_duration_s = sum(r.duration_s for r in runs)
    run_ffmpeg(cmd, timeout=encode_timeout_s(total_duration_s), context="配乐时间轴渲染")
    return out_path


def _monologue_duck_expr(windows: list[Interval]) -> str | None:
    if not windows:
        return None
    clauses = "+".join(f"between(t,{s:.3f},{e:.3f})" for s, e in windows)
    return f"volume={10 ** (MONOLOGUE_DUCK_EXTRA_DB / 20):.6f}:enable='{clauses}'"


def _music_chain(monologue_windows: list[Interval]) -> list[str]:
    duck = _monologue_duck_expr(monologue_windows)
    duck_chain = f"[music_in]{duck}[music_duck]" if duck else "[music_in]anull[music_duck]"
    return [
        f"[1:a]aresample={FINAL_AUDIO_RATE}[music_in]", duck_chain,
        "[music_duck][dlg_sc]sidechaincompress=threshold=0.05:ratio=8:attack=5:release=300:makeup=1[music_ducked]",
        f"[music_ducked]volume={MUSIC_MAKEUP_DB}dB[music_final]",
    ]


def mix_audio_track(
    candidate_path: Path, music_bed_path: Path | None, monologue_track_path: Path | None,
    monologue_windows: list[Interval], total_duration_s: float, work_dir: Path,
) -> Path:
    """只替换音轨（``-c:v copy``），视频像素不重编码。``music_bed_path``/
    ``monologue_track_path`` 至少一个非 None——两者都是 None 时不该调用本函数
    （调用方应直接跳过整个混音阶段，见 ``app.final_edit_enhance.apply``）。"""
    inputs = ["-i", str(candidate_path)]
    mix_labels = ["[dlg_main]"]
    next_input_idx = 1
    if music_bed_path is not None:
        # 只有真的要做侧链闪避时才 asplit 出第二路给 sidechaincompress 用；
        # 只开独白不开配乐时没有侧链消费者，asplit 出的第二路会成为
        # "unconnected output" 被 ffmpeg 直接拒绝（2026-09-28 真实 ffmpeg 跑出
        # 的红：libavfilter 不接受声明了却没人消费的具名 pad）。
        graph = [f"[0:a]aresample={FINAL_AUDIO_RATE},asplit=2[dlg_main][dlg_sc]"]
        inputs += ["-i", str(music_bed_path)]
        graph.extend(_music_chain(monologue_windows))
        mix_labels.append("[music_final]")
        next_input_idx += 1
    else:
        graph = [f"[0:a]aresample={FINAL_AUDIO_RATE}[dlg_main]"]
    if monologue_track_path is not None:
        inputs += ["-i", str(monologue_track_path)]
        graph.append(f"[{next_input_idx}:a]aresample={FINAL_AUDIO_RATE},volume={MONOLOGUE_TRACK_DB}dB[mono_final]")
        mix_labels.append("[mono_final]")
    graph.append(f"{''.join(mix_labels)}amix=inputs={len(mix_labels)}:duration=first:normalize=0[amixed]")
    graph.append("[amixed]loudnorm=I=-16:TP=-1.5:LRA=11[aout]")
    out_path = work_dir / "mixed.mp4"
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error", *inputs,
        "-filter_complex", ";".join(graph),
        "-map", "0:v", "-map", "[aout]", "-c:v", "copy",
        "-c:a", "aac", "-b:a", "192k", "-ar", str(FINAL_AUDIO_RATE),
        "-t", f"{total_duration_s:.3f}", "-movflags", "+faststart", str(out_path),
    ]
    run_ffmpeg(cmd, timeout=encode_timeout_s(total_duration_s), context="配乐/独白混音")
    return out_path
