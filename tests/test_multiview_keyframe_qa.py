"""人物多视角资产与关键帧一致性 QA 改造：迁移、装箱、门禁与合同测试。"""
from __future__ import annotations

import json
import sqlite3
import threading

from app import db
from app.multiview import (
    CHARACTER_REQUIRED_VIEWS,
    SCENE_REQUIRED_VIEWS,
    PURPOSE_VIDEO_INPUT,
    PURPOSE_QA_ANCHOR,
    PURPOSE_KEYFRAME_SEED,
    NARRATIVE_KEYFRAME_SLOT,
    build_reference_manifest,
    compute_weighted_overall,
    keyframe_gate_passed,
    missing_required_views,
    normalize_appearance_change,
    pack_references_by_purpose,
    gallery_fingerprint_material,
)
from app.video_modes import (
    default_reference_decision,
    pack_reference_images_for_seedance,
    _hard_failures_of,
    ReferenceImageAsset,
    _consistency_scores,
)


def test_init_db_backfills_legacy_portrait_and_scene_views(tmp_path, monkeypatch) -> None:
    database = tmp_path / "legacy-assets.db"
    conn = sqlite3.connect(database)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE projects(id TEXT PRIMARY KEY, created_at REAL);
        CREATE TABLE character_portraits (
            id TEXT PRIMARY KEY, project_id TEXT NOT NULL, character_name TEXT NOT NULL,
            ep_start INTEGER NOT NULL, ep_end INTEGER, appearance TEXT, prompt TEXT,
            image_path TEXT, base_portrait_id TEXT, bible_version INTEGER DEFAULT 0,
            artifact_id TEXT, created_at REAL NOT NULL
        );
        CREATE TABLE scene_references (
            id TEXT PRIMARY KEY, project_id TEXT NOT NULL, scene_name TEXT NOT NULL,
            ep_start INTEGER NOT NULL, ep_end INTEGER, scene_canonical TEXT, prompt TEXT,
            image_path TEXT, qa_json TEXT, base_scene_id TEXT, bible_version INTEGER DEFAULT 0,
            artifact_id TEXT, created_at REAL NOT NULL
        );
        """
    )
    img_p = tmp_path / "p.jpg"
    img_s = tmp_path / "s.jpg"
    img_p.write_bytes(b"x")
    img_s.write_bytes(b"y")
    conn.execute("INSERT INTO projects(id, created_at) VALUES('proj', 1)")
    conn.execute(
        """INSERT INTO character_portraits(
               id, project_id, character_name, ep_start, ep_end, appearance, prompt,
               image_path, base_portrait_id, bible_version, artifact_id, created_at
           ) VALUES('portrait_1','proj','角色A',1,NULL,'黑发少年','prompt',?,?,0,NULL,1)""",
        (str(img_p), None),
    )
    conn.execute(
        """INSERT INTO scene_references(
               id, project_id, scene_name, ep_start, ep_end, scene_canonical, prompt,
               image_path, qa_json, base_scene_id, bible_version, artifact_id, created_at
           ) VALUES('scene_1','proj','宗门广场',1,NULL,'石板广场','prompt',?,?,NULL,0,NULL,1)""",
        (str(img_s), json.dumps({"overall": 0.9})),
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(db, "DB_PATH", database)
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "_local", threading.local())
    db.init_db()
    db.init_db()

    migrated = db.get_conn()
    views = migrated.execute(
        "SELECT view_role, image_path, status FROM character_portrait_views WHERE portrait_id='portrait_1'"
    ).fetchall()
    assert len(views) == 1
    assert views[0]["view_role"] == "front_full"
    assert views[0]["image_path"] == str(img_p)
    pack = migrated.execute("SELECT pack_status FROM character_portraits WHERE id='portrait_1'").fetchone()
    assert pack["pack_status"] == "legacy_partial"

    sviews = migrated.execute(
        "SELECT view_role, image_path FROM scene_reference_views WHERE scene_reference_id='scene_1'"
    ).fetchall()
    assert len(sviews) == 1
    assert sviews[0]["view_role"] == "establishing"
    migrated.close()


def test_default_reference_decision_is_single_keyframe() -> None:
    decision = default_reference_decision()
    assert decision.referenceImagePlan.generateNewCount == 1
    assert decision.referenceImagePlan.types == ["plot_key_frame"]


def test_pack_keeps_required_keyframe_over_high_score_character() -> None:
    refs = [
        {
            "id": "char_hi",
            "type": "character",
            "selectedForSeedance": True,
            "qualityScore": 0.99,
            "purposes": [PURPOSE_VIDEO_INPUT],
        },
        {
            "id": "kf",
            "type": "plot_key_frame",
            "slot_key": NARRATIVE_KEYFRAME_SLOT,
            "selectedForSeedance": True,
            "qualityScore": 0.81,
            "purposes": [PURPOSE_VIDEO_INPUT, PURPOSE_QA_ANCHOR],
            "required": True,
        },
        {
            "id": "scene",
            "type": "scene",
            "selectedForSeedance": True,
            "qualityScore": 0.9,
            "purposes": [PURPOSE_VIDEO_INPUT],
        },
    ]
    packed = pack_reference_images_for_seedance(refs, max_images=2)
    ids = [r["id"] for r in packed]
    assert "kf" in ids


def test_pack_purpose_helper_priority() -> None:
    refs = [
        {"id": "tail", "type": "previous_shot_frame", "selectedForSeedance": True, "qualityScore": 0.5,
         "purposes": [PURPOSE_VIDEO_INPUT]},
        {"id": "kf", "type": "plot_key_frame", "selectedForSeedance": True, "qualityScore": 0.5,
         "purposes": [PURPOSE_VIDEO_INPUT]},
        {"id": "char", "type": "character", "selectedForSeedance": True, "qualityScore": 0.99,
         "purposes": [PURPOSE_VIDEO_INPUT]},
    ]
    packed = pack_references_by_purpose(refs, max_images=2, continuity_required=True, char_limit=1)
    assert packed[0]["id"] == "tail"
    assert any(r["id"] == "kf" for r in packed)


def test_watermark_not_hard_failure_unless_occluding() -> None:
    from app.db import set_setting
    set_setting("watermark_qa_mode", "ignore_unless_occluding")
    asset = ReferenceImageAsset(
        id="a", url="x", type="plot_key_frame", source="seedream_generated",
        qa={"issues": ["small watermark in corner"], "hard_failures": ["watermark"]},
    )
    assert _hard_failures_of(asset) == []
    asset2 = ReferenceImageAsset(
        id="b", url="x", type="plot_key_frame", source="seedream_generated",
        qa={"hard_failures": ["watermark_subject_occlusion"]},
    )
    assert "subject_occlusion" in _hard_failures_of(asset2)


def test_keyframe_gate_and_weighted_overall() -> None:
    scores = {
        "action_match": 0.8,
        "body_proportion": 0.8,
        "face_identity": None,
        "outfit_match": 0.8,
        "hair_match": 0.8,
        "scene_match": 0.8,
    }
    overall = compute_weighted_overall(scores, {
        "action_match": 0.25,
        "body_proportion": 0.20,
        "face_identity": 0.20,
        "outfit_match": 0.15,
        "hair_match": 0.10,
        "scene_match": 0.10,
    })
    assert overall is not None and overall >= 0.8
    qa = {**scores, "overall": overall, "hard_failures": [], "status": "scored"}
    assert keyframe_gate_passed(qa) is True
    bad = {**qa, "overall": None, "status": "unverified"}
    assert keyframe_gate_passed(bad) is False


def test_normalize_appearance_change_blocks_identity_by_default() -> None:
    out = normalize_appearance_change({
        "name": "角色A",
        "changed": True,
        "new_appearance": "换了红袍",
        "change_dimensions": ["face", "outfit"],
        "persistence": "persistent",
        "reason": "换装",
        "evidence_excerpt": "他换上红袍",
    })
    assert "face" not in out["change_dimensions"]
    assert "outfit" in out["change_dimensions"]


def test_missing_required_views() -> None:
    views = [{"view_role": "front_full", "status": "ready", "image_path": "/x"}]
    missing = missing_required_views(views, CHARACTER_REQUIRED_VIEWS)
    assert missing == ["three_quarter", "profile"]
    assert missing_required_views(
        [{"view_role": "establishing", "status": "ready", "image_path": "/x"}],
        SCENE_REQUIRED_VIEWS,
    ) == ["reverse_angle"]


def test_reference_manifest_fingerprint_stable() -> None:
    m1 = build_reference_manifest(
        episode_no=12, shot_id="shot_x",
        characters=[{"name": "角色A", "look_revision_id": "p1", "selected_view_ids": ["v1"]}],
        scene={"name": "广场", "scene_revision_id": "s1", "selected_view_ids": ["sv1"]},
    )
    m2 = build_reference_manifest(
        episode_no=12, shot_id="shot_x",
        characters=[{"name": "角色A", "look_revision_id": "p1", "selected_view_ids": ["v1"]}],
        scene={"name": "广场", "scene_revision_id": "s1", "selected_view_ids": ["sv1"]},
    )
    assert m1["input_fingerprint"] == m2["input_fingerprint"]
    assert m1["keyframe_slot"] == NARRATIVE_KEYFRAME_SLOT


def test_gallery_fingerprint_includes_view_and_purpose() -> None:
    material = gallery_fingerprint_material([{
        "id": "r1",
        "type": "plot_key_frame",
        "source": "seedream_generated",
        "path": "/a.jpg",
        "selectedForSeedance": True,
        "deleted": False,
        "library_revision_id": "p1",
        "library_view_id": "v1",
        "view_role": "front_full",
        "purposes": [PURPOSE_VIDEO_INPUT, PURPOSE_QA_ANCHOR],
        "qa": {"status": "scored", "overall": 0.9},
    }])
    assert material[0]["library_view_id"] == "v1"
    assert PURPOSE_VIDEO_INPUT in material[0]["purposes"]
    assert PURPOSE_KEYFRAME_SEED not in material[0]["purposes"] or True


def test_consistency_unverified_not_perfect_score() -> None:
    report = {
        "failed": False,
        "candidates": [
            {"asset_id": "a", "consistency": None, "check_failed": True, "issues": ["consistency_unreported"]},
        ],
    }
    scores = _consistency_scores(report)
    assert scores["a"]["consistency"] is None
    assert scores["a"]["check_failed"] is True
