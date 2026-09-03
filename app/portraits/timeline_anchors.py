"""时间线锚点提取（WS4，2026-09-03）：从原文逐字提取年代/年龄锚点，供人物
造型按时间段选用，替代此前完全空置的 ``world.era``。

背景（见 ``app.stages.bible_generate.generate_bible`` 的 docstring）：era/genre
不再由模型判定，也不在生成人物谱时编造兜底——B 库 6 个在线项目 ``world.era``
全部为空。但《跑不快的孩子》全文有几十个逐字出现的年份/年龄锚点（"8 岁土场"
"2004 首秀 17 岁"……），主角只有一条覆盖 13～17 岁的外观描述，用它画 8 岁或
35 岁都是错的。本模块不是给 era "编"一个值，而是给它一个真正从原文数据推导
出来的消费者：era 只由 :func:`derive_world_era` 从已核验的锚点拼出，没有锚点
就保持空——这是"从数据推导"而不是模型臆造（CLAUDE.md「禁止黑白名单/臆造
兜底」的同一条原则）。

提取本身按章调用模型（``model_gateway.chat_structured``，与
``app.portraits.card_merge._card_merge_verdict`` 同一种"结构化选择题"调用
形态），每条候选锚点必须逐字核验：``evidence`` 是该章原文的逐字子串，
``value``（对 age 还有 ``subject``）必须是 ``evidence`` 的子串——核验口径与
``app.stages.identity_evidence._alias_declaration_verified`` 完全一致（"不
确定不登记"，单条锚点核验不过就丢弃，不影响同章其它条目、也不重试整批）。

本模块只提供能力（提取 + 推导 + 落 artifact + 写回 world.era 的独立函数），
不在任何现有生成流程里自动调用——``generate_bible`` 明确不再发起模型调用
（见其 docstring 里 2026-08-31 那次架构转向的教训：一次「可有可无」的判定
调用曾在真实项目上直接触发 HiAgent content_filter 把用户卡死）。接入哪个
工作流触发本模块由后续改动决定，本次改动范围只到"能力就绪、单独可测"。
"""
from __future__ import annotations

import logging
import re
from typing import Literal

from pydantic import BaseModel, Field

from app.bible_store import mutate_bible_json
from app.db import get_conn
from app.evidence import repository as evidence_repository
from app.harness import model_gateway
from app.harness.types import EvidenceArtifact

logger = logging.getLogger(__name__)

TIMELINE_ANCHOR_CONTRACT_VERSION = "portraits-timeline-anchor.v1"

_ANCHOR_KINDS = ("year", "age", "era", "relative")
_MAX_EVIDENCE_CHARS = 60


class _TimelineAnchorCandidate(BaseModel):
    """模型响应里的单条候选——不含 chapter_index，那是后端按调用的章节回填的，
    不是模型自己申报的字段（避免模型把章号也当成"需要凑出来"的东西）。"""

    kind: Literal["year", "age", "era", "relative"]
    value: str
    subject: str = ""
    evidence: str


class _TimelineAnchorBatchResponse(BaseModel):
    anchors: list[_TimelineAnchorCandidate] = Field(default_factory=list)


class TimelineAnchor(BaseModel):
    """核验通过、已落库/可用的时间线锚点——比响应候选多一个 ``chapter_index``。"""

    kind: Literal["year", "age", "era", "relative"]
    value: str
    subject: str = ""
    evidence: str
    chapter_index: int


