import sqlite3

from app import db
from app import benchmarks


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    for statement in db.MIGRATIONS:
        try:
            conn.execute(statement)
        except sqlite3.OperationalError:
            pass
    return conn


def test_dual_track_gate_detects_regression() -> None:
    baseline = [{"verified_delivery_rate": .8, "first_pass_acceptance_rate": .7, "evidence_coverage": .9}]
    candidate = [{"verified_delivery_rate": .7, "first_pass_acceptance_rate": .8, "evidence_coverage": .95}]
    result = benchmarks.compare_tracks(baseline, candidate)
    assert result["passed"] is False
    assert {item["code"] for item in result["regressions"]} == {"VERIFIED_DELIVERY_REGRESSION"}


def test_release_gate_requires_three_distinct_real_projects(monkeypatch) -> None:
    conn = _conn()
    monkeypatch.setattr(benchmarks, "get_conn", lambda: conn)
    sample = [{"verified_delivery_rate": 1, "first_pass_acceptance_rate": 1, "evidence_coverage": 1}]
    for project_id in ("p1", "p2", "p3"):
        conn.execute(
            "INSERT INTO projects(id,name,status,created_at) VALUES(?,?, 'done', 0)",
            (project_id, project_id),
        )
        benchmarks.record_benchmark(
            project_id=project_id, baseline_label="legacy", candidate_label="harness",
            baseline_samples=sample, candidate_samples=sample,
            is_real_project=True, attested_by="qa-owner", attestation_note="脱敏真实交付项目",
        )
    result = benchmarks.release_gate_status()
    assert result["passed"] is True
    assert result["passing_projects"] == ["p1", "p2", "p3"]


def test_release_gate_ignores_unattested_projects(monkeypatch) -> None:
    conn = _conn()
    monkeypatch.setattr(benchmarks, "get_conn", lambda: conn)
    sample = [{"verified_delivery_rate": 1, "first_pass_acceptance_rate": 1, "evidence_coverage": 1}]
    for project_id in ("demo1", "demo2", "demo3"):
        benchmarks.record_benchmark(
            project_id=project_id, baseline_label="legacy", candidate_label="harness",
            baseline_samples=sample, candidate_samples=sample,
        )
    assert benchmarks.release_gate_status()["passed"] is False


def _screenplay_samples(*, candidate: bool) -> list[dict]:
    return [
        {
            "episode_id": f"ep-{episode_no:02d}",
            "total_tokens": 60 if candidate else 100,
            "provider_time_s": 65 if candidate else 100,
            "wall_time_s": 70 if candidate else 100,
            "model_call_count": 7 if candidate else 6,
            "direct_pass": 1,
            "format_retries": 0,
            "semantic_retries": 0,
            "fidelity_rounds": 0,
            "document_patches": 0,
            "hard_failure": 0,
            "must_fix_issues": 0,
            "source_coverage": 1,
            "dialogue_evidence_rate": 1,
            "identity_conflicts": 0,
            "unresolved_identities": 0,
            "dangling_references": 0,
            "duplicate_billing": 0,
            "state_drift": 0,
            "human_quality_delta": 0,
        }
        for episode_no in range(1, 31)
        for _repeat in range(2)
    ]


def test_screenplay_dual_track_requires_30_episodes_and_passes_targets() -> None:
    baseline = _screenplay_samples(candidate=False)
    candidate = _screenplay_samples(candidate=True)
    result = benchmarks.compare_screenplay_tracks(baseline, candidate)
    assert result["passed"] is True
    assert result["qualified_episode_count"] == 30
    assert result["deltas"]["token_reduction"] == .4
    assert result["deltas"]["p90_wall_time_reduction"] == .3

    insufficient = benchmarks.compare_screenplay_tracks(
        baseline[:20], candidate[:20],
    )
    assert insufficient["passed"] is False
    assert "INSUFFICIENT_REAL_EPISODES" in {
        item["code"] for item in insufficient["regressions"]
    }


def test_record_screenplay_benchmark_persists_real_samples(monkeypatch) -> None:
    conn = _conn()
    monkeypatch.setattr(benchmarks, "get_conn", lambda: conn)
    conn.execute(
        "INSERT INTO projects(id,name,status,created_at) VALUES('p-screenplay','P','done',0)"
    )
    result = benchmarks.record_screenplay_benchmark(
        project_id="p-screenplay",
        baseline_samples=_screenplay_samples(candidate=False),
        candidate_samples=_screenplay_samples(candidate=True),
        attested_by="qa-owner",
        attestation_note="同模型同输入真实双轨",
    )
    assert result["passed"] is True
    row = conn.execute(
        "SELECT mode,is_real_project,sample_count,status FROM benchmark_runs WHERE id=?",
        (result["benchmark_id"],),
    ).fetchone()
    assert dict(row) == {
        "mode": "screenplay_dual_track",
        "is_real_project": 1,
        "sample_count": 60,
        "status": "passed",
    }
