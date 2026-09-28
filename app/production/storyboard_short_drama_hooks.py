"""短剧节奏档：开篇钩子/结尾钩子（``opening_hook``/``ending_hook``）的确定性核验。

背景（docs/AI视频行业方法论对标_可学做法_2026-09-26.md §4.3）：短剧节奏档
把整章压成约 90 秒，若开场拖沓、结尾没有悬念，观众第一屏就划走——这是短剧
与长剧在叙事节奏上的核心差异。模型在节拍表里提名哪个节拍是开篇/结尾钩子，
代码核验这条提名是否真实成立（模型提名、代码核验，见 CLAUDE.md「判据从
数据推导」）。

与 ``app.production.screenplay_markers`` 的「（钩子：…）」括号标记是两套独立
机制：那套只对剧本体格式生效，靠原文里作者显式写的括号标注推导必拍镜头；
这里是节拍级判定，novel 体（没有任何括号标记）一样适用，不依赖原文格式。

四条判据（任一不满足即判定这条提名不成立）：

1. ``beat_id`` 必须真实存在于 ``beat_sheet``、且 ``importance=="key"``——
   钩子必须是推动主线的关键节拍，不能是可删的闲笔；
2. ``opening_hook`` 对应节拍必须被第 1 段（``segments[0]``）的 ``beat_ids``
   引用，``ending_hook`` 必须被最后一段引用——钩子必须真的出现在集的开头/
   结尾，不能挂一个提名却放在中间；
3. ``evidence_quote`` 经 ``textmatch.condense``（去标点空白，容忍排版差异）
   后必须是该节拍 ``segment_indexes`` 覆盖原文的子串——逐字取自原文，不是
   对 ``summary`` 的改写或凭空编造；
4. 加固项：``evidence_quote`` 落在的原文句单元不得同时出现在模型自己声明的
   ``dropped_source_spans`` 删减区间里——钩子是这一集必须完整呈现的内容，
   不能一边提名为钩子一边把它划进删减范围（两个字段自相矛盾）。``condense``
   子串包含判定不做位置消歧，同一句原文重复出现时（如两句台词字面重复）
   会命中多个候选单元；只有当**全部**候选单元都落在删减区间时才判定为
   自相矛盾——只要存在一个未被删减的候选单元，这句证据就仍有安全的落点，
   不判定失败（宁可漏检也不误报，与下面「零命中即跳过」同一个哲学）。

``HookBeatSoftCheck`` 与 ``storyboard_short_drama.SegmentCountSoftCap``/
``storyboard_short_drama_budget.DialogueBudgetSoftCap`` 同一套语义重试-降级
策略：前几次不满足当业务错误打回模型重试，最后一次仍不满足则放行、留档
``hooks.status="warning"``（不让钩子这一个维度压垮整集，见 CLAUDE.md
「单次长调用会漏掉整个类别……这类缺失必须有可见信号，不得静默通过」——
放行但留痕，不是静默通过）。

``hook_summary`` 是留档用途，按**最终持久化的节拍表**事后重算（不依赖生成
期 ``HookBeatSoftCheck`` 的重试状态）：``_generate_beat_sheet`` 只返回
``(draft, projected_segment_count)`` 二元组，若把软检查的中间状态也塞进
返回值需要级联改两处调用点与四个测试文件的解包处（见方案调研报告
``pack_wiring``/``shortdrama_delivery`` 两份），而「这一集是否满足钩子条件」
只取决于最终持久化的 ``beat_draft``——与 ``over_target``（按最终段数判定，
不挂生成期中间状态）同一个判据哲学（CLAUDE.md「挂产物信号，不挂状态
字段」）。容量归一化拆段时每个新段完整继承原段的 ``beat_ids``、且拆分保持
原有顺序（``storyboard_capacity_normalize._split_one_segment``），所以在
归一化前后用「首段/末段」判定的结果一致，不会因为拆段而失真。

忠实档（``adaptation_mode != "short_drama"``）本模块全部函数恒返回空/None，
不触碰忠实档草稿（没有这两个字段，``getattr`` 兜底）。
"""
from __future__ import annotations

