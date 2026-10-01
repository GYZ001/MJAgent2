"""分镜台阶段一：段级动作容量——给"一场戏要不要拆成多段"补上动作维度，与既有
台词容量（``storyboard_dialogue_ledger``）同一个形状（2026-10-01，真实证据见下）。

背景：第 1 集重做第二轮分镜（``/tmp/mjtest/ep1_redo/segments_r2.json``，原文
``/tmp/mjtest/ep1_redo/source_segments.json``）逐段核查发现，动作密集、台词稀少
的戏被整体塞进一个 15 秒段、4 镜已满仍塞不下：第 1 段镜头1「狂风吹倒绿萝→插座
窜火花→顶灯熄灭+明暗转换」、第 3 段镜头3「蹲回墙角→照墙角→塞毛巾→盯脸盆→翻
手机→划屏幕→呼气」、第 10 段镜头4「抬眼、俯身、逐颗扣扣子、直起身、转头点头」、
第 5/27/19 段双人各自动作叠加 4 个、第 6 段镜头1 三个动作叠加约 30 字台词。正文
复核（``storyboard_prose_review``）发现了这些问题，但段落已是 ``MAX_SHOTS_PER_
SEGMENT=4`` 镜满额，重写一次改不动，原样放行。根因：阶段一 ``_beat_sheet_
rules()`` 判断"要不要把一场戏拆成更多 15 秒段"只认台词容量（见
``storyboard_dialogue_ledger.beat_sheet_dialogue_ledger_rules`` 的"只有台词容量
确实装不下时，才允许把同一场戏拆成多段"），不认动作容量。

与台词容量不是同一种可确定性机制：台词能用正则从原文抠出来（见
``storyboard_dialogue_ledger.extract_dialogue_targets``），"这段原文需要几个
关键动作"是语义判断，代码判不出——只有读原文的模型分得清（CLAUDE.md「判据从
数据推导，模型提名、代码核验」）。所以这里反过来：模型在节拍表阶段，为它划给
每一段的内容结构化申报一份关键动作清单（``_AiSegmentPlan.key_actions``，口径
与 ``storyboard_action_density.key_action_definition()`` 完全相同——两侧共用
同一句原文，不会各数各的），代码只核验这份自报清单的条数是否超过这一段的
承载量，超了必须拆成更多段。

承载量简化：严格算应该是这一段最终几个镜头各自的上限之和，其中台词镜只能装 1
个（见 ``storyboard_action_density`` 新增口径）、其余镜头各装 ``MAX_KEY_ACTIONS_
PER_SHOT``（=2）个——但阶段一还不知道"这一段最终会有几镜、哪一镜落了台词"（镜头
划分与台词分配到具体镜头都是阶段二的产物），没有可靠依据做这个扣减。这里只核验
``MAX_SHOTS_PER_SEGMENT × MAX_KEY_ACTIONS_PER_SHOT``（4×2=8）的总上限，不做更细
的扣减——简化后的承载量只会比真实承载量更宽松（忽略了台词镜拉低上限的部分），
不会制造误报，只会在真实承载量更紧时漏报一部分，留给阶段二既有的单镜密度闸门
（``storyboard_action_density.ActionDensitySoftCheck``）兜底。

超限走与台词容量/情绪转折提名同一种"带指引重试 → 耗尽降级为可见告警"节奏
（``SegmentActionCapacitySoftCheck``，形状同 ``storyboard_beat_causality.
EmotionalTurnSoftCheck``），不阻断整集生成（CLAUDE.md「修补器与校验器死锁」：
已经满镜的段落改不动，硬挡会制造死锁）；耗尽后的可见信号走 ``StoryboardPack
Segment.degraded_capabilities``（``segment_advisories_for_plan``，与
``storyboard_pack.generate_storyboard_pack`` 里 ``paratext_strip_notes`` 同一个
拼接点，但这是单段自己的超限，只拼进它自己的产物，不像 paratext 那样整集广播），
不是只写后端日志。

不落库：与 ``storyboard_action_density.ShotActionBeats`` 同一先例（见该模块
docstring「不落库」），``key_actions`` 只服务于"怎么分段"这一次判断，不写入
``StoryboardPackSegment``/``shots`` 表。

依赖方向：只 import ``storyboard_action_density``（叶子，零依赖
``storyboard_beat_sheet``/``storyboard_pack``），不 import ``storyboard_pack``
本身——``MAX_SHOTS_PER_SEGMENT`` 由调用方显式传参（参照 ``storyboard_action_
density.over_limit_remedy`` 由调用方传参的做法），避免循环导入（见
``storyboard_beat_sheet`` 模块 docstring「依赖方向是单向的」）。
"""
from __future__ import annotations

