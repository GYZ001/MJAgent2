"""PRD QA-SO：QA 只评分，禁止驱动重抽/过滤/拦截（阶段 2 媒体路径）。"""
from __future__ import annotations

from pathlib import Path

from app.evidence.media import record_reference_asset, select_best_video_candidate
from app.harness.types import Issue, IssueSeverity
from app.media_pipeline.retry_policy import RetryKind, decide_qa_retake
from app.multiview import keyframe_gate_passed, pack_is_ready, scene_primary_is_usable
from app.video_issues import issues_from_qa
from app.video_modes import ReferenceImageAsset, apply_keep_gate, consistency_retries, reference_gen_retries
from app.video_repair_router import route as route_video_repair


def test_decide_qa_retake_always_denies() -> None:
    decision = decide_qa_retake(
        auto_retake_count=0,
        qa_overall=0.1,
        hard_failures=["character_duplicate", "text_error"],
    )
    assert decision.allow is False
    assert decision.kind == RetryKind.QA_RETAKE
    assert decision.create_new_version is False


def test_reference_retries_are_zero() -> None:
    assert reference_gen_retries() == 0
    assert consistency_retries() == 0


def test_apply_keep_gate_keeps_low_score_assets() -> None:
    asset = ReferenceImageAsset(
        id="a1",
        url="file:///tmp/x.jpg",
        type="plot_key_frame",
        source="generated",
        path="/tmp/x.jpg",
        qualityScore=0.05,
    )
    assert apply_keep_gate(asset) is True
    assert asset.selectedForSeedance is True


def test_keyframe_gate_ignores_low_scores() -> None:
    assert keyframe_gate_passed({
        "overall": 0.1,
        "action_match": 0.1,
        "body_proportion": 0.1,
        "face_identity": 0.1,
        "hard_failures": ["watermark"],
        "status": "failed",
    }) is True
    assert keyframe_gate_passed({
        "overall": None,
        "status": "unverified",
        "hard_failures": ["watermark"],
    }) is True
    assert keyframe_gate_passed({
        "overall": 0.95,
        "status": "scored",
        "hard_failures": ["relative_scale_mismatch"],
    }) is False


def test_pack_ready_is_structural_only() -> None:
    views = [
        {"view_role": "front_full", "status": "ready", "image_path": "/a.jpg"},
        {"view_role": "three_quarter", "status": "ready", "image_path": "/b.jpg"},
        {"view_role": "profile", "status": "ready", "image_path": "/c.jpg"},
    ]
    assert pack_is_ready("failed", views, ("front_full", "three_quarter", "profile")) is True
    assert pack_is_ready("ready", views[:1], ("front_full", "three_quarter", "profile")) is False


def test_scene_primary_usable_ignores_failed_qa(tmp_path: Path) -> None:
    image = tmp_path / "scene.jpg"
    image.write_bytes(b"\xff\xd8\xff\xd9")
    row = {
        "image_path": str(image),
        "qa_json": '{"status":"failed","hard_failures":["watermark"],"overall":0.1}',
    }
    views = [{
        "view_role": "establishing",
        "image_path": str(image),
        "qa_json": '{"status":"failed","hard_failures":["watermark"]}',
    }]
    assert scene_primary_is_usable(row, views) is True


def test_video_qa_issues_are_warnings_not_blockers() -> None:
    issues = issues_from_qa(
        {
            "overall": 0.2,
            "hard_failures": ["character_duplicate", "text_error"],
            "failure_types": ["character_duplicate", "text_error"],
            "issues": ["分身", "乱码文字"],
        },
        {"passed": True, "issues": []},
        shot_id="s1",
    )
    qa_issues = [i for i in issues if i.code.startswith("VIDEO_QA_")]
    assert qa_issues
    assert all(i.severity == IssueSeverity.WARNING for i in qa_issues)


def test_repair_router_rejects_qa_only_issues() -> None:
    plan = route_video_repair([
        Issue(
            code="VIDEO_QA_CHARACTER_DUPLICATE",
            severity=IssueSeverity.WARNING,
            subject="s1",
            message="分身",
        ),
    ])
    assert plan.is_paid is False
    assert plan.strategy == "handoff_human"
    assert "不自动" in plan.reason or "评分" in plan.reason


def test_record_reference_asset_commits_low_score_warning(tmp_path, monkeypatch) -> None:
    from app import db
    from app.evidence import repository as evidence_repository

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "qa-score.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()

    image = tmp_path / "portrait.jpg"
    image.write_bytes(b"\xff\xd8\xff\xd9")

    artifact = record_reference_asset(
        asset_type="character_portrait",
        scope_id="proj:Hero:1",
        file_path=str(image),
        content={"character_name": "Hero"},
        qa={
            "overall": 0.1,
            "issues": ["不像"],
            "hard_gate_passed": True,
            "status": "warning",
        },
        min_score=0.9,
    )
    assert artifact["status"] in {"approved", "validated"}
    evals = evidence_repository.get_evaluations(artifact["id"])
    assert any(row["evaluator_type"] == "model" for row in evals)
    model = next(row for row in evals if row["evaluator_type"] == "model")
    assert int(model["hard_gate_passed"]) == 1


def test_record_reference_asset_deletes_explicit_qa_reject(tmp_path, monkeypatch) -> None:
    from app import db

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "qa-reject.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()

    image = tmp_path / "rejected-portrait.jpg"
    image.write_bytes(b"\xff\xd8\xff\xd9")
    artifact = record_reference_asset(
        asset_type="character_portrait",
        scope_id="proj:Hero:1",
        file_path=str(image),
        content={"character_name": "Hero"},
        qa={
            "overall": 0.9,
            "hard_failures": ["face_mismatch"],
            "status": "failed",
        },
    )

    assert artifact["status"] == "rejected_deleted"
    assert artifact["id"] is None
    assert not image.exists()
    assert db.get_conn().execute(
        "SELECT COUNT(*) AS n FROM artifacts WHERE scope_id='proj:Hero:1'",
    ).fetchone()["n"] == 0


def test_select_best_adopts_technical_even_below_threshold(monkeypatch) -> None:
    import json
    import sqlite3

    from app.evidence import media

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
      CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT);
      CREATE TABLE shots(id TEXT PRIMARY KEY,episode_id TEXT,adopted_version_id TEXT);
      CREATE TABLE shot_versions(id TEXT PRIMARY KEY,shot_id TEXT,version_no INTEGER,status TEXT,
        technical_validation_json TEXT,qa_json TEXT,adoption_reason TEXT,image_inputs TEXT);
      INSERT INTO settings VALUES('auto_retake_threshold','0.8');
      INSERT INTO shots VALUES('s','e',NULL);
    """)
    technical = json.dumps({"passed": True})
    conn.execute(
        "INSERT INTO shot_versions VALUES('low','s',1,'succeeded',?,?,NULL,?)",
        (technical, json.dumps({"overall": 0.2, "failure_types": ["story_repeat"]}), "{}"),
    )
    conn.execute(
        "INSERT INTO shot_versions VALUES('high','s',2,'succeeded',?,?,NULL,?)",
        (technical, json.dumps({"overall": 0.5}), "{}"),
    )
    conn.commit()
    monkeypatch.setattr(media, "get_conn", lambda: conn)
    monkeypatch.setattr(media, "grade_shot_video", lambda *a, **k: {"grade": "B"})
    monkeypatch.setattr(media, "merge_observed_state_out_into_shot_contract", lambda *a, **k: None)

    selected = select_best_video_candidate("s")
    assert selected is not None
    assert selected["version_id"] == "high"
    assert selected["fallback"] is True
