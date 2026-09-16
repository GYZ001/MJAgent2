"""分镜段落台词与后段必保台词冲突的确定性修补（WS2-2d）。

背景：``storyboard_dialogue_repeat._preemption_errors`` 判定本段台词是否
是后面某段必保台词的原样/子串/改写版（二元组覆盖率 ≥
``_PREEMPT_BIGRAM_COVERAGE``），命中即阻断整段重试——真实回归：段 4 写了
「跟我去公司，别出声。」，是段 5 必保台词「算了，跟我去公司当社畜吧……
绝对不能出声，知道吗？」的压缩版。这句话本身该由第 5 段原样说出，第 4
段既然已经提前/改写说了，留给模型重试大概率只是换一种方式再抢说一遍
（这句话确实该出现在这里附近，模型不容易凭空想到"干脆别说"）。

修补：把本段 dialogue 里命中 ``_preempts`` 的那一行直接删掉，留给后面
真正拥有这句必保台词的那一段原样说出；删除后本段 dialogue 可以为空——
空 dialogue 合法（``required_dialogue_missing_errors`` 检查的是"必保台词
是否被覆盖到"，不要求每一段都必须有台词，参见
``app.production.storyboard_dialogue_ledger``）。不做任何改写/替换，只做
删除，不产生模型没写过的新台词。

2026-09-16（龙猫出爪连播第 3、4 集）加一条硬保护 ``required_texts``：**本段
自己的必保原话一律不删**。真实死锁——第 4 集第 2 段必保 Q09「我写了三个月了。」
与第 6 段预留的「我写了三个月，你看了三秒。」二元组覆盖率 0.83，第 3 集 Q23
与第 10 段近义句 0.64，两条都被判成「抢说后段台词」删掉，紧接着
``quote_provenance_errors`` 报「必保引用 QNN 须保留完整原话」——删的正是同一轮
校验要求必须在的那一句，模型逐字照录也过不了，三次重试烧完整集失败。
归属判据很明确：这句话已经由台账分配给本段、带着本段的 quote_id，后段出现措辞
相近的句子是后段的事，不构成本段「抢说」。连续剧对白复现同一措辞极常见，
这不是偶发形态。``required_texts`` 必传（CLAUDE.md「可选参数是缺陷的温床」）：
漏传会让保护静默失效，重新变回删本段必保台词的老行为。
"""
from __future__ import annotations

import logging
from typing import Any

from app.production.storyboard_dialogue_repeat import (
    _normalize,
    _preempts,
    repeated_delivery_errors,
)
from app.production.storyboard_speech_render import remove_draft_utterance

_LOGGER = logging.getLogger(__name__)


def repair_preempted_dialogue(
    draft: Any, reserved: list[tuple[int, str]], *, current_segment_no: int,
    required_texts: list[str],
) -> list[str]:
    """就地删除 ``draft.dialogue`` 里与后段必保台词冲突的行；返回修改记录
    （空表示没动）。``required_texts`` 是本段必保原话，命中的行一律保留。"""
    own = {_normalize(text) for text in required_texts if _normalize(text)}
    kept: list[Any] = []
    notes: list[str] = []
    for line in draft.dialogue:
        normalized = _normalize(line.line)
        if normalized in own:
            kept.append(line)  # 本段自己的必保原话，归属已定，不是抢说
            continue
        hit = next(
            (
                (segment_no, text) for segment_no, text in reserved
                if _preempts(normalized, _normalize(text))
            ),
            None,
        )
        if hit is None:
            kept.append(line)
            continue
        segment_no, text = hit
        remove_draft_utterance(draft, line)
        notes.append(
            f"第 {current_segment_no} 段 {line.speaker_identity_id} 的台词「{line.line}」是第 "
            f"{segment_no} 段必保台词「{text}」的提前版或改写版，已从本段删除，留给第 {segment_no} 段原样说出"
        )
    if len(kept) != len(draft.dialogue):
        draft.dialogue = kept
    return notes


def repaired_repeated_delivery_errors(
    draft: Any, delivered_lines: list[tuple[int, str, str]], *,
    current_segment_no: int, reserved: list[tuple[int, str]],
    required_texts: list[str],
) -> list[str]:
    """自校验前的确定性修补入口：先删掉抢说后段必保台词的行并留痕，再跑
    原有的跨段/预留台词冲突检查；删不掉的重复（同说话人跨段原样重复等）
    仍由原校验拦截。"""
    for note in repair_preempted_dialogue(
        draft, reserved, current_segment_no=current_segment_no, required_texts=required_texts,
    ):
        _LOGGER.info("[STORYBOARD_DIALOGUE_REPAIR] %s", note)
    return repeated_delivery_errors(
        delivered_lines,
        [(line.speaker_identity_id, line.line) for line in draft.dialogue],
        current_segment_no=current_segment_no,
        reserved=reserved,
    )


__all__ = ["repair_preempted_dialogue", "repaired_repeated_delivery_errors"]
