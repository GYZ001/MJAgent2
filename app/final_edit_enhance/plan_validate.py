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
from app.final_edit_enhance.plan_schema import (
    TEASER_CLIP_LENGTH_S, MonologueLineDraft, MusicCueDraft, TeaserClipDraft,
)

TEASER_TOTAL_MIN_S = 8.0
TEASER_TOTAL_MAX_S = 12.0
TEASER_CLIP_COUNT_MIN = 3
# 片段时长固定为 TEASER_CLIP_LENGTH_S=3 秒后，总长由「段数 × 3 秒」决定：
# 3 段=9s、4 段=12s，都落在 8-12s 目标区间；5 段会是 15s，超出既有总长预算，
# 所以上限从（可变片长时代的）5 段收紧到 4 段——不是棘轮意义上的收紧质量
# 门槛，是配合片长改为固定值之后重新推导的同一个总长约束。
TEASER_CLIP_COUNT_MAX = 4
MONOLOGUE_LINE_COUNT_MIN = 3
MONOLOGUE_LINE_COUNT_MAX = 8
MONOLOGUE_SPEECH_RATE_CHARS_PER_S = 4.5
MONOLOGUE_WINDOW_MARGIN_S = 1.0  # 首尾各留 1 秒余量，不把独白贴着台词边界播
# 短剧原文里较短的一句完整内心独白大致的口播时长（按 MONOLOGUE_SPEECH_RATE_
# CHARS_PER_S 换算约 13-14 字，例如"我不会认输"这类 5-12 字的短句量级）——
# 用它反推 app.final_edit_enhance.apply.MONOLOGUE_MIN_WINDOW_S「候选静默
# 窗口至少要多长才可能塞下任意一句独白」，取代原先固定 30 秒的门槛（2026-09-29
# 生产实测：这部台词密集的短剧全集最长的无台词间隙只有 27.7 秒，固定 30s
# 阈值筛出的候选窗口永远是空列表，模型只能自己编造越界的 window_index）。
MONOLOGUE_MIN_LINE_S = 3.0
# 三个 15 秒叙事段的量级：短于此仍是「几秒一换」的翻版——本模块要修的失败模式
# 本身（见 app.final_edit_enhance.plan_generate 系统提示词）。最后一段允许更短
# （全集本身可能就没剩多少），只对非末尾段落强制。
MUSIC_SECTION_MIN_DURATION_S = 45.0


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


def validate_music_sections(
    cues: list[MusicCueDraft], *, context: EpisodeContext,
) -> tuple[list[MusicCueDraft], list[dict[str, Any]], str | None]:
    """把已经通过 ``validate_music_cues`` 的换曲点当「分段起点」核验，按
    ``shot_no`` 排序后逐个检查：与上一个存活换曲点相距不足
    ``MUSIC_SECTION_MIN_DURATION_S`` 就丢弃——前一个换曲点的曲子据此继续
    播放（展开逻辑见 ``app.final_edit_enhance.music_runs.expand_sparse_cues``），
    与本模块「丢弃条目、不整体失败」的既有策略一致。最后一个存活换曲点到
    全集结束这一段不做最短时长校验——收尾段允许因为全集本身较短而不足。

    返回 ``(kept, dropped, first_gap_reason)``：``first_gap_reason`` 不是
    丢弃项，是"首条换曲点没有落在本集第一个参与合成的段"这一提示——片头到
    首条换曲点之间没有配乐是诚实的静音，不强行编造一条覆盖片头的 cue（不
    兜底填充），只在生成阶段作为语义错误要求模型重答一次。
    """
    shots = _shots_by_no(context)
    ordered = sorted((c for c in cues if c.shot_no in shots), key=lambda c: c.shot_no)
    kept: list[MusicCueDraft] = []
    dropped: list[dict[str, Any]] = []
    anchor_start_s: float | None = None
    for cue in ordered:
        start_s = shots[cue.shot_no].start_s
        if anchor_start_s is not None and start_s - anchor_start_s < MUSIC_SECTION_MIN_DURATION_S - 1e-6:
            dropped.append({
                "item": cue.model_dump(),
                "reason": (
                    f"段 {cue.shot_no} 距上一次换曲仅 {start_s - anchor_start_s:.1f}s，"
                    f"不足最短情绪段时长 {MUSIC_SECTION_MIN_DURATION_S:.0f}s，沿用上一首曲子"
                ),
            })
            continue
        kept.append(cue)
        anchor_start_s = start_s
    first_gap_reason = None
    if kept and context.shots and kept[0].shot_no != context.shots[0].shot_no:
        first_gap_reason = (
            f"首条配乐换曲点在段 {kept[0].shot_no}，未覆盖片头段 "
            f"{context.shots[0].shot_no}，片头到首条换曲点之间不会有配乐"
        )
    return kept, dropped, first_gap_reason


