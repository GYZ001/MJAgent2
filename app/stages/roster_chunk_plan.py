"""角色点名——分块读取计划：超预算章节切分为多块（不截断）与分块失败的绝对记账。"""
from __future__ import annotations

from typing import Any

# 切分时给"【标题】\n"前缀留出余量，避免拼上标题后又撞回 `_call_chunk` 自身的
# 输入硬上限校验（BIBLE_ROLL_CALL_CHUNK_INPUT_MAX_CHARS），把一次合规切分误判成失败。
_CHUNK_SPLIT_HEADER_MARGIN_CHARS = 200


def _split_oversized_chapter(chapter: dict[str, Any], budget: int) -> list[dict[str, Any]]:
    """一章原文超预算时按字符切成多块，各自独立发一次点名请求，不丢正文。

    纯字符切片，不做语义分句：切分点落在句子中间不影响下游核验——结构闸 G1/G2
    核对的是 `_chapters_by_idx` 建出的完整原文，不是这份切片，切片只用来生成
    点名请求本身。章号 `idx` 原样保留，多个子块共享同一个 idx。
    """
    content = (chapter.get("content") or "").strip()
    piece_size = max(1, budget - _CHUNK_SPLIT_HEADER_MARGIN_CHARS)
    if len(content) <= piece_size:
        return [chapter]
    return [
        {**chapter, "content": content[start:start + piece_size]}
        for start in range(0, len(content), piece_size)
    ]


def _expand_chunk_plan(
    chunks: list[list[dict[str, Any]]], budget: int,
) -> list[list[dict[str, Any]]]:
    """把点名分块计划里超预算的单章分块原地展开成多个子块，其余分块不变。"""
    expanded: list[list[dict[str, Any]]] = []
    for group in chunks:
        if len(group) == 1:
            expanded.extend([piece] for piece in _split_oversized_chapter(group[0], budget))
        else:
            expanded.append(group)
    return expanded


def _failed_chunk_meta(
    chunks: list[list[dict[str, Any]]], chunk_results: list[Any],
) -> dict[str, Any]:
    """点名分块失败的绝对损失记账：只记比例会让「20/60」和「6/20」看起来接近，
    实际绝对损失差三倍多。落绝对计数与失败覆盖到的章号，供人工核对丢了哪几章。
    """
    failed_chapters: list[int] = []
    failed = 0
    for chunk, result in zip(chunks, chunk_results, strict=True):
        if not isinstance(result, BaseException):
            continue
        failed += 1
        for item in chunk:
            idx = item.get("idx")
            if idx is not None and idx not in failed_chapters:
                failed_chapters.append(idx)
    return {
        "failed_chunk_count": failed,
        "total_chunk_count": len(chunk_results),
        "failed_chapters": sorted(failed_chapters),
    }