def _timeline_anchor_prompt(chapter_title: str, content: str) -> str:
    return f"""任务：从下面这段原文中提取【时间线锚点】——原文里逐字出现的、能确定故事发生时间的信息。

原文（{chapter_title}）：
{content}

只提取以下四类，每一类都必须是原文逐字出现的表述，不得依据你的常识、背景知识
或对这部作品的了解补充、纠正或推算：
1. year（具体年份）：原文逐字写出的公历/纪年年份，如"2004年""2009年"；value 填
   这个年份本身的逐字文本。
2. age（人物年龄）：原文逐字写出"谁、几岁"，如"他八岁那年""9岁的里奥"；value
   填年龄本身的逐字文本（如"八岁"/"9岁"），subject 填这个年龄属于谁——必须是
   原文里出现的称谓原文（人名、代称均可，但必须逐字取自原文，不得替换成你知道
   的正式名字或后世通称）。
3. era（时代/朝代表述）：原文逐字写出的时代背景，如"东汉末年""黄巾起义"；
   value 填这段表述的逐字文本。
4. relative（相对时间推移）：原文逐字写出的相对时间推移表述，如"三年后""又过
   了半载"；value 填这段表述的逐字文本。

每一条都必须给 evidence：包含该 value（对 age 还要包含 subject）的一段连续原文
逐字引句，长度不超过 {_MAX_EVIDENCE_CHARS} 字，必须能在上面的原文里精确找到——
不得意译、不得跨句拼接、不得添加原文没有的标点或文字。

这段原文里没有满足以上任一类的表述时，输出空数组，不要为了凑数勉强归类，也
不要引用你在这段原文之外读到的时间信息（哪怕你认得这部作品、知道人物的真实
年表）。

只输出符合 Schema 的 JSON：
{{"anchors": [{{"kind": "year|age|era|relative", "value": str, "subject": str, "evidence": str}}]}}"""


def _anchor_is_literal(candidate: _TimelineAnchorCandidate, chapter_text: str) -> bool:
    """逐字核验：evidence 是该章原文子串，value（age 时还有 subject）是 evidence 子串。

    与 ``app.stages.identity_evidence._alias_declaration_verified`` 同一套
    "不确定不登记"判据：任一步不满足就不采信这一条，不影响同批其它候选。

    ``evidence in chapter_text`` 用去空白后的文本比对：源章节导入时留有词中
    换行等空白噪声（实测西游记/跑不快样本各占约一半的"未通过核验"，去空白
    比对后逐字内容完全一致——例如原文「堕下泪\\n来」，模型给出的引句是自然
    连续的「堕下泪来」），这是数据里的排版噪声不是模型编造，不应该按不通过
    处理；空白之外的任何字符差异仍然按不通过处理（不放松真正的逐字要求）。
    """
    value = (candidate.value or "").strip()
    evidence = (candidate.evidence or "").strip()
    if not value or not evidence or value not in evidence:
        return False
    if _strip_whitespace(evidence) not in _strip_whitespace(chapter_text):
        return False
    if candidate.kind == "age":
        subject = (candidate.subject or "").strip()
        if not subject or subject not in evidence:
            return False
    return True


def _strip_whitespace(text: str) -> str:
    return re.sub(r"\s+", "", text)


async def extract_chapter_timeline_anchors(chapter: dict) -> list[TimelineAnchor]:
    """单章提取并逐字核验时间线锚点；未通过核验的单条丢弃，不影响同章其它条目。"""
    content = (chapter.get("content") or "").strip()
    if not content:
        return []
    try:
        chapter_idx = int(chapter.get("idx"))
    except (TypeError, ValueError):
        return []
    title = chapter.get("title") or f"第{chapter_idx}章"
    operation_id = "portraits_timeline_anchor:" + evidence_repository.content_hash(
        {"chapter_idx": chapter_idx, "content": content}
    )
    response = await model_gateway.chat_structured(
        [{"role": "user", "content": _timeline_anchor_prompt(title, content)}],
        model_type=_TimelineAnchorBatchResponse,
        validate=None,
        operation_id=operation_id,
        max_tokens=3000,
        temperature=0.0,
        call_meta={"stage_key": "portraits_timeline_anchor", "chapter_idx": chapter_idx},
    )
    verified: list[TimelineAnchor] = []
    for candidate in response.anchors:
        if not _anchor_is_literal(candidate, content):
            logger.warning(
                "时间线锚点未通过逐字核验，丢弃：chapter=%s kind=%s value=%r",
                chapter_idx, candidate.kind, candidate.value,
            )
            continue
        verified.append(TimelineAnchor(**candidate.model_dump(), chapter_index=chapter_idx))
    return verified


