"""WS9：把 WS4 的时间线锚点接进映射台产出（``episode_prep_pack.timeline``）。

映射台每次生成 ``episode_prep_pack`` 时，先确保本集涉及的章节都已提取过时间线
锚点——缓存粒度是单章、按 ``content_hash`` 判断要不要重新抽取（章节原文改了才
重跑，没改就复用，CLAUDE.md「模型调用免费但要幂等」：``app.harness.model_
gateway.chat_structured`` 本身不按 ``operation_id`` 去重，不接一层缓存会导致
分集每次重跑映射包都重新调用模型）。缓存持久化用独立的 evidence artifact 类型
（``prep_pack_timeline_cache``，project 级），不复用
``app.portraits.timeline_anchors`` 自己写的 ``timeline_anchors`` 类型——本模块
不改那份代码一行，也不假设自己是它唯一的写者。

再从累计锚点里筛出与本集 ``source_segment_indexes`` 位置重叠的部分写进
``payload["timeline"]``；有锚点时顺带用全项目累计锚点（不止本集）调用
``apply_world_era`` 写回 ``world.era``——避免每集只用自己那部分章节的锚点，
era 随处理顺序不同的集而反复摆动。

任何一步失败（模型调用异常、章节数据缺失等）都必须 fail-soft：这是映射台已
运行多轮的既有能力之上新叠的增量信号，不是核心正确性所在
（CLAUDE.md「可运行性 > 核心正确性」），一次时间线抽取失败不得打断整条映射
台流水线。
"""
from __future__ import annotations

import logging
from typing import Any

from app.db import get_conn
from app.evidence import repository as evidence_repository
from app.harness.types import EvidenceArtifact
from app.portraits.timeline_anchor_key import anchor_key as _anchor_key
from app.portraits.timeline_anchor_key import display_label as _display_label
from app.portraits.timeline_anchors import (
    TimelineAnchor,
    _project_known_non_era_names,
    apply_world_era,
    derive_world_era,
    extract_chapter_timeline_anchors,
)
from app.source_chapters import _episode_chapters, _episode_source_blocks
from app.source_excerpt import index_source_segments
from app.source_paratext import paratext_segment_indexes

logger = logging.getLogger(__name__)

_CACHE_ARTIFACT_TYPE = "prep_pack_timeline_cache"
_CACHE_CONTRACT_VERSION = "prep_pack-timeline-cache.v1"


def _load_timeline_cache(project_id: str) -> dict[str, Any]:
    artifact = evidence_repository.latest_artifact(_CACHE_ARTIFACT_TYPE, "project", project_id)
    content = artifact.get("content") if artifact else None
    if not isinstance(content, dict) or not isinstance(content.get("chapters"), dict):
        return {"chapters": {}}
    return content


def _persist_timeline_cache(project_id: str, cache: dict[str, Any]) -> None:
    evidence_repository.create_artifact(
        EvidenceArtifact(
            type=_CACHE_ARTIFACT_TYPE,
            scope_type="project",
            scope_id=project_id,
            status="approved",
            trust_level="T2",
            content=cache,
            contract_version=_CACHE_CONTRACT_VERSION,
            prompt_version=_CACHE_CONTRACT_VERSION,
            model_snapshot={"chapter_count": len(cache.get("chapters") or {})},
        ),
        conn=get_conn(),
    )


async def _ensure_chapters_cached(project_id: str, chapter_rows: list[dict]) -> dict[str, Any]:
    """按单章 content_hash 判断是否需要重新抽取；返回更新（如有）后的完整缓存。

    ``rejected_era_values`` 复用 WS10-B 同一份结构性复核（真实事故：我欲封天
    「赵国」「靠山宗」被误标成 era）——本函数绕开 ``extract_project_timeline_
    anchors``（它每次都对整批传入章节重新落一条 project 级 artifact，不做
    按章缓存）直接调用单章原语，因此这道复核必须在这里自己算一遍，不能假设
    调用方替我们做了。
    """
    cache = _load_timeline_cache(project_id)
    chapters_cache: dict[str, Any] = cache["chapters"]
    rejected_era_values = _project_known_non_era_names(get_conn(), project_id)
    changed = False
    for row in chapter_rows:
        idx = int(row["idx"])
        content = row.get("content") or ""
        digest = evidence_repository.content_hash({"chapter_idx": idx, "content": content})
        cached_entry = chapters_cache.get(str(idx))
        if isinstance(cached_entry, dict) and cached_entry.get("content_hash") == digest:
            continue
        anchors = await extract_chapter_timeline_anchors(
            {"idx": idx, "title": row.get("title"), "content": content},
            rejected_era_values=rejected_era_values,
        )
        chapters_cache[str(idx)] = {
            "content_hash": digest,
            "anchors": [anchor.model_dump() for anchor in anchors],
        }
        changed = True
    if changed:
        _persist_timeline_cache(project_id, cache)
    return cache


