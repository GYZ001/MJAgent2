"""成片合成三项增强的唯一编排入口：``app.media_exec.concat.concatenate_episode``
只调用这一个函数（见该模块的两处调用点），本包所有其余模块都只服务于它。

三个项目开关（``app.project_settings``）全部关闭时，本函数不产生任何文件 IO、
直接原样返回调用方传入的 ``candidate_path``/时长/字幕——"开关全关时成片逐字
节行为不变"（见 ``tests/test_final_edit_enhance_apply.py`` 的回归用例）。

任何一步失败（模型调用异常、曲库/音色不可用、ffmpeg 失败……）都在本函数内部
兜底：捕获后退回"这一项跳过"，绝不让新增的增强层拖垮已经跑通的主交付流程
（与 ``app.final_edit`` 质量增强失败必须回退硬拼是同一条设计原则）。调用方
因此不需要包一层 try/except——``concat.py`` 的"最少接线"就是一次直接调用。
"""
from __future__ import annotations

import asyncio
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.final_edit_enhance import context as context_mod
from app.final_edit_enhance import music_mix, monologue_audio, monologue_burn, plan_generate, plan_store, silence, teaser
from app.final_edit_enhance.ffutil import probe_duration_s
from app.final_edit_enhance.music_library import MusicLibrary, load_music_library, resolve_library_dir
from app.final_edit_enhance.music_runs import MusicRun, build_music_runs
from app.final_edit_enhance.subtitle_shift import shift_and_augment_subtitles
from app.db import get_setting
from app.project_settings import enhance_monologue_enabled, enhance_music_bed_enabled, enhance_teaser_enabled
from app.subtitles.ass import SubtitleStyle

MONOLOGUE_MIN_WINDOW_S = 30.0  # 冻结常量，见 pilot 设计文档「冻结的关键常量」


@dataclass(frozen=True)
class EnhancementResult:
    video_path: Path
    duration_s: float
    subtitles: dict[str, Any]
    enhancements: dict[str, Any]


def _disabled_fragment(reason: str) -> dict[str, Any]:
    return {"applied": False, "reason": reason}


def _attach_rejected(fragment: dict[str, Any], dropped: tuple[dict[str, Any], ...], feature: str) -> dict[str, Any]:
    """把编排计划里模型提名过、但被 ``plan_validate`` 判定不满足而丢弃的同
    ``feature`` 条目合并进展示片段——"不满足的条目丢弃并在报告里写明原因"
    不能只写进内部缓存文件，用户必须在成片报告里看到模型提名了什么、为什么
    没被采用（而不仅仅是一个已采纳条目的精简清单）。"""
    items = [{"item": d["item"], "reason": d["reason"]} for d in dropped if d["feature"] == feature]
    return {**fragment, "rejected": items} if items else fragment


def _switches(conn: Any, project_id: str) -> tuple[bool, bool, bool]:
    return (
        enhance_music_bed_enabled(conn, project_id),
        enhance_teaser_enabled(conn, project_id),
        enhance_monologue_enabled(conn, project_id),
    )


def _unchanged_result(candidate_path: Path, base_duration_s: float, subtitles: dict[str, Any], reasons: dict[str, str]) -> EnhancementResult:
    return EnhancementResult(
        video_path=candidate_path, duration_s=base_duration_s, subtitles=subtitles,
        enhancements={
            "music_bed": _disabled_fragment(reasons["music_bed"]),
            "teaser": _disabled_fragment(reasons["teaser"]),
            "monologue": _disabled_fragment(reasons["monologue"]),
        },
    )


def _music_bed_stage(
    music_on: bool, library: MusicLibrary | None, plan: plan_generate.EnhancementPlan, context: context_mod.EpisodeContext, work_dir: Path,
) -> tuple[Path | None, dict[str, Any]]:
    if not music_on:
        return None, _disabled_fragment("项目未开启统一配乐")
    if library is None:
        return None, _disabled_fragment("曲库缺失：目录下没有可用的 manifest.json 或音频文件")
    if not plan.music_cues:
        return None, _disabled_fragment("编排计划未给出任何可用的配乐提名")
    cue_by_shot = {c.shot_no: c.track_id for c in plan.music_cues}
    timeline = [(s.shot_no, s.start_s, s.duration_s) for s in context.shots]
    runs: list[MusicRun] = build_music_runs(cue_by_shot, timeline)
    try:
        music_bed_path = music_mix.build_music_bed(runs, library, work_dir)
    except (RuntimeError, ValueError) as exc:
        return None, _disabled_fragment(f"配乐渲染失败：{exc}")
    tracks_used = sorted({r.track_id for r in runs if r.track_id is not None})
    fragment = {
        "applied": True, "reason": "",
        "tracks": [{"track_id": t, "title": library.by_id(t).title} for t in tracks_used],
        "cues": [{"shot_no": c.shot_no, "track_id": c.track_id} for c in plan.music_cues],
    }
    return music_bed_path, fragment