def _persist_timeline_anchors_artifact(
    project_id: str, chapter_count: int, anchors: list[TimelineAnchor],
) -> dict:
    return evidence_repository.create_artifact(
        EvidenceArtifact(
            type="timeline_anchors",
            scope_type="project",
            scope_id=project_id,
            status="approved",
            trust_level="T2",
            content={
                "chapter_count": chapter_count,
                "anchors": [anchor.model_dump() for anchor in anchors],
            },
            contract_version=TIMELINE_ANCHOR_CONTRACT_VERSION,
            prompt_version=TIMELINE_ANCHOR_CONTRACT_VERSION,
            model_snapshot={"anchor_count": len(anchors)},
        ),
        conn=get_conn(),
    )


async def extract_project_timeline_anchors(
    project_id: str, chapters: list[dict],
) -> list[TimelineAnchor]:
    """遍历全部章节提取锚点，落一条 project 级 ``timeline_anchors`` artifact。"""
    anchors: list[TimelineAnchor] = []
    for chapter in chapters:
        anchors.extend(await extract_chapter_timeline_anchors(chapter))
    _persist_timeline_anchors_artifact(project_id, len(chapters), anchors)
    return anchors


def _leading_digits(value: str) -> int:
    """value 开头那一段数字（年份），不是整串拼接——真实样本里 year 锚点常带
    月日（"2000 年 9 月"/"2004 年 10 月 16 日"），拼接全部数字会把"9 月"的
    "9"接到年份后面变成 20009，比"2022 年"的 2022 还大，排序整个错位。"""
    match = re.match(r"\s*(\d+)", value)
    return int(match.group(1)) if match else 0


def _looks_like_year(value: str) -> bool:
    """value 是否是真的年份文本，不是被误标成 ``kind=\"year\"`` 的纯月/日片段。

    真实样本（proj_ce9fcf749b23《跑不快的孩子》）：模型把承接上文年份的
    "11 月 22 日"/"12 月 18 日"（这段原文里指 2022 年世界杯决赛，年份靠上文
    语境而非本条 value 本身表达）也标成了 kind="year"，直接参与排序会把
    "11"当成比"2022"更小的年份，拼出"11 月 22 日～2022 年"这种不成立的区间。
    判据是数字位数（结构性，不针对具体数值）：开头没有数字（如中式纪年
    "中平六年"）视为可用；开头数字长度 <3（月/日惯常是 1~2 位）视为不可用；
    ≥3 位数字（真实年份至少 3~4 位）视为可用。
    """
    match = re.match(r"\s*(\d+)", value)
    return not match or len(match.group(1)) >= 3


def derive_world_era(anchors: list[TimelineAnchor]) -> str:
    """从锚点推导 world.era：有数字年份锚点时取首尾逐字拼成区间；否则退到
    时代表述去重拼接；两者都没有则返回空串——era 不是任何模型可以直接判定
    的字段，只能由确凿的原文锚点拼出，找不到就如实留空（不臆造兜底）。
    """
    year_values = sorted(
        {
            anchor.value.strip() for anchor in anchors
            if anchor.kind == "year" and anchor.value.strip() and _looks_like_year(anchor.value.strip())
        },
        key=_leading_digits,
    )
    if len(year_values) >= 2:
        return f"{year_values[0]}～{year_values[-1]}"
    if len(year_values) == 1:
        return year_values[0]
    era_values: list[str] = []
    seen: set[str] = set()
    for anchor in anchors:
        value = anchor.value.strip()
        if anchor.kind == "era" and value and value not in seen:
            seen.add(value)
            era_values.append(value)
    return "、".join(era_values)


def apply_world_era(conn, project_id: str, anchors: list[TimelineAnchor]) -> bool:
    """把推导出的 era 写回 world.era；推不出 era 时不写（不覆盖已有值，也不
    清空），返回是否真的写入。"""
    era = derive_world_era(anchors)
    if not era:
        return False

    def sync(data: dict) -> bool:
        world = data.setdefault("world", {})
        if world.get("era") == era:
            return False
        world["era"] = era
        return True

    return mutate_bible_json(conn, project_id, sync)
