"""把「哪些原文属于旁文本」这条判据收成一份实现，供造人物的路径复用。

背景（生产缺陷 R9）：网文章节的正文里常常直接粘着**作者的话**——求收藏/推荐票、
感谢读者、活动公告。「我欲封天」1616 章里 209 章（12.9%）如此。

剧本链路已经能正确处理它：叙事蓝图会把这些 SRC 段判成
`narrative_layer=paratext / render_policy=exclude_from_spine`，
`finalize_screenplay_ir` 再把它们整体剔出 events/beats/units，
来源覆盖记 `audit_only`。实测蓝图里 1736 个 paratext 节点 vs 15748 个 story，
判得准。

但**造人物的两条路径跑在这套分类之前，而且完全不看它**：

* `generate_bible` 读前 N 章的原始 `content` 建项目人物谱；
* `_screenplay_character_discovery` 是剧本 stage 0，早于蓝图。

于是作者本人被建成了人物卡（`耳根`，role=重要配角，外貌是模型编的）。
经 `identity_authority_registry` 无条件注册后，它成为**每一集**的可引用身份，
证据字段写着「角色圣经已登记身份」——条目自己就是自己的证据。

这里刻意**不做**关键词判定：蓝图那层明文禁止
「按 SRC 编号、章节位置、characters 是否为空或文本关键词分类」，
因为那样会误伤（角色名里含「他」、正文里出现「收藏」等）。
判据与蓝图共用同一段措辞，只有一份定义。

**只用于造人物的输入**，绝不改剧本链路的源文本：那里需要完整原文，
删字会让 SRC 段编号整体错位，破坏来源覆盖审计。
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from collections import OrderedDict
from threading import RLock
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# 与 `app/stages.py` 蓝图提示词 1a 条同源的判据措辞。改这里等于同时改两处的口径。
PARATEXT_RULE = (
    "旁文本 = 不属于故事叙述本身、仅保留来源审计的文字："
    "作者对读者说话（求收藏/推荐票/月票/订阅、感谢读者、活动与更新公告、"
    "书评区互动、作者自称），以及卷首语、编者按、后记、作品副标题与系列名/卷名标题行"
    "（如「××成长史 · 四集中篇」）之类的框外文字。"
    "故事叙述本身——包括人物的动作、对白、心理、场景描写——一律不是旁文本。"
    "只按「这段文字是否在讲故事」判断，"
    "不得按段落位置、长度或是否出现某个词来判断。"
)

_CACHE: OrderedDict[str, tuple["ParatextAnchor", ...]] = OrderedDict()
_CACHE_SIZE = 256
_CACHE_LOCK = RLock()


class ParatextAnchor(BaseModel):
    """一段旁文本的首尾锚点。

    刻意**不**让模型逐字复述整段：实测它能准确判出哪段是作者的话，
    却复述不出逐字相同的长文本（194 字那段就抄漏了），
    于是精确匹配整段删除会静默失效。判断交给模型，定位与切割交给程序。
    """

    model_config = ConfigDict(extra="forbid")

    start: str = Field(description="该段旁文本开头的原文，逐字抄至少 10 个字")
    end: str = Field(description="该段旁文本结尾的原文，逐字抄至少 10 个字")


class ParatextSpans(BaseModel):
    model_config = ConfigDict(extra="forbid")

    spans: list[ParatextAnchor] = Field(default_factory=list)


def _cache_key(text: str) -> str:
    return hashlib.blake2b(text.encode("utf-8"), digest_size=16).hexdigest()


def _cached(text: str) -> tuple[ParatextAnchor, ...] | None:
    key = _cache_key(text)
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit is not None:
            _CACHE.move_to_end(key)
        return hit


def _remember(text: str, spans: tuple[ParatextAnchor, ...]) -> None:
    key = _cache_key(text)
    with _CACHE_LOCK:
        _CACHE[key] = spans
        _CACHE.move_to_end(key)
        while len(_CACHE) > _CACHE_SIZE:
            _CACHE.popitem(last=False)


MIN_ANCHOR_CHARS = 8
# 单个区间超过这个比例，多半是锚点定歪了、把正文一起圈了进去——只丢这一段。
MAX_REGION_FRACTION = 0.4
# 全部区间加起来还超过这个比例，说明整次判定不可信——整体放弃。
MAX_REMOVED_FRACTION = 0.5


def _anchor_region(text: str, start: str, end: str) -> tuple[int, int] | None:
    """用首尾锚点在原文里定位一段区间。任何一项不成立就返回 None（不删）。"""
    head, tail = (start or "").strip(), (end or "").strip()
    if len(head) < MIN_ANCHOR_CHARS or len(tail) < MIN_ANCHOR_CHARS:
        return None
    begin = text.find(head)
    if begin < 0:
        return None
    tail_at = text.find(tail, begin + len(head))
    if tail_at < 0:
        # 允许首尾锚点重叠在同一句（整段就是一句话）。
        if text.startswith(tail, begin):
            tail_at = begin
        else:
            return None
    return begin, tail_at + len(tail)


def _resolved_regions(text: str, spans: list[ParatextAnchor]) -> list[tuple[int, int]]:
    """锚点 -> 已合并、已按比例封顶的绝对偏移区间。

    `remove_spans`（一次性删除）和 `chapter_paratext_offsets`（持久化偏移，
    见下）共用这一份判据——"什么算可信的删除区间"只能有一处定义，两个
    调用方不能各自重新判断一遍上限/合并逻辑。
    """
    regions: list[tuple[int, int]] = []
    for span in spans:
        region = _anchor_region(text, span.start, span.end)
        if region is not None:
            regions.append(region)
    if not regions:
        return []
    # 先按段丢弃明显定歪的区间：一个坏锚点不该让其它有效删除一起作废。
    region_cap = len(text) * MAX_REGION_FRACTION
    regions = [r for r in regions if (r[1] - r[0]) <= region_cap]
    if not regions:
        return []
    regions.sort()
    merged: list[list[int]] = []
    for begin, finish in regions:
        if merged and begin <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], finish)
        else:
            merged.append([begin, finish])
    removed = sum(finish - begin for begin, finish in merged)
    if removed > len(text) * MAX_REMOVED_FRACTION:
        # 逐段都不算离谱，合起来仍占掉一半以上：整次判定不可信，放弃。
        return []
    return [(begin, finish) for begin, finish in merged]


def remove_spans(text: str, spans: list[ParatextAnchor]) -> str:
    """按锚点区间删除旁文本。

    程序负责定位与切割，并对总删除比例设上限——模型指错一次也不会
    把正文删掉大半。区间重叠时合并，从后往前删以免下标漂移。
    """
    regions = _resolved_regions(text, spans)
    return remove_offsets(text, regions) if regions else text


def remove_offsets(text: str, regions: list[tuple[int, int]]) -> str:
    """偏移版删除：输入已经是绝对字符区间（不是锚点字符串），不需要再
    `text.find()` 重新查找——供 `chapters.paratext_json` 持久化后的消费方
    使用。合并/裁剪判据已经在写入前由 `_resolved_regions` 做过一次，这里
    只做纯切割 + 折叠多余空行，收尾逻辑与 `remove_spans` 一致。区间之间
    允许重叠（会被一起吃掉），调用方不需要自己先去重。
    """
    if not regions:
        return text
    merged = sorted({(min(a, b), max(a, b)) for a, b in regions})
    out = text
    for begin, finish in reversed(merged):
        out = out[:begin] + out[finish:]
    return re.sub(r"\n{3,}", "\n\n", out).strip()


async def paratext_spans(text: str, *, operation_id: str) -> tuple[ParatextAnchor, ...]:
    """让模型逐字指认原文里的旁文本片段。失败时返回空元组（保守：不删）。"""
    body = (text or "").strip()
    if len(body) < 200:
        return ()
    hit = _cached(body)
    if hit is not None:
        return hit

    from app.harness import model_gateway

    prompt = (
        f"{PARATEXT_RULE}\n\n"
        "任务：从下面这段小说原文里找出所有旁文本片段。\n"
        "每个片段只需给出首尾锚点：start 逐字抄该段**开头**的至少 10 个字，"
        "end 逐字抄该段**结尾**的至少 10 个字（含标点）。"
        "两个锚点都必须能在原文里逐字找到，中间的内容由后端自己截取，"
        "所以不要复述整段。没有旁文本就返回空列表。\n\n"
        f"原文：\n{body}"
    )
    try:
        result = await model_gateway.chat_structured(
            [{"role": "user", "content": prompt}],
            model_type=ParatextSpans,
            validate=None,
            operation_id=operation_id,
            max_tokens=2048,
            temperature=0.0,
            # 之前没有 call_meta，读超时落在通用 TIMEOUT_CHAT_READ（300s）兜底上；
            # 这个调用正常只要几秒，全库因此白等到 300s 才失败的记录里它占比
            # 最大（见 app/config.py::TIMEOUT_CHAT_PARATEXT_READ 的实测口径）。
            # 这里只标 stage_key，不按 operation_id 里的项目/集号做名单式判断。
            call_meta={"stage_key": "screenplay_source_paratext"},
        )
    except Exception:
        # 旁文本剔除是**净化**步骤，不是门禁：判不出来就退回原文，
        # 让下游照旧工作，绝不因为它挡住建人物谱/人物发现。
        return ()
    spans = tuple(
        s for s in (result.spans or [])
        if (s.start or "").strip() and (s.end or "").strip()
    )
    _remember(body, spans)
    return spans


async def strip_paratext(text: str, *, operation_id: str) -> str:
    """返回剔除旁文本后的原文；判不出来或删空了就原样返回。"""
    body = (text or "")
    spans = await paratext_spans(body, operation_id=operation_id)
    if not spans:
        return body
    return remove_spans(body, list(spans))


def _row_value(row: Any, key: str) -> Any:
    """兼容 `sqlite3.Row` 与普通 dict 的按键取值：两者缺键时抛的异常类型
    不同（dict 抛 KeyError，`sqlite3.Row` 抛 IndexError），统一收成 None。
    """
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return None


def _load_chapter_paratext_cache(row: Any) -> dict[str, Any] | None:
    """解析 `chapters.paratext_json` 里已持久化的记录；缺列/未算过/格式
    不对一律返回 None（视为"尚未计算"）——fail-closed：宁可重算，不可信
    一条读不懂的缓存。"""
    raw = _row_value(row, "paratext_json")
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if "content_hash" not in data or not isinstance(data.get("spans"), list):
        return None
    return data


def _cached_chapter_regions(chapter_row: Any) -> list[tuple[int, int]] | None:
    """已落库且哈希与当前 content 一致的 paratext 偏移；未算过/过期/格式不对返回 None。"""
    content = _row_value(chapter_row, "content") or ""
    cached = _load_chapter_paratext_cache(chapter_row)
    if cached is None or cached.get("content_hash") != _cache_key(content):
        return None
    return [
        (int(item["start"]), int(item["end"]))
        for item in cached.get("spans", [])
        if isinstance(item, dict) and "start" in item and "end" in item
    ]


def cached_chapter_paratext_offsets(chapter_row: Any) -> list[tuple[int, int]]:
    """只读取已落库的 paratext 偏移（相对该章 ``content``），绝不发起模型调用。

    给同步的门禁用（如分镜原文覆盖判据）：映射台已经算过并写回
    ``chapters.paratext_json`` 的章直接复用同一份判定；没算过或缓存过期就当没有
    副文本——门禁只会因此更严（多算缺口），不会放过真正的漏戏。
    """
    return _cached_chapter_regions(chapter_row) or []


async def chapter_paratext_offsets(
    conn, chapter_row: Any, *, operation_id: str,
) -> tuple[list[tuple[int, int]], bool]:
    """取/算/落库一章的 paratext 字符偏移（相对该章 `content` 本身，不含
    任何跨章拼接前缀）。返回 ``(regions, cache_hit)``。

    命中缓存（`content_hash` 匹配）直接返回，零模型调用；未命中调用
    `paratext_spans` 算一次、原子写回 `chapters.paratext_json`。惰性：
    谁先问谁先算，算完落库，后来者白捡（见
    logs/paratext_single_source_plan.md Q3——世界书按 scope 抽样问到的
    31/1616 章会被它第一次问到时算掉，其余 1585 章会在各自集第一次进
    映射台时算掉）。

    fail-closed：判不出来（模型失败/结构闸拒绝）落一条"算过但没找到任何
    paratext"（`spans=[]`）——语义等价于 `strip_paratext` 原有的保守行为
    （净化失败退回原文），不是"不确定"，不会因为这次没找到就每次重问。

    并发写回幂等：两次算出的结果即便字面不同也都是对同一份 PARATEXT_RULE
    的合法回答，后写者覆盖先写者不影响正确性——`chapters.content` 在本
    仓库确认写入后不再原地修改（导入一次性 INSERT，重新导入走整项目
    删除重建），不需要额外加锁。
    """
    content = _row_value(chapter_row, "content") or ""
    content_hash = _cache_key(content)
    cached_regions = _cached_chapter_regions(chapter_row)
    if cached_regions is not None:
        return cached_regions, True

    anchors = await paratext_spans(content, operation_id=operation_id)
    regions = _resolved_regions(content, list(anchors)) if anchors else []
    chapter_id = _row_value(chapter_row, "id")
    if chapter_id is not None:
        # 合并写入，不整体覆盖：paratext_json 在导入时已经写过 sections（小节边界，
        # 见 app.novel.structure._extract_sections），这里只负责 content_hash/spans/
        # computed_at 三个自己的键，其它键原样保留（WS10-C：此前整体覆盖会把导入时
        # 已经算好的 sections 悄悄冲掉）。刻意不用 _load_chapter_paratext_cache——
        # 它对"是否是一份可信的已算缓存"是 fail-closed 的（缺 content_hash/spans
        # 就判 None），会把导入时写的纯 sections JSON 当成空，合并时反而把 sections
        # 冲掉；这里只是想读出已有的任意字段原样带走，不判断可信度。
        raw_existing = _row_value(chapter_row, "paratext_json")
        try:
            merged = json.loads(raw_existing) if raw_existing else {}
        except (TypeError, ValueError):
            merged = {}
        if not isinstance(merged, dict):
            merged = {}
        merged.update({
            "content_hash": content_hash,
            "spans": [{"start": s, "end": e} for s, e in regions],
            "computed_at": time.time(),
        })
        conn.execute(
            "UPDATE chapters SET paratext_json=? WHERE id=?",
            (json.dumps(merged, ensure_ascii=False), chapter_id),
        )
        conn.commit()
    return regions, False


def paratext_segment_indexes(
    segments: list[Any], regions: list[tuple[int, int]],
) -> set[int]:
    """给定 `index_source_segments` 产出的段列表和一组绝对偏移区间（同一份
    文本坐标系），返回被区间覆盖到的 1-based 段序号集合——纯算术区间重叠
    判断，不发起任何模型调用。序号口径与
    `app.source_excerpt.chapter_title_segment_indexes` /
    `app.production.prep_pack._chunk_segments` 的
    `enumerate(segments, start=1)` 完全一致，三处共用同一份编号约定。
    """
    if not regions:
        return set()
    result: set[int] = set()
    for index, segment in enumerate(segments, start=1):
        seg_start, seg_end = segment.start_offset, segment.end_offset
        for start, end in regions:
            if seg_start < end and start < seg_end:
                result.add(index)
                break
    return result


def paratext_cache_clear() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()
