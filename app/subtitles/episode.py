"""整集字幕编排（PRD §4/§9/§10）。L4：碰 store/engine 缓存与调度、字体解析
（复用 ``app.final_edit._font_path``）、说话人展示名解析（复用
``app.production.storyboard_speech_render``）。

``app.final_edit._compose`` 反向需要本模块的 ``compose_artifacts``：两个模块
同层（L4）互相需要对方的符号，final_edit.py 一侧一律用函数内延迟 import 打破
循环——本文件在模块顶层 import ``app.final_edit._font_path`` 是安全的，只要
final_edit.py 不在模块顶层 import 回本模块（见该文件 ``_compose`` 内注释）。

不 import ``app.media_exec.*``（L5，调用方）——那会是真正的上行边。
"""
from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.atomic_io import atomic_write_text
from app.final_edit import _font_path
from app.media_urls import build_media_url
from app.production.storyboard_identity_contract import effective_delivery_kind
from app.production.storyboard_speech_render import speaker_names
from app.subtitles import align, engine, settings, store
from app.subtitles.align import LineSpec, ShotAlignment
from app.subtitles.ass import SubtitleStyle, ffmpeg_ass_filter, font_family_from_file, render_ass, render_srt
from app.subtitles.cues import Cue, build_cues
from app.subtitles.timeline import PieceTiming, place_on_timeline

_LEGACY_DELIVERY_KIND = {"narration": "narration", "offscreen_voice": "offscreen_dialogue"}


@dataclass(frozen=True)
class ShotSubtitlePlan:
    shot_no: int
    version_id: str
    cues: tuple[Cue, ...]
    alignment: ShotAlignment
    cache_hit: bool


@dataclass(frozen=True)
class EpisodeSubtitlePlan:
    shots: dict[int, ShotSubtitlePlan]
    style: SubtitleStyle
    fonts_dir: Path
    engine_id: str
    model_id: str
    asr_elapsed_s: float
    asr_shots: int
    cache_hits: int


@dataclass(frozen=True)
class EpisodeSubtitleArtifacts:
    ass_path: Path
    ass_text: str
    cues: tuple[Cue, ...]
    filter_arg: str


# ---------------------------------------------------------------------------
# 台词账本 → LineSpec（优先分镜 2.x 合同，退回旧版 shots.dialogues）
# ---------------------------------------------------------------------------


def _row_field(shot_row: Any, key: str, default: Any = None) -> Any:
    if isinstance(shot_row, dict):
        return shot_row.get(key, default)
    return shot_row[key] if key in shot_row.keys() else default


def _speaker_display_name(identity_id: str, names: dict[str, str]) -> str:
    if identity_id in names:
        return names[identity_id]
    return identity_id.split(":", 1)[-1] if identity_id else ""


def _segment_line_specs(segment: dict, dialogue: list) -> list[LineSpec]:
    names = speaker_names(segment)
    specs: list[LineSpec] = []
    for index, item in enumerate(dialogue, start=1):
        if not isinstance(item, dict):
            continue
        identity = str(item.get("speaker_identity_id") or "")
        specs.append(LineSpec(
            utterance_id=str(item.get("utterance_id") or f"U{index:02d}"),
            text=str(item.get("line") or ""),
            speaker=_speaker_display_name(identity, names),
            delivery_kind=str(item.get("delivery_kind") or effective_delivery_kind(item)),
        ))
    return specs


def _legacy_line_specs(legacy: list) -> list[LineSpec]:
    specs: list[LineSpec] = []
    for index, item in enumerate(legacy, start=1):
        if not isinstance(item, dict):
            continue
        delivery = str(item.get("delivery") or "spoken_dialogue")
        specs.append(LineSpec(
            utterance_id=f"L{index:02d}",
            text=str(item.get("line") or ""),
            speaker=str(item.get("speaker") or ""),
            delivery_kind=_LEGACY_DELIVERY_KIND.get(delivery, "spoken_dialogue"),
        ))
    return specs


def shot_line_specs(shot_row: Any) -> list[LineSpec]:
    """优先 ``shot_contract_json.storyboard_pack_segment.dialogue[]``；没有则
    退回旧版 ``shots.dialogues[]``（speaker/line/delivery）。"""
    try:
        contract = json.loads(_row_field(shot_row, "shot_contract_json") or "null")
    except (TypeError, ValueError):
        contract = None
    segment = contract.get("storyboard_pack_segment") if isinstance(contract, dict) else None
    dialogue = (segment or {}).get("dialogue") if isinstance(segment, dict) else None
    if dialogue:
        return _segment_line_specs(segment, dialogue)
    try:
        legacy = json.loads(_row_field(shot_row, "dialogues") or "[]")
    except (TypeError, ValueError):
        legacy = []
    return _legacy_line_specs(legacy)


