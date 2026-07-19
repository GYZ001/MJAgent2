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
