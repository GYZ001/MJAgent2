"""基于历史成功率的 per-shot 成本预测（确定性，不调模型）。"""
from __future__ import annotations

from typing import Any

from app.compiler import shot_cost_cny
from app.config import IMAGE_PRICE_PER_UNIT
from app.db import get_conn
from app import video_modes


def initial_shot_generation_cost(duration_s: float) -> float:
    """Match the exact first-pass reservation shown at UI approval."""
    return round(
        shot_cost_cny(int(duration_s or 5))
        + IMAGE_PRICE_PER_UNIT
        * video_modes.estimated_keyframe_generation_count(),
        6,
    )


def historical_attempt_stats(
    *,
    project_id: str | None = None,
    episode_id: str | None = None,
    limit: int = 500,
) -> dict[str, float]:
    """从已落盘 shot_versions 统计平均付费次数与单次成本。"""
    conn = get_conn()
    clauses = ["v.status='succeeded'", "COALESCE(v.cost_cny,0) > 0"]
    params: list[Any] = []
    if episode_id:
        clauses.append("s.episode_id=?")
        params.append(episode_id)
    elif project_id:
        clauses.append("e.project_id=?")
        params.append(project_id)
    where = " AND ".join(clauses)
    rows = conn.execute(
        f"""SELECT v.shot_id, v.cost_cny, v.image_inputs
            FROM shot_versions v
            JOIN shots s ON s.id=v.shot_id
            JOIN episodes e ON e.id=s.episode_id
            WHERE {where}
            ORDER BY v.created_at DESC LIMIT ?""",
        (*params, int(limit)),
    ).fetchall()
    if not rows:
        return {
            "samples": 0,
            "avg_cost_per_paid_version": 4.0,
            "avg_paid_attempts_per_shot": 1.6,
            "success_rate": 0.55,
        }

    by_shot: dict[str, list[float]] = {}
    for row in rows:
        by_shot.setdefault(row["shot_id"], []).append(float(row["cost_cny"] or 0))
    costs = [c for cs in by_shot.values() for c in cs]
    avg_cost = sum(costs) / max(1, len(costs))
    avg_attempts = sum(len(cs) for cs in by_shot.values()) / max(1, len(by_shot))

    # 失败版本粗估成功率
    fail_q = ["v.status='failed'"]
    fail_params: list[Any] = []
    if episode_id:
        fail_q.append("s.episode_id=?")
        fail_params.append(episode_id)
    elif project_id:
        fail_q.append("e.project_id=?")
        fail_params.append(project_id)
    fails = conn.execute(
        f"""SELECT COUNT(*) AS c FROM shot_versions v
            JOIN shots s ON s.id=v.shot_id
            JOIN episodes e ON e.id=s.episode_id
            WHERE {' AND '.join(fail_q)}""",
        fail_params,
    ).fetchone()["c"]
    succ = len(costs)
    success_rate = succ / max(1, succ + int(fails or 0))
    return {
        "samples": float(len(costs)),
        "avg_cost_per_paid_version": round(avg_cost, 3),
        "avg_paid_attempts_per_shot": round(avg_attempts, 3),
        "success_rate": round(min(0.95, max(0.2, success_rate)), 3),
    }


def predict_shot_completion_cost(
    duration_s: float,
    *,
    project_id: str | None = None,
    episode_id: str | None = None,
    grade: str = "C",
    retry_factor: float | None = None,
) -> dict[str, float]:
    """预测单镜补齐到可用的期望成本。"""
    stats = historical_attempt_stats(project_id=project_id, episode_id=episode_id)
    base = initial_shot_generation_cost(duration_s)
    hist = float(stats["avg_cost_per_paid_version"]) or base
    unit = max(base, hist * 0.5)  # 不完全信任历史离群
    if retry_factor is None:
        # 成功率越低，期望重试越多
        sr = float(stats["success_rate"]) or 0.55
        retry_factor = max(1.2, min(3.0, 1.0 / max(0.25, sr)))
        if grade == "C":
            retry_factor *= 1.15
        elif grade == "B":
            retry_factor *= 0.85
    expected = unit * float(retry_factor)
    low = unit * 1.0
    high = unit * max(float(retry_factor), float(stats["avg_paid_attempts_per_shot"]))
    return {
        "expected_cny": round(expected, 2),
        "low_cny": round(low, 2),
        "high_cny": round(high, 2),
        "unit_cny": round(unit, 2),
        "retry_factor": round(float(retry_factor), 3),
        **{f"hist_{k}": v for k, v in stats.items()},
    }


def predict_episode_completion_cost(
    episode_id: str,
    *,
    uncovered_shot_ids: list[str] | None = None,
) -> dict[str, Any]:
    """集级补齐成本区间。"""
    conn = get_conn()
    ep = conn.execute(
        "SELECT id, project_id FROM episodes WHERE id=?", (episode_id,)
    ).fetchone()
    if not ep:
        return {"expected_cny": 0, "low_cny": 0, "high_cny": 0, "shots": []}
    if uncovered_shot_ids is None:
        rows = conn.execute(
            """SELECT id, duration_s FROM shots
               WHERE episode_id=?
                 AND (adopted_version_id IS NULL OR adopted_version_id='')
               ORDER BY shot_no""",
            (episode_id,),
        ).fetchall()
    else:
        if not uncovered_shot_ids:
            return {"expected_cny": 0, "low_cny": 0, "high_cny": 0, "shots": []}
        ph = ",".join("?" * len(uncovered_shot_ids))
        rows = conn.execute(
            f"SELECT id, duration_s FROM shots WHERE id IN ({ph})",
            uncovered_shot_ids,
        ).fetchall()
    per = []
    for row in rows:
        pred = predict_shot_completion_cost(
            float(row["duration_s"] or 5),
            project_id=ep["project_id"],
            episode_id=episode_id,
            grade="C",
        )
        per.append({"shot_id": row["id"], **pred})
    return {
        "expected_cny": round(sum(p["expected_cny"] for p in per), 2),
        "low_cny": round(sum(p["low_cny"] for p in per), 2),
        "high_cny": round(sum(p["high_cny"] for p in per), 2),
        "shot_count": len(per),
        "shots": per,
    }