# ---------------------------------------------------------------------------
# 整集编排：缓存命中/未命中分流 → 一次 ASR 批处理 → 逐镜对齐 → 逐镜 cue
# ---------------------------------------------------------------------------


def _shot_rows_by_no(conn: Any, episode_id: str) -> dict[int, Any]:
    rows = conn.execute(
        "SELECT * FROM shots WHERE episode_id=? ORDER BY shot_no", (episode_id,),
    ).fetchall()
    return {int(row["shot_no"]): row for row in rows}


def _collect_jobs_and_cache(
    conn: Any, piece_specs: list[tuple[int, str, float]], probe_by_shot: dict[int, dict],
    manifest_by_shot: dict[int, dict], shot_rows: dict[int, Any], model_id: str,
) -> tuple[dict[str, Path], dict[int, tuple[str, str, list[LineSpec]]], dict[int, tuple[dict, list[LineSpec], str]]]:
    """按 (shot_version_id, media_sha256, model_id) 查缓存；未命中且有音轨的收集进
    jobs（无音轨镜头不提交 ASR——真实 ffmpeg 对无音轨视频抽音轨会失败，
    ``align.align_shot(..., has_audio=False)`` 本就不看 tokens，见
    ``_build_shot_plans`` 的 pending 分支；无音轨镜头仍走 pending，只是永远拿不到
    对应的 asr_results 条目，自然产出全 missing/no_audio）。
    """
    jobs: dict[str, Path] = {}
    pending: dict[int, tuple[str, str, list[LineSpec]]] = {}
    cached: dict[int, tuple[dict, list[LineSpec], str]] = {}
    for shot_no, path, _rate in piece_specs:
        manifest_item = manifest_by_shot.get(shot_no)
        row = shot_rows.get(shot_no)
        if manifest_item is None or row is None:
            continue
        version_id = str(manifest_item["adopted_version_id"])
        media_sha = str(manifest_item["file_sha256"])
        lines = shot_line_specs(row)
        cached_result = store.get_alignment(
            conn, shot_version_id=version_id, media_sha256=media_sha, model_id=model_id,
        )
        if cached_result is not None:
            cached[shot_no] = (cached_result, lines, version_id)
            continue
        if bool((probe_by_shot.get(shot_no) or {}).get("has_audio", True)):
            jobs[version_id] = Path(path)
        pending[shot_no] = (version_id, media_sha, lines)
    return jobs, pending, cached


def _build_shot_plans(
    piece_specs: list[tuple[int, str, float]], probe_by_shot: dict[int, dict],
    cached: dict[int, tuple[dict, list[LineSpec], str]],
    pending: dict[int, tuple[str, str, list[LineSpec]]],
    asr_results: dict[str, Any], status: engine.EngineStatus, max_chars_per_line: int,
) -> tuple[dict[int, ShotSubtitlePlan], int]:
    shots: dict[int, ShotSubtitlePlan] = {}
    cache_hits = 0
    for shot_no, _path, rate in piece_specs:
        if shot_no in cached:
            result_dict, _lines, version_id = cached[shot_no]
            alignment = align.alignment_from_dict(result_dict)
            cache_hits += 1
        elif shot_no in pending:
            version_id, media_sha, lines = pending[shot_no]
            asr_result = asr_results.get(version_id)
            tokens = tuple(
                align.AsrToken(text=t, start_s=s) for t, s in (asr_result.tokens if asr_result else ())
            )
            has_audio = bool((probe_by_shot.get(shot_no) or {}).get("has_audio", True))
            alignment = align.align_shot(lines, tokens, has_audio=has_audio)
            store.put_alignment(
                shot_version_id=version_id, media_sha256=media_sha, engine_id=status.engine_id,
                model_id=status.model_id, result=align.alignment_to_dict(alignment),
            )
        else:
            continue
        video_duration_s = float((probe_by_shot.get(shot_no) or {}).get("video_duration_s") or 0.0)
        effective_duration_s = max(0.05, video_duration_s / rate)
        shot_cues = tuple(build_cues(
            shot_no, alignment, rate=rate, effective_duration_s=effective_duration_s,
            max_chars_per_line=max_chars_per_line,
        ))
        shots[shot_no] = ShotSubtitlePlan(
            shot_no=shot_no, version_id=version_id, cues=shot_cues, alignment=alignment,
            cache_hit=shot_no in cached,
        )
    return shots, cache_hits


