from __future__ import annotations

import threading
from typing import Any

import pytest

from app import db
from app.evidence import repository as evidence_repository
from app.harness.types import EvidenceArtifact
from app.production.publish import publish_storyboard
from app.production.revision import (
    ensure_production_revision,
    mark_baseline_generated,
)
from app.schemas import Shot, Storyboard, StoryboardOutline, StoryboardOutlineShot
from app.storyboard_authority import OUTLINE_AUTHORITY_VERSION


@pytest.fixture()
def publish_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "publish-storyboard.db")
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "_local", threading.local())
    db.init_db()
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO projects(id,name,status,created_at) VALUES('p1','P','planned',1)"
    )
    conn.execute(
        """INSERT INTO episodes(
               id,project_id,episode_no,title,target_duration_s,status,created_at
           ) VALUES('e1','p1',1,'E',15,'scripted',1)"""
    )
    for shot_no in range(1, 4):
        conn.execute(
            """INSERT INTO shots(
                   id,episode_id,script_id,shot_no,duration_s,shot_size,camera_move,
                   scene_setting,characters,action_desc,first_frame_desc,last_frame_desc,
                   source_excerpt,narration,dialogues,transition,continuity_from_prev,
                   shot_contract_json,continuity_mode,observed_state_out
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                f"s{shot_no}",
                "e1",
                "script-1",
                shot_no,
                5,
                "中景",
                "固定",
                "日，广场",
                "[]",
                f"旧投影第{shot_no}镜",
                "",
                "",
                "",
                None,
                "[]",
                "硬切",
                0,
                "{}",
                "",
                "",
            ),
        )
    conn.commit()
    yield conn
    conn.close()


def _board() -> Storyboard:
    return Storyboard(
        episode_no=1,
        shots=[
            Shot(
                shot_no=shot_no,
                duration_s=10,
                shot_size="中景",
                camera_move="固定",
                scene_setting="日，广场",
                characters=["少年"],
                action_desc=f"少年完成第{shot_no}步动作。",
                first_frame_desc="少年站在石碑前。",
                last_frame_desc="少年确认石碑上的结果。",
                source_excerpt="少年走到石碑前，抬头查看结果。",
                prompt_contract_version="renderability_v1",
                is_final=shot_no == 3,
            )
            for shot_no in range(1, 4)
        ],
    )


def _outline() -> StoryboardOutline:
    return StoryboardOutline(
        episode_no=1,
        shots=[
            StoryboardOutlineShot(
                shot_no=shot_no,
                duration_s=10,
                beat=f"第{shot_no}步",
            )
            for shot_no in range(1, 4)
        ],
    )


def _publish_case() -> dict[str, Any]:
    board = _board()
    artifact = evidence_repository.create_artifact(
        EvidenceArtifact(
            type="storyboard",
            scope_type="episode",
            scope_id="e1",
            status="validated",
            trust_level="T2",
            content=board.model_dump(mode="json"),
            contract_version="storyboard-test.v1",
        )
    )
    revision = ensure_production_revision(
        episode_id="e1",
        kind="storyboard",
        input_fingerprint=artifact["content_hash"],
        contract_version="storyboard-test.v1",
        qa_profile_version="storyboard-test-qa.v1",
        resume=False,
    )
    revision = mark_baseline_generated(
        revision.id,
        baseline_artifact_id=artifact["id"],
        working_artifact_id=artifact["id"],
    )
    return {
        "episode_id": "e1",
        "revision_id": revision.id,
        "artifact_id": artifact["id"],
        "artifact_hash": artifact["content_hash"],
        "evaluation_ids": [],
        "shots_payload": board.model_dump(mode="json")["shots"],
        "outline_json": _outline().model_dump_json(),
        "input_fingerprint": revision.input_fingerprint,
        "contract_version": revision.contract_version,
        "qa_profile_version": revision.qa_profile_version,
    }


def _release_snapshot(conn) -> dict[str, Any]:
    episode = conn.execute(
        """SELECT target_duration_s,planning_target_duration_s,
                  planning_duration_source,target_duration_authority,
                  storyboard_outline_json,storyboard_outline_revision,
                  storyboard_outline_fingerprint,storyboard_outline_artifact_id,
                  storyboard_artifact_id,published_storyboard_artifact_id,
                  working_storyboard_artifact_id,
                  storyboard_production_revision_id,
                  storyboard_completion_certificate_id,status,narrative_status
             FROM episodes WHERE id='e1'"""
    ).fetchone()
    return {
        "episode": dict(episode),
        "storyboard_artifacts": [
            dict(row)
            for row in conn.execute(
                """SELECT id,status,trust_level,approved_at
                     FROM artifacts WHERE type='storyboard' ORDER BY id"""
            ).fetchall()
        ],
        "outline_artifacts": conn.execute(
            "SELECT COUNT(*) AS count FROM artifacts WHERE type='storyboard_outline'"
        ).fetchone()["count"],
        "certificate_rows": conn.execute(
            "SELECT COUNT(*) AS count FROM completion_certificates"
        ).fetchone()["count"],
        "certificate_artifacts": conn.execute(
            "SELECT COUNT(*) AS count FROM artifacts WHERE type='completion_certificate'"
        ).fetchone()["count"],
    }


def test_publish_storyboard_revision_mismatch_rolls_back_legacy_authority(
    publish_db,
) -> None:
    case = _publish_case()
    publish_db.execute(
        "UPDATE production_revisions SET status='superseded' WHERE id=?",
        (case["revision_id"],),
    )
    publish_db.commit()
    before = _release_snapshot(publish_db)

    with pytest.raises(ValueError, match="production revision 已失效"):
        publish_storyboard(**case)

    assert _release_snapshot(publish_db) == before


def test_publish_storyboard_certificate_failure_rolls_back_legacy_authority(
    publish_db,
) -> None:
    case = _publish_case()
    before = _release_snapshot(publish_db)

    with pytest.raises(ValueError, match="artifact_hash 与存储内容不一致"):
        publish_storyboard(**{**case, "artifact_hash": "mismatched-hash"})

    assert _release_snapshot(publish_db) == before
    revision = publish_db.execute(
        "SELECT status,published_artifact_id FROM production_revisions WHERE id=?",
        (case["revision_id"],),
    ).fetchone()
    assert dict(revision) == {"status": "active", "published_artifact_id": None}


def test_publish_storyboard_commits_complete_legacy_release(publish_db) -> None:
    case = _publish_case()

    result = publish_storyboard(**case)

    snapshot = _release_snapshot(publish_db)
    episode = snapshot["episode"]
    assert result == {
        "episode_id": "e1",
        "artifact_id": case["artifact_id"],
        "certificate_id": episode["storyboard_completion_certificate_id"],
        "shot_count": 3,
        "status": "scripted",
    }
    assert episode["target_duration_s"] == 30
    assert episode["planning_target_duration_s"] == 15
    assert episode["target_duration_authority"] == OUTLINE_AUTHORITY_VERSION
    assert episode["storyboard_outline_revision"] == 1
    assert episode["storyboard_outline_artifact_id"]
    assert episode["storyboard_artifact_id"] == case["artifact_id"]
    assert episode["published_storyboard_artifact_id"] == case["artifact_id"]
    assert episode["working_storyboard_artifact_id"] == case["artifact_id"]
    assert episode["storyboard_production_revision_id"] == case["revision_id"]
    assert episode["status"] == "scripted"
    assert episode["narrative_status"] == "legacy_unvalidated"
    storyboard_artifact = snapshot["storyboard_artifacts"][0]
    assert {
        key: storyboard_artifact[key]
        for key in ("id", "status", "trust_level")
    } == {
        "id": case["artifact_id"],
        "status": "approved",
        "trust_level": "T2",
    }
    assert storyboard_artifact["approved_at"] is not None
    assert snapshot["outline_artifacts"] == 1
    assert snapshot["certificate_rows"] == 1
    assert snapshot["certificate_artifacts"] == 1

    revision = publish_db.execute(
        "SELECT status,working_artifact_id,published_artifact_id "
        "FROM production_revisions WHERE id=?",
        (case["revision_id"],),
    ).fetchone()
    assert dict(revision) == {
        "status": "published",
        "working_artifact_id": case["artifact_id"],
        "published_artifact_id": case["artifact_id"],
    }
    certificate = publish_db.execute(
        "SELECT consumed_at FROM completion_certificates WHERE id=?",
        (result["certificate_id"],),
    ).fetchone()
    assert certificate is not None and certificate["consumed_at"] is not None


def test_downstream_storyboard_authority_rejects_wrong_artifact_type(
    publish_db,
) -> None:
    from app.downstream_authority import verify_current_storyboard_release_authority

    case = _publish_case()
    publish_storyboard(**case)
    publish_db.execute("UPDATE episodes SET status='confirmed' WHERE id='e1'")
    publish_db.commit()

    authority = verify_current_storyboard_release_authority("e1", conn=publish_db)
    assert authority["published_storyboard_artifact_id"] == case["artifact_id"]

    publish_db.execute(
        "UPDATE artifacts SET type='delivery_package' WHERE id=?",
        (case["artifact_id"],),
    )
    publish_db.commit()
    with pytest.raises(ValueError, match="不是本集已批准发布权威"):
        verify_current_storyboard_release_authority("e1", conn=publish_db)


@pytest.mark.parametrize(
    "mutation",
    [
        "UPDATE episodes SET status='scripted' WHERE id='e1'",
        "UPDATE episodes SET storyboard_artifact_id='wrong' WHERE id='e1'",
        "UPDATE production_revisions SET status='active' WHERE episode_id='e1' AND kind='storyboard'",
        "UPDATE production_revisions SET working_artifact_id='wrong' WHERE episode_id='e1' AND kind='storyboard'",
        "UPDATE production_revisions SET input_fingerprint='wrong' WHERE episode_id='e1' AND kind='storyboard'",
        "UPDATE production_revisions SET contract_version='wrong' WHERE episode_id='e1' AND kind='storyboard'",
        "UPDATE production_revisions SET qa_profile_version='wrong' WHERE episode_id='e1' AND kind='storyboard'",
        "UPDATE artifacts SET status='validated' WHERE type='storyboard' AND scope_id='e1'",
        "UPDATE artifacts SET content_json='{}' WHERE type='storyboard' AND scope_id='e1'",
        "UPDATE completion_certificates SET consumed_at=NULL WHERE kind='storyboard' AND scope_id='e1'",
        "UPDATE completion_certificates SET artifact_hash='wrong' WHERE kind='storyboard' AND scope_id='e1'",
        "UPDATE completion_certificates SET input_fingerprint='wrong' WHERE kind='storyboard' AND scope_id='e1'",
        "UPDATE completion_certificates SET contract_version='wrong' WHERE kind='storyboard' AND scope_id='e1'",
        "UPDATE completion_certificates SET qa_profile_version='wrong' WHERE kind='storyboard' AND scope_id='e1'",
    ],
)
def test_downstream_storyboard_authority_drift_matrix_fails_closed(
    publish_db, mutation: str,
) -> None:
    from app.downstream_authority import verify_current_storyboard_release_authority

    case = _publish_case()
    publish_storyboard(**case)
    publish_db.execute("UPDATE episodes SET status='confirmed' WHERE id='e1'")
    publish_db.commit()
    assert verify_current_storyboard_release_authority("e1", conn=publish_db)

    publish_db.execute(mutation)
    publish_db.commit()
    with pytest.raises(ValueError):
        verify_current_storyboard_release_authority("e1", conn=publish_db)


def test_publish_storyboard_exact_retry_returns_existing_release(publish_db) -> None:
    case = _publish_case()
    first = publish_storyboard(**case)
    before_retry = _release_snapshot(publish_db)

    with pytest.raises(ValueError, match="artifact_hash 不匹配"):
        publish_storyboard(**{**case, "artifact_hash": "mismatched-retry-hash"})
    assert _release_snapshot(publish_db) == before_retry

    retried = publish_storyboard(**case)

    assert retried == first
    assert _release_snapshot(publish_db) == before_retry
