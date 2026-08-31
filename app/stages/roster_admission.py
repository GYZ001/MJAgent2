"""角色点名——按称呼形态与实测分布得出的准入门槛，以及通道 B（剧情权威）判据
（见 roster_recurring.py 里 `_recurring_character_names` 的调用点与事故背景）。"""
from __future__ import annotations

import asyncio
import hashlib
from typing import Any

from app.harness import model_gateway

from .bible_shared import _bible_short_json_call_meta
from .common import (
    BIBLE_RECURRING_MIN_ONSTAGE_CHAPTERS,
    BIBLE_SMALL_VERDICT_TIMEOUT_S,
    BIBLE_STATISTICAL_MIN_MENTIONS,
)
from .constants import SYSTEM_PREFIX
from .roster_candidates import _MentionedCharacterImportanceResolution

# 姓名/尊称是稳定的个体标识，换个场合还是指同一个人；代称/未判定的形态里混着
# 「绿袍男子」这类靠衣着指人、换个场合就指别人的类别称谓，仍要求跨章复现。
_ROSTER_STABLE_NAME_FORMS = frozenset({"personal_name", "honorific"})


def _roster_onstage_chapter_floor(name_form: str) -> int:
    """通道 A 的跨章门槛按称呼形态分档：姓名/尊称降到 1 章，代称/未判定仍要求
    跨 2 章。挡「绿袍男子」的是它靠衣着指人这个性质，不是它只出现一章；这个
    性质已经由资格裁决模型判进 name_form，这里复用结论，不新拍阈值。
    """
    if name_form in _ROSTER_STABLE_NAME_FORMS:
        return 1
    return BIBLE_RECURRING_MIN_ONSTAGE_CHAPTERS


def _roster_statistical_mention_floor(channel_a_mentions: list[int]) -> int:
    """通道 C 门槛不再是跨作品固定值：改成本次通道 A（在场证据核验）候选提及数
    中位数的 20%，地板 5。通道 A 一个人都没进（单章短篇等）时退回
    BIBLE_STATISTICAL_MIN_MENTIONS，fail-safe 方向不变松。

    通道 A 进来的都是逐条证据核验过的真人，它们的提及量级就是这本书里一个真
    配角长什么样的经验分布，比一个跨作品常数贴得多。
    """
    if not channel_a_mentions:
        return BIBLE_STATISTICAL_MIN_MENTIONS
    ordered = sorted(channel_a_mentions)
    mid = len(ordered) // 2
    median = ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2
    return max(5, round(median * 0.2))


async def _roster_mentioned_importance_verdict(
    appellation: str,
    *,
    mentioned_dossiers: dict[str, list[dict[str, Any]]],
    mention_counts: dict[str, int],
    chapter_counts: dict[str, int],
    ambiguous_appellations: set[str],
    project_id: str | None,
) -> tuple[str, bool]:
    """通道 B（剧情权威）判据：仅被提及、尚未真实出场的具名人物是否该保留在
    人物谱——原文是否赋予其持续剧情作用（建宗门/制度、造成核心冲突、留下仍
    在生效的规则/遗产、被明确设为后续行动目标）。"""
    dossier = mentioned_dossiers.get(appellation, [])[:6]
    if not dossier or appellation in ambiguous_appellations:
        return appellation, False
    catalog = "\n\n".join(
        f"[第{item['chapter_idx']}章·段{item['segment_index']}] {item['text']}"
        for item in dossier
    )
    prompt = f"""任务：判断仅被提及、尚未真实出场的具名人物「{appellation}」是否应作为未来重要角色保留在人物谱。

原文卷宗：
{catalog}

全文机械信号：称呼/别名命中 {mention_counts.get(appellation, 0)} 次，覆盖 {chapter_counts.get(appellation, 0)} 章。

只有原文明确显示其具备持续剧情作用时才 retain，例如：创建宗门或制度、造成当前核心冲突、留下仍在生效的规则/遗产、被明确设为后续行动目标。仅有家世介绍、欠债对象、路人背景、一次性比较或传闻，一律 drop；证据不足选 uncertain。不得根据常识或作品知识补充。
"""
    try:
        resolution = await asyncio.wait_for(
            model_gateway.chat_structured(
                [{"role": "system", "content": SYSTEM_PREFIX},
                 {"role": "user", "content": prompt}],
                model_type=_MentionedCharacterImportanceResolution,
                validate=None,
                operation_id="mentioned_character_importance:" + hashlib.sha256(
                    f"{appellation}:{catalog}".encode("utf-8")
                ).hexdigest(),
                temperature=0.0,
                max_tokens=384,
                call_meta=_bible_short_json_call_meta({
                    "stage": "未出场角色重要性裁决",
                    "stage_key": "mentioned_character_importance",
                    "call_role": "stage_validate",
                    "character_name": appellation,
                    "project_id": project_id,
                }),
            ),
            timeout=BIBLE_SMALL_VERDICT_TIMEOUT_S,
        )
    except Exception:  # noqa: BLE001 - 不确定不登记
        return appellation, False
    valid_chapters = {int(item["chapter_idx"]) for item in dossier}
    return appellation, (
        resolution.verdict == "retain"
        and resolution.supporting_chapter_index in valid_chapters
    )
