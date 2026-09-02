"""人物谱生成——候选名单条目模型与角色详情合同。

本文件此前已删除一批只服务于已下线单角色详情生成原语（bible_generate.py，
同批删除）的候选名单归一化符号，生产零调用且零测试引用。随「人物谱旧点名
管线整体退场」（2026-09-01）又删除了一个证据检索原语——它依赖的点名管线
协作模块已确认是一条彼此连通、除测试外无任何生产调用方的完整链路，本轮
一并整体删除，唯一消费者 `tests/test_bible_parallelism.py` 的对应用例已同步
删除。

`_attach_roster_source_appellations`（连同它的私有 helper `_cooccurrence_quote`）
曾以"两个测试文件直接单测它"为由从 `roster_recurring.py` 搬到本文件保留；
2026-09-01 复核确认 app/ 内没有任何生产调用（只剩 `stages/__init__.py` 的
再导出与若干注释），是仅测试保活的死函数，本轮随同两处调用测试一并删除
（事故编号 ERR-20260828-9fcabe 与"免检通道显式登记 is_exclusive=False"这层
业务纪律的描述保留在 `app/schemas/character.py`、`app/portraits/card_aliases.py`、
`app/portraits/card_rebind.py` 的相关注释里，不因函数删除而丢失）。

本文件保留的 `_BibleRosterEntry`/`_CharacterDetail`/`_sanitize_character_detail_payload`
仍被 `tests/test_bible_parallelism.py`、`tests/test_alias_exclusivity.py`、
`tests/test_character_alias.py` 等直接单元测试或再导出消费，一个字未动。
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.schemas import (AppearanceEvidence, CharacterAlias,  # noqa: F401 -- World 经 __init__ 再导出
                         Relationship, World)


class _BibleRosterEntry(BaseModel):
    name: str
    role: str
    source_appellations: list[str] = Field(default_factory=list)
    # source_appellations 里哪几项只是点名模型顺口报的别名，没有经过任何核验。
    # 其余各项（primary_appellation / formal_name / 被降级的那个显示名）是这个
    # 候选赖以进入必收名单的身份标识本身——在场证据逐条过了结构闸、独立裁决闸和
    # 段号钉证，名单成立就意味着它们成立。candidate.aliases 没有这层保证：点名
    # 提示词允许模型随手申报，代码一路没有核对过它们指的是不是同一个人。
    # 检索用途（证据包召回、详情提示词的"原文称呼"）照旧吃全集，宽一点无害；
    # 只有"登记进人物谱 aliases"这一步必须把两者分开（原免检通道
    # `_attach_roster_source_appellations` 已随点名管线整体退场删除）。
    unverified_appellations: list[str] = Field(default_factory=list)
    presence_status: Literal["onstage", "mentioned_only"] = "onstage"
    importance_score: float = 0.0
    importance_signals: list[str] = Field(default_factory=list)
    portrait_eligible: bool = True
    appearance_status: Literal["grounded", "insufficient_evidence", "deferred"] = "grounded"


class _CharacterDetail(BaseModel):
    appearance_canonical: str
    period_costume_canonical: str = ""
    personality: str = ""
    speech_style: str = ""
    relationships: list[Relationship] = Field(default_factory=list)
    aliases: list[CharacterAlias] = Field(default_factory=list)
    source_evidence: list[AppearanceEvidence] = Field(default_factory=list)


def _sanitize_character_detail_payload(payload: dict) -> dict:
    """丢掉缺证据锚点的别名/外观证据，不让整条角色详情校验失败。

    真实事故：孟浩详情三次都因 aliases[].evidence_chapter_index=null 整单作废，
    随后被 `_generate_character_detail_batch` 从名单静默删除，人物谱里没有主角。
    别名合同是「不确定不登记」，缺锚点应丢那一条，不是拒绝这个人。
    """
    data = dict(payload)
    aliases = data.get("aliases")
    if isinstance(aliases, list):
        data["aliases"] = [
            item for item in aliases
            if isinstance(item, dict)
            and item.get("evidence_chapter_index") is not None
            and str(item.get("text") or "").strip()
            and str(item.get("evidence_quote") or "").strip()
        ]
    evidence = data.get("source_evidence")
    if isinstance(evidence, list):
        data["source_evidence"] = [
            item for item in evidence
            if isinstance(item, dict)
            and item.get("evidence_chapter_index") is not None
            and str(item.get("evidence_quote") or "").strip()
        ]
    return data