from typing import Any

from app import textmatch
from app.production import storyboard_short_drama as _short_drama
from app.production.storyboard_segment_ranges import evidence_quote_unit_keys as _quote_unit_keys

#: 留档展示用的截断长度，与 storyboard_short_drama_review._EXCERPT_MAX_CHARS
#: 同一个值（不能直接 import 那个模块的常量——review 模块要 import 本模块
#: 构造 HookBeatSoftCheck，反过来 import 会成环），仅展示用途不影响核验本身。
_EXCERPT_MAX_CHARS = 60


def short_drama_hook_rules() -> list[str]:
    """短剧节奏档阶段一 rules[] 新增的开篇/结尾钩子正面陈述（CLAUDE.md
    Prompts：写清楚什么算钩子、从哪里挑、必须怎么核验，不写"不许怎样"的
    禁令）。"""
    return [
        "本集必须在 opening_hook/ending_hook 两个字段里各提名一个节拍：两者"
        "都必须真实存在于 beat_sheet、importance=key，并给出 evidence_quote"
        "（逐字取自该节拍 segment_indexes 覆盖的原文本身，不得改写、概括或"
        "替换用词）；opening_hook 对应的节拍必须被第 1 段（segment_no=1）的 "
        "beat_ids 引用，ending_hook 对应的节拍必须被最后一段引用。",
        "开篇钩子：原文开场就带出的冲突、悬念，或强烈情绪反应——第 1 段结束"
        "时观众应该已经有一个想继续往下看的理由；从原文实际内容里挑最贴近"
        "这一标准的一个节拍，不要为了填这个字段而硬造冲突或夸大原文语气。",
        "结尾钩子：本章末尾留下的、尚未解决的悬念或情绪，能把观众推向下"
        "一集——同样从原文实际内容里挑，不要编造原文没有的悬念；如果本章"
        "末尾确实是情节完结、没有明显钩子，选原文里最接近「留有余味、引人"
        "接续」的一个节拍，不要虚构一个不存在的转折。",
        "opening_hook/ending_hook 对应节拍覆盖的原文，不得同时出现在你自己"
        "声明的 dropped_source_spans 删减区间里——钩子是本集必须完整呈现的"
        "内容，不能一边提名为钩子一边把它划进删减范围。",
    ]


def _hook_problems(
    nomination: Any, beats_by_id: dict[str, Any], referenced_beat_ids: set[str],
    source_segments: list[Any], dropped_units: frozenset[tuple[int, int]],
) -> list[str]:
    """单个钩子提名的确定性核验，返回问题文案列表（空列表=通过），见模块
    docstring 的四条判据。"""
    problems: list[str] = []
    beat = beats_by_id.get(nomination.beat_id)
    if beat is None:
        return [f"引用的节拍 {nomination.beat_id} 不存在"]
    if getattr(beat, "importance", None) != "key":
        problems.append(f"引用的节拍 {nomination.beat_id} 不是 importance=key")
    if nomination.beat_id not in referenced_beat_ids:
        problems.append(f"节拍 {nomination.beat_id} 没有被对应段（首段/末段）的 beat_ids 引用")
    covered_text = "".join(
        source_segments[i - 1].text for i in beat.segment_indexes if 1 <= i <= len(source_segments)
    )
    condensed_quote = textmatch.condense(nomination.evidence_quote)
    if not condensed_quote or condensed_quote not in textmatch.condense(covered_text):
        problems.append(f"evidence_quote 不是节拍 {nomination.beat_id} 覆盖原文的子串")
    hit_units = _quote_unit_keys(nomination.evidence_quote, beat.segment_indexes, source_segments)
    # 子串包含判定不消歧位置，原文重复短句会让 hit_units 命中多个候选单元；
    # 只有全部候选都落在删减区间才判定自相矛盾——存在任一未删减的候选就说明
    # 这句证据仍有安全落点，不误伤（见模块 docstring 加固项一节）。
    if hit_units and hit_units <= dropped_units:
        problems.append(f"evidence_quote 所在原文单元 {sorted(hit_units)} 已被声明为删减区间")
    return problems


