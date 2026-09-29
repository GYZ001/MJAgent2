"""片头高能预告：从"烧字幕之前的画面"剪出计划里核验过的片段，硬切拼接，首尾
各 0.3 秒淡入淡出，再整体接到正片前面。

"烧字幕之前的画面"落点：不从已经合成好的正片（字幕/AI 标识已经烧进像素）截取，
改从每段自己的原始源视频文件（``piece_specs`` 里 ``concatenate_episode`` 已经
拿到的路径，与 ``app.media_exec.concat._draft_concat_pieces``/
``app.final_edit.render_episode_final_edit`` 读的是同一份文件）直接剪，天然
不带任何字幕/标识叠加层——不依赖那两条合成路径各自的临时目录残留物，避免
本包反向耦合它们的内部实现（那会是 L4 依赖 L5 的禁止上行边，见
``app/LAYERS.toml`` 本包声明）。

段内起止秒是"段"（``shot_no``）自己时间轴上的秒数（对应 ``shots.duration_s``，
即 playback_rate 换算后的有效时长）；换算到源文件里的真实时间戳要乘回
``playback_rate``（``effective_duration = video_duration_s / rate``，见
``app.media_exec.concat._draft_concat_pieces`` 的同一换算公式）。
"""
from __future__ import annotations

from pathlib import Path

from app.final_edit import FINAL_FPS
from app.final_edit_enhance.ffutil import probe_duration_s, run_ffmpeg
from app.final_edit_enhance.plan_generate import ResolvedTeaserClip
from app.media_pipeline.delivery_encode import DELIVERY_VIDEO_ARGS, canvas_filter, encode_timeout_s
from app.media_pipeline.loudness import FINAL_AUDIO_RATE

EDGE_FADE_S = 0.3


def _source_seconds(clip_seconds: float, rate: float) -> float:
    return clip_seconds * rate


def _atempo_chain(rate: float) -> str:
    """``atempo`` 单次只接受 0.5-2.0（ffmpeg 官方限制），倍速超出范围时按官方
    推荐写法链式拆分成多段。"""
    remaining = rate
    stages: list[str] = []
    while remaining > 2.0:
        stages.append("atempo=2.0")
        remaining /= 2.0
    while remaining < 0.5:
        stages.append("atempo=0.5")
        remaining /= 0.5
    stages.append(f"atempo={remaining:.6f}")
    return ",".join(stages)


def _extract_clip(
    source_path: str, start_s: float, end_s: float, rate: float,
    play_res: tuple[int, int], out_path: Path, timeout_s: float,
) -> None:
    """``rate``：该段在正片里的实际播放倍速（``app.video_playback.
    normalize_playback_rate``），源文件本身是原速——预告片要复现观众在正片里
    看到的同样观感，不能只搬运原速素材（2026-09-28 真实 ffmpeg 小样测出：不
    换算时 rate=2.0 的段剪出来的片段时长是请求值的 2 倍）。"""
    video_speed = "" if abs(rate - 1.0) < 1e-6 else f"setpts=PTS/{rate:.6f},"
    audio_speed = "" if abs(rate - 1.0) < 1e-6 else f"{_atempo_chain(rate)},"
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-ss", f"{start_s:.6f}", "-to", f"{end_s:.6f}", "-i", source_path,
        "-vf", f"{video_speed}{canvas_filter(*play_res)},fps={FINAL_FPS},setsar=1",
        "-af", f"{audio_speed}aresample={FINAL_AUDIO_RATE}",
        *DELIVERY_VIDEO_ARGS, "-c:a", "aac", "-ar", str(FINAL_AUDIO_RATE), str(out_path),
    ]
    run_ffmpeg(cmd, timeout=timeout_s, context="预告片段提取")


