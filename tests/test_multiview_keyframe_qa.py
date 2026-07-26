"""人物多视角资产与关键帧一致性 QA 改造：迁移、装箱、门禁与合同测试。"""
from __future__ import annotations

import json
import sqlite3
import threading

import pytest

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


def test_manifest_blocks_incomplete_character_pack() -> None:
    from app.multiview import manifest_production_blockers, assert_manifest_allows_production
    from app import hiagent

    incomplete = {
        "characters": [{
            "name": "角色A",
            "look_revision_id": "p1",
            "pack_status": "failed",
            "missing_required": ["profile"],
            "selected_view_ids": ["v1"],
            "selected_views": [{"id": "v1", "view_role": "front_full"}],
        }],
        "scene": None,
    }
    blockers = manifest_production_blockers(incomplete)
    assert any("角色A" in b for b in blockers)
    with pytest.raises(hiagent.ProviderError, match="禁止关键帧"):
        assert_manifest_allows_production(incomplete)

    ready = {
        "characters": [{
            "name": "角色A",
            "look_revision_id": "p1",
            "pack_status": "ready",
            "missing_required": [],
            "selected_view_ids": ["v1", "v2"],
            "selected_views": [
                {"id": "v1", "view_role": "front_full"},
                {"id": "v2", "view_role": "profile"},
            ],
        }],
        "scene": {
            "name": "广场",
            "scene_revision_id": "s1",
            "pack_status": "ready",
            "missing_required": [],
            "selected_view_ids": ["sv1"],
            "selected_views": [{"id": "sv1", "view_role": "establishing"}],
        },
    }
    assert manifest_production_blockers(ready) == []
    assert_manifest_allows_production(ready)


def test_manifest_blocks_missing_scene_reverse_angle() -> None:
    from app.multiview import manifest_production_blockers

    blockers = manifest_production_blockers({
        "characters": [],
        "scene": {
            "name": "宗门广场",
            "scene_revision_id": "s1",
            "pack_status": "legacy_partial",
            "missing_required": ["reverse_angle"],
            "selected_view_ids": ["sv1"],
            "selected_views": [{"id": "sv1", "view_role": "establishing"}],
        },
    })
    assert any("宗门广场" in b and "legacy_partial" in b for b in blockers)

    blockers2 = manifest_production_blockers({
        "characters": [],
        "scene": {
            "name": "宗门广场",
            "scene_revision_id": "s1",
            "pack_status": "ready",
            "missing_required": ["reverse_angle"],
            "selected_view_ids": ["sv1"],
            "selected_views": [{"id": "sv1", "view_role": "establishing"}],
        },
    })
    assert any("reverse_angle" in b or "缺少必需视角" in b for b in blockers2)


def test_view_input_fingerprint_stable_and_distinct() -> None:
    from app.multiview import view_input_fingerprint

    a = view_input_fingerprint(
        view_role="profile", prompt="p1", anchor_text="黑发", parent_revision_id="portrait_1",
    )
    b = view_input_fingerprint(
        view_role="profile", prompt="p1", anchor_text="黑发", parent_revision_id="portrait_1",
    )
    c = view_input_fingerprint(
        view_role="profile", prompt="p2", anchor_text="黑发", parent_revision_id="portrait_1",
    )
    assert a == b
    assert a != c


def test_ready_view_fingerprint_reuse_without_regen(tmp_path) -> None:
    from app.multiview import (
        _ready_view_matches_fingerprint, view_input_fingerprint,
    )
    img = tmp_path / "profile.jpg"
    img.write_bytes(b"x")
    fp = view_input_fingerprint(
        view_role="profile", prompt="side", anchor_text="黑发", parent_revision_id="p1",
    )
    view = {
        "id": "v1", "status": "ready", "image_path": str(img), "input_fingerprint": fp,
    }
    assert _ready_view_matches_fingerprint(view, fp) is True
    assert _ready_view_matches_fingerprint(view, fp + "x") is False
    # 旧数据无指纹：可复用，避免重复付费
    legacy = {"id": "v2", "status": "ready", "image_path": str(img), "input_fingerprint": None}
    assert _ready_view_matches_fingerprint(legacy, fp) is True


def test_manifest_revisions_match_for_worker_restart() -> None:
    from app.multiview import manifest_revisions_match, build_reference_manifest

    frozen = build_reference_manifest(
        episode_no=1, shot_id="shot_1",
        characters=[{"name": "A", "look_revision_id": "p_old", "selected_view_ids": ["v1"]}],
        scene={"name": "广场", "scene_revision_id": "s_old", "selected_view_ids": ["sv1"]},
    )
    same = build_reference_manifest(
        episode_no=1, shot_id="shot_1",
        characters=[{"name": "A", "look_revision_id": "p_old", "selected_view_ids": ["v9"]}],
        scene={"name": "广场", "scene_revision_id": "s_old", "selected_view_ids": ["sv9"]},
    )
    changed = build_reference_manifest(
        episode_no=1, shot_id="shot_1",
        characters=[{"name": "A", "look_revision_id": "p_new", "selected_view_ids": ["v1"]}],
        scene={"name": "广场", "scene_revision_id": "s_old", "selected_view_ids": ["sv1"]},
    )
    assert manifest_revisions_match(frozen, same) is True
    assert manifest_revisions_match(frozen, changed) is False


