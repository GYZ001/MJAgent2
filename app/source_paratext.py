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
import re
from collections import OrderedDict
from threading import RLock
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# 与 `app/stages.py` 蓝图提示词 1a 条同源的判据措辞。改这里等于同时改两处的口径。
PARATEXT_RULE = (
    "旁文本 = 不属于故事叙述本身、仅保留来源审计的文字："
    "作者对读者说话（求收藏/推荐票/月票/订阅、感谢读者、活动与更新公告、"
    "书评区互动、作者自称），以及卷首语、编者按、后记之类的框外文字。"
    "故事叙述本身——包括人物的动作、对白、心理、场景描写——一律不是旁文本。"
    "只按「这段文字是否在讲故事」判断，"
    "不得按段落位置、长度或是否出现某个词来判断。"
)

_CACHE: OrderedDict[str, tuple[str, ...]] = OrderedDict()
_CACHE_SIZE = 256
_CACHE_LOCK = RLock()


class ParatextSpans(BaseModel):
    """模型只需要指出旁文本片段的原文起始句，程序自己定位与删除。"""

    model_config = ConfigDict(extra="forbid")

    spans: list[str] = Field(default_factory=list)


def _cache_key(text: str) -> str:
    return hashlib.blake2b(text.encode("utf-8"), digest_size=16).hexdigest()


def _cached(text: str) -> tuple[str, ...] | None:
    key = _cache_key(text)
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit is not None:
            _CACHE.move_to_end(key)
        return hit


def _remember(text: str, spans: tuple[str, ...]) -> None:
    key = _cache_key(text)
    with _CACHE_LOCK:
        _CACHE[key] = spans
        _CACHE.move_to_end(key)
        while len(_CACHE) > _CACHE_SIZE:
            _CACHE.popitem(last=False)


def remove_spans(text: str, spans: list[str] | tuple[str, ...]) -> str:
    """删除模型指认的旁文本片段。

    只删**逐字命中**的片段：模型若把片段抄错一个字就当它没指认，
    宁可漏删也不能凭模糊匹配删掉正文。
    """
    out = text
    for span in spans:
        candidate = (span or "").strip()
        if len(candidate) < 8:
            # 太短的片段无法证明是旁文本，删了容易伤到正文。
            continue
        if candidate in out:
            out = out.replace(candidate, "")
    return re.sub(r"\n{3,}", "\n\n", out).strip()


async def paratext_spans(text: str, *, operation_id: str) -> tuple[str, ...]:
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
        "把每个片段的原文**逐字**抄进 spans（含首尾标点，不要改写、不要省略号）。"
        "没有旁文本就返回空列表。\n\n"
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
        )
    except Exception:
        # 旁文本剔除是**净化**步骤，不是门禁：判不出来就退回原文，
        # 让下游照旧工作，绝不因为它挡住建人物谱/人物发现。
        return ()
    spans = tuple(s for s in (result.spans or []) if (s or "").strip())
    _remember(body, spans)
    return spans


async def strip_paratext(text: str, *, operation_id: str) -> str:
    """返回剔除旁文本后的原文；判不出来或删空了就原样返回。"""
    body = (text or "")
    spans = await paratext_spans(body, operation_id=operation_id)
    if not spans:
        return body
    cleaned = remove_spans(body, spans)
    # 只要清洗后内容明显不足，就判定这次判定不可信，退回原文。
    if len(cleaned) < len(body.strip()) * 0.5:
        return body
    return cleaned


def paratext_cache_clear() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()


def _spans_debug(text: str) -> dict[str, Any]:  # pragma: no cover - 诊断用
    return {"cached": _cached(text), "key": _cache_key(text)}
