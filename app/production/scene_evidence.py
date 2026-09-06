"""场景判定的原文依据与结构候选（2026-09-06 第 12/13 轮场景库近重复：六个广场、四个洞府）。

根因不是模型能力：``assess_new_scene`` 的「本场景的原文依据」一直只是场次标签本身
（B 库 45 条场景的 discovery_sources[0] 全是「这片广场」「洞府」这类两三个字），模型没有
任何材料判断第 6 集的「广场」是不是第 16 集的「外宗广场」，只能各起各的 location_key。
这里做两件事，都从数据推导、不设名单：

- **原文依据**：从本集原文句单元里取含该地点字面的段落（先整串，再按尾部子串退让到 ≥2 字），
  给判定用（分镜台一侧仍只给地点标签：场次 summary/source_basis 是剧情概括，既有测试钉着不让它进锚点串）。
- **结构候选**：既有场景的名字/别名/location_key 与本地点字面互含（≥2 字）的卡，连同各自的
  原文摘录一起摆到模型面前做选择题；「场景/外景/内景」是语法后缀不是地名，比较前剥掉。
  选择权仍在模型，落库仍走既有 ``resolve_existing_anchor_name``（结构 location_key 命中优先）。
"""
from __future__ import annotations

from typing import Any

from app.production.scene_granularity import decode_granularity_tag
from app.scene_contract import split_legacy_scene_setting
from app.source_excerpt import SourceSegment

EVIDENCE_SEGMENT_LIMIT = 3
EVIDENCE_SEGMENT_CHARS = 300
_GENERIC_SUFFIXES = ("场景", "外景", "内景")


def location_text(label: str) -> str:
    """场次标签里的地点部分（「夜 外宗广场」→「外宗广场」），剥掉语法后缀。"""
    _time, location = split_legacy_scene_setting(str(label or ""))
    text = (location or str(label or "")).strip()
    for suffix in _GENERIC_SUFFIXES:
        if text.endswith(suffix) and len(text) > len(suffix):
            text = text[: -len(suffix)]
    return text


def scene_label_evidence(label: str, segments: list[SourceSegment]) -> str:
    """含该地点字面的原文段（最多 ``EVIDENCE_SEGMENT_LIMIT`` 段）；整串找不到时按尾部
    子串退让（「外宗中心广场」→「中心广场」→「广场」），退到 2 字为止；没有就空串。"""
    location = location_text(label)
    for start in range(0, max(1, len(location) - 1)):
        needle = location[start:]
        if len(needle) < 2:
            break
        hits = [seg.text.strip()[:EVIDENCE_SEGMENT_CHARS] for seg in segments if needle in seg.text]
        if hits:
            return "\n".join(hits[:EVIDENCE_SEGMENT_LIMIT])
    return ""


def evidence_by_label(labels: list[str], segments: list[SourceSegment]) -> dict[str, str]:
    return {label: scene_label_evidence(label, segments) for label in labels}


def _scene_forms(scene: Any) -> list[str]:
    tag = decode_granularity_tag(list(getattr(scene, "discovery_sources", None) or [])) or {}
    forms = [str(getattr(scene, "name", "") or ""), *(str(a) for a in getattr(scene, "aliases", None) or []), str(tag.get("location_key") or "")]
    return [location_text(f) for f in forms if f]


def structural_scene_candidates(label: str, scenes: list[Any]) -> list[Any]:
    """名字/别名/location_key 与本地点字面互含、或共享尾二字中心词（「这片广场」/「外宗广场」）的既有场景。"""
    location = location_text(label)
    if len(location) < 2:
        return []
    out: list[Any] = []
    for scene in scenes:
        forms = [f for f in _scene_forms(scene) if len(f) >= 2]
        if any(f in location or location in f or f[-2:] == location[-2:] for f in forms):  # 互含，或共享尾二字（中心词）
            out.append(scene)
    return out


def candidate_block(candidates: list[Any]) -> str:
    """提示词里的结构候选段：名字、别名、location_key、各自的原文摘录（没有候选返回空串）。"""
    lines: list[str] = []
    for scene in candidates:
        sources = [str(s) for s in getattr(scene, "discovery_sources", None) or []]
        tag = decode_granularity_tag(sources) or {}
        excerpt = next((s for s in sources if not decode_granularity_tag([s])), "")[:120].replace("\n", " ")
        aliases = "、".join(str(a) for a in getattr(scene, "aliases", None) or []) or "（无）"
        lines.append(f"- {scene.name}｜别名：{aliases}｜location_key：{tag.get('location_key') or '（无）'}｜原文摘录：{excerpt or '（无）'}")
    return "\n".join(lines)
