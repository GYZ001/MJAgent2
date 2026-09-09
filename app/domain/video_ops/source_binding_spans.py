"""确认时核验完整分镜引用，兼容只保存了最长连续区间的旧分镜包。"""
import json

from app.source_chapters import _episode_source_blocks
from app.source_excerpt import index_source_segments
from app.production.storyboard_source_spans import segment_source_bindings, source_binding_is_current


def complete_pack_bindings(conn, episode, bindings: list) -> list:
    """旧包只在已存摘录及章节哈希仍成立时恢复同章引用，不修改业务数据。"""
    try:
        indexes = [int(i) for i in json.loads(episode["source_chapters"] or "[]")]
    except (TypeError, ValueError):
        return bindings
    if not indexes:
        return bindings
    marks = ",".join("?" for _ in indexes)
    sources = [dict(row) for row in conn.execute(
        f"SELECT id,idx,title,content FROM chapters WHERE project_id=? AND idx IN ({marks}) ORDER BY idx",
        (episode["project_id"], *indexes),
    )]
    by_id = {source["id"]: source for source in sources}
    full_text, _offsets = _episode_source_blocks(sources)
    segments = index_source_segments(full_text)
    result = []
    for binding in bindings:
        expanded = _expand_binding(dict(binding), by_id, sources, full_text, segments)
        result.extend(expanded or [binding])
    return result


def _expand_binding(binding: dict, by_id: dict, sources: list, full_text: str, segments: list) -> list:
    source = by_id.get(binding.get("chapter_id"))
    if not source or not source_binding_is_current(binding, source):
        return []
    try:
        segment = json.loads(binding.get("shot_contract_json") or "{}").get("storyboard_pack_segment") or {}
        if not segment or not str(segment.get("prompt_text") or "").strip():
            return []
        stored = segment.get("source_bindings")
        if stored:
            if all(source_binding_is_current(item, by_id.get(item.get("chapter_id"), {})) for item in stored):
                return stored
            return []
        expanded = segment_source_bindings(segment, segments=segments, full_source_text=full_text, authorized_sources=sources)
        # 旧记录只有主摘录所在章节的版本证明，不能据此替其它章节背书。
        return [item for item in expanded if item["chapter_id"] == binding["chapter_id"]]
    except (AttributeError, TypeError, ValueError):
        return []
