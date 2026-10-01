"""连贯性备忘里「检测到了却只写日志」的两类缺陷补产物信号（2026-10-01，
《顾念长安》第 1 集第三轮分镜 ``/tmp/mjtest/ep1_redo/segments_r3.json``
多代理核查驱动）。

拆成独立模块而不是加进 ``app.production.storyboard_continuity_memo``：该文件
已经在 500 行文件行数硬顶上（``app/FILE_CONVENTIONS.toml`` 默认
``max_lines_python=500``，零余量，2026-09-28 已经拆过一次
``storyboard_travel_direction``），新增判据放不进去；见 CLAUDE.md「装不下时
先想怎么拆，不要先想加基线」。

两类缺陷（②已退场，见下）、共同根因都是 CLAUDE.md「闸门放行分支必须是产品里的可见信号」：

① ``storyboard_continuity_memo.layout_change_advisories`` 此前只在
   ``continuity_memo_errors`` 里 ``log.warning``，从不进
   ``degraded_capabilities``——第 26 段相对 23-25 段左右站位整体翻转、
   ``layout_change_source_quote`` 为空，本来命中了这条判据却没人看得到。
② （已退场，2026-10-01 同日）道具 ``location`` 跨段变化告警：位置是自由文本，
   模型每段换一种说法（「床尾旁」「床尾旁的地板上」「床尾一侧」），逐字比对几乎
   每段都命中——第 1 集第五轮 35 段触发 874 条，真问题（行李箱无人触碰却换了
   位置）被淹没，界面上只会教人无视这类提示。纯字符串判据表达不了「位置真的变了」，
   在有结构化位置字段之前不做；道具挪位交给正文复核的 prop_appearance 判据与成片复查。

两条判据的输出形状照抄 ``storyboard_action_density.segment_advisories``：
返回已经打好 ``[TAG][未拦截]`` 前缀的字符串列表，调用方
``storyboard_pack._segment_content_advisories`` 直接 ``*`` 拼进
``degraded_capabilities``，不在那边重复加前缀。``segment_continuity_location_
advisories`` 额外封装「取上一段备忘 + 取本段原文」两步，让
``storyboard_pack.py``（已在自己的 500 行/单函数 50 代码行硬顶上）的两处调用点
各自只需一行接入，不必展开这几行搬过去占用它自己的行数预算。
"""
from __future__ import annotations

from typing import Any

from app.production.storyboard_continuity_memo import _AiContinuityMemo, layout_change_advisories
from app.production.storyboard_segment_ranges import segment_source_payload


def continuity_memo_location_advisories(
    memo: _AiContinuityMemo, previous_memo: _AiContinuityMemo | None, segment_source_text: str,
) -> list[str]:
    """layout 跨段变化告警的出口，供 ``_segment_content_advisories`` 拼进
    ``degraded_capabilities``，见模块 docstring①。"""
    return [
        f"[STORYBOARD_CONTINUITY_MEMO_LAYOUT][未拦截] {advisory}"
        for advisory in layout_change_advisories(memo, previous_memo, segment_source_text)
    ]


def segment_continuity_location_advisories(
    draft: Any, plan: Any, by_segment_no: dict[int, Any],
    segments: list[Any], paratext_indexes: set[int],
) -> list[str]:
    """封装"取上一段备忘 + 取本段原文 + 调用 ``continuity_memo_location_
    advisories``"三步，供 ``storyboard_pack`` 两处调用点各自一行接入。"""
    previous_draft = by_segment_no.get(plan.segment_no - 1)
    previous_memo = previous_draft.continuity_memo if previous_draft is not None else None
    source_text = segment_source_payload(plan, segments, paratext_indexes)["source_text_by_segment"]
    return continuity_memo_location_advisories(draft.continuity_memo, previous_memo, source_text)
