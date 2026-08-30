"""Top-level per-issue repair planning entry points (plan_screenplay_patch and
its async wrapper) plus small heuristics for filling in dramatic-field
defaults and locating the scene/opening-anchor context for an issue.

Split out of app/production/screenplay_repair.py.
"""
from __future__ import annotations

import re
from app.harness.types import Issue
from app.production.patch import PatchOperation
from app.schemas import EpisodeScreenplay
from typing import Any

from .gates import _SCENE_NUMBER_RE
from .llm_field_patch import _llm_field_patch


def plan_screenplay_patch(
    issue: Issue,
    script: EpisodeScreenplay,
    *,
    source_text: str = "",
    strategy_history: dict[str, list[str]] | None = None,
) -> list[PatchOperation]:
    """Retired sync mapper kept as a no-op compatibility entrypoint.

    Repair intent is semantic and open-ended.  The async planner below owns
    candidate generation and validates the selected operations against the
    complete document; neither issue codes nor prose may select an edit.
    """
    _ = (issue, script, source_text, strategy_history)
    return []


async def _plan_screenplay_repair_operations(
    issue: Issue,
    script: EpisodeScreenplay,
    *,
    source_text: str,
    strategy_history: dict[str, list[str]],
    episode: dict[str, Any] | None = None,
) -> list[PatchOperation]:
    """Prefer bounded document operations; use semantic planning for graph gaps."""
    operations = plan_screenplay_patch(
        issue,
        script,
        source_text=source_text,
        strategy_history=strategy_history,
    )
    if operations:
        return operations
    return await _llm_field_patch(
        issue,
        script,
        source_text=source_text,
        strategy_history=strategy_history.get(issue.fingerprint, []),
        episode=episode,
    )


def _heuristic_fill_dramatic_field(field: str, script: EpisodeScreenplay) -> str:
    spine = script.plot_spine
    premise = (spine.episode_premise if spine else "") or script.episode_premise or script.logline or ""
    ending = (spine.must_keep_ending if spine else "") or script.ending_hook or ""
    if field == "stakes":
        if script.stakes.strip():
            return ""
        base = premise or ending or script.title
        return f"若失败将无法推进「{base[:40]}」，失去本集目标与立场。"
    if field == "obstacle":
        if script.obstacle.strip():
            return ""
        return f"外部阻力与内心犹豫阻碍实现：{premise[:60] or script.title}"
    if field == "protagonist_goal":
        if script.protagonist_goal.strip():
            return ""
        who = ""
        if spine and spine.spine_beats:
            who = spine.spine_beats[0].who
        return f"{who or '主角'}完成本集目标：{premise[:60] or ending[:60]}"
    if field == "dramatic_question":
        if script.dramatic_question.strip():
            return ""
        return f"主角能否在阻力下完成：{premise[:50] or script.title}？"
    return ""


def _scene_from_issue(issue: Issue, script: EpisodeScreenplay) -> tuple[str, Any | None]:
    evidence = issue.evidence or {}
    candidates = [
        str(node).upper()
        for node in (evidence.get("related_node_ids") or [])
        if re.fullmatch(r"SC\d+", str(node), re.I)
    ]
    if not candidates:
        text = f"{evidence.get('path') or ''} {issue.message or ''}"
        match = _SCENE_NUMBER_RE.search(text)
        if match:
            number = int(match.group(1) or match.group(2))
            candidates.append(f"SC{number:02d}")
    if not candidates:
        return "", None
    scene_id = candidates[0]
    scene_no = int(scene_id[2:])
    scene = next(
        (item for item in (script.scene_outline or []) if int(item.scene_no) == scene_no),
        None,
    )
    return scene_id, scene


def _derive_scene_story_function(scene: Any) -> str:
    """从本场已有事实确定性补全功能描述，不引入新剧情。"""

    def compact(value: Any, limit: int) -> str:
        text = re.sub(r"\s+", "", str(value or "")).strip("，。；;：:、 ")
        return text[:limit].rstrip("，。；;：:、 ")

    # 场功能是短段元数据，不值得为省几个字截成“情绪从”这类半句。
    # 这里的宽限只防御模型异常长输入，正常的冲突与转折应完整保留。
    conflict = compact(getattr(scene, "conflict", ""), 48)
    turn = compact(getattr(scene, "turn", ""), 48)
    summary = compact(getattr(scene, "summary", ""), 64)
    heading = compact(getattr(scene, "scene_heading", ""), 16)
    if conflict and turn:
        return f"呈现{conflict}，推动{turn}"
    if summary and turn:
        return f"呈现{summary}，推动{turn}"
    if summary:
        return f"呈现{summary}并推进本场局势"
    if heading:
        return f"承接{heading}场景并推动本场局势变化"
    return "推动本场核心冲突并形成后续状态变化"


def _opening_anchor_from_issue(message: str) -> str:
    match = re.search(
        r"原文开场第一句对白未作为\s+dialogue_chains\[0\]\.turns\[0\]"
        r"[：:]\s*(.+?)(?:；|;|$)",
        message,
    )
    return match.group(1).strip() if match else ""


