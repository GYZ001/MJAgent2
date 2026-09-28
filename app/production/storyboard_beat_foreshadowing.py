"""P0-C：伏笔/悬念/类型信号保全核验（2026-09-27）。

背景（docs/AI视频行业方法论对标_可学做法_2026-09-26.md，方法论对标 P0）：
真实回归里出现过"黑鸟跟踪、纸鹤钟声两处玄幻伏笔一处都没被采用"——原文里
埋下的伏笔/悬念/类型信号（不属于本段主线情节，容易被节拍归组时当成闲笔
略过）从节拍表阶段就被漏掉，之后再也没有任何环节能把它捞回来。

与 P0-A 情绪因果（``app.production.storyboard_beat_causality``）结构完全
同构，去掉 stimulus/时序：模型在 ``_AiBeatSheetDraft.foreshadowing_beats``
里逐条提名一个伏笔/类型信号节拍，代码核验这条提名是否真实成立。忠实档/
短剧档共用（挂在基类），判据从数据推导，不设最小/最大数量。

阶段一（``foreshadowing_beat_sheet_rules``）只接入 ``_generate_beat_sheet``
的 ``validate`` lambda（通过 ``ForeshadowingSoftCheck``），**绝不写入
``_validate_beat_sheet_draft``**——同 P0-A，是对已确认缺陷 D-α 的直接修正，
也是验收红线。

阶段二"是否真的被写成画面"（``segment_advisories``）同 P0-A：纯事后
advisory，不参与 ``chat_structured`` 重试，复用已在生产可见的
``degraded_capabilities`` 通道；能力边界同样只能判定"证据文字是否以改写
措辞出现"，不承诺"是否单独成镜"。
"""
from __future__ import annotations

from typing import Any

from app import textmatch
from app.production import storyboard_short_drama as _short_drama
from app.production.screenplay_markers import beat_is_shot
from app.production.storyboard_segment_ranges import evidence_quote_unit_keys


def foreshadowing_beat_sheet_rules() -> list[str]:
    """阶段一 rules[]，两档都无条件追加（不按 adaptation_mode 分支）。"""
    return [
        "找出原文里埋下的伏笔、悬念或类型信号（不属于本段主线情节、容易在"
        "归组节拍时被当成闲笔略过的内容，例如反常的物件、若有若无的跟踪者、"
        "一句意味不明的台词、超自然/类型化的征兆），在 foreshadowing_beats "
        "里逐条提名：beat_id 是这条伏笔/信号所在的节拍，signal_kind 填 "
        "foreshadowing（为后续情节埋下的伏笔/悬念）或 genre_signal（推理/"
        "悬疑/玄幻等类型特征信号），evidence_quote 逐字取自该节拍覆盖的"
        "原文、写出伏笔/信号本身的具体内容（不是概括）。真实案例：原文里"
        "黑鸟跟踪、纸鹤钟声这类玄幻伏笔容易被当成不影响主线的闲笔整段略过——"
        "原文里有几处这类内容就提名几条，不设上限；确实没有时留空，不要为了"
        "填这个字段而硬造原文没有的伏笔。",
    ]


def _signal_problems(
    signal: Any, beats_by_id: dict[str, Any], referenced_beat_ids: set[str],
    source_segments: list[Any], dropped_units: frozenset[tuple[int, int]],
) -> list[str]:
    """单条提名的确定性核验，返回问题文案（空=通过）。四条判据：
    (1) beat 存在 (2) evidence_quote 是该 beat 覆盖原文子串
    (3) beat_id 被某段 beat_ids 引用 (4) 短剧档：证据命中单元不得全部落入
    dropped_source_spans。"""
    beat = beats_by_id.get(signal.beat_id)
    if beat is None:
        return [f"引用的节拍 {signal.beat_id} 不存在"]
    problems: list[str] = []
    covered_text = "".join(
        source_segments[i - 1].text for i in beat.segment_indexes if 1 <= i <= len(source_segments)
    )
    condensed_quote = textmatch.condense(signal.evidence_quote)
    if not condensed_quote or condensed_quote not in textmatch.condense(covered_text):
        problems.append(f"evidence_quote 不是节拍 {signal.beat_id} 覆盖原文的子串")
    if signal.beat_id not in referenced_beat_ids:
        problems.append(f"节拍 {signal.beat_id} 没有被任何段的 beat_ids 引用")
    hit_units = evidence_quote_unit_keys(signal.evidence_quote, beat.segment_indexes, source_segments)
    if hit_units and hit_units <= dropped_units:
        problems.append(f"evidence_quote 所在原文单元 {sorted(hit_units)} 已被声明为删减区间")
    return problems


def _dropped_units_for(draft: Any, source_segments: list[Any]) -> frozenset[tuple[int, int]]:
    """忠实档 ``dropped_source_spans`` 恒缺失，``getattr`` 兜底为空集合，
    与 ``storyboard_beat_causality``/``storyboard_short_drama_hooks`` 同一份
    口径。"""
    spans = getattr(draft, "dropped_source_spans", None) or []
    return frozenset(_short_drama._declared_units(spans, source_segments))


