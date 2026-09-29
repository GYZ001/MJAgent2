"""编排计划草稿的代码核验（模型提名、代码核验；CLAUDE.md 禁止黑白名单/关键词
枚举——这里全部判据都从 ``EpisodeContext``/曲库/静默窗口这些真实数据推导，
不写死镜号、角色名或曲目 ID）。

三个 ``validate_*`` 函数都返回 ``(valid, dropped)``：``dropped`` 是
``{"item": 原始草稿的可 JSON 化摘要, "reason": 中文原因}`` 的列表，供
``app.final_edit_enhance.report`` 原样写进成片报告（"不满足…丢弃并在报告里
写明原因，不兜底编造"）。
"""
from __future__ import annotations

from typing import Any

from app import textmatch
from app.final_edit_enhance.context import EpisodeContext
from app.final_edit_enhance.music_library import MusicLibrary
from app.final_edit_enhance.plan_schema import MonologueLineDraft, MusicCueDraft, TeaserClipDraft

TEASER_TOTAL_MIN_S = 8.0
TEASER_TOTAL_MAX_S = 12.0
TEASER_CLIP_MIN_S = 1.5
TEASER_CLIP_MAX_S = 4.0
TEASER_CLIP_COUNT_MIN = 3
TEASER_CLIP_COUNT_MAX = 5
MONOLOGUE_LINE_COUNT_MIN = 3
MONOLOGUE_LINE_COUNT_MAX = 8
MONOLOGUE_SPEECH_RATE_CHARS_PER_S = 4.5
MONOLOGUE_WINDOW_MARGIN_S = 1.0  # 首尾各留 1 秒余量，不把独白贴着台词边界播


def _shots_by_no(context: EpisodeContext) -> dict[int, Any]:
    return {shot.shot_no: shot for shot in context.shots}


def validate_music_cues(
    cues: list[MusicCueDraft], *, context: EpisodeContext, library: MusicLibrary,
) -> tuple[list[MusicCueDraft], list[dict[str, Any]]]:
    shots = _shots_by_no(context)
    seen_shot_nos: set[int] = set()
    valid: list[MusicCueDraft] = []
    dropped: list[dict[str, Any]] = []
    for cue in cues:
        item = cue.model_dump()
        if cue.shot_no not in shots:
            dropped.append({"item": item, "reason": f"段号 {cue.shot_no} 不在本集实际参与合成的段列表中"})
            continue
        if cue.shot_no in seen_shot_nos:
            dropped.append({"item": item, "reason": f"段号 {cue.shot_no} 重复提名，只保留第一条"})
            continue
        if library.by_id(cue.track_id) is None:
            dropped.append({"item": item, "reason": f"曲目 ID「{cue.track_id}」不在曲库清单中"})
            continue
        seen_shot_nos.add(cue.shot_no)
        valid.append(cue)
    return valid, dropped


def _teaser_item_errors(clip: TeaserClipDraft, shots: dict[int, Any]) -> str | None:
    shot = shots.get(clip.shot_no)
    if shot is None:
        return f"段号 {clip.shot_no} 不在本集实际参与合成的段列表中"
    if not (0 <= clip.start_s < clip.end_s <= shot.duration_s + 1e-6):
        return f"起止秒 [{clip.start_s},{clip.end_s}] 超出段 {clip.shot_no} 时长 {shot.duration_s:.2f}s"
    clip_len = clip.end_s - clip.start_s
    if not (TEASER_CLIP_MIN_S - 1e-6 <= clip_len <= TEASER_CLIP_MAX_S + 1e-6):
        return f"单段片长 {clip_len:.2f}s 不在 {TEASER_CLIP_MIN_S}-{TEASER_CLIP_MAX_S}s 区间"
    return None


def validate_teaser_clips(
    clips: list[TeaserClipDraft], *, context: EpisodeContext,
) -> tuple[list[TeaserClipDraft], list[dict[str, Any]]]:
    shots = _shots_by_no(context)
    valid: list[TeaserClipDraft] = []
    dropped: list[dict[str, Any]] = []
    for clip in clips:
        reason = _teaser_item_errors(clip, shots)
        if reason is not None:
            dropped.append({"item": clip.model_dump(), "reason": reason})
        else:
            valid.append(clip)
    return valid, dropped


def teaser_total_duration_s(clips: list[TeaserClipDraft]) -> float:
    return sum(clip.end_s - clip.start_s for clip in clips)


def _estimated_speech_span_s(text: str) -> float:
    return len(text) / MONOLOGUE_SPEECH_RATE_CHARS_PER_S + 2 * MONOLOGUE_WINDOW_MARGIN_S


def validate_monologue_lines(
    lines: list[MonologueLineDraft], *, context: EpisodeContext, windows: list[tuple[float, float]],
) -> tuple[list[MonologueLineDraft], list[dict[str, Any]]]:
    """``windows``：调用方（``plan_generate``）算好并原样发给模型的候选静默窗口
    列表，``window_index`` 是其中的下标——不接受模型自己报的秒数。"""
    condensed_source = textmatch.condense(context.source_text)
    remaining_capacity = list(windows)
    valid: list[MonologueLineDraft] = []
    dropped: list[dict[str, Any]] = []
    for line in lines:
        item = line.model_dump()
        reason = _monologue_line_error(line, context, condensed_source, windows)
        if reason is not None:
            dropped.append({"item": item, "reason": reason})
            continue
        start, end = remaining_capacity[line.window_index]
        needed = _estimated_speech_span_s(line.text)
        if needed > end - start + 1e-6:
            dropped.append({"item": item, "reason": f"静默窗口 #{line.window_index} 容量不足以放下这句独白"})
            continue
        remaining_capacity[line.window_index] = (start + needed, end)
        valid.append(line)
    return valid, dropped


def allocate_monologue_placements(
    lines: list[MonologueLineDraft], windows: list[tuple[float, float]],
) -> list[tuple[float, float]]:
    """给已经通过 ``validate_monologue_lines`` 的独白（按传入顺序）重新算出各自
    在窗口内实际占用的 ``(start, end)``——不是回退到整段候选窗口的原始边界。

    复用与 ``validate_monologue_lines`` 完全相同的"按 ``remaining_capacity``
    顺序消耗"算法，只在已通过校验的子集上重放：被丢弃的条目本就不消耗容量，
    所以重放结果与校验时完全一致。调用方必须只传入 ``valid``（校验通过的）
    条目，否则窗口下标/容量假设不成立。"""
    remaining_capacity = list(windows)
    placements: list[tuple[float, float]] = []
    for line in lines:
        start, end = remaining_capacity[line.window_index]
        needed = _estimated_speech_span_s(line.text)
        placements.append((start, start + needed))
        remaining_capacity[line.window_index] = (start + needed, end)
    return placements


def _monologue_line_error(
    line: MonologueLineDraft, context: EpisodeContext, condensed_source: str, windows: list[tuple[float, float]],
) -> str | None:
    if not (0 <= line.window_index < len(windows)):
        return f"静默窗口下标 {line.window_index} 越界（候选共 {len(windows)} 个）"
    if line.character_name not in context.character_roster:
        return f"角色「{line.character_name}」不在本集人物谱中"
    text = (line.text or "").strip()
    if not text:
        return "独白正文为空"
    if textmatch.condense(text) not in condensed_source:
        return "独白正文不是本集原文的逐字子串"
    return None
