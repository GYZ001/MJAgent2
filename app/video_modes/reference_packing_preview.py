"""参考图装箱前的候选序列预览：按最终装箱会用的同一套排序，看谁会被截断挡在外面。

与 ``app.video_modes.seedance_pack.pack_reference_images_for_seedance`` 共用同一套
去重/关键帧分组/角色配额计算（必须完全一致——两边如果各写一份，"预判超限"与
"真正装箱"可能对不上）；``seedance_pack.py`` 在 ``app/FILE_CONVENTIONS.toml`` 的
line_count 棘轮基线里零余量（2026-10-03 核实：448 基线 / 448 实测），这部分预处理
原样从那边搬出来，``pack_reference_images_for_seedance`` 改成调用本模块的薄壳。

排序本身（``ref_pack_priority``：超限时谁先被挤掉）仍然复用 ``app.multiview``，
不重新发明优先级规则；但 ``app.multiview.pack_references_by_purpose`` 同样在
line_count 棘轮基线里零余量（2195 基线 / 2193 实测，只剩 2 行），容不下"返回
截断前完整序列"这个新接口，所以"过滤 + 必需人物钉入 + 不截断"这一半在本模块
重新实现一份，不从那边抽取——与 ``app.video_modes.prop_composite_pack``
（唯一调用方，超限时把本应丢弃的道具与最后一个放得下的道具合成一张拼图）配套。
"""
from __future__ import annotations

from typing import Any

from .keyframe_contract import is_narrative_keyframe_slot
from .mode_selection import _MAX_TIMELINE_KEYFRAMES, max_character_reference_images, max_reference_images