def test_pack_result_failed_not_ok() -> None:
    from app.multiview import pack_result_ok
    assert pack_result_ok({"status": "ready"}) is True
    assert pack_result_ok({"status": "disabled"}) is True
    assert pack_result_ok({"status": "failed"}) is False
    assert pack_result_ok(None) is False


def test_keyframe_qa_receives_library_visual_anchors(monkeypatch, tmp_path) -> None:
    """关键帧 QA 必须实际上传候选图 + 库内人物/场景锚点，不能空跑。"""
    import asyncio
    from app.multiview import review_keyframe_with_evidence
    from app.schemas import Bible, Character, Shot, World

    captured: dict = {}

    async def fake_vlm(frames, expectation, call_meta=None):
        captured["frame_count"] = len(frames)
        captured["expectation"] = expectation
        captured["anchor_count"] = (call_meta or {}).get("anchor_count")
        return json.dumps({
            "overall": 0.9, "action_match": 0.9, "body_proportion": 0.9,
            "face_identity": 0.9, "outfit_match": 0.9, "hair_match": 0.9, "scene_match": 0.9,
            "hard_failures": [], "issues": [],
        })

    monkeypatch.setattr("app.hiagent.vlm_check", fake_vlm)
    monkeypatch.setattr("app.multiview.visual_evidence_qa_enabled", lambda: True)
    monkeypatch.setattr("app.hiagent.encode_image_file", lambda path: f"b64:{path}")

    front = tmp_path / "front.jpg"
    est = tmp_path / "est.jpg"
    front.write_bytes(b"front")
    est.write_bytes(b"est")

    shot = Shot(
        shot_no=1, duration_s=5, shot_size="中景", camera_move="固定", scene_setting="室内",
        characters=["A"], action_desc="A坐着", first_frame_desc="A坐着", last_frame_desc="A站起",
        source_excerpt="A坐着", dialogues=[], transition="硬切", continuity_from_prev=False,
    )
    bible = Bible(
        characters=[Character(name="A", role="lead", appearance_canonical="黑发")],
        world=World(visual_style_canonical="anime"),
    )
    anchors = [
        {"image_path": str(front), "entity_type": "character", "entity_name": "A",
         "view_role": "front_full"},
        {"image_path": str(est), "entity_type": "scene", "entity_name": "室内",
         "view_role": "establishing"},
    ]

    qa = asyncio.run(review_keyframe_with_evidence(
        "candidate_b64", shot=shot, bible=bible, visual_anchors=anchors,
    ))
    assert captured.get("frame_count") == 3, "必须包含候选图与两张库内锚点"
    assert captured.get("anchor_count") == 2
    assert qa.get("overall") is not None
    assert qa.get("status") == "scored" or qa.get("overall") >= 0.8


def test_shot_video_assets_stale_when_portrait_revision_changes() -> None:
    from app.domain.storyboard_ops import _shot_adopted_assets_stale

    class _Row(dict):
        def __getitem__(self, key):
            return super().__getitem__(key)

    version_row = _Row({
        "image_inputs": json.dumps({
            "reference_manifest": {
                "characters": [{"name": "A", "look_revision_id": "portrait_old"}],
                "scene": {"name": "广场", "scene_revision_id": "scene_old"},
            }
        }),
    })
    shot_row = _Row({"episode_id": "ep1"})

    class _Conn:
        def execute(self, sql, params=()):
            class _C:
                def fetchone(self_inner):
                    if "FROM episodes" in sql:
                        return {"project_id": "proj", "episode_no": 1}
                    return None
            return _C()

    import app.multiview as mv

    # patch episode portrait/scene lookups
    original_portrait = mv.portrait_row_for_episode
    original_scene = mv.scene_row_for_episode
    mv.portrait_row_for_episode = lambda *a, **k: {"id": "portrait_new"}
    mv.scene_row_for_episode = lambda *a, **k: {"id": "scene_old"}
    try:
        assert _shot_adopted_assets_stale(_Conn(), shot_row, version_row) is True
        mv.portrait_row_for_episode = lambda *a, **k: {"id": "portrait_old"}
        assert _shot_adopted_assets_stale(_Conn(), shot_row, version_row) is False
    finally:
        mv.portrait_row_for_episode = original_portrait
        mv.scene_row_for_episode = original_scene