def hook_beat_errors(draft: Any, source_segments: list[Any], *, adaptation_mode: str) -> list[str]:
    """短剧档阶段一/二 blocking 校验用：opening_hook/ending_hook 任一不满足
    模块 docstring 的四条判据就报错，触发语义重试；忠实档、或字段缺失
    （异常兜底，正常路径下短剧档草稿两个字段都是必填）时空操作。"""
    if adaptation_mode != "short_drama":
        return []
    opening = getattr(draft, "opening_hook", None)
    ending = getattr(draft, "ending_hook", None)
    if opening is None or ending is None or not draft.segments:
        return []
    beats_by_id = {beat.beat_id: beat for beat in draft.beat_sheet}
    spans = getattr(draft, "dropped_source_spans", None) or []
    dropped_units = frozenset(_short_drama._declared_units(spans, source_segments))
    errors = [
        f"opening_hook {problem}"
        for problem in _hook_problems(opening, beats_by_id, set(draft.segments[0].beat_ids), source_segments, dropped_units)
    ]
    errors.extend(
        f"ending_hook {problem}"
        for problem in _hook_problems(ending, beats_by_id, set(draft.segments[-1].beat_ids), source_segments, dropped_units)
    )
    return errors


class HookBeatSoftCheck:
    """开篇/结尾钩子的语义重试-降级：与 ``SegmentCountSoftCap`` 同一套让步
    策略（见该类 docstring）——前几次不满足当业务错误打回语义重试，最后一次
    仍不满足则放行、不再产生错误（留档降级为 warning 由 ``hook_summary``
    事后重算，本类只管重试期间要不要打回）。``retry_limit`` 必须与调用方传
    给 ``chat_structured`` 的同一个 ``semantic_retry_limit`` 值一致，不重复
    写字面量。忠实档永远不产生任何错误。"""

    def __init__(self, *, adaptation_mode: str, retry_limit: int, source_segments: list[Any]) -> None:
        self._active = adaptation_mode == "short_drama"
        self._retry_limit = retry_limit
        self._attempt = 0
        self._source_segments = source_segments

    def errors(self, draft: Any) -> list[str]:
        if not self._active:
            return []
        problems = hook_beat_errors(draft, self._source_segments, adaptation_mode="short_drama")
        is_last_attempt = self._attempt >= self._retry_limit
        self._attempt += 1
        if not problems or is_last_attempt:
            return []
        return problems


def hook_summary(draft: Any, source_segments: list[Any], *, adaptation_mode: str) -> dict[str, Any] | None:
    """留档用途，按最终持久化的节拍表事后重算，见模块 docstring「事后重算」
    一节。忠实档，或字段缺失（老草稿/异常兜底）时返回 None——老留档没有这个
    字段，读取方按 ``dict.get("hooks")`` 降级为 None，不抛异常（同
    ``drop_review`` 既有模式）。"""
    if adaptation_mode != "short_drama":
        return None
    opening = getattr(draft, "opening_hook", None)
    ending = getattr(draft, "ending_hook", None)
    if opening is None or ending is None or not draft.segments:
        return None
    beats_by_id = {beat.beat_id: beat for beat in draft.beat_sheet}
    spans = getattr(draft, "dropped_source_spans", None) or []
    dropped_units = frozenset(_short_drama._declared_units(spans, source_segments))
    opening_problems = _hook_problems(opening, beats_by_id, set(draft.segments[0].beat_ids), source_segments, dropped_units)
    ending_problems = _hook_problems(ending, beats_by_id, set(draft.segments[-1].beat_ids), source_segments, dropped_units)
    return {
        "status": "warning" if opening_problems or ending_problems else "ok",
        "opening": {
            "beat_id": opening.beat_id,
            "evidence_quote": opening.evidence_quote[:_EXCERPT_MAX_CHARS],
            "problems": opening_problems,
        },
        "ending": {
            "beat_id": ending.beat_id,
            "evidence_quote": ending.evidence_quote[:_EXCERPT_MAX_CHARS],
            "problems": ending_problems,
        },
    }
