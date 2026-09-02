"""映射台判定的「背景交代段」在分镜台的处置：只能并入相邻事件段作画外音，不得单独成段。

映射台覆盖账（``coverage_ledger``）把每个原文段三分：``delivered``（有事件、有登记人物/场景）、
``paratext``（非正文）、``retained_as_context``（既非正文也无事件的背景交代——开篇诗、史评、
世界观铺陈）。分镜台原本只消费 paratext 账，背景段被当成普通正文：真实事故（2026-09-02，
《西游记》第一回「开篇吟诵西游诗」与《三国演义_白话文版》第一回「东汉末年朝政腐败」，都是
原文第 2–3 段）——节拍表把两段背景交代单独切成 15 秒的段 01，逐段提示词阶段因为该段原文
范围在资产清单里没有任何人物/场景，只能按规则写外貌描述（「中年文士」「中年留三绺长须的
藏青色官袍官员」），生成台在参考图模式下没有任何可绑定的参考图，任务停在「等待人工处理」，
而界面只给「重新生成」——重试必现，没有出路。

判据全部取自映射台的账，不做任何新的语义判断：一个段的原文来源若全落在背景段∪非正文段里，
它就没有任何可视化的事件与素材来源，必须并入相邻的事件段（其文字改写为画外音）。
"""
from __future__ import annotations

from typing import Any


def context_segment_indexes(payload: dict[str, Any]) -> set[int]:
    """映射台已算好的背景交代段号；账缺失/形状不对时返回空集（绝不把「账不存在」读成「全是背景」）。"""
    ledger = payload.get("coverage_ledger")
    if not isinstance(ledger, dict):
        return set()
    raw = ledger.get("retained_as_context")
    if not isinstance(raw, list):
        return set()
    return {int(item) for item in raw if isinstance(item, int) and not isinstance(item, bool)}


def context_segment_rule(context_indexes: set[int]) -> str | None:
    """rules[] 里的正面陈述；没有背景段时返回 None。"""
    if not context_indexes:
        return None
    return (
        f"以下原文段号是映射台判定的背景交代（无事件、无登记人物/场景，如开篇诗、史评、世界观铺陈）："
        f"{sorted(context_indexes)}——它们的文字只能作为画外音并入相邻的事件段，不得单独成段，也不得为其"
        "虚构出场人物；任何 segment 的 source_segment_indexes 若包含这些段号，必须同时包含至少一个"
        "不在此列表中的事件段号（画面和人物来自那个事件段）"
    )


def context_only_segment_errors(
    segments: list[Any], context_indexes: set[int], paratext_indexes: set[int],
) -> list[str]:
    """来源全是背景段∪非正文段的 segment：没有可视化来源，报错并给出唯一修法（并入相邻事件段）。"""
    if not context_indexes:
        return []
    non_visual = context_indexes | paratext_indexes
    errors: list[str] = []
    for seg in segments:
        sources = [int(i) for i in (seg.source_segment_indexes or [])]
        if sources and all(i in non_visual for i in sources):
            errors.append(
                f"第 {seg.segment_no} 段的 source_segment_indexes {sorted(sources)} 全部是映射台判定的背景交代段"
                f"（{sorted(context_indexes)}），没有任何可视化的事件与人物/场景来源：请删掉这一段，把它的文字作为"
                "画外音并入相邻的事件段（把这些段号加进那个段的 source_segment_indexes），并重排全部 segment_no；"
                "这属于修复本身，不受「保持其余已验证字段不变」的限制"
            )
    return errors