def _cached_anchors(cache: dict[str, Any]) -> list[TimelineAnchor]:
    anchors: list[TimelineAnchor] = []
    for entry in (cache.get("chapters") or {}).values():
        if not isinstance(entry, dict):
            continue
        for raw in entry.get("anchors") or []:
            try:
                anchors.append(TimelineAnchor(**raw))
            except (TypeError, ValueError):
                continue
    return anchors


def _chapter_region(chapter_row: dict, start_offset: int) -> tuple[int, int]:
    content = chapter_row.get("content") or ""
    return (start_offset, start_offset + len(content))


def _segment_timeline_entries(
    segments: list,
    chapter_rows: list[dict],
    content_offsets: list[int],
    anchors: list[TimelineAnchor],
) -> list[dict[str, Any]]:
    """按章节字符区间把本集锚点分摊到重叠的段落上（章粒度，见模块 docstring）。"""
    anchors_by_chapter: dict[int, list[TimelineAnchor]] = {}
    for anchor in anchors:
        anchors_by_chapter.setdefault(anchor.chapter_index, []).append(anchor)
    entries_by_segment: dict[int, list[dict[str, Any]]] = {}
    for row, start_offset in zip(chapter_rows, content_offsets, strict=True):
        idx = int(row["idx"])
        chapter_anchors = anchors_by_chapter.get(idx)
        if not chapter_anchors:
            continue
        region = _chapter_region(row, start_offset)
        for segment_index in paratext_segment_indexes(segments, [region]):
            payload_anchors = entries_by_segment.setdefault(segment_index, [])
            for anchor in chapter_anchors:
                payload_anchors.append({
                    "kind": anchor.kind,
                    "value": anchor.value,
                    "subject": anchor.subject,
                    "evidence": anchor.evidence,
                    "chapter_index": anchor.chapter_index,
                    "anchor_key": _anchor_key(anchor.kind, anchor.value),
                    "label": _display_label(anchor.kind, anchor.value),
                })
    return [
        {"index": index, "anchors": entries_by_segment[index]}
        for index in sorted(entries_by_segment)
    ]


async def attach_episode_timeline(
    payload: dict[str, Any],
    *,
    project_id: str,
    chapter_indexes: list[int],
    source_text: str,
    conn: Any,
) -> dict[str, Any]:
    """给映射包 payload 加 ``timeline`` 字段；任何异常都吞掉、原样返回 payload。"""
    try:
        chapter_rows = _episode_chapters(conn, {"source_chapters": chapter_indexes, "project_id": project_id})
        if not chapter_rows:
            return payload
        cache = await _ensure_chapters_cached(project_id, chapter_rows)
        all_anchors = _cached_anchors(cache)
        if apply_world_era(conn, project_id, all_anchors) and conn.in_transaction:
            conn.commit()
        rebuilt_text, content_offsets = _episode_source_blocks(chapter_rows)
        if rebuilt_text != source_text:
            return payload
        segments = index_source_segments(source_text)
        if not segments:
            return payload
        episode_chapter_indexes = {int(row["idx"]) for row in chapter_rows}
        episode_anchors = [a for a in all_anchors if a.chapter_index in episode_chapter_indexes]
        result = dict(payload)
        result["timeline"] = {
            "segments": _segment_timeline_entries(segments, chapter_rows, content_offsets, episode_anchors),
            "era": derive_world_era(all_anchors),
        }
        return result
    except Exception:  # noqa: BLE001 -- 增量信号，失败不得打断映射台主流程
        logger.warning("episode_prep_pack 时间线锚点接线失败，跳过本次 timeline 字段", exc_info=True)
        return payload


__all__ = ["attach_episode_timeline"]
