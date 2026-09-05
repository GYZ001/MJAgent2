"""模型抄写的 shot_id 走样时，按分镜台发布的 shot_no 归位（2026-09-05 我欲封天第 20 集）。

模型把 ``shot_38e5d84cf03c`` 写成 ``shot_38e5d84cf0307``，整集计划 UNKNOWN_SHOT_ID +
SHOT_COVERAGE_INCOMPLETE，补齐运行直接 VIDEO_PLAN_INVALID 收口。shot_no 与 id 同源、本集内
唯一，是可以逐字比对的权威；两者都对不上才拒绝。
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from app import db
from app.video_plan import (
    EpisodeVideoGenerationPlan,
    ProviderVideoCapabilitySnapshot,
    ShotVideoGenerationPlan,
    VideoGenerationMode,
    VideoPlanValidationError,
    bind_plan_release_identity,
    current_storyboard_release_manifest,
    save_capability_snapshot,
    validate_episode_plan,
)


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    for statement in db.MIGRATIONS:
        try:
            conn.execute(statement)
        except sqlite3.OperationalError:
            pass
    conn.execute("INSERT INTO projects(id,name,created_at) VALUES('p','P',0)")
    conn.execute(
        """INSERT INTO episodes(id,project_id,episode_no,storyboard_artifact_id,target_video_model,created_at)
           VALUES('e','p',1,'storyboard_rev_1','provider',0)"""
    )
    for number in (1, 2):
        conn.execute(
            """INSERT INTO shots(id,shot_uid,episode_id,shot_no,duration_s,shot_size,camera_move,
                                 scene_setting,characters,action_desc,dialogues,transition,shot_contract_json)
               VALUES(?,?,?,?,5,'中景','固定','空间','[]','动作','[]','硬切',?)""",
            (f"shot_38e5d84cf03{number}", f"uid-{number}", "e", number,
             json.dumps({"shot_id": f"SH-{number}"}, ensure_ascii=False)),
        )
    return conn


def _snapshot(conn: sqlite3.Connection) -> ProviderVideoCapabilitySnapshot:
    snapshot = ProviderVideoCapabilitySnapshot(
        id="cap-1", provider="provider", model="model",
        supports_reference_image=True, supports_first_frame=True, supports_last_frame=True,
        supports_first_last_pair=True, supports_reference_video=True, probe_time=1, technical_success=True,
    )
    save_capability_snapshot(snapshot, conn=conn)
    return snapshot


def _item(shot_id: str, shot_no: int, index: int) -> ShotVideoGenerationPlan:
    return ShotVideoGenerationPlan(
        shot_plan_id=f"svp-{index}", episode_video_plan_id="evp-1",
        source_storyboard_revision_id="storyboard_rev_1", shot_id=shot_id, published_shot_id=shot_id,
        shot_no=shot_no, mode=VideoGenerationMode.REFERENCE_IMAGE_MODE, confidence=0.9,
        capability_snapshot_id="cap-1",
    )


def _plan(items: list[ShotVideoGenerationPlan]) -> EpisodeVideoGenerationPlan:
    return EpisodeVideoGenerationPlan(
        episode_video_plan_id="evp-1", episode_id="e", plan_revision=1,
        source_storyboard_revision_id="storyboard_rev_1", capability_snapshot_id="cap-1", shots=items,
    )


def test_mangled_shot_id_is_resolved_by_authoritative_shot_no() -> None:
    conn = _conn()
    snapshot = _snapshot(conn)
    rows = conn.execute("SELECT * FROM shots ORDER BY shot_no").fetchall()
    plan = _plan([_item("shot_38e5d84cf031", 1, 1), _item("shot_38e5d84cf0307", 2, 2)])
    manifest = current_storyboard_release_manifest("e", conn=conn)
    # 绑定发布身份时同样按 shot_no 归位，否则走样的那一镜拿不到契约指纹、被判 STALE。
    bind_plan_release_identity(plan, list(rows), manifest)
    validated = validate_episode_plan(plan, list(rows), snapshot, release_manifest=manifest)
    assert [(s.shot_id, s.shot_no) for s in validated.shots] == [
        ("shot_38e5d84cf031", 1), ("shot_38e5d84cf032", 2),
    ]


def test_unknown_id_with_unknown_shot_no_is_still_rejected() -> None:
    conn = _conn()
    snapshot = _snapshot(conn)
    rows = conn.execute("SELECT * FROM shots ORDER BY shot_no").fetchall()
    plan = _plan([_item("shot_38e5d84cf031", 1, 1), _item("shot_38e5d84cf0307", 99, 2)])
    manifest = current_storyboard_release_manifest("e", conn=conn)
    bind_plan_release_identity(plan, list(rows), manifest)
    with pytest.raises(VideoPlanValidationError) as excinfo:
        validate_episode_plan(plan, list(rows), snapshot, release_manifest=manifest)
    codes = {issue.get("code") for issue in excinfo.value.issues}
    assert "UNKNOWN_SHOT_ID" in codes
