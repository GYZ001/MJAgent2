"""把分镜的每段原文引用绑定到真实章节偏移，不丢弃不连续的引用。"""
import hashlib

from app.source_chapters import _episode_source_blocks
from app.source_excerpt import SourceSegment
from app.production.storyboard_segment_ranges import split_source_units


def _selected_intervals(indexes: list[int], ranges: list[dict], segments: list[SourceSegment]) -> list[tuple[int, int]]:
    selected = []
    wanted = set(indexes)
    if any(r.get("source_segment_index") not in wanted for r in ranges):
        raise ValueError("原文单元范围不属于本段原文引用")
    for index in sorted(wanted):
        if not isinstance(index, int) or not 1 <= index <= len(segments):
            raise ValueError("原文段号越界，无法建立完整引用")
        segment = segments[index - 1]
        matches = [r for r in ranges if r.get("source_segment_index") == index]
        if not ranges:
            selected.append((segment.start_offset, segment.end_offset))
            continue
        if len(matches) != 1:
            raise ValueError("原文段需要唯一的单元范围")
        units = split_source_units(segment.text)
        start, end = matches[0].get("from_unit"), matches[0].get("to_unit")
        if not isinstance(start, int) or not isinstance(end, int) or not 1 <= start <= end <= len(units):
            raise ValueError("原文单元范围越界")
        selected.append((segment.start_offset + units[start - 1][0], segment.start_offset + units[end - 1][1]))
    return selected


def segment_source_bindings(segment: dict, *, segments: list[SourceSegment], full_source_text: str, authorized_sources: list[dict]) -> list[dict]:
    """每个引用区间独立存证；章节前缀和空行不冒充正文，也不填未引用的中间剧情。"""
    rebuilt, offsets = _episode_source_blocks(authorized_sources)
    if rebuilt != full_source_text:
        raise ValueError("授权章节与分镜原文版本不一致")
    intervals = _selected_intervals(segment.get("source_segment_indexes") or [], segment.get("source_unit_ranges") or [], segments)
    bindings = []
    for source, origin in zip(authorized_sources, offsets, strict=True):
        content = source["content"]
        spans = []
        for start, end in intervals:
            left, right = max(0, start - origin), min(len(content), end - origin)
            if left >= right:
                continue
            if spans and (left <= spans[-1][1] or not content[spans[-1][1]:left].strip()):
                spans[-1] = (spans[-1][0], max(spans[-1][1], right))
            else:
                spans.append((left, right))
        for start, end in spans:
            bindings.append({
                "binding_kind": "source_excerpt", "chapter_id": int(source["id"]), "chapter_idx": int(source["idx"]),
                "source_version_hash": hashlib.sha256(content.encode()).hexdigest(),
                "start_offset": start, "end_offset": end,
                "excerpt_hash": hashlib.sha256(content[start:end].encode()).hexdigest(),
            })
    return bindings


def source_binding_is_current(binding: dict, source: dict) -> bool:
    """独立复核章节、原文版本、区间和摘录哈希。"""
    try:
        start, end = int(binding["start_offset"]), int(binding["end_offset"])
        content = source["content"]
        return (
            int(binding["chapter_id"]) == int(source["id"])
            and int(binding["chapter_idx"]) == int(source["idx"])
            and 0 <= start < end <= len(content)
            and binding["source_version_hash"] == hashlib.sha256(content.encode()).hexdigest()
            and binding["excerpt_hash"] == hashlib.sha256(content[start:end].encode()).hexdigest()
        )
    except (KeyError, TypeError, ValueError):
        return False
