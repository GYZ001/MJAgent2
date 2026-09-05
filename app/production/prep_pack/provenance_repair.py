"""场景来源证明缺 anchor_phrase 的确定性修补（WS2-2c）。

背景：``_prep_pack_verify_manifest_provenance``（见 ``provenance.py``）要求
method 属于 ``resolution``/``discovery`` 的场景绑定必须带非空
``anchor_phrase``——真实 B 库 7 天内多次撞见「资产来源证明自校验失败：
场景「X」的 provenance.method='resolution' 缺少 anchor_phrase」。此前这
类失败会先经过 ``scene_degrade.degrade_scene_provenance_failures``
就地降级：场景从"已解析"改标 ``unresolved=True``，不再阻断整集发布，
但那条场景从此失去可用状态、必须人工在映射台补绑。这里在自校验之前先
尝试确定性修补：display_name 本来就已知是"哪个场景"，只是绑定这条
provenance 时没能带上一句能逐字核验的原文，而本集原文里往往确实逐字
出现过这个名字——把这类可救的场景救回来，救不回来的才继续走原有的
「缺 anchor_phrase」拦截/降级路径。

修补规则（不编造，只做定位）：在全部原文段里逐字查找场景
``display_name``；命中后，用与 ``provenance.py`` 里
``_prep_pack_locate_stitched_quote`` 同一套终止标点集合
（``_PREP_PACK_TERMINAL_MARKS``）把命中的那一段切成句子，取第一句包含
该名字的完整句子作为 ``anchor_phrase``——这个句子本身就是该段原文的
连续子串，不改写、不拼接，因此自校验的逐字比对必然通过。全文没有任何
一处逐字出现这个名字时不做任何改动，保持原有校验/降级行为，不允许
编造一个不存在的锚点（见 CLAUDE.md「不得兜底填充」；对照 provenance.py
docstring 里已修过的 ERR-20260828-91bc95——那次教训是"引用格式可以剥
但内容不能改写"，这里同样只取原文本来就有的连续文字，不新增一个字）。
"""
from __future__ import annotations

import logging
import re
from typing import Any

from app.source_excerpt import SourceSegment

from .provenance import (
    _PREP_PACK_SCENE_METHODS_REQUIRING_ANCHOR,
    _PREP_PACK_TERMINAL_MARKS,
    _prep_pack_verify_manifest_provenance,
)

_LOGGER = logging.getLogger(__name__)
_SENTENCE_SPLIT_RE = re.compile("(?<=[" + re.escape(_PREP_PACK_TERMINAL_MARKS) + "])")


def _first_sentence_containing(segments: list[SourceSegment], name: str) -> tuple[int, str] | None:
    """全部原文段里第一处逐字出现 `name` 的整句；(1-based segment_index, 句子)。

    句子边界与 provenance.py 同一套终止标点，切出来的每一句都是该段原文
    的连续子串，找不到就返回 None——不猜、不拼接。
    """
    for index, segment in enumerate(segments, start=1):
        if name not in segment.text:
            continue
        for sentence in _SENTENCE_SPLIT_RE.split(segment.text):
            sentence = sentence.strip()
            if sentence and name in sentence:
                return index, sentence
    return None


def repair_scene_anchor_phrases(
    segments: list[SourceSegment], asset_manifest: dict[str, Any],
) -> list[str]:
    """就地补齐 method 属于 resolution/discovery 的场景绑定缺失的
    anchor_phrase；返回修改记录（空表示没动）。"""
    notes: list[str] = []
    for scene in asset_manifest.get("scenes") or []:
        provenance = scene.get("provenance")
        if not isinstance(provenance, dict):
            continue
        method = str(provenance.get("method") or "")
        if method not in _PREP_PACK_SCENE_METHODS_REQUIRING_ANCHOR:
            continue
        if str(provenance.get("anchor_phrase") or "").strip():
            continue
        name = str(scene.get("display_name") or "").strip()
        if not name:
            continue
        hit = _first_sentence_containing(segments, name)
        if hit is None:
            continue
        segment_index, sentence = hit
        provenance["anchor_phrase"] = sentence
        provenance["anchor_segments"] = [segment_index]
        notes.append(
            f"场景「{name}」provenance.method={method!r} 缺 anchor_phrase，"
            f"已从原文第 {segment_index} 段定位含名称整句「{sentence}」补齐锚点"
        )
    return notes


def verify_manifest_provenance_with_repair(
    segments: list[SourceSegment], asset_manifest: dict[str, Any], source_text: str = "",
) -> list[str]:
    """自校验前的确定性修补入口：先补 anchor_phrase 能补的场景绑定并留痕，
    再跑原有自校验；修不了的仍由原校验拦截（原有 scene_degrade 降级路径
    不变）。"""
    for note in repair_scene_anchor_phrases(segments, asset_manifest):
        _LOGGER.info("[PROVENANCE_REPAIR] %s", note)
    return _prep_pack_verify_manifest_provenance(segments, asset_manifest, source_text)


__all__ = ["repair_scene_anchor_phrases", "verify_manifest_provenance_with_repair"]