async def _monologue_stage(
    conn: Any, project_id: str, monologue_on: bool, plan: plan_generate.EnhancementPlan, work_dir: Path,
) -> tuple[list[monologue_audio.MonologueAudioItem], dict[str, Any]]:
    if not monologue_on:
        return [], _disabled_fragment("项目未开启主角内心独白")
    if not plan.monologue_lines:
        return [], _disabled_fragment("编排计划未给出任何可用的独白台词")
    applied, skipped = await monologue_audio.synthesize_monologue_lines(conn, project_id, plan.monologue_lines, work_dir)
    if not applied:
        reason = "；".join(s["reason"] for s in skipped) or "没有独白台词成功合成"
        return [], _disabled_fragment(reason[:500])
    fragment = {
        "applied": True, "reason": "",
        "lines": [{"character_name": i.character_name, "text": i.text, "start_s": i.start_s} for i in applied],
        "skipped": skipped,
    }
    return applied, fragment


def _teaser_stage(
    teaser_on: bool, plan: plan_generate.EnhancementPlan, piece_specs: list[tuple[int, str, float]],
    play_res: tuple[int, int], main_video_path: Path, work_dir: Path,
) -> tuple[Path, float, dict[str, Any]]:
    if not teaser_on:
        return main_video_path, 0.0, _disabled_fragment("项目未开启片头预告")
    if not plan.teaser_clips:
        return main_video_path, 0.0, _disabled_fragment("编排计划未给出任何可用的预告片段")
    try:
        teaser_path = teaser.build_teaser(plan.teaser_clips, piece_specs, play_res, work_dir)
        merged_path = teaser.prepend_teaser(teaser_path, main_video_path, play_res, work_dir)
        offset_s = probe_duration_s(str(teaser_path))
    except (RuntimeError, ValueError) as exc:
        return main_video_path, 0.0, _disabled_fragment(f"预告片剪辑失败：{exc}")
    fragment = {
        "applied": True, "reason": "", "duration_s": round(offset_s, 2),
        "clips": [{"shot_no": c.shot_no, "start_s": c.start_s, "end_s": c.end_s, "reason": c.reason} for c in plan.teaser_clips],
    }
    return merged_path, offset_s, fragment


def _monologue_style(style: SubtitleStyle | None) -> SubtitleStyle:
    return style if style is not None else monologue_burn.default_style()