def prepare_episode_subtitles(
    conn: Any, *, episode_id: str, piece_specs: list[tuple[int, str, float]],
    probe_by_shot: dict[int, dict], manifest_items: list[dict],
) -> EpisodeSubtitlePlan:
    """引擎不就绪直接抛 ``AsrEngineError``（在任何编码开始之前失败）；否则逐镜
    走缓存或一次批量 ASR，返回整集字幕方案。"""
    status = engine.engine_status()
    if not status.ok:
        raise engine.AsrEngineError("字幕语音识别引擎不可用：" + "；".join(status.problems))
    font_path = _font_path()
    style = settings.style_from_settings(font_family=font_family_from_file(font_path))
    manifest_by_shot = {int(item["shot_no"]): item for item in manifest_items}
    shot_rows = _shot_rows_by_no(conn, episode_id)
    jobs, pending, cached = _collect_jobs_and_cache(
        conn, piece_specs, probe_by_shot, manifest_by_shot, shot_rows, status.model_id,
    )
    started_at = time.perf_counter()
    asr_results = engine.transcribe_media(jobs) if jobs else {}
    asr_elapsed_s = round(time.perf_counter() - started_at, 3) if jobs else 0.0
    shots, cache_hits = _build_shot_plans(
        piece_specs, probe_by_shot, cached, pending, asr_results, status, style.max_chars_per_line,
    )
    return EpisodeSubtitlePlan(
        shots=shots, style=style, fonts_dir=font_path.parent, engine_id=status.engine_id,
        model_id=status.model_id, asr_elapsed_s=asr_elapsed_s, asr_shots=len(jobs), cache_hits=cache_hits,
    )


def plan_or_none(
    conn: Any, *, episode_id: str, piece_specs: list[tuple[int, str, float]],
    probe_by_shot: dict[int, dict], manifest_items: list[dict],
) -> EpisodeSubtitlePlan | None:
    """concat.py 专用封装：开关关闭返回 None；引擎/对齐失败转成用户可读 ValueError。"""
    if not settings.burn_in_enabled():
        return None
    try:
        return prepare_episode_subtitles(
            conn, episode_id=episode_id, piece_specs=piece_specs,
            probe_by_shot=probe_by_shot, manifest_items=manifest_items,
        )
    except (engine.AsrEngineError, RuntimeError) as exc:
        raise ValueError(f"字幕嵌入未就绪：{exc}；上一版成片仍保留") from exc


# ---------------------------------------------------------------------------
# 渲染：镜内 cue → 整集时间轴 → ASS/SRT
# ---------------------------------------------------------------------------


def write_episode_ass(
    plan: EpisodeSubtitlePlan, pieces: Sequence[PieceTiming], work_dir: Path,
) -> EpisodeSubtitleArtifacts:
    cues_by_shot = {shot_no: shot.cues for shot_no, shot in plan.shots.items()}
    timeline_cues = tuple(place_on_timeline(cues_by_shot, pieces))
    ass_text = render_ass(timeline_cues, plan.style)
    ass_path = Path(work_dir) / "episode.ass"
    ass_path.write_text(ass_text, encoding="utf-8")
    filter_arg = ffmpeg_ass_filter(ass_path, plan.fonts_dir)
    return EpisodeSubtitleArtifacts(ass_path=ass_path, ass_text=ass_text, cues=timeline_cues, filter_arg=filter_arg)


def write_episode_ass_sequential(
    plan: EpisodeSubtitlePlan, piece_durations: list[tuple[int, float]], work_dir: Path,
) -> EpisodeSubtitleArtifacts:
    """``concat.py`` draft_concat 专用：全部零转场顺序拼接（xfade_before_s 恒为 0）。"""
    pieces = [PieceTiming(shot_no, dur, 0.0) for shot_no, dur in piece_durations]
    return write_episode_ass(plan, pieces, work_dir)


def compose_artifacts(
    prepared: list[dict[str, Any]], reports: list[dict[str, Any]],
    subtitle_plan: EpisodeSubtitlePlan | None, work_dir: Path | None,
) -> EpisodeSubtitleArtifacts | None:
    """``final_edit._compose`` 专用：按已完成的转场时长表拼 PieceTiming 并渲染整集 ASS。"""
    if subtitle_plan is None or work_dir is None:
        return None
    pieces = [PieceTiming(int(prepared[0]["shot_no"]), float(prepared[0]["duration_s"]), 0.0)]
    pieces.extend(
        PieceTiming(
            int(prepared[i]["shot_no"]), float(prepared[i]["duration_s"]), float(reports[i - 1]["duration_s"]),
        )
        for i in range(1, len(prepared))
    )
    return write_episode_ass(subtitle_plan, pieces, work_dir)


# ---------------------------------------------------------------------------
# 报告与边车文件
# ---------------------------------------------------------------------------


