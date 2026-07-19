from __future__ import annotations

import json
from statistics import mean
from typing import Any

from app.db import get_conn, new_id, now, rows_to_dicts


DEFAULT_THRESHOLDS = {
    "verified_delivery_rate_delta_min": 0.0,
    "first_pass_acceptance_delta_min": 0.0,
    "evidence_coverage_min": 0.9,
    "duplicate_billing_max": 0,
    "state_drift_max": 0,
}


def project_quality_metrics(project_id: str) -> dict[str, Any]:
    conn = get_conn()
    episodes = rows_to_dicts(conn.execute(
        "SELECT * FROM episodes WHERE project_id=? ORDER BY episode_no", (project_id,)
    ).fetchall())
    deliveries = rows_to_dicts(conn.execute(
        """SELECT dp.* FROM delivery_packages dp JOIN episodes e ON e.id=dp.episode_id
           WHERE e.project_id=? ORDER BY dp.created_at""",
        (project_id,),
    ).fetchall())
    first_by_episode: dict[str, dict[str, Any]] = {}
    coverages: list[float] = []
    for item in deliveries:
        first_by_episode.setdefault(item["episode_id"], item)
        try:
            report = json.loads(item["quality_report_json"] or "{}")
            coverages.append(float(report.get("evidence_coverage") or 0))
        except (TypeError, ValueError, json.JSONDecodeError):
            coverages.append(0.0)
    accepted_first = sum(item["status"] == "approved" for item in first_by_episode.values())
    feedback = rows_to_dicts(conn.execute(
        """SELECT cf.* FROM customer_feedback cf JOIN episodes e ON e.id=cf.episode_id
           WHERE e.project_id=?""",
        (project_id,),
    ).fetchall())
    accepted_by_customer = {
        item["episode_id"] for item in feedback if item["rating"] is not None and item["rating"] >= 4
    }
    delivered_ids = {item["episode_id"] for item in deliveries if item["status"] == "approved"}
    verified = len(delivered_ids & accepted_by_customer) if feedback else len(delivered_ids)
    provider_task_duplicates = conn.execute(
        """SELECT COUNT(*) AS c FROM (
               SELECT v.provider_task_id FROM shot_versions v
               JOIN shots s ON s.id=v.shot_id JOIN episodes e ON e.id=s.episode_id
               WHERE e.project_id=? AND v.provider_task_id IS NOT NULL
               GROUP BY v.provider_task_id HAVING COUNT(*)>1
           )""",
        (project_id,),
    ).fetchone()["c"]
    state_drift = conn.execute(
        """SELECT COUNT(*) AS c FROM jobs WHERE project_id=? AND (
             status='running' AND (lease_owner IS NULL OR lease_expires_at IS NULL)
           )""",
        (project_id,),
    ).fetchone()["c"]
    denominator = max(len(episodes), 1)
    return {
        "project_id": project_id,
        "episode_count": len(episodes),
        "delivery_count": len(delivered_ids),
        "verified_delivery_rate": verified / denominator,
        "first_pass_acceptance_rate": accepted_first / max(len(first_by_episode), 1),
        "evidence_coverage": mean(coverages) if coverages else 0.0,
        "duplicate_billing": int(provider_task_duplicates),
        "state_drift": int(state_drift),
    }


