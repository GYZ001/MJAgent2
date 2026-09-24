"""短剧节奏档台词预算：确定性给模型一份「保留多少字才装得下」的账，与段数
软上限（``storyboard_short_drama.SegmentCountSoftCap``）同构、同一套重试
让步策略。

背景（2026-09-24 真实三集验证，见 ``storyboard_short_drama.DIALOGUE_BUDGET_
CHARS`` 文档）：段数软上限只管"模型自己声明打算分几段"，管不住"保留的台词
多到必须被容量归一化机械拆出更多段"——三次真实生成里模型规划段数都达标，
但几乎不删台词，归一化按 15 秒口播容量拆段后全部撑回超目标。台词预算把
"总共能留多少字"这件事在阶段一提示词里显式摆给模型，让它在决定 kept_lines/
dropped_lines 时就有一个可执行的数字目标，而不是等它间接撞上拆分后的段数。

放在独立叶子模块（不进 ``storyboard_short_drama.py``）：``DIALOGUE_BUDGET_
CHARS``/``MAX_SEGMENT_COUNT`` 真源在 ``storyboard_short_drama``，本模块单向
依赖它，避免 ``storyboard_short_drama.adaptation_summary`` 需要引用本模块的
常量时反过来成环。
"""
from __future__ import annotations

from typing import Any

from app.production.storyboard_dialogue_ledger import DialogueQuote, _AiKeptLine
from app.production.storyboard_short_drama import DIALOGUE_BUDGET_CHARS


def dialogue_budget_payload(quotes: list[DialogueQuote]) -> dict[str, int]:
    """喂给模型的台词预算三件套：原文台词总字数、预算字数、需要删掉的字数。

    ``total_chars`` 是全集 ``dialogue_targets`` 的合计（去留决定还没做出来，
    按定义就是"如果全部保留会有多少字"）；口径与 ``storyboard_capacity_
    normalize``/``dialogue_ledger_summary`` 同一个 ``DialogueQuote.
    content_chars``（纯文字字数，不含标点空白），不另起一套统计。
    """
    total_chars = sum(q.content_chars for q in quotes)
    return {
        "total_chars": total_chars,
        "budget_chars": DIALOGUE_BUDGET_CHARS,
        "over_budget_chars": max(0, total_chars - DIALOGUE_BUDGET_CHARS),
    }


def dialogue_budget_rule(budget: dict[str, int]) -> str:
    """阶段一 rules[] 里台词预算的正面陈述（CLAUDE.md Prompts：写清楚取值从
    哪来、必须怎么做、装不下时优先删哪一类，不写"不许怎样"的禁令）。"""
    over = budget["over_budget_chars"]
    status = f"超出预算 {over} 字" if over else "未超预算"
    return (
        f"本集台词预算 {budget['budget_chars']} 字（短剧节奏档全集总段数软上限"
        f"折算，与上面的段数软上限同一把尺子）；dialogue_targets 里全部原文"
        f"台词合计 {budget['total_chars']} 字，{status}。kept_lines 里保留"
        "台词的纯文字字数合计不能超过这个预算。超出预算时，优先从非关键"
        "台词——寒暄客套、重复表达、插科打趣、与主线无关的闲聊、或画面本身"
        "已能交代清楚的台词——里选择弃置，每条弃置都要放进 dropped_lines 并"
        "写清具体理由（例如「与主线无关的寒暄，画面已能交代人物关系」）；"
        "推动主线情节、交代关键设定、或构成钩子/悬念的台词必须保留在 "
        "kept_lines 里，即使超预算也不能删。只删到总字数装进预算为止，不要"
        "删过头；保留下来的台词仍必须逐字取自原文，不得改写、概括或替换"
        "用词。"
    )


def kept_dialogue_chars(kept_lines: list[_AiKeptLine], quotes: list[DialogueQuote]) -> int:
    """``kept_lines`` 里全部台词的纯文字字数合计——留档 ``adaptation_summary``
    的 ``kept_dialogue_chars``、以及下面 ``DialogueBudgetSoftCap`` 共用同一份
    计算，不另起一套统计。"""
    quotes_by_id = {q.quote_id: q for q in quotes}
    return sum(quotes_by_id[item.quote_id].content_chars for item in kept_lines if item.quote_id in quotes_by_id)


class DialogueBudgetSoftCap:
    """台词预算软约束：与 ``storyboard_short_drama.SegmentCountSoftCap`` 同一
    套重试让步策略——前几次语义重试当业务错误打回，最后一次（``retry_limit``
    必须与调用方传给 ``chat_structured`` 的同一个 ``semantic_retry_limit``
    值一致）降级为不再打回，不让台词预算这一个维度压垮整集。忠实档
    （``adaptation_mode != "short_drama"``）永远不产生任何错误。
    """

    def __init__(self, *, adaptation_mode: str, retry_limit: int, quotes: list[DialogueQuote]) -> None:
        self._active = adaptation_mode == "short_drama"
        self._retry_limit = retry_limit
        self._attempt = 0
        self._quotes_by_id = {q.quote_id: q for q in quotes}

    def errors(self, draft: Any) -> list[str]:
        if not self._active:
            return []
        kept_chars = sum(
            self._quotes_by_id[item.quote_id].content_chars
            for item in draft.kept_lines if item.quote_id in self._quotes_by_id
        )
        is_last_attempt = self._attempt >= self._retry_limit
        self._attempt += 1
        if kept_chars <= DIALOGUE_BUDGET_CHARS or is_last_attempt:
            return []
        return [
            f"kept_lines 保留台词合计 {kept_chars} 字，超过短剧节奏档台词预算 "
            f"{DIALOGUE_BUDGET_CHARS} 字：请从非关键台词（寒暄、重复、与主线"
            f"无关的闲聊）里再删约 {kept_chars - DIALOGUE_BUDGET_CHARS} 字，"
            "移入 dropped_lines 并写明理由，推动主线/关键设定/钩子悬念的台词"
            "不要动"
        ]
