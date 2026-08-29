"""最终编辑：统一片段规格、确定性文字、转场、音频衔接与边界风险报告。

该模块不调用生成模型，也不把内容质量问题升格为交付门禁。调用方在编辑
图失败时必须回退到普通硬拼，保留可播整集。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.continuity import (
    apply_shot_contract,
    required_text_strategy,
    structured_boundary_issues,
)
from app.schemas import Shot

FINAL_WIDTH = 720
FINAL_HEIGHT = 1280
FINAL_FPS = 24
FINAL_AUDIO_RATE = 48_000

_FONT_CANDIDATES = (
    "/System/Library/Fonts/Supplemental/Songti.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
)


@dataclass(frozen=True)
class TransitionSpec:
    edit_type: str
    ffmpeg_name: str
    duration_s: float
    audio_overlap_ms: int


def _run_ffmpeg(command: list[str], *, timeout: float, context: str) -> None:
    try:
        subprocess.run(command, check=True, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"{context}超时") from exc
    except subprocess.CalledProcessError as exc:
        raw = exc.stderr or b""
        detail = raw.decode("utf-8", "replace")[-1600:].strip()
        raise RuntimeError(f"{context}失败" + (f"：{detail}" if detail else "")) from exc


def transition_spec(value: str | None) -> TransitionSpec:
    transition = str(value or "硬切").strip()
    if transition in {"叠化", "声音延续+叠化", "声音先行+淡入"}:
        return TransitionSpec("dissolve", "dissolve", 0.32, 320)
    if transition in {"淡出淡入", "黑场", "闪黑"}:
        return TransitionSpec("dip_black", "fadeblack", 0.28, 280)
    if transition == "闪白":
        return TransitionSpec("dip_white", "fadewhite", 0.20, 200)
    if transition == "甩镜":
        return TransitionSpec("whip", "smoothleft", 0.20, 200)
    if transition == "遮挡转场":
        return TransitionSpec("cover", "coverleft", 0.20, 200)
    if transition == "匹配剪辑":
        return TransitionSpec("match_cut", "fadefast", 0.08, 80)
    # 硬切的视频只使用约 3 帧过渡，同时消除环境底噪瞬断。
    return TransitionSpec("cut", "fadefast", 0.12, 120)


def shot_from_row(row: Any) -> Shot:
    def get(key: str, default: Any = None) -> Any:
        if isinstance(row, dict):
            return row.get(key, default)
        return row[key] if key in row.keys() else default

    shot = Shot(
        shot_no=int(get("shot_no") or 0),
        duration_s=int(get("duration_s") or 5),
        shot_size=get("shot_size") or "中景",
        camera_move=get("camera_move") or "固定",
        scene_setting=get("scene_setting") or "",
        scene_name=get("scene_name") or "",
        characters=json.loads(get("characters") or "[]"),
        action_desc=get("action_desc") or "",
        first_frame_desc=get("first_frame_desc") or "",
        last_frame_desc=get("last_frame_desc") or "",
        source_excerpt=get("source_excerpt") or "",
        narration=get("narration"),
        dialogues=json.loads(get("dialogues") or "[]"),
        transition=get("transition") or "硬切",
        continuity_from_prev=bool(get("continuity_from_prev")),
        continuity_mode=get("continuity_mode") or "",
        observed_state_out=get("observed_state_out") or "",
    )
    apply_shot_contract(shot, get("shot_contract_json"))
    return shot


def _probe_media(path: str) -> dict[str, Any]:
    raw = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries",
            "format=duration:stream=codec_type,duration", "-of", "json", path,
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout
    payload = json.loads(raw or "{}")
    streams = payload.get("streams") or []
    try:
        duration = float((payload.get("format") or {}).get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    # 容器 duration 取音视频流较长者，源片段音轨往往比视频轨长几十毫秒；
    # 一旦拿它当作画面基准去裁剪音频，就会把这段多出来的时间重新灌回音频，
    # 音画错位原样复现。视频流自身的 duration 才是画面真正的权威时长。
    video_duration = 0.0
    for stream in streams:
        if isinstance(stream, dict) and stream.get("codec_type") == "video":
            try:
                video_duration = float(stream.get("duration") or 0)
            except (TypeError, ValueError):
                video_duration = 0.0
            break
    return {
        "duration_s": duration,
        "video_duration_s": video_duration or duration,
        "has_audio": any(
            isinstance(stream, dict) and stream.get("codec_type") == "audio"
            for stream in streams
        ),
    }


def audio_normalize_filter(*, atempo_rate: float | None, duration_s: float) -> str:
    """ffmpeg 音频滤镜链，draft_concat 与 render_episode_final_edit 共用。

    统一重采样到 FINAL_AUDIO_RATE、清零 PTS，再用 apad+atrim 把音轨精确对齐到
    `duration_s`（调用方传入的权威时长——通常是该镜视频流的实测时长，必要时
    已按倍速折算）。两条路径共用同一份逻辑，不允许只有一条做对，另一条假设
    「模型视频没有音轨」而放任音频原样直粘。
    """
    parts: list[str] = []
    if atempo_rate is not None and abs(atempo_rate - 1.0) > 1e-6:
        parts.append(f"atempo={atempo_rate:.6f}")
    parts.append(f"aresample={FINAL_AUDIO_RATE}")
    parts.append("asetpts=PTS-STARTPTS")
    parts.append(f"apad=whole_dur={duration_s:.6f}")
    parts.append(f"atrim=duration={duration_s:.6f}")
    return ",".join(parts)


def _font_path() -> Path:
    configured = str(os.getenv("MANJU_CJK_FONT_PATH") or "").strip()
    candidates = (configured, *_FONT_CANDIDATES) if configured else _FONT_CANDIDATES
    for candidate in candidates:
        path = Path(candidate)
        if path.is_file():
            return path
    raise RuntimeError(
        "未找到可用 CJK 字体；请通过 MANJU_CJK_FONT_PATH 配置完整中文 TTF/TTC/OTF"
    )


def _split_text(text: str, max_chars: int) -> list[str]:
    compact = "".join(str(text or "").splitlines()).strip()
    if not compact:
        return []
    return [compact[index:index + max_chars] for index in range(0, len(compact), max_chars)]


def render_text_card(
    exact_text: str,
    surface: str,
    destination: Path,
    *,
    font_role: str = "classical_serif",
) -> dict[str, Any]:
    """用确定性布局渲染整帧中文插入卡。"""
    from PIL import Image, ImageDraw, ImageFont

    text = str(exact_text or "").strip()
    if not text:
        raise ValueError("确定性文字卡缺少 exact_text")
    if len(text) > 64:
        raise ValueError("确定性文字卡最多支持 64 个字符")
    font_path = _font_path()
    destination.parent.mkdir(parents=True, exist_ok=True)

    image = Image.new("RGB", (FINAL_WIDTH, FINAL_HEIGHT), (30, 24, 18))
    draw = ImageDraw.Draw(image)
    for y in range(FINAL_HEIGHT):
        tone = int(232 - 30 * abs(y - FINAL_HEIGHT / 2) / (FINAL_HEIGHT / 2))
        draw.line(
            (0, y, FINAL_WIDTH, y),
            fill=(max(150, tone - 9), max(126, tone - 36), max(88, tone - 73)),
        )
    draw.rounded_rectangle((54, 128, 666, 1152), radius=22, outline=(80, 48, 26), width=5)
    draw.rounded_rectangle((70, 144, 650, 1136), radius=16, outline=(143, 96, 51), width=2)

    max_chars = 8 if len(text) <= 24 else 12
    lines = _split_text(text, max_chars)
    font_size = 92 if len(lines) <= 2 else (74 if len(lines) <= 4 else 58)
    font = ImageFont.truetype(str(font_path), font_size)
    label_font = ImageFont.truetype(str(font_path), 30)
    line_height = int(font_size * 1.5)
    block_height = line_height * len(lines)
    y = (FINAL_HEIGHT - block_height) // 2
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        line_width = bbox[2] - bbox[0]
        draw.text(
            ((FINAL_WIDTH - line_width) // 2, y),
            line,
            font=font,
            fill=(48, 29, 20),
            stroke_width=1,
            stroke_fill=(96, 62, 38),
        )
        y += line_height
    label = str(surface or "画面文字").strip()[:24]
    label_bbox = draw.textbbox((0, 0), label, font=label_font)
    draw.text(
        ((FINAL_WIDTH - (label_bbox[2] - label_bbox[0])) // 2, 1030),
        label,
        font=label_font,
        fill=(102, 68, 42),
    )
    image.save(destination, format="PNG", optimize=True)
    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    return {
        "path": str(destination),
        "sha256": digest,
        "exact_text": text,
        "surface": surface,
        "font_path": str(font_path),
        "font_role": font_role,
        "width": FINAL_WIDTH,
        "height": FINAL_HEIGHT,
    }


def _text_window(shot: Shot, source_duration_s: float, playback_rate: float) -> tuple[float, float] | None:
    required = shot.required_text
    if required_text_strategy(shot) != "deterministic_insert" or not required:
        return None
    duration = max(0.1, source_duration_s / playback_rate)
    start = max(0.0, min(float(required.appear_start_s or 0) / playback_rate, duration - 0.1))
    raw_end = required.stable_until_s
    if raw_end is None:
        end = start + min(2.0, duration - start)
    else:
        end = float(raw_end) / playback_rate
    end = max(start + min(0.5, duration - start), min(end, start + 2.5, duration))
    return round(start, 3), round(end, 3)


def _prepare_clip(
    source_path: str,
    destination: Path,
    *,
    shot: Shot,
    playback_rate: float,
    text_enabled: bool,
    work_dir: Path,
) -> dict[str, Any]:
    probe = _probe_media(source_path)
    source_duration = probe["video_duration_s"] or float(shot.duration_s or 5)
    rate = max(0.5, min(2.0, float(playback_rate or 1.0)))
    effective_duration = max(0.1, source_duration / rate)
    inputs = ["-i", source_path]
    filters: list[str] = []
    text_report: dict[str, Any] | None = None
    video_label = "base"
    video_chain = (
        f"[0:v]setpts=(PTS-STARTPTS)/{rate:.6f},"
        f"scale={FINAL_WIDTH}:{FINAL_HEIGHT}:force_original_aspect_ratio=increase,"
        f"crop={FINAL_WIDTH}:{FINAL_HEIGHT},fps={FINAL_FPS},settb=AVTB,format=rgba[{video_label}]"
    )
    filters.append(video_chain)

    text_window = _text_window(shot, source_duration, rate) if text_enabled else None
    if text_window and shot.required_text:
        card_path = work_dir / f"shot-{shot.shot_no}-text.png"
        text_report = render_text_card(
            shot.required_text.exact_text,
            shot.required_text.surface,
            card_path,
            font_role=shot.required_text.font_role,
        )
        inputs += ["-loop", "1", "-i", str(card_path)]
        start, end = text_window
        fade_duration = min(0.12, max(0.04, (end - start) / 4))
        fade_out_start = max(start, end - fade_duration)
        filters.extend([
            f"[1:v]scale={FINAL_WIDTH}:{FINAL_HEIGHT},format=rgba,"
            f"fade=t=in:st={start:.3f}:d={fade_duration:.3f}:alpha=1,"
            f"fade=t=out:st={fade_out_start:.3f}:d={fade_duration:.3f}:alpha=1[textcard]",
            f"[base][textcard]overlay=0:0:enable='between(t,{start:.3f},{end:.3f})':shortest=1,"
            "format=yuv420p[vout]",
        ])
        text_report.update({
            "start_s": start,
            "end_s": end,
            "fade_duration_s": round(fade_duration, 3),
            "strategy": "deterministic_insert",
        })
    else:
        filters.append("[base]format=yuv420p[vout]")

    audio_input_index = 0
    if not probe["has_audio"]:
        audio_input_index = 2 if text_window else 1
        inputs += [
            "-f", "lavfi", "-t", f"{effective_duration:.3f}",
            "-i", f"anullsrc=channel_layout=stereo:sample_rate={FINAL_AUDIO_RATE}",
        ]
    if probe["has_audio"]:
        filters.append(
            f"[0:a]{audio_normalize_filter(atempo_rate=rate, duration_s=effective_duration)}[aout]"
        )
    else:
        filters.append(
            f"[{audio_input_index}:a]"
            f"{audio_normalize_filter(atempo_rate=None, duration_s=effective_duration)}[aout]"
        )

    destination.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "ffmpeg", "-y", "-loglevel", "error", *inputs,
        "-filter_complex", ";".join(filters),
        "-map", "[vout]", "-map", "[aout]",
        "-t", f"{effective_duration:.3f}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", "-ar", str(FINAL_AUDIO_RATE),
        "-movflags", "+faststart", str(destination),
    ]
    _run_ffmpeg(
        command,
        timeout=max(120.0, effective_duration * 12.0),
        context=f"镜 {shot.shot_no} 片段标准化",
    )
    if not destination.is_file() or destination.stat().st_size <= 0:
        raise RuntimeError(f"镜 {shot.shot_no} 统一规格未产出有效片段")
    prepared_probe = _probe_media(str(destination))
    return {
        "shot_no": shot.shot_no,
        "path": str(destination),
        "duration_s": prepared_probe["video_duration_s"] or effective_duration,
        "text_insert": text_report,
    }


def boundary_report(shots: list[Shot]) -> dict[str, Any]:
    boundaries: list[dict[str, Any]] = []
    unverified: list[dict[str, int]] = []
    for index in range(1, len(shots)):
        prev, current = shots[index - 1], shots[index]
        issues = structured_boundary_issues(prev, current)
        before = prev.continuity_state_out
        after = current.continuity_state_in
        before_has_state = bool(
            before.characters or before.props or any(before.scene.model_dump().values())
        )
        after_has_state = bool(
            after.characters or after.props or any(after.scene.model_dump().values())
        )
        if not (before_has_state or after_has_state):
            unverified.append({"from_shot_no": prev.shot_no, "to_shot_no": current.shot_no})
        boundaries.append({
            "from_shot_no": prev.shot_no,
            "to_shot_no": current.shot_no,
            "continuity_mode": current.continuity_mode,
            "issues": issues,
            "runtime_blocking": False,
        })
    return {
        "boundaries": boundaries,
        "issue_count": sum(len(item["issues"]) for item in boundaries),
        "unverified_boundaries": unverified,
        "runtime_blocking": False,
    }


def _text_owners(shots: list[Shot]) -> tuple[dict[str, int], list[dict[str, Any]]]:
    grouped: dict[str, list[Shot]] = {}
    for shot in shots:
        required = shot.required_text
        if required and required_text_strategy(shot) == "deterministic_insert":
            exact = (required.exact_text or "").strip()
            if exact:
                grouped.setdefault(exact, []).append(shot)
    owners: dict[str, int] = {}
    warnings: list[dict[str, Any]] = []
    for exact, candidates in grouped.items():
        explicit = [
            shot for shot in candidates
            if shot.required_text and shot.required_text.delivery_owner_shot_no == shot.shot_no
        ]
        owner = explicit[0] if explicit else candidates[0]
        owners[exact] = owner.shot_no
        if len(candidates) > 1:
            warnings.append({
                "code": "DUPLICATE_TEXT_DELIVERY_COLLAPSED",
                "exact_text": exact,
                "owner_shot_no": owner.shot_no,
                "skipped_shot_nos": [shot.shot_no for shot in candidates if shot.shot_no != owner.shot_no],
                "runtime_blocking": False,
            })
    return owners, warnings


def _compose(prepared: list[dict[str, Any]], transitions: list[TransitionSpec], destination: Path) -> dict[str, Any]:
    if len(prepared) == 1:
        shutil.copyfile(prepared[0]["path"], destination)
        return {"total_duration_s": prepared[0]["duration_s"], "transitions": []}

    command = ["ffmpeg", "-y", "-loglevel", "error"]
    for item in prepared:
        command += ["-i", item["path"]]
    filters: list[str] = []
    for index in range(len(prepared)):
        filters.extend([
            f"[{index}:v]settb=AVTB,setpts=PTS-STARTPTS[v{index}]",
            f"[{index}:a]aresample={FINAL_AUDIO_RATE},asetpts=PTS-STARTPTS[a{index}]",
        ])
    video_label = "v0"
    audio_label = "a0"
    cumulative = float(prepared[0]["duration_s"])
    reports: list[dict[str, Any]] = []
    for index in range(1, len(prepared)):
        spec = transitions[index - 1]
        next_duration = float(prepared[index]["duration_s"])
        duration = min(spec.duration_s, max(0.04, min(cumulative, next_duration) / 3))
        offset = max(0.0, cumulative - duration)
        next_video = f"vx{index}"
        next_audio = f"ax{index}"
        filters.append(
            f"[{video_label}][v{index}]xfade=transition={spec.ffmpeg_name}:"
            f"duration={duration:.3f}:offset={offset:.3f}[{next_video}]"
        )
        filters.append(
            f"[{audio_label}][a{index}]acrossfade=d={duration:.3f}:c1=tri:c2=tri[{next_audio}]"
        )
        reports.append({
            "from_shot_no": prepared[index - 1]["shot_no"],
            "to_shot_no": prepared[index]["shot_no"],
            "edit_type": spec.edit_type,
            "duration_s": round(duration, 3),
            "audio_overlap_ms": round(duration * 1000),
        })
        cumulative = cumulative + next_duration - duration
        video_label = next_video
        audio_label = next_audio
    base_command = list(command)

    def command_with_normalizer(normalizer: str) -> list[str]:
        normalized_filters = [*filters, f"[{audio_label}]{normalizer}[aout]"]
        return [
            *base_command,
            "-filter_complex", ";".join(normalized_filters),
            "-map", f"[{video_label}]", "-map", "[aout]",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "160k", "-ar", str(FINAL_AUDIO_RATE),
            "-movflags", "+faststart", str(destination),
        ]

    timeout = max(180.0, cumulative * 12.0 + 60.0)
    audio_normalization = "loudnorm"
    try:
        _run_ffmpeg(
            command_with_normalizer("loudnorm=I=-16:TP=-1.5:LRA=11"),
            timeout=timeout,
            context="最终转场与音轨合成",
        )
    except RuntimeError as loudnorm_error:
        # 纯静音轨的响度是 -inf，部分 ffmpeg loudnorm 会产生 NaN。
        # 用不改变声道语义的动态归一化重试，仍保证完整交付。
        audio_normalization = "dynaudnorm_fallback"
        destination.unlink(missing_ok=True)
        try:
            _run_ffmpeg(
                command_with_normalizer("dynaudnorm=f=150:g=15:p=0.95,alimiter=limit=0.95"),
                timeout=timeout,
                context="最终转场与静音兼容归一化",
            )
        except RuntimeError as fallback_error:
            raise RuntimeError(f"{loudnorm_error}；归一化降级也失败：{fallback_error}") from fallback_error
    if not destination.is_file() or destination.stat().st_size <= 0:
        raise RuntimeError("最终编辑未产出有效文件")
    return {
        "total_duration_s": round(cumulative, 3),
        "transitions": reports,
        "audio_normalization": audio_normalization,
    }


def render_episode_final_edit(
    conn: Any,
    episode_id: str,
    piece_specs: list[tuple[int, str, float]],
    destination: Path,
    work_dir: Path,
) -> dict[str, Any]:
    """尝试完成确定性最终编辑；失败信息由调用方用于回退硬拼。"""
    rows = conn.execute(
        "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no",
        (episode_id,),
    ).fetchall()
    shot_by_no = {int(row["shot_no"]): shot_from_row(row) for row in rows}
    ordered_shots = [shot_by_no[shot_no] for shot_no, _path, _rate in piece_specs]
    owners, text_warnings = _text_owners(ordered_shots)
    prepared: list[dict[str, Any]] = []
    text_failures: list[dict[str, Any]] = []
    for shot_no, source_path, rate in piece_specs:
        shot = shot_by_no[shot_no]
        required = shot.required_text
        exact = (required.exact_text or "").strip() if required else ""
        text_enabled = bool(exact and owners.get(exact) == shot_no)
        try:
            item = _prepare_clip(
                source_path,
                work_dir / f"prepared-{shot_no}.mp4",
                shot=shot,
                playback_rate=rate,
                text_enabled=text_enabled,
                work_dir=work_dir,
            )
        except Exception as exc:
            # 文字层失败时先尝试不带文字的统一规格，不让内容后期阻断成片。
            if not text_enabled:
                raise
            text_failures.append({
                "shot_no": shot_no,
                "exact_text": exact,
                "error": str(exc),
                "runtime_blocking": False,
            })
            item = _prepare_clip(
                source_path,
                work_dir / f"prepared-{shot_no}.mp4",
                shot=shot,
                playback_rate=rate,
                text_enabled=False,
                work_dir=work_dir,
            )
        prepared.append(item)

    # transition 存在“后一镜”上，表示它如何从前一镜进入。部分合成跨过
    # 缺镜时不能假装两镜直接连续，固定使用硬切，待中间镜头完成后再重算转场。
    transition_specs = [
        transition_spec(current.transition)
        if current.shot_no == previous.shot_no + 1
        else transition_spec("硬切")
        for previous, current in zip(ordered_shots, ordered_shots[1:])
    ]
    compose_report = _compose(prepared, transition_specs, destination)
    return {
        "ok": True,
        "prepared_shots": len(prepared),
        "text_inserts": [item["text_insert"] for item in prepared if item.get("text_insert")],
        "text_warnings": text_warnings,
        "text_failures": text_failures,
        "boundary_report": boundary_report(ordered_shots),
        **compose_report,
    }