def _dedupe_usable(refs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    usable: list[dict[str, Any]] = []
    seen: set[str] = set()
    for r in refs:
        if not (r.get("selectedForSeedance") and not r.get("deleted")):
            continue
        key = str(r.get("path") or r.get("image_path") or r.get("url") or r.get("id") or "")
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        usable.append(r)
    return usable


def _keyframe_score(ref: dict[str, Any]) -> tuple[float, int]:
    value = ref.get("qualityScore")
    if value is None and isinstance(ref.get("qa"), dict):
        value = ref["qa"].get("overall")
    try:
        numeric = float(value) if value is not None else float("-inf")
    except (TypeError, ValueError):
        numeric = float("-inf")
    try:
        candidate_no = int(ref.get("candidate_no") or 1)
    except (TypeError, ValueError):
        candidate_no = 1
    return (numeric, -candidate_no)


def _keyframe_timeline_order(ref: dict[str, Any]) -> tuple[float, int]:
    try:
        ratio = float(ref.get("keyframe_time_ratio"))
    except (TypeError, ValueError):
        ratio = 1.0
    try:
        index = int(ref.get("keyframe_index") or 999)
    except (TypeError, ValueError):
        index = 999
    return ratio, index


def _split_keyframes(
    usable: list[dict[str, Any]], *, max_keyframes: int | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    keyframe_winners: dict[str, dict[str, Any]] = {}
    non_keyframes: list[dict[str, Any]] = []
    for ref in usable:
        if ref.get("type") != "plot_key_frame" and not is_narrative_keyframe_slot(ref.get("slot_key")):
            non_keyframes.append(ref)
            continue
        group_key = str(ref.get("slot_key") or "__legacy_narrative_keyframe__")
        current = keyframe_winners.get(group_key)
        if current is None or _keyframe_score(ref) > _keyframe_score(current):
            keyframe_winners[group_key] = ref
    timeline_winners = list(keyframe_winners.values())
    declared_totals: list[int] = []
    for ref in timeline_winners:
        try:
            declared_totals.append(int(ref.get("keyframe_total")))
        except (TypeError, ValueError):
            continue
    if max_keyframes is not None:
        keyframe_limit = max(1, min(int(max_keyframes), _MAX_TIMELINE_KEYFRAMES))
    else:
        keyframe_limit = 1 if declared_totals and max(declared_totals) <= 1 else _MAX_TIMELINE_KEYFRAMES
    if len(timeline_winners) > keyframe_limit:
        master = next((r for r in timeline_winners if r.get("slot_key") == "narrative_keyframe"), None)
        chosen = [master] if master is not None else []
        for ref in sorted(timeline_winners, key=_keyframe_timeline_order):
            if len(chosen) >= keyframe_limit:
                break
            if ref not in chosen:
                chosen.append(ref)
        timeline_winners = chosen
    timeline_winners.sort(key=_keyframe_timeline_order)
    return non_keyframes, timeline_winners


def _resolve_char_limit(usable: list[dict[str, Any]]) -> int:
    identities = {
        str(ref.get("entity_name") or "").strip()
        or next(
            (
                str(n).strip()
                for n in (ref.get("relatedCharacterIds") or ref.get("related_character_ids") or [])
                if str(n).strip()
            ),
            "",
        )
        for ref in usable
        if str(ref.get("type") or "") == "character"
    }
    identities.discard("")
    return max(max_character_reference_images(), len(identities))


def prepare_usable_refs_and_limits(
    refs: list[dict[str, Any]], *, max_images: int | None = None, max_keyframes: int | None = None,
) -> tuple[list[dict[str, Any]], int, int]:
    """返回 (关键帧分组去重后的候选池, max_images 上限, 角色配额)。"""
    limit = max_images if max_images is not None else max_reference_images()
    usable = _dedupe_usable(refs)
    if not usable:
        return [], limit, 0
    non_keyframes, timeline_winners = _split_keyframes(usable, max_keyframes=max_keyframes)
    usable = non_keyframes + timeline_winners
    return usable, limit, _resolve_char_limit(usable)


def _eligible_sorted(
    usable: list[dict[str, Any]], *, char_limit: int, required_identity_names: list[str] | None,
) -> list[dict[str, Any]]:
    """谁会先被挤掉：与 ``app.multiview.pack_references_by_purpose`` 同一排序键
    （``ref_pack_priority``）与同一必需人物钉入规则，只是不做 ``max_images``
    截断——截断是调用方各自的事（真正装箱 vs 只预览）。"""
    from app.multiview import ref_pack_priority  # 只有本函数用到，不提到模块顶层常驻

    required_names = list(dict.fromkeys(
        str(n).strip() for n in (required_identity_names or []) if str(n).strip()
    ))

    def _identity_names(ref: dict[str, Any]) -> set[str]:
        names = {
            str(n).strip()
            for n in (ref.get("relatedCharacterIds") or ref.get("related_character_ids") or [])
            if str(n).strip()
        }
        entity_name = str(ref.get("entity_name") or "").strip()
        if entity_name:
            names.add(entity_name)
        return names

    required_refs: list[dict[str, Any]] = []
    for name in required_names:
        match = min(
            (r for r in usable if str(r.get("type") or "") == "character" and name in _identity_names(r)),
            key=ref_pack_priority, default=None,
        )
        if match is not None and match not in required_refs:
            required_refs.append(match)

    limit_characters = max(0, int(char_limit), len(required_names))
    characters_seen = len(required_refs)
    eligible: list[dict[str, Any]] = list(required_refs)
    for ref in sorted(usable, key=ref_pack_priority):
        if ref in required_refs:
            continue
        if str(ref.get("type") or "") == "character":
            if characters_seen >= limit_characters:
                continue
            characters_seen += 1
        eligible.append(ref)
    return eligible


def reference_packing_preview(
    refs: list[dict[str, Any]], *, max_images: int | None = None,
    max_keyframes: int | None = None, required_identity_names: list[str] | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """返回 ``(完整排序但未截断的候选序列, max_images 上限)``，供
    ``app.video_modes.prop_composite_pack`` 在真正装箱发生前探测"超限时道具
    会被丢弃"。"""
    usable, limit, char_limit = prepare_usable_refs_and_limits(
        refs, max_images=max_images, max_keyframes=max_keyframes,
    )
    if not usable:
        return [], limit
    eligible = _eligible_sorted(usable, char_limit=char_limit, required_identity_names=required_identity_names)
    return eligible, limit