async def _run(
    conn: Any, *, ep_row: Any, episode_id: str, candidate_path: Path, final_path: Path, subtitles_section: dict[str, Any],
    piece_specs: list[tuple[int, str, float]], play_res: tuple[int, int], style: SubtitleStyle | None,
    base_duration_s: float, work_dir: Path, video_delivery_manifest_hash: str,
) -> EnhancementResult:
    project_id = str(ep_row["project_id"])
    music_on, teaser_on, monologue_on = _switches(conn, project_id)
    if not (music_on or teaser_on or monologue_on):
        reason = "项目未开启该增强"
        return _unchanged_result(candidate_path, base_duration_s, subtitles_section, {
            "music_bed": reason, "teaser": reason, "monologue": reason,
        })

    context = context_mod.build_episode_context(conn, ep_row, piece_specs)
    library = load_music_library(resolve_library_dir(get_setting("music_library_dir")))
    cues = subtitles_section.get("cues_timeline") or [] if subtitles_section.get("enabled") else []
    dialogue_spans = [(float(c["start_s"]), float(c["end_s"])) for c in cues]
    windows = silence.windows_at_least(silence.speech_free_windows(dialogue_spans, base_duration_s), MONOLOGUE_MIN_WINDOW_S)
    fingerprint = plan_generate.plan_fingerprint(
        manifest_hash=video_delivery_manifest_hash, context=context, library=library,
        switches=(music_on, teaser_on, monologue_on),
    )
    cache = plan_store.cache_path(final_path)
    plan = plan_store.load_cached_plan(cache, fingerprint)
    if plan is None:
        plan = await plan_generate.generate_plan(
            context=context, library=library, windows=windows, episode_id=episode_id, fingerprint=fingerprint,
        )
        plan_store.save_plan(cache, fingerprint, plan)

    music_bed_path, music_fragment = _music_bed_stage(music_on, library, plan, context, work_dir)
    monologue_items, monologue_fragment = await _monologue_stage(conn, project_id, monologue_on, plan, work_dir)
    monologue_track_path = monologue_audio.build_monologue_track(monologue_items, base_duration_s, work_dir) if monologue_items else None

    video_path = candidate_path
    if music_bed_path is not None or monologue_track_path is not None:
        # 只用独白真正播放的区间做"独白播放时配乐再压低"——不能传全部候选静默
        # 窗口，否则只开配乐不开独白时，任意 >=30s 的空镜也会被无端压低音量
        # （2026-09-28 评审发现）。
        monologue_play_windows = [(i.start_s, i.start_s + i.duration_s) for i in monologue_items]
        video_path = music_mix.mix_audio_track(
            candidate_path, music_bed_path, monologue_track_path, monologue_play_windows, base_duration_s, work_dir,
        )
    if monologue_items:
        video_path = monologue_burn.burn_monologue_captions(
            video_path, monologue_items, _monologue_style(style), play_res, work_dir, base_duration_s,
        )

    final_path, offset_s, teaser_fragment = _teaser_stage(teaser_on, plan, piece_specs, play_res, video_path, work_dir)
    new_duration_s = probe_duration_s(str(final_path)) if offset_s > 0 else base_duration_s
    updated_subtitles = shift_and_augment_subtitles(
        subtitles_section, offset_s=offset_s, monologue_items=monologue_items,
        style=_monologue_style(style), play_res=play_res,
    )
    enhancements = {
        "music_bed": _attach_rejected(music_fragment, plan.dropped, "music_bed"),
        "teaser": _attach_rejected(teaser_fragment, plan.dropped, "teaser"),
        "monologue": _attach_rejected(monologue_fragment, plan.dropped, "monologue"),
    }
    return EnhancementResult(video_path=final_path, duration_s=new_duration_s, subtitles=updated_subtitles, enhancements=enhancements)


async def apply_enhancements(
    conn: Any, *, ep_row: Any, episode_id: str, candidate_path: Path, final_path: Path, subtitles_section: dict[str, Any],
    piece_specs: list[tuple[int, str, float]], play_res: tuple[int, int], style: SubtitleStyle | None,
    base_duration_s: float, work_dir: Path, video_delivery_manifest_hash: str,
) -> EnhancementResult:
    try:
        return await _run(
            conn, ep_row=ep_row, episode_id=episode_id, candidate_path=candidate_path, final_path=final_path,
            subtitles_section=subtitles_section, piece_specs=piece_specs, play_res=play_res, style=style,
            base_duration_s=base_duration_s, work_dir=work_dir,
            video_delivery_manifest_hash=video_delivery_manifest_hash,
        )
    except Exception as exc:  # noqa: BLE001 - 增强层失败必须回退到未增强的原始交付
        reason = f"成片增强整体失败，已回退到未增强版本：{type(exc).__name__}: {exc}"[:500]
        traceback.print_exc()
        return _unchanged_result(candidate_path, base_duration_s, subtitles_section, {
            "music_bed": reason, "teaser": reason, "monologue": reason,
        })


def apply_enhancements_sync(
    conn: Any, *, ep_row: Any, episode_id: str, candidate_path: Path, final_path: Path, subtitles_section: dict[str, Any],
    piece_specs: list[tuple[int, str, float]], play_res: tuple[int, int], style: SubtitleStyle | None,
    base_duration_s: float, work_dir: Path, video_delivery_manifest_hash: str,
) -> EnhancementResult:
    """``app.media_exec.concat.concatenate_episode`` 是同步函数、且已经跑在
    ``asyncio.to_thread`` 派生的工作线程里（见 ``app.capabilities.handlers.
    delivery``/``app.domain.video_ops.misc`` 的调用点）——该线程没有正在运行
    的事件循环，``asyncio.run`` 在这里开一个新循环是安全的，不会与主线程的
    循环冲突。"""
    return asyncio.run(apply_enhancements(
        conn, ep_row=ep_row, episode_id=episode_id, candidate_path=candidate_path, final_path=final_path,
        subtitles_section=subtitles_section, piece_specs=piece_specs, play_res=play_res, style=style,
        base_duration_s=base_duration_s, work_dir=work_dir, video_delivery_manifest_hash=video_delivery_manifest_hash,
    ))
