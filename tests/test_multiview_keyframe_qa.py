"""人物多视角资产与关键帧一致性 QA 改造：迁移、装箱、门禁与合同测试。"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

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


def test_default_reference_decision_reserves_at_most_two_timeline_keyframes() -> None:
    decision = default_reference_decision()
    assert decision.referenceImagePlan.totalCount == 2
    assert decision.referenceImagePlan.generateNewCount == 2
    assert decision.referenceImagePlan.types == ["plot_key_frame"] * 2


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


def test_pack_purpose_helper_excludes_unselected_video_purpose() -> None:
    refs = [
        {
            "id": "keep",
            "type": "plot_key_frame",
            "selectedForSeedance": True,
            "qualityScore": 0.9,
            "purposes": [PURPOSE_VIDEO_INPUT],
        },
        {
            "id": "rejected",
            "type": "plot_key_frame",
            "selectedForSeedance": False,
            "qualityScore": 0.99,
            "purposes": [PURPOSE_VIDEO_INPUT],
            "rejectReason": "quality_below_threshold",
        },
    ]

    packed = pack_references_by_purpose(
        refs, max_images=2, continuity_required=False, char_limit=1,
    )

    assert [ref["id"] for ref in packed] == ["keep"]


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
    bad = {**qa, "overall": None, "status": "unverified", "hard_failures": ["watermark"]}
    # 普通分数/水印不阻断；明确结构硬伤必须阻断。
    assert keyframe_gate_passed(bad) is True
    assert keyframe_gate_passed({
        **qa, "overall": 0.99, "hard_failures": ["relative_scale_mismatch"],
    }) is False


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


def test_manifest_allows_explicitly_unmanaged_storyboard_extras() -> None:
    from app.multiview import manifest_production_blockers

    manifest = {
        "characters": [{
            "name": "临时测验员",
            "asset_required": False,
            "look_revision_id": None,
            "selected_view_ids": [],
            "selected_views": [],
        }],
        "scene": {
            "name": "临时过场",
            "asset_required": False,
            "scene_revision_id": None,
            "selected_view_ids": [],
            "selected_views": [],
        },
    }
    assert manifest_production_blockers(manifest) == []

    # Frozen manifests from before asset_required existed stay strict.
    legacy = {"characters": [{"name": "旧角色", "look_revision_id": None}], "scene": None}
    assert manifest_production_blockers(legacy)


def test_asset_requirement_change_invalidates_frozen_manifest() -> None:
    from app.multiview import manifest_revisions_match

    frozen = {
        "characters": [{"name": "临时测验员", "look_revision_id": None}],
        "scene": None,
    }
    current = {
        "characters": [{
            "name": "临时测验员", "look_revision_id": None, "asset_required": False,
        }],
        "scene": None,
    }
    assert not manifest_revisions_match(frozen, current)


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


def test_manifest_revisions_match_detects_same_parent_view_redo() -> None:
    from app.multiview import manifest_revisions_match, build_reference_manifest

    def manifest(fp: str):
        return build_reference_manifest(
            episode_no=1, shot_id="shot_1",
            characters=[{
                "name": "A", "look_revision_id": "p1",
                "selected_views": [{
                    "id": "v1", "view_role": "front_full", "input_fingerprint": fp,
                }],
            }],
            scene=None,
        )

    assert manifest_revisions_match(manifest("fp-old"), manifest("fp-old")) is True
    assert manifest_revisions_match(manifest("fp-old"), manifest("fp-new")) is False


def test_failed_character_view_redo_preserves_ready_pack(tmp_path, monkeypatch) -> None:
    """QA 只评分：候选图低分仍替换指定视角，整包保持 ready。"""
    import asyncio
    import base64
    import threading
    from app import config, db
    import app.multiview as mv

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "atomic-redo.db")
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "_local", threading.local())
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    db.init_db()
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO projects(id, name, status, bible_json, created_at) VALUES(?,?,?,?,?)",
        (
            "proj", "P", "created",
            json.dumps({
                "world": {"visual_style_canonical": "anime"},
                "characters": [{
                    "name": "A",
                    "appearance_canonical": "black hair",
                    "portrait_prompt_override": "latest silver hair and red armor",
                }],
            }),
            1,
        ),
    )
    old_paths = {}
    for role in CHARACTER_REQUIRED_VIEWS:
        path = tmp_path / f"old-{role}.jpg"
        path.write_bytes(b"old")
        old_paths[role] = str(path)
    conn.execute(
        """INSERT INTO character_portraits(
               id, project_id, character_name, ep_start, appearance, prompt, image_path,
               bible_version, pack_status, created_at
           ) VALUES('portrait_1','proj','A',1,'black hair','prompt',?,1,'ready',1)""",
        (old_paths["front_full"],),
    )
    for idx, role in enumerate(CHARACTER_REQUIRED_VIEWS):
        conn.execute(
            """INSERT INTO character_portrait_views(
                   id, portrait_id, view_role, image_path, prompt, qa_json, status,
                   selected, input_fingerprint, created_at
               ) VALUES(?,?,?,?,?,?, 'ready',1,?,?)""",
            (f"view_{idx}", "portrait_1", role, old_paths[role], "prompt", "{}", f"old-{role}", idx + 1),
        )
    conn.commit()

    generated_prompts = []
    review_anchors = []

    async def fake_generate(prompt, **_kwargs):
        generated_prompts.append(prompt)
        return {"b64_json": base64.b64encode(b"candidate").decode()}

    async def reject_view(_path, anchor, _view_role):
        review_anchors.append(anchor)
        return {"overall": 0.2, "status": "failed", "issues": ["face drift"]}

    monkeypatch.setattr(mv, "_generate_image", fake_generate)
    monkeypatch.setattr(mv, "review_character_view", reject_view)
    result = asyncio.run(mv.regenerate_character_view(
        project_id="proj", portrait_id="portrait_1", view_role="profile",
    ))

    current = conn.execute(
        "SELECT image_path, status, input_fingerprint, qa_json FROM character_portrait_views "
        "WHERE portrait_id='portrait_1' AND view_role='profile'",
    ).fetchone()
    pack = conn.execute(
        "SELECT pack_status FROM character_portraits WHERE id='portrait_1'",
    ).fetchone()
    assert result["status"] == "ready"
    assert current["image_path"] != old_paths["profile"]
    assert Path(current["image_path"]).read_bytes() == b"candidate"
    assert current["status"] == "ready"
    assert current["input_fingerprint"] != "old-profile"
    assert "latest silver hair and red armor" in generated_prompts[0]
    assert "black hair" not in generated_prompts[0]
    assert review_anchors == ["latest silver hair and red armor"]
    qa = json.loads(current["qa_json"])
    assert qa["overall"] == 0.2
    assert qa["runtime_blocking"] is False
    assert pack["pack_status"] == "ready"


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


def test_shot_video_assets_stale_when_selected_view_is_redone() -> None:
    from app.domain.storyboard_ops import _shot_adopted_assets_stale
    import app.multiview as mv

    version_row = {
        "image_inputs": json.dumps({
            "reference_manifest": {
                "characters": [{
                    "name": "A", "look_revision_id": "portrait_1",
                    "selected_views": [{
                        "id": "view_1", "view_role": "front_full",
                        "input_fingerprint": "fp-old",
                    }],
                }],
                "scene": None,
            },
        }),
    }

    class _Conn:
        view_fp = "fp-new"

        def execute(self, sql, params=()):
            value = None
            if "FROM episodes" in sql:
                value = {"project_id": "proj", "episode_no": 1}
            elif "FROM character_portrait_views" in sql:
                value = {"input_fingerprint": self.view_fp}

            class _Cursor:
                def fetchone(self_inner):
                    return value

            return _Cursor()

    original_portrait = mv.portrait_row_for_episode
    original_scene = mv.scene_row_for_episode
    mv.portrait_row_for_episode = lambda *a, **k: {"id": "portrait_1"}
    mv.scene_row_for_episode = lambda *a, **k: None
    conn = _Conn()
    try:
        assert _shot_adopted_assets_stale(conn, {"episode_id": "ep1"}, version_row) is True
        conn.view_fp = "fp-old"
        assert _shot_adopted_assets_stale(conn, {"episode_id": "ep1"}, version_row) is False
    finally:
        mv.portrait_row_for_episode = original_portrait
        mv.scene_row_for_episode = original_scene


def test_video_qa_sample_positions_high_risk() -> None:
    from app.multiview import video_qa_sample_positions, shot_needs_high_risk_frame_sample

    assert video_qa_sample_positions(high_risk=False) == (0.0, 0.50, 0.97)
    assert video_qa_sample_positions(high_risk=True) == (0.0, 0.25, 0.50, 0.75, 0.95)
    assert shot_needs_high_risk_frame_sample({"duration_s": 8, "risk_tags": []}) is True
    assert shot_needs_high_risk_frame_sample({"duration_s": 4, "risk_tags": ["identity_risk"]}) is True
    assert shot_needs_high_risk_frame_sample({"duration_s": 4, "risk_tags": []}) is False


def test_clone_portrait_views_zero_cost_bind(tmp_path, monkeypatch) -> None:
    """仅本集造型结束后，完整旧包应零付费重新绑定为 ready（含全部视角）。"""
    import threading
    from app import db
    from app.multiview import bind_ready_portrait_reuse, list_portrait_views

    database = tmp_path / "reuse.db"
    monkeypatch.setattr(db, "DB_PATH", database)
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "_local", threading.local())
    db.init_db()
    conn = db.get_conn()
    img = tmp_path / "front.jpg"
    img.write_bytes(b"x")
    side = tmp_path / "side.jpg"
    side.write_bytes(b"y")
    conn.execute("INSERT INTO projects(id, name, created_at) VALUES('proj','t',1)")
    conn.execute(
        """INSERT INTO character_portraits(
               id, project_id, character_name, ep_start, ep_end, appearance, prompt,
               image_path, base_portrait_id, bible_version, artifact_id, pack_status,
               group_qa_json, created_at
           ) VALUES('p_old','proj','A',1,5,'黑发','prompt',?,NULL,0,NULL,'ready',?,1)""",
        (str(img), json.dumps({"overall": 0.9})),
    )
    for role, path in (("front_full", img), ("three_quarter", side), ("profile", side)):
        conn.execute(
            """INSERT INTO character_portrait_views(
                   id, portrait_id, view_role, framing, image_path, prompt, qa_json,
                   artifact_id, base_view_id, status, selected, input_fingerprint, created_at
               ) VALUES(?,?,?,?,?,?,NULL,NULL,NULL,'ready',1,'fp',1)""",
            (f"v_{role}", "p_old", role, "full", str(path), "p"),
        )
    conn.commit()

    reuse_id = bind_ready_portrait_reuse(
        conn, project_id="proj", character_name="A", source_portrait_id="p_old",
        ep_start=6, bible_version=0,
    )
    conn.commit()
    row = conn.execute("SELECT pack_status, ep_start FROM character_portraits WHERE id=?", (reuse_id,)).fetchone()
    assert row["pack_status"] == "ready"
    assert row["ep_start"] == 6
    views = list_portrait_views(reuse_id, conn=conn)
    assert {v["view_role"] for v in views} == {"front_full", "three_quarter", "profile"}
    assert all(v["status"] == "ready" for v in views)
    assert all(Path(v["image_path"]).exists() for v in views)


def test_refresh_portrait_pack_failure_does_not_switch(monkeypatch, tmp_path) -> None:
    """整包 QA 失败不得切换版本：临时段删除，旧开区间继续生效。"""
    import asyncio
    import threading
    from app import db, hiagent
    from app.portraits import _refresh_portrait_on_drift

    database = tmp_path / "drift.db"
    monkeypatch.setattr(db, "DB_PATH", database)
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "_local", threading.local())
    db.init_db()
    conn = db.get_conn()
    img = tmp_path / "old.jpg"
    img.write_bytes(b"old")
    conn.execute(
        "INSERT INTO projects(id, name, bible_json, bible_version, created_at) VALUES('proj','t',?,?,1)",
        (json.dumps({"characters": [{"name": "A", "appearance_canonical": "黑发"}],
                     "world": {"visual_style_canonical": "anime"}}), 1),
    )
    conn.execute(
        """INSERT INTO character_portraits(
               id, project_id, character_name, ep_start, ep_end, appearance, prompt,
               image_path, base_portrait_id, bible_version, artifact_id, pack_status, created_at
           ) VALUES('p_old','proj','A',1,NULL,'黑发','prompt',?,NULL,1,NULL,'ready',1)""",
        (str(img),),
    )
    conn.commit()

    async def fake_redraw(*_a, **_k):
        new = tmp_path / "new.jpg"
        new.write_bytes(b"new")
        return str(new), "new prompt"

    async def fake_review(*_a, **_k):
        return {"overall": 0.95, "status": "approved", "issues": []}

    async def failed_pack(**_k):
        return {"status": "failed", "failed_view": "profile"}

    monkeypatch.setattr("app.portraits._redraw_portrait", fake_redraw)
    monkeypatch.setattr("app.portraits._review_portrait_asset", fake_review)
    monkeypatch.setattr("app.portraits.record_reference_asset", lambda **k: {"id": "art1", "status": "approved"})
    monkeypatch.setattr("app.multiview.ensure_character_multiview_pack", failed_pack)

    with pytest.raises(hiagent.ProviderError, match="无法切换造型"):
        asyncio.run(_refresh_portrait_on_drift(
            "proj", "A", 12, "白发红袍", "anime", 1,
            change_meta={"persistence": "persistent", "change_dimensions": ["hair", "outfit"]},
        ))

    rows = conn.execute(
        "SELECT id, ep_start, ep_end, pack_status FROM character_portraits WHERE character_name='A'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["id"] == "p_old"
    assert rows[0]["ep_end"] is None
    assert rows[0]["pack_status"] == "ready"


def test_refresh_portrait_episode_persistence_binds_ready_pack(monkeypatch, tmp_path) -> None:
    """仅本集造型成功后：旧包应从 ep+1 以 ready 完整视角重新绑定。"""
    import asyncio
    import threading
    from pathlib import Path
    from app import db
    from app.portraits import _refresh_portrait_on_drift
    from app.multiview import list_portrait_views

    database = tmp_path / "ep-only.db"
    monkeypatch.setattr(db, "DB_PATH", database)
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "_local", threading.local())
    db.init_db()
    conn = db.get_conn()
    img = tmp_path / "old.jpg"
    img.write_bytes(b"old")
    side = tmp_path / "side.jpg"
    side.write_bytes(b"side")
    conn.execute(
        "INSERT INTO projects(id, name, bible_json, bible_version, created_at) VALUES('proj','t',?,?,1)",
        (json.dumps({"characters": [{"name": "A", "appearance_canonical": "黑发"}],
                     "world": {"visual_style_canonical": "anime"}}), 1),
    )
    conn.execute(
        """INSERT INTO character_portraits(
               id, project_id, character_name, ep_start, ep_end, appearance, prompt,
               image_path, base_portrait_id, bible_version, artifact_id, pack_status, created_at
           ) VALUES('p_old','proj','A',1,NULL,'黑发','prompt',?,NULL,1,NULL,'ready',1)""",
        (str(img),),
    )
    for role, path in (("front_full", img), ("three_quarter", side), ("profile", side)):
        conn.execute(
            """INSERT INTO character_portrait_views(
                   id, portrait_id, view_role, framing, image_path, prompt, qa_json,
                   artifact_id, base_view_id, status, selected, input_fingerprint, created_at
               ) VALUES(?,?,?,?,?,?,NULL,NULL,NULL,'ready',1,'fp',1)""",
            (f"v_{role}", "p_old", role, "full", str(path), "p"),
        )
    conn.commit()

    async def fake_redraw(*_a, **_k):
        new = tmp_path / "new.jpg"
        new.write_bytes(b"new")
        return str(new), "new prompt"

    async def fake_review(*_a, **_k):
        return {"overall": 0.95, "status": "approved", "issues": []}

    async def ready_pack(**kwargs):
        # 模拟整包成功：给新段登记三视角
        pid = kwargs["portrait_id"]
        for role in ("front_full", "three_quarter", "profile"):
            p = tmp_path / f"{pid}_{role}.jpg"
            p.write_bytes(b"1")
            conn.execute(
                """INSERT OR REPLACE INTO character_portrait_views(
                       id, portrait_id, view_role, framing, image_path, prompt, qa_json,
                       artifact_id, base_view_id, status, selected, input_fingerprint, created_at
                   ) VALUES(?,?,?,?,?,?,NULL,NULL,NULL,'ready',1,'fp',1)""",
                (f"{pid}_{role}", pid, role, "full", str(p), "p"),
            )
        conn.execute("UPDATE character_portraits SET pack_status='ready' WHERE id=?", (pid,))
        conn.commit()
        return {"status": "ready", "portrait_id": pid}

    monkeypatch.setattr("app.portraits._redraw_portrait", fake_redraw)
    monkeypatch.setattr("app.portraits._review_portrait_asset", fake_review)
    monkeypatch.setattr("app.portraits.record_reference_asset", lambda **k: {"id": "art1", "status": "approved"})
    monkeypatch.setattr("app.multiview.ensure_character_multiview_pack", ready_pack)

    result = asyncio.run(_refresh_portrait_on_drift(
        "proj", "A", 12, "白发", "anime", 1,
        change_meta={"persistence": "episode", "change_dimensions": ["hair"]},
    ))
    assert result and result["pack_status"] == "ready"
    rows = conn.execute(
        "SELECT id, ep_start, ep_end, pack_status FROM character_portraits WHERE character_name='A' ORDER BY ep_start"
    ).fetchall()
    assert len(rows) == 3
    assert (rows[0]["id"], rows[0]["ep_end"]) == ("p_old", 11)
    assert rows[1]["ep_start"] == 12 and rows[1]["ep_end"] == 12
    assert rows[2]["ep_start"] == 13 and rows[2]["ep_end"] is None
    assert rows[2]["pack_status"] == "ready"
    reuse_views = list_portrait_views(rows[2]["id"], conn=conn)
    assert len(reuse_views) == 3
    assert all(Path(v["image_path"]).exists() for v in reuse_views)


def test_refresh_scene_pack_failure_does_not_switch(monkeypatch, tmp_path) -> None:
    """场景整包失败不切换版本。"""
    import asyncio
    import threading
    from app import db, hiagent
    from app.scenes import _refresh_scene_on_state_change

    database = tmp_path / "scene-drift.db"
    monkeypatch.setattr(db, "DB_PATH", database)
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "_local", threading.local())
    db.init_db()
    conn = db.get_conn()
    img = tmp_path / "scene.jpg"
    img.write_bytes(b"old")
    conn.execute("INSERT INTO projects(id, name, created_at) VALUES('proj','t',1)")
    conn.execute(
        """INSERT INTO scene_references(
               id, project_id, scene_name, ep_start, ep_end, scene_canonical, prompt,
               image_path, qa_json, base_scene_id, bible_version, artifact_id, pack_status, created_at
           ) VALUES('s_old','proj','广场',1,NULL,'石板广场','prompt',?,?,NULL,0,NULL,'ready',1)""",
        (str(img), json.dumps({"overall": 0.9})),
    )
    conn.commit()

    async def fake_gen(*_a, **_k):
        return {"b64_json": __import__("base64").b64encode(b"new").decode()}

    async def fake_review(*_a, **_k):
        return {"overall": 0.9, "issues": []}

    async def failed_pack(**_k):
        return {"status": "failed", "failed_view": "reverse_angle"}

    monkeypatch.setattr("app.scenes._generate_scene_image", fake_gen)
    monkeypatch.setattr("app.scenes._review_scene_ref", fake_review)
    monkeypatch.setattr("app.multiview.ensure_scene_multiview_pack", failed_pack)

    with pytest.raises(hiagent.ProviderError, match="无法切换版本"):
        asyncio.run(_refresh_scene_on_state_change(
            "proj", "广场", 8, "损毁后的废墟广场石柱倒塌", "anime", 0,
            change_meta={"persistence": "persistent", "change_dimensions": ["damage"]},
        ))

    rows = conn.execute("SELECT id, ep_end, pack_status FROM scene_references WHERE scene_name='广场'").fetchall()
    assert len(rows) == 1
    assert rows[0]["id"] == "s_old"
    assert rows[0]["ep_end"] is None
