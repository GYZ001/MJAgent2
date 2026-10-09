"""把 ``ProjectPlan`` 渲染成中文可读报告/JSON，并扫描世界书里落在"idx 含义
可能已变化"的章节号上的引用——这部分只报告，不改任何数据（CLAUDE.md
「只报告不改」同款克制：模型/人工证据的归并交给使用者自己判断）。
"""
from __future__ import annotations

import json
from typing import Any

from app.domain.projects.rechapter.models import ProjectPlan

_REF_KEYS = ("evidence_chapter_index", "valid_from_chapter", "valid_to_chapter")


def affected_old_idx(plan: ProjectPlan) -> set[int]:
    """只有"尾段"（之后没有冻结章节）允许章数变化、没做序号稳定性校验，
    所以它覆盖的旧 idx 范围里，idx 的"含义"（对应原著第几章）不再保证不变；
    中间段一旦被接受就已经通过了序号稳定性校验，含义不变，不纳入。"""
    affected: set[int] = set()
    for segment in plan.segments:
        if segment.accepted and segment.is_tail:
            affected.update(range(segment.start_idx, segment.end_idx + 1))
    return affected


def _walk_reference_counts(node: Any, affected_idx: set[int], counts: dict[str, int]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if key in _REF_KEYS:
                try:
                    if int(value) in affected_idx:
                        counts[key] = counts.get(key, 0) + 1
                except (TypeError, ValueError):
                    pass
            _walk_reference_counts(value, affected_idx, counts)
    elif isinstance(node, list):
        for item in node:
            _walk_reference_counts(item, affected_idx, counts)


def scan_bible_chapter_references(bible_json_text: str | None, affected_idx: set[int]) -> dict[str, int]:
    """递归扫描 ``projects.bible_json``，统计三类章节引用落在受影响 idx 上的次数。"""
    if not bible_json_text or not affected_idx:
        return {}
    try:
        payload = json.loads(bible_json_text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    counts: dict[str, int] = {}
    _walk_reference_counts(payload, affected_idx, counts)
    return counts


def _compact_ranges(numbers: list[int]) -> str:
    numbers = sorted(numbers)
    ranges: list[tuple[int, int]] = []
    start = prev = numbers[0]
    for n in numbers[1:]:
        if n == prev + 1:
            prev = n
            continue
        ranges.append((start, prev))
        start = prev = n
    ranges.append((start, prev))
    return "、".join(f"{a}" if a == b else f"{a}-{b}" for a, b in ranges)


def series_task_gaps(conn: Any, project_id: str, episode_nos: list[int]) -> str:
    """新增集号区间里，还没有任何 ``series_tasks`` 覆盖的部分。"""
    if not episode_nos:
        return ""
    covered = [
        (row["episode_from"], row["episode_to"])
        for row in conn.execute(
            "SELECT episode_from, episode_to FROM series_tasks WHERE project_id=?", (project_id,)
        ).fetchall()
    ]
    uncovered = [n for n in episode_nos if not any(a <= n <= b for a, b in covered)]
    return _compact_ranges(uncovered) if uncovered else ""


def _segment_line(segment) -> str:
    status = "接受" if segment.accepted else f"拒绝（{segment.reason}）"
    counts = f"旧{len(segment.old_chapters)}章" + (
        f"→新{len(segment.new_chapters)}章" if segment.accepted else ""
    )
    prefix_note = f"；剔除残片原文：{segment.excluded_prefix!r}" if segment.excluded_prefix else ""
    gained_note = f"；新增 {segment.gained_chars} 字（补救分支补出，已计数）" if segment.gained_chars else ""
    return f"  段 idx {segment.start_idx}-{segment.end_idx}：{status}，{counts}{prefix_note}{gained_note}"


def format_report(plan: ProjectPlan) -> str:
    """CLI dry-run 打印的中文报告正文。"""
    lines = [f"项目 {plan.project_id}："]
    if not plan.ok:
        lines.append(f"  拒绝：{plan.reject_reason}")
        return "\n".join(lines)
    lines.append(f"  冻结集数：{len(plan.frozen_episode_ids)}；冻结章节数：{len(plan.frozen_idx)}")
    lines.append(f"  章节总数：{plan.old_chapter_count} → {plan.new_chapter_count}")
    for segment in plan.segments:
        lines.append(_segment_line(segment))
    action_counts: dict[str, int] = {}
    for write in plan.episode_writes:
        action_counts[write.action] = action_counts.get(write.action, 0) + 1
    lines.append(f"  分集变更：{action_counts or '无'}")
    if plan.unedited_episode_skips:
        lines.append("  未覆盖（人工已改动）：" + "；".join(plan.unedited_episode_skips))
    if plan.bible_reference_report:
        lines.append(f"  世界书引用落在含义可能已变化的章节上：{plan.bible_reference_report}")
    if plan.series_task_gap_report:
        lines.append(f"  尚无连播任务覆盖的新增集号：{plan.series_task_gap_report}")
    if plan.rejected_segment_count:
        lines.append(
            f"  有 {plan.rejected_segment_count} 段被拒、保持原样，本次不写入这些段。"
        )
    elif not plan.changed:
        lines.append("  本项目无需改动（新旧切章结果一致）。")
    return "\n".join(lines)


def plan_to_json(plan: ProjectPlan) -> dict:
    """机器可读结果（``--json``），只含摘要字段，不含整章原文。"""
    return {
        "project_id": plan.project_id,
        "ok": plan.ok,
        "reject_reason": plan.reject_reason,
        "changed": plan.changed,
        "rejected_segment_count": plan.rejected_segment_count,
        "old_chapter_count": plan.old_chapter_count,
        "new_chapter_count": plan.new_chapter_count,
        "frozen_episode_count": len(plan.frozen_episode_ids),
        "frozen_chapter_count": len(plan.frozen_idx),
        "segments": [
            {
                "start_idx": s.start_idx, "end_idx": s.end_idx, "accepted": s.accepted,
                "reason": s.reason, "old_count": len(s.old_chapters), "new_count": len(s.new_chapters),
                "excluded_prefix": s.excluded_prefix, "gained_chars": s.gained_chars,
                "lost_samples": s.lost_samples,
            }
            for s in plan.segments
        ],
        "episode_write_counts": {
            action: sum(1 for w in plan.episode_writes if w.action == action)
            for action in ("update", "insert", "delete")
        },
        "unedited_episode_skips": plan.unedited_episode_skips,
        "bible_reference_report": plan.bible_reference_report,
        "series_task_gap_report": plan.series_task_gap_report,
    }
