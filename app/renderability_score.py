"""Renderability 金样对照打分（PRD §4.2）。

不调用 LLM；对已落库/导出的剧本+分镜 JSON 做确定性指标对照。
"""
from __future__ import annotations

from typing import Any

from app.renderability import (
    PREFERRED_SHOT_DURATION_S,
    SPINE_BEATS_MIN,
)


def _spine_beats(screenplay: dict[str, Any] | None) -> list[dict[str, Any]]:
    spine = (screenplay or {}).get("plot_spine") or {}
    return list(spine.get("spine_beats") or [])


def _drop_list(screenplay: dict[str, Any] | None) -> list[str]:
    spine = (screenplay or {}).get("plot_spine") or {}
    return [str(x).strip() for x in (spine.get("drop_list") or []) if str(x).strip()]


def score_renderability_sample(
    *,
    screenplay: dict[str, Any] | None,
    storyboard: dict[str, Any] | None,
    baseline: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """按 PRD §4.2 打分；返回 metrics / gates / vs_baseline。"""
    shots = list((storyboard or {}).get("shots") or [])
    n_shots = len(shots)
    beats = _spine_beats(screenplay)
    key_lines = [x for x in ((screenplay or {}).get("key_lines") or []) if str(x).strip()]
    drops = _drop_list(screenplay)
    must_keep = [b for b in beats if b.get("must_keep", True)]
    total_dur = sum(int(s.get("duration_s") or 0) for s in shots)
    gt5 = [s for s in shots if int(s.get("duration_s") or 0) > PREFERRED_SHOT_DURATION_S]

    # drop 回流粗检：drop 文案大段出现在分镜 action/covers
    board_text = "\n".join(
        f"{s.get('action_desc') or ''} {s.get('covers') or ''} {s.get('beat') or ''}"
        for s in shots
    )
    drop_reappear = [
        d for d in drops
        if len(d) >= 6 and d[:8] in board_text
    ]

    # spine 覆盖：beat 的 who+does 是否在分镜文本里有痕迹
    uncovered = []
    for b in must_keep:
        token = f"{b.get('who') or ''}{b.get('does') or ''}".strip()
        core = token[:6] if len(token) >= 6 else token
        if core and core not in board_text:
            uncovered.append(b.get("beat_id") or token)

    metrics = {
        "shot_count": n_shots,
        "total_duration_s": total_dur,
        "spine_beat_count": len(beats),
        "must_keep_count": len(must_keep),
        "key_lines_count": len(key_lines),
        "drop_list_count": len(drops),
        "duration_gt5_count": len(gt5),
        "preferred_duration_s": PREFERRED_SHOT_DURATION_S,
        "drop_reappear_count": len(drop_reappear),
        "spine_uncovered_count": len(uncovered),
        "spine_uncovered": uncovered[:8],
        "contract_version": "renderability_v1",
    }

    gates = {
        "shot_count_within_hard_max": True,
        "spine_beats_in_range": len(beats) >= SPINE_BEATS_MIN,
        "no_drop_reappear": len(drop_reappear) == 0,
        "spine_fully_covered": len(uncovered) == 0 and bool(must_keep),
    }
    gates["all_hard_pass"] = all(
        gates[k] for k in (
            "shot_count_within_hard_max",
            "spine_beats_in_range",
            "no_drop_reappear",
        )
    )

    vs_baseline: dict[str, Any] = {}
    if baseline:
        base_shots = int(baseline.get("shot_count") or 0)
        vs_baseline = {
            "baseline_label": baseline.get("label") or "legacy",
            "baseline_shot_count": base_shots,
            "candidate_shot_count": n_shots,
            "shot_count_ratio": (round(n_shots / base_shots, 3) if base_shots else None),
            "shot_count_le_70pct_baseline": (
                n_shots <= int(base_shots * 0.7) if base_shots else None
            ),
            "baseline_total_duration_s": baseline.get("total_duration_s"),
            "candidate_total_duration_s": total_dur,
        }

    return {
        "metrics": metrics,
        "gates": gates,
        "vs_baseline": vs_baseline,
        "passed": bool(gates.get("all_hard_pass")),
    }