def build_teaser(
    clips: tuple[ResolvedTeaserClip, ...], piece_specs: list[tuple[int, str, float]],
    play_res: tuple[int, int], work_dir: Path,
) -> Path:
    """``clips`` 必须已经过 ``plan_validate.validate_teaser_clips`` 核验；本函数
    不重复校验起止秒是否越界，只管剪辑与拼接。"""
    if not clips:
        raise ValueError("预告片段列表为空")
    rate_by_shot = {shot_no: rate for shot_no, _path, rate in piece_specs}
    path_by_shot = {shot_no: path for shot_no, path, _rate in piece_specs}
    total_duration_s = sum(c.end_s - c.start_s for c in clips)
    timeout_s = encode_timeout_s(total_duration_s)
    prepared: list[Path] = []
    for i, clip in enumerate(clips):
        rate = rate_by_shot[clip.shot_no]
        start_source = _source_seconds(clip.start_s, rate)
        end_source = _source_seconds(clip.end_s, rate)
        out_path = work_dir / f"teaser-clip-{i}.mp4"
        _extract_clip(path_by_shot[clip.shot_no], start_source, end_source, rate, play_res, out_path, timeout_s)
        prepared.append(out_path)
    listfile = work_dir / "teaser-clips.txt"
    lines = []
    for prepared_path in prepared:
        safe = str(prepared_path).replace("'", "'\\''")
        lines.append(f"file '{safe}'")
    listfile.write_text("\n".join(lines), encoding="utf-8")
    concat_out = work_dir / "teaser-concat.mp4"
    run_ffmpeg(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(listfile),
         "-c", "copy", str(concat_out)],
        timeout=timeout_s, context="预告片段拼接",
    )
    faded_out = work_dir / "teaser.mp4"
    fade_out_st = max(0.0, total_duration_s - EDGE_FADE_S)
    run_ffmpeg(
        [
            "ffmpeg", "-y", "-loglevel", "error", "-i", str(concat_out),
            "-vf", f"fade=t=in:st=0:d={EDGE_FADE_S}:alpha=0,fade=t=out:st={fade_out_st:.6f}:d={EDGE_FADE_S}",
            "-af", f"afade=t=in:d={EDGE_FADE_S},afade=t=out:st={fade_out_st:.6f}:d={EDGE_FADE_S}",
            *DELIVERY_VIDEO_ARGS, "-c:a", "aac", "-ar", str(FINAL_AUDIO_RATE), str(faded_out),
        ],
        timeout=timeout_s, context="预告片首尾淡入淡出",
    )
    return faded_out


def prepend_teaser(teaser_path: Path, main_path: Path, play_res: tuple[int, int], work_dir: Path) -> Path:
    """把预告接到正片前面；用 ``filter_complex concat``（重编码两路）而不是
    demuxer 流拷贝——预告是刚重编码出来的新文件，正片是既有合成路径产出的
    候选文件，两者编码参数不保证逐字节一致，流拷贝在参数不一致时会失败或产出
    损坏文件（与 ``app.domain.series_ops.merge`` 处理跨集参数不一致时改用
    ``filter_complex`` 是同一个理由）。"""
    out_path = work_dir / "with-teaser.mp4"
    graph = (
        f"[0:v]scale={play_res[0]}:{play_res[1]},setsar=1,fps={FINAL_FPS}[v0];"
        f"[1:v]scale={play_res[0]}:{play_res[1]},setsar=1,fps={FINAL_FPS}[v1];"
        f"[0:a]aresample={FINAL_AUDIO_RATE}[a0];[1:a]aresample={FINAL_AUDIO_RATE}[a1];"
        "[v0][a0][v1][a1]concat=n=2:v=1:a=1[outv][outa]"
    )
    total_s = probe_duration_s(str(teaser_path)) + probe_duration_s(str(main_path))
    run_ffmpeg(
        [
            "ffmpeg", "-y", "-loglevel", "error", "-i", str(teaser_path), "-i", str(main_path),
            "-filter_complex", graph, "-map", "[outv]", "-map", "[outa]",
            *DELIVERY_VIDEO_ARGS, "-c:a", "aac", "-ar", str(FINAL_AUDIO_RATE),
            "-movflags", "+faststart", str(out_path),
        ],
        timeout=encode_timeout_s(total_s), context="预告片拼接进正片",
    )
    return out_path