def _line_report_entries(plan: EpisodeSubtitlePlan) -> tuple[list[dict], list[dict], list[dict], int]:
    lines: list[dict] = []
    missing: list[dict] = []
    extra_speech: list[dict] = []
    lines_aligned = 0
    for shot_no in sorted(plan.shots):
        shot = plan.shots[shot_no]
        for line in shot.alignment.lines:
            entry = {
                "shot_no": shot_no, "utterance_id": line.utterance_id, "status": line.status,
                "match_ratio": round(line.match_ratio, 3), "start_s": line.start_s, "end_s": line.end_s,
            }
            if line.status == "aligned":
                lines_aligned += 1
                entry.update(
                    exact_chars=line.exact_chars, matched_chars=line.matched_chars, total_chars=line.total_chars,
                )
            else:
                missing.append({
                    "shot_no": shot_no, "utterance_id": line.utterance_id, "line": line.text,
                    "match_ratio": round(line.match_ratio, 3), "reason": line.reason,
                })
            lines.append(entry)
        extra_speech.extend(
            {"shot_no": shot_no, "text": e.text, "start_s": e.start_s, "end_s": e.end_s}
            for e in shot.alignment.extra_speech
        )
    return lines, missing, extra_speech, lines_aligned


def report_section(plan: EpisodeSubtitlePlan | None, artifacts: EpisodeSubtitleArtifacts | None) -> dict[str, Any]:
    """关闭时只写 ``{"enabled": false}``，见 ``episode.edit-report.json`` 契约。"""
    if plan is None:
        return {"enabled": False}
    lines, missing, extra_speech, lines_aligned = _line_report_entries(plan)
    cues_timeline = [
        {"shot_no": c.shot_no, "utterance_id": c.utterance_id, "text": c.text, "start_s": c.start_s, "end_s": c.end_s}
        for c in (artifacts.cues if artifacts else ())
    ]
    ass_text = artifacts.ass_text if artifacts else ""
    srt_text = render_srt(artifacts.cues) if artifacts else ""
    return {
        "enabled": True, "engine_id": plan.engine_id, "model_id": plan.model_id,
        "font_family": plan.style.font_family,
        "lines_total": len(lines), "lines_aligned": lines_aligned, "lines_missing": len(missing),
        "cues": len(cues_timeline),
        "missing": missing, "extra_speech": extra_speech, "lines": lines, "cues_timeline": cues_timeline,
        "ass_text": ass_text, "ass_sha256": hashlib.sha256(ass_text.encode("utf-8")).hexdigest(),
        "srt_sha256": hashlib.sha256(srt_text.encode("utf-8")).hexdigest(),
        "cache_hits": plan.cache_hits, "asr_shots": plan.asr_shots, "asr_elapsed_s": plan.asr_elapsed_s,
    }


def trim_report_for_projection(report: dict | None) -> dict | None:
    """``episode_mix_status`` 用：去掉体积大又对轮询无用的 ass_text/cues_timeline；
    只处理内存副本，落盘的报告文件不受影响。"""
    if not isinstance(report, dict) or not isinstance(report.get("subtitles"), dict):
        return report
    trimmed = dict(report)
    trimmed["subtitles"] = {k: v for k, v in report["subtitles"].items() if k not in ("ass_text", "cues_timeline")}
    return trimmed


def _write_if_changed(path: Path, text: str) -> None:
    if path.is_file():
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest == hashlib.sha256(text.encode("utf-8")).hexdigest():
            return
    atomic_write_text(path, text)


def materialize_sidecars(final_path: Path, report: dict[str, Any]) -> None:
    """幂等（内容 sha256 相同不重写）；``subtitles.enabled`` 非真时删除旧边车。"""
    srt_path = final_path.with_name("episode.srt")
    ass_path = final_path.with_name("episode.ass")
    subtitles = report.get("subtitles") if isinstance(report, dict) else None
    if not isinstance(subtitles, dict) or not subtitles.get("enabled"):
        srt_path.unlink(missing_ok=True)
        ass_path.unlink(missing_ok=True)
        return
    cues = tuple(
        Cue(shot_no=c["shot_no"], utterance_id=c["utterance_id"], text=c["text"], start_s=c["start_s"], end_s=c["end_s"])
        for c in subtitles.get("cues_timeline") or []
    )
    _write_if_changed(srt_path, render_srt(cues))
    _write_if_changed(ass_path, str(subtitles.get("ass_text") or ""))


def srt_sidecar_url(final_path: Path, report: dict[str, Any] | None) -> str | None:
    """挂产物信号，不挂状态字段：文件存在且哈希与报告一致才给 URL。"""
    srt_path = final_path.with_name("episode.srt")
    subtitles = report.get("subtitles") if isinstance(report, dict) else None
    if not srt_path.is_file() or not isinstance(subtitles, dict):
        return None
    expected = str(subtitles.get("srt_sha256") or "")
    if not expected or hashlib.sha256(srt_path.read_bytes()).hexdigest() != expected:
        return None
    return build_media_url(srt_path, version=expected[:12])
