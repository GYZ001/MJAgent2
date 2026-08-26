"""交付优先 QA：内容问题驱动有限重试，但永不阻断或删除素材。"""
from __future__ import annotations

from pathlib import Path

from app.evidence import repository as evidence_repository
from app.evidence.media import record_reference_asset, select_best_video_candidate
from app.harness.types import Issue, IssueSeverity
from app.multiview import keyframe_gate_passed, pack_is_ready, scene_primary_is_usable
from app.video_issues import issues_from_qa
from app.video_modes import ReferenceImageAsset, apply_keep_gate, consistency_retries, reference_gen_retries
from app.video_repair_router import route as route_video_repair


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
        "identity_contract_passed": True,
    }) is True
    assert keyframe_gate_passed({
        "overall": None,
        "status": "unverified",
        "hard_failures": ["watermark"],
        "identity_contract_passed": False,
    }) is False
    assert keyframe_gate_passed({
        "overall": 0.95,
        "status": "scored",
        "hard_failures": ["relative_scale_mismatch"],
        "identity_contract_passed": True,
    }) is True


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


def test_video_clip_contract_blocks_auto_adoption_without_failure_code_list() -> None:
    issues = issues_from_qa(
        {
            "overall": 0.2,
            "contract_facts": [
                "no_character_duplicate_failed",
                "text_match_below_contract",
            ],
            "issues": ["分身", "乱码文字"],
            "whole_clip_usable": False,
            "runtime_blocking": True,
            "blocking_facts": ["whole_clip_contract_failed"],
        },
        {"passed": True, "issues": []},
        shot_id="s1",
    )
    qa_issues = [i for i in issues if i.code.startswith("VIDEO_QA_")]
    assert qa_issues
    contract_facts = [
        issue for issue in qa_issues
        if issue.code == "VIDEO_QA_CONTRACT_FACT"
    ]
    assert len(contract_facts) == 3
    assert all(issue.severity == IssueSeverity.WARNING for issue in contract_facts)
    clip_issue = next(
        issue for issue in qa_issues
        if issue.code == "VIDEO_QA_CLIP_CONTRACT_FAILED"
    )
    assert clip_issue.severity == IssueSeverity.BLOCKER


def test_repair_router_uses_generic_clip_contract_failure() -> None:
    plan = route_video_repair([
        Issue(
            code="VIDEO_QA_CLIP_CONTRACT_FAILED",
            severity=IssueSeverity.BLOCKER,
            subject="s1",
            message="完整片段生产合同未通过",
            evidence={
                "rule_id": "whole_clip_usable",
                "runtime_blocking": True,
                "recommended_level": "L1",
            },
        ),
    ])
    assert plan.is_paid is True
    assert plan.level == "L1"
    assert plan.strategy == "retake_same_input"
    assert plan.pause_state is None


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


def test_record_reference_asset_keeps_explicit_qa_reject_as_warning(tmp_path, monkeypatch) -> None:
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

    assert artifact["status"] == "approved"
    assert artifact["id"]
    assert image.exists()
    evaluations = evidence_repository.get_evaluations(artifact["id"])
    model = next(row for row in evaluations if row["evaluator_type"] == "model")
    assert model["evaluation_role"] == "score_only"
    assert model["runtime_blocking"] == 0


def test_select_best_immediately_adopts_first_technical_candidate(monkeypatch) -> None:
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
        (
            technical,
            json.dumps({
                "overall": 0.2,
                "contract_facts": ["no_story_repeat_failed"],
                "whole_clip_usable": False,
                "runtime_blocking": False,
            }),
            "{}",
        ),
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
    assert selected["version_id"] == "low"
    forced = select_best_video_candidate("s", force_best=True)
    assert forced is not None
    assert forced["version_id"] == "low"
    assert forced["fallback"] is True


def test_select_best_ignores_stale_qa_json_and_uses_technical_only(
    monkeypatch,
) -> None:
    """VLM 质检已下线：即使历史 qa_json 里还留着 runtime_blocking 等旧字段，
    候选筛选也只看技术校验，不再读取/排除它们。"""
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
      INSERT INTO shots VALUES('s','e',NULL);
    """)
    technical = json.dumps({"passed": True})
    conn.execute(
        "INSERT INTO shot_versions VALUES('first','s',1,'succeeded',?,?,NULL,?)",
        (
            technical,
            json.dumps({
                "overall": 0.95,
                "contract_facts": ["character_match_below_contract"],
                "whole_clip_usable": False,
                "runtime_blocking": True,
            }),
            "{}",
        ),
    )
    conn.execute(
        "INSERT INTO shot_versions VALUES('second','s',2,'succeeded',?,?,NULL,?)",
        (
            technical,
            json.dumps({
                "overall": 0.7,
                "contract_facts": [],
                "whole_clip_usable": True,
            }),
            "{}",
        ),
    )
    conn.commit()
    monkeypatch.setattr(media, "get_conn", lambda: conn)
    monkeypatch.setattr(
        media,
        "grade_shot_video",
        lambda *a, **k: {"grade": "A"},
    )
    monkeypatch.setattr(
        media,
        "merge_observed_state_out_into_shot_contract",
        lambda *a, **k: None,
    )

    selected = select_best_video_candidate("s", force_best=True)

    assert selected is not None
    assert selected["version_id"] == "first"
    assert conn.execute(
        "SELECT adopted_version_id FROM shots WHERE id='s'",
    ).fetchone()[0] == "first"
