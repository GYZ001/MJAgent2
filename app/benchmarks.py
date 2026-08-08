from __future__ import annotations

import json
import math
from statistics import mean, stdev
from typing import Any

from app.db import get_conn, new_id, now, rows_to_dicts


DEFAULT_THRESHOLDS = {
    "verified_delivery_rate_delta_min": 0.0,
    "first_pass_acceptance_delta_min": 0.0,
    "evidence_coverage_min": 0.9,
    "duplicate_billing_max": 0,
    "state_drift_max": 0,
}


SCREENPLAY_THRESHOLDS = {
    "minimum_episode_count": 30,
    "minimum_repeats_per_track": 2,
    "token_reduction_min": 0.30,
    "p90_wall_time_reduction_min": 0.25,
    "hard_failure_rate_max": 0.05,
    "source_coverage_min": 1.0,
    "dialogue_evidence_rate_min": 1.0,
    "unresolved_identities_max": 0.0,
    "dangling_references_max": 0.0,
    "must_fix_issues_max": 0.0,
    "duplicate_billing_max": 0.0,
    "state_drift_max": 0.0,
    "quality_delta_ci95_lower_min": -0.02,
}


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def compare_screenplay_tracks(
    baseline_samples: list[dict[str, Any]],
    candidate_samples: list[dict[str, Any]],
    *,
    thresholds: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Evaluate the M5 real dual-track screenplay acceptance contract."""
    policy = {**SCREENPLAY_THRESHOLDS, **(thresholds or {})}

    def values(samples: list[dict[str, Any]], key: str) -> list[float]:
        return [float(sample.get(key) or 0) for sample in samples]

    def aggregate(samples: list[dict[str, Any]]) -> dict[str, float]:
        metric_keys = (
            "total_tokens", "provider_time_s", "wall_time_s", "model_call_count",
            "direct_pass", "format_retries", "semantic_retries", "fidelity_rounds",
            "document_patches", "hard_failure", "must_fix_issues",
            "source_coverage", "dialogue_evidence_rate", "identity_conflicts",
            "unresolved_identities", "dangling_references", "duplicate_billing",
            "state_drift",
        )
        result = {
            key: mean(values(samples, key)) if samples else 0.0
            for key in metric_keys
        }
        result["p90_wall_time_s"] = _percentile(values(samples, "wall_time_s"), 0.90)
        return result

    baseline = aggregate(baseline_samples)
    candidate = aggregate(candidate_samples)
    token_reduction = (
        (baseline["total_tokens"] - candidate["total_tokens"])
        / baseline["total_tokens"]
        if baseline["total_tokens"] > 0 else 0.0
    )
    p90_reduction = (
        (baseline["p90_wall_time_s"] - candidate["p90_wall_time_s"])
        / baseline["p90_wall_time_s"]
        if baseline["p90_wall_time_s"] > 0 else 0.0
    )
    quality_deltas = values(candidate_samples, "human_quality_delta")
    quality_mean = mean(quality_deltas) if quality_deltas else 0.0
    quality_ci_lower = (
        quality_mean - 1.96 * stdev(quality_deltas) / math.sqrt(len(quality_deltas))
        if len(quality_deltas) > 1 else quality_mean
    )
    episode_ids = {
        str(sample.get("episode_id") or "")
        for sample in [*baseline_samples, *candidate_samples]
        if str(sample.get("episode_id") or "")
    }
    baseline_repeats: dict[str, int] = {}
    candidate_repeats: dict[str, int] = {}
    for sample in baseline_samples:
        key = str(sample.get("episode_id") or "")
        baseline_repeats[key] = baseline_repeats.get(key, 0) + 1
    for sample in candidate_samples:
        key = str(sample.get("episode_id") or "")
        candidate_repeats[key] = candidate_repeats.get(key, 0) + 1
    qualified_episodes = {
        episode_id for episode_id in episode_ids
        if baseline_repeats.get(episode_id, 0) >= policy["minimum_repeats_per_track"]
        and candidate_repeats.get(episode_id, 0) >= policy["minimum_repeats_per_track"]
    }

    regressions: list[dict[str, Any]] = []

    def require(code: str, passed: bool, actual: float, expected: str) -> None:
        if not passed:
            regressions.append({"code": code, "actual": actual, "expected": expected})

    require(
        "INSUFFICIENT_REAL_EPISODES",
        len(qualified_episodes) >= policy["minimum_episode_count"],
        float(len(qualified_episodes)),
        f">={policy['minimum_episode_count']}",
    )
    require("TOKEN_REDUCTION_LOW", token_reduction >= policy["token_reduction_min"], token_reduction, f">={policy['token_reduction_min']}")
    require("P90_WALL_TIME_REDUCTION_LOW", p90_reduction >= policy["p90_wall_time_reduction_min"], p90_reduction, f">={policy['p90_wall_time_reduction_min']}")
    for code, key, comparison, expected in (
        ("HARD_FAILURE_RATE_HIGH", "hard_failure", "max", policy["hard_failure_rate_max"]),
        ("SOURCE_COVERAGE_LOW", "source_coverage", "min", policy["source_coverage_min"]),
        ("DIALOGUE_EVIDENCE_LOW", "dialogue_evidence_rate", "min", policy["dialogue_evidence_rate_min"]),
        ("UNRESOLVED_IDENTITY", "unresolved_identities", "max", policy["unresolved_identities_max"]),
        ("DANGLING_REFERENCE", "dangling_references", "max", policy["dangling_references_max"]),
        ("MUST_FIX_REMAINS", "must_fix_issues", "max", policy["must_fix_issues_max"]),
        ("DUPLICATE_BILLING", "duplicate_billing", "max", policy["duplicate_billing_max"]),
        ("STATE_DRIFT", "state_drift", "max", policy["state_drift_max"]),
    ):
        actual = candidate[key]
        passed = actual >= expected if comparison == "min" else actual <= expected
        require(code, passed, actual, f"{'>=' if comparison == 'min' else '<='}{expected}")
    require(
        "HUMAN_QUALITY_NONINFERIORITY_FAILED",
        quality_ci_lower >= policy["quality_delta_ci95_lower_min"],
        quality_ci_lower,
        f">={policy['quality_delta_ci95_lower_min']}",
    )
    return {
        "passed": not regressions,
        "baseline": baseline,
        "candidate": candidate,
        "deltas": {
            "token_reduction": token_reduction,
            "p90_wall_time_reduction": p90_reduction,
            "human_quality_delta_mean": quality_mean,
            "human_quality_delta_ci95_lower": quality_ci_lower,
        },
        "thresholds": policy,
        "regressions": regressions,
        "sample_count": min(len(baseline_samples), len(candidate_samples)),
        "qualified_episode_count": len(qualified_episodes),
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