def compare_tracks(
    baseline_samples: list[dict[str, Any]],
    candidate_samples: list[dict[str, Any]],
    *,
    thresholds: dict[str, float] | None = None,
) -> dict[str, Any]:
    policy = {**DEFAULT_THRESHOLDS, **(thresholds or {})}

    def aggregate(samples: list[dict[str, Any]]) -> dict[str, float]:
        keys = (
            "verified_delivery_rate", "first_pass_acceptance_rate", "evidence_coverage",
            "duplicate_billing", "state_drift",
        )
        return {
            key: mean(float(sample.get(key) or 0) for sample in samples) if samples else 0.0
            for key in keys
        }

    baseline = aggregate(baseline_samples)
    candidate = aggregate(candidate_samples)
    regressions: list[dict[str, Any]] = []

    def require(code: str, passed: bool, actual: float, expected: str) -> None:
        if not passed:
            regressions.append({"code": code, "actual": actual, "expected": expected})

    vdr_delta = candidate["verified_delivery_rate"] - baseline["verified_delivery_rate"]
    first_delta = candidate["first_pass_acceptance_rate"] - baseline["first_pass_acceptance_rate"]
    require(
        "VERIFIED_DELIVERY_REGRESSION",
        vdr_delta >= policy["verified_delivery_rate_delta_min"],
        vdr_delta,
        f">={policy['verified_delivery_rate_delta_min']}",
    )
    require(
        "FIRST_PASS_REGRESSION",
        first_delta >= policy["first_pass_acceptance_delta_min"],
        first_delta,
        f">={policy['first_pass_acceptance_delta_min']}",
    )
    require(
        "EVIDENCE_COVERAGE_LOW",
        candidate["evidence_coverage"] >= policy["evidence_coverage_min"],
        candidate["evidence_coverage"],
        f">={policy['evidence_coverage_min']}",
    )
    require(
        "DUPLICATE_BILLING",
        candidate["duplicate_billing"] <= policy["duplicate_billing_max"],
        candidate["duplicate_billing"],
        f"<={policy['duplicate_billing_max']}",
    )
    require(
        "STATE_DRIFT",
        candidate["state_drift"] <= policy["state_drift_max"],
        candidate["state_drift"],
        f"<={policy['state_drift_max']}",
    )
    return {
        "passed": not regressions,
        "baseline": baseline,
        "candidate": candidate,
        "deltas": {
            "verified_delivery_rate": vdr_delta,
            "first_pass_acceptance_rate": first_delta,
        },
        "thresholds": policy,
        "regressions": regressions,
        "sample_count": min(len(baseline_samples), len(candidate_samples)),
    }


def record_benchmark(
    *,
    project_id: str | None,
    baseline_label: str,
    candidate_label: str,
    baseline_samples: list[dict[str, Any]],
    candidate_samples: list[dict[str, Any]],
    thresholds: dict[str, float] | None = None,
    mode: str = "dual_track",
    is_real_project: bool = False,
    attested_by: str | None = None,
    attestation_note: str | None = None,
) -> dict[str, Any]:
    if is_real_project and (not project_id or not (attested_by or "").strip()):
        raise ValueError("真实项目基准必须关联 project_id 并记录 attested_by")
    result = compare_tracks(baseline_samples, candidate_samples, thresholds=thresholds)
    benchmark_id = new_id("bench")
    conn = get_conn()
    conn.execute(
        """INSERT INTO benchmark_runs(
               id, project_id, mode, baseline_label, candidate_label, status, sample_count,
               metrics_json, thresholds_json, regressions_json, is_real_project,
               attested_by, attestation_note, created_at, finished_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            benchmark_id, project_id, mode, baseline_label, candidate_label,
            "passed" if result["passed"] else "failed", result["sample_count"],
            json.dumps({
                "baseline": result["baseline"], "candidate": result["candidate"],
                "deltas": result["deltas"],
            }, ensure_ascii=False),
            json.dumps(result["thresholds"], ensure_ascii=False),
            json.dumps(result["regressions"], ensure_ascii=False), int(is_real_project),
            attested_by, attestation_note, now(), now(),
        ),
    )
    conn.commit()
    return {"benchmark_id": benchmark_id, **result}


def release_gate_status() -> dict[str, Any]:
    """Phase 5 gate: at least three real project benchmarks must pass."""
    conn = get_conn()
    rows = rows_to_dicts(conn.execute(
        """SELECT b.* FROM benchmark_runs b JOIN (
             SELECT project_id, MAX(created_at) AS latest FROM benchmark_runs
             WHERE project_id IS NOT NULL AND is_real_project=1 GROUP BY project_id
           ) x ON x.project_id=b.project_id AND x.latest=b.created_at"""
    ).fetchall())
    passing_projects = sorted({row["project_id"] for row in rows if row["status"] == "passed"})
    return {
        "passed": len(passing_projects) >= 3,
        "required_projects": 3,
        "passing_projects": passing_projects,
        "evaluated_projects": len(rows),
        "reason": None if len(passing_projects) >= 3 else "至少需要 3 个真实项目通过最新双轨基准",
    }