def teaser_clip_end_s(clip: TeaserClipDraft) -> float:
    """预告片段的结束秒数不是模型给的，是 ``start_s`` 加固定片长算出来的——
    唯一权威算法，``teaser.py``/``plan_generate.py`` 都要调这个函数，不要
    各自重复 ``start_s + TEASER_CLIP_LENGTH_S``。"""
    return clip.start_s + TEASER_CLIP_LENGTH_S


def _teaser_item_errors(clip: TeaserClipDraft, shots: dict[int, Any]) -> str | None:
    shot = shots.get(clip.shot_no)
    if shot is None:
        return f"段号 {clip.shot_no} 不在本集实际参与合成的段列表中"
    if clip.start_s < 0:
        return f"起始秒 {clip.start_s} 不能为负"
    end_s = teaser_clip_end_s(clip)
    if end_s > shot.duration_s + 1e-6:
        return (
            f"起始秒 {clip.start_s} + 固定片长 {TEASER_CLIP_LENGTH_S:.0f}s = {end_s:.2f}s，"
            f"超出段 {clip.shot_no} 时长 {shot.duration_s:.2f}s"
        )
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
    """片长固定，总长就是「段数 × 固定片长」——不再需要逐条把 end_s-start_s
    加总（草稿里已经没有 end_s 这个字段了）。"""
    return len(clips) * TEASER_CLIP_LENGTH_S


def estimated_speech_span_s(text: str) -> float:
    """一句独白（含首尾余量）预计占用的秒数——``validate_monologue_lines``/
    ``allocate_monologue_placements``（本模块）与
    ``app.final_edit_enhance.apply.MONOLOGUE_MIN_WINDOW_S``（候选窗口筛选
    阈值）共用同一份速率/余量参数，避免两处各自维护一份"多长算够"的判断。"""
    return len(text) / MONOLOGUE_SPEECH_RATE_CHARS_PER_S + 2 * MONOLOGUE_WINDOW_MARGIN_S


def _episode_delivered_lines_condensed(context: EpisodeContext) -> tuple[str, ...]:
    """本集已经播出的台词/旁白（含 ``speaker == "旁白"`` 的叙述句），压成
    纯内容字符串——独白正文不能与这里的任何一句重复或互相包含（见
    ``_monologue_line_error`` 里"已经说出口的话不能再当独白"判据）。"""
    condensed = (textmatch.condense(d.text) for shot in context.shots for d in shot.dialogue)
    return tuple(line for line in condensed if line)


def validate_monologue_lines(
    lines: list[MonologueLineDraft], *, context: EpisodeContext, windows: list[tuple[float, float]],
) -> tuple[list[MonologueLineDraft], list[dict[str, Any]]]:
    """``windows``：调用方（``plan_generate``）算好并原样发给模型的候选静默窗口
    列表，``window_index`` 是其中的下标——不接受模型自己报的秒数。"""
    condensed_source = textmatch.condense(context.source_text)
    delivered_lines = _episode_delivered_lines_condensed(context)
    remaining_capacity = list(windows)
    valid: list[MonologueLineDraft] = []
    dropped: list[dict[str, Any]] = []
    for line in lines:
        item = line.model_dump()
        reason = _monologue_line_error(line, context, condensed_source, windows, delivered_lines)
        if reason is not None:
            dropped.append({"item": item, "reason": reason})
            continue
        start, end = remaining_capacity[line.window_index]
        needed = estimated_speech_span_s(line.text)
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
        needed = estimated_speech_span_s(line.text)
        placements.append((start, start + needed))
        remaining_capacity[line.window_index] = (start + needed, end)
    return placements


def _monologue_line_error(
    line: MonologueLineDraft, context: EpisodeContext, condensed_source: str, windows: list[tuple[float, float]],
    delivered_lines: tuple[str, ...],
) -> str | None:
    if not (0 <= line.window_index < len(windows)):
        return f"静默窗口下标 {line.window_index} 越界（候选共 {len(windows)} 个）"
    if line.character_name not in context.character_roster:
        return f"角色「{line.character_name}」不在本集人物谱中"
    text = (line.text or "").strip()
    if not text:
        return "独白正文为空"
    condensed_line = textmatch.condense(text)
    if condensed_line not in condensed_source:
        return "独白正文不是本集原文的逐字子串"
    if any(condensed_line in existing or existing in condensed_line for existing in delivered_lines):
        return "独白正文与本集已经出现过的台词/旁白重复（或互为子串），不能是已经说出口的话"
    return None