from typing import Any

from app.production import storyboard_action_density as _action_density


def _segment_capacity(*, max_shots: int, max_per_shot: int) -> int:
    """承载量：max_shots 个镜头各装 max_per_shot 个（简化，不扣减台词镜，见模块 docstring）。"""
    return max_shots * max_per_shot


def segment_key_actions_rule(
    *, max_shots: int, max_per_shot: int = _action_density.MAX_KEY_ACTIONS_PER_SHOT,
) -> str:
    """阶段一正面陈述：``key_actions`` 怎么申报、承载量是多少、超了怎么办。"""
    capacity = _segment_capacity(max_shots=max_shots, max_per_shot=max_per_shot)
    return (
        "key_actions：为这一段将要呈现的内容申报关键动作清单，按发生顺序各写一个短语。"
        f"{_action_density.key_action_definition()}这一段最多 {max_shots} 个镜头、每镜最多 "
        f"{max_per_shot} 个关键动作，这一段能装下的关键动作总量不超过 {capacity} 个"
        f"（{max_shots}×{max_per_shot}）——这场戏需要呈现的关键动作超过这个数时，必须把它拆成"
        "更多 15 秒段落来呈现，新增段落、重排全部 segments[].segment_no 是预期动作，原文节拍"
        "一个都不删，不要为了凑少数段而压缩动作或把动作硬塞进一镜。"
    )


def segment_action_capacity_errors(
    segments: list[Any], *, max_shots: int, max_per_shot: int = _action_density.MAX_KEY_ACTIONS_PER_SHOT,
) -> list[str]:
    """阻断判据：每段自报的关键动作条数是否超过本段承载量（简化总量，见模块 docstring）。"""
    capacity = _segment_capacity(max_shots=max_shots, max_per_shot=max_per_shot)
    return [
        f"第 {segment.segment_no} 段 key_actions 申报了 {len(segment.key_actions)} 个关键动作"
        f"（{'、'.join(segment.key_actions)}），超过本段承载量 {capacity} 个（{max_shots} 镜 × 每镜最多 "
        f"{max_per_shot} 个）：请把这场戏拆成更多 15 秒段落，新增段落并重排全部 segments[].segment_no，"
        "原文节拍一个都不删"
        for segment in segments if len(segment.key_actions) > capacity
    ]


class SegmentActionCapacitySoftCheck:
    """前 ``retry_limit`` 次把超限当阻断打回模型重试，最后一次仍超限则放行
    （与 ``storyboard_beat_causality.EmotionalTurnSoftCheck``/``storyboard_
    short_drama_hooks.HookBeatSoftCheck`` 同一套让步策略）——已经满镜的段落
    靠重写改不动，硬挡会制造修补器-校验器死锁（CLAUDE.md）。
    """

    def __init__(self, *, retry_limit: int, max_shots: int) -> None:
        self._retry_limit = retry_limit
        self._attempt = 0
        self._max_shots = max_shots

    def errors(self, draft: Any) -> list[str]:
        problems = segment_action_capacity_errors(draft.segments, max_shots=self._max_shots)
        is_last_attempt = self._attempt >= self._retry_limit
        self._attempt += 1
        if not problems or is_last_attempt:
            return []
        return problems


def segment_advisories_for_plan(plan: Any, *, max_shots: int) -> list[str]:
    """非阻断，供 ``storyboard_pack.generate_storyboard_pack`` 拼进这一段自己
    的 ``degraded_capabilities``。与 ``SegmentActionCapacitySoftCheck`` 同源
    重算（``segment_action_capacity_errors``），独立于它的内部重试计数，保证
    放行分支仍是产品里的可见信号（CLAUDE.md「放行分支必须是产品里的可见
    信号」），不依赖 SoftCheck 是否已经耗尽重试。
    """
    return [
        f"[STORYBOARD_PACK_SEGMENT_ACTION_CAPACITY][未拦截] {error}"
        for error in segment_action_capacity_errors([plan], max_shots=max_shots)
    ]
