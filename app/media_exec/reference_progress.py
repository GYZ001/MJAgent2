"""参考图画廊与叙事关键帧候选的就绪度/进度纯函数（拆分自 ``run_job.py``）。

四个函数都是无副作用的元数据读取：``_reference_gallery_ready``/``_auto_retake``
判定一条 job 的 ``image_inputs`` 元数据是否已具备参考图画廊/是否需要自动重
拍；``_completed_reference_slots``/``_narrative_keyframe_candidate_progress`` 统
计参考槽位与叙事关键帧候选的完成度，供调度优先级
（``.worker_lifecycle``）与生成预算展示复用。不接触数据库、不做 I/O。
"""

from __future__ import annotations

import json
from typing import Any

from app import video_modes


def _reference_gallery_ready(raw_meta: str | None) -> bool:
    try:
        meta = json.loads(raw_meta or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    if meta.get("reference_static_ready") and meta.get("reference_generation_complete") is False:
        return False
    return bool(meta.get("reference_images")) and meta.get("reference_generation_complete") is not False


def _auto_retake(raw_meta: str | None) -> bool:
    try:
        return int(json.loads(raw_meta or "{}").get("auto_retake_count") or 0) > 0
    except (TypeError, ValueError, json.JSONDecodeError):
        return False


def _completed_reference_slots(raw_meta: str | None) -> int:
    try:
        meta = json.loads(raw_meta or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return 0
    slots = meta.get("reference_slots") or {}
    if isinstance(slots, dict):
        return sum(
            1
            for slot_key, slot in slots.items()
            if isinstance(slot, dict)
            and (
                video_modes.is_narrative_keyframe_slot(str(slot_key))
                or not str(slot_key).startswith("narrative_keyframe")
            )
            and slot.get("status") in {"passed", "unverified", "scored_warning"}
        )
    refs = meta.get("reference_images") or []
    return len([r for r in refs if r.get("selectedForSeedance", True) and not r.get("deleted")])


def _narrative_keyframe_candidate_progress(meta: dict[str, Any]) -> tuple[int, int]:
    """Aggregate generated candidates across every timeline keyframe slot.

    ``narrative_keyframe`` is the decisive master beat; sibling timeline beats
    use ``narrative_keyframe_*``.  Candidate records are intentionally kept out
    of ``reference_images`` until a winner is selected, so progress must come
    from the slot checkpoints rather than the public gallery.
    """
    slots = meta.get("reference_slots") or {}
    if not isinstance(slots, dict):
        slots = {}

    sequence = meta.get("keyframe_sequence")
    sequence_keys: list[str] = []
    if isinstance(sequence, dict) and isinstance(sequence.get("beats"), list):
        sequence_keys = list(dict.fromkeys(
            str(beat.get("slot_key") or "")
            for beat in sequence["beats"]
            if isinstance(beat, dict) and str(beat.get("slot_key") or "")
        ))
    if sequence_keys:
        slot_items = [(slot_key, slots.get(slot_key) or {}) for slot_key in sequence_keys]
    else:
        slot_items = [
            (str(slot_key), raw_slot)
            for slot_key, raw_slot in slots.items()
            if video_modes.is_narrative_keyframe_slot(str(slot_key))
        ]

    current = 0
    total = 0
    matched = False
    terminal_statuses = {"passed", "unverified", "scored_warning"}
    for slot_key, raw_slot in slot_items:
        if not isinstance(raw_slot, dict):
            raw_slot = {}
        matched = True
        default_target = (
            video_modes.keyframe_candidate_count()
            if str(slot_key) == "narrative_keyframe"
            else video_modes.supporting_keyframe_candidate_count()
        )
        try:
            target = max(1, int(raw_slot.get("candidate_target") or default_target))
        except (TypeError, ValueError):
            target = default_target

        records = raw_slot.get("candidates") or []
        candidate_nos: set[int] = set()
        if isinstance(records, list):
            for ordinal, record in enumerate(records, start=1):
                if not isinstance(record, dict):
                    continue
                try:
                    candidate_no = int(record.get("candidate_no") or ordinal)
                except (TypeError, ValueError):
                    candidate_no = ordinal
                if 1 <= candidate_no <= target:
                    candidate_nos.add(candidate_no)
        done = min(target, len(candidate_nos))
        # Legacy/final winner checkpoints may not retain the candidate audit
        # list.  A terminal logical slot is nevertheless complete.
        if done == 0 and raw_slot.get("status") in terminal_statuses:
            done = target
        current += done
        total += target

    if not matched:
        return 0, video_modes.estimated_keyframe_generation_count()
    return min(current, total), total

__all__ = [name for name in globals() if not name.startswith("__")]