def foreshadowing_signal_errors(draft: Any, source_segments: list[Any]) -> list[str]:
    """聚合 ``draft.foreshadowing_beats`` 的全部 ``_signal_problems``；不接收
    ``adaptation_mode``——判据本身两档都跑。"""
    beats_by_id = {beat.beat_id: beat for beat in draft.beat_sheet}
    referenced_beat_ids = {beat_id for seg in draft.segments for beat_id in seg.beat_ids}
    dropped_units = _dropped_units_for(draft, source_segments)
    errors: list[str] = []
    for signal in draft.foreshadowing_beats:
        errors.extend(
            f"伏笔/类型信号提名 {signal.beat_id}：{problem}"
            for problem in _signal_problems(signal, beats_by_id, referenced_beat_ids, source_segments, dropped_units)
        )
    return errors


class ForeshadowingSoftCheck:
    """前 ``retry_limit`` 次不满足当业务错误打回模型重试，最后一次仍不满足
    则放行、不再产生错误（同 ``EmotionalTurnSoftCheck``/``HookBeatSoftCheck``
    同一套让步策略）。不要 ``adaptation_mode`` 参数——判据本身两档都跑。"""

    def __init__(self, *, retry_limit: int, source_segments: list[Any]) -> None:
        self._retry_limit = retry_limit
        self._attempt = 0
        self._source_segments = source_segments

    def errors(self, draft: Any) -> list[str]:
        problems = foreshadowing_signal_errors(draft, self._source_segments)
        is_last_attempt = self._attempt >= self._retry_limit
        self._attempt += 1
        if not problems or is_last_attempt:
            return []
        return problems


def foreshadowing_summary(draft: Any, source_segments: list[Any]) -> dict[str, Any]:
    """按最终持久化 beat_draft 事后重算。三态：
    ``{"status": "no_signals_nominated", "problem_count": 0}``
    （``foreshadowing_beats`` 为空）；``{"status": "ok"/"warning",
    "problem_count": N}``（N = 有 >=1 条 ``_signal_problems`` 非空的提名数）。
    两档都计算（不像 ``hook_summary`` 那样忠实档返回 None）。"""
    signals = draft.foreshadowing_beats
    if not signals:
        return {"status": "no_signals_nominated", "problem_count": 0}
    beats_by_id = {beat.beat_id: beat for beat in draft.beat_sheet}
    referenced_beat_ids = {beat_id for seg in draft.segments for beat_id in seg.beat_ids}
    dropped_units = _dropped_units_for(draft, source_segments)
    problem_count = sum(
        1 for signal in signals
        if _signal_problems(signal, beats_by_id, referenced_beat_ids, source_segments, dropped_units)
    )
    return {"status": "warning" if problem_count else "ok", "problem_count": problem_count}


def moments_for_segment(segment_beat_ids: list[str], signals: list[Any], covered: set[str]) -> list[Any]:
    """本段（source: ``plan.beat_ids``）首次认领的提名，同 ``storyboard_beat_
    causality.moments_for_segment``——覆盖容量拆分续段完整继承 ``beat_ids``
    这一事实，避免同一处伏笔被连续几个续段反复索要画面。"""
    claimed: list[Any] = []
    for signal in signals:
        if signal.beat_id in segment_beat_ids and signal.beat_id not in covered:
            claimed.append(signal)
            covered.add(signal.beat_id)
    return claimed


def segment_rule_text(signals_here: list[Any], segment_beat_ids: list[str]) -> list[str]:
    """阶段二 per-segment 正面陈述：本段必须给出体现它的画面，不能因为不是
    主线情节就略过。"""
    return [
        f"节拍 {signal.beat_id} 埋着一处伏笔/类型信号「{signal.evidence_quote}」：本段必须给出"
        "体现它的画面，不能因为不是主线情节就略过"
        for signal in signals_here
    ]


def segment_advisories(signals_here: list[Any], prompt_text: str) -> list[str]:
    """非阻断，供 ``_segment_content_advisories`` 合并进 ``degraded_
    capabilities``。能力边界同 P0-A：只能判定"证据文字是否以改写后的措辞
    出现"，判不出"是否真的单独成镜"；用户可见文案给出路。"""
    advisories: list[str] = []
    for signal in signals_here:
        if not beat_is_shot(f"必现内容：{signal.evidence_quote}", prompt_text):
            advisories.append(
                "[STORYBOARD_PACK_FORESHADOWING_NOT_SHOWN][未拦截] 伏笔/类型信号"
                f"「{signal.evidence_quote[:40]}」看起来没有被写成画面（只能判断证据文字有没有"
                "以改写措辞出现，判不出是否单独成镜），请人工核查——可在分镜台编辑本段镜头稿补上"
            )
    return advisories
