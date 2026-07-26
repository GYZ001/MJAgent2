import hashlib
import json
import sqlite3
from pathlib import Path

from app import db, delivery
from app.evidence import repository
from app.harness.types import Evaluation, EvidenceArtifact


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


def _approved_artifact(kind: str, scope_type: str, scope_id: str, *, file_path: str | None = None) -> str:
    artifact = repository.create_artifact(EvidenceArtifact(
        type=kind, scope_type=scope_type, scope_id=scope_id,
        content={"kind": kind}, file_path=file_path, status="validated", trust_level="T2",
    ))
    artifact = repository.commit_artifact(None, artifact["id"], [Evaluation(
        evaluator_type="deterministic", evaluator_name="test", evaluator_version="1",
        status="passed", hard_gate_passed=True, score=100,
    )])
    return artifact["id"]


def test_delivery_package_reaches_t5_and_feedback_preserves_snapshot(tmp_path, monkeypatch) -> None:
    conn = _conn()
    monkeypatch.setattr(repository, "get_conn", lambda: conn)
    monkeypatch.setattr(delivery, "get_conn", lambda: conn)
    monkeypatch.setattr(delivery.config, "PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(delivery, "validate_video_file", lambda path, expected_duration_s=5: {
        "passed": True,
        "issues": [],
        "evidence": {"path": path, "duration_s": expected_duration_s},
    })

    bible_id = _approved_artifact("character_bible", "project", "p")
    screenplay_id = _approved_artifact("episode_screenplay", "episode", "e")
    storyboard_id = _approved_artifact("storyboard", "episode", "e")
    video = tmp_path / "candidate.mp4"
    video.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"video-bytes")
    final_video = tmp_path / "p" / "episodes" / "1" / "final" / "episode.mp4"
    final_video.parent.mkdir(parents=True)
    final_video.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"final-video")
    video_artifact_id = _approved_artifact("shot_video", "shot", "s", file_path=str(video))

    bible = {"world": {"era": "架空", "genre": "玄幻", "visual_style_canonical": "国漫"}, "characters": []}
    conn.execute(
        "INSERT INTO projects(id,name,status,bible_json,bible_artifact_id,created_at) VALUES('p','P','planned',?,?,0)",
        (json.dumps(bible, ensure_ascii=False), bible_id),
    )
    conn.execute(
        "INSERT INTO episodes(id,project_id,episode_no,title,screenplay_json,screenplay_artifact_id,storyboard_artifact_id,status,created_at) "
        "VALUES('e','p',1,'E','{}',?,?,'done',0)", (screenplay_id, storyboard_id),
    )
    conn.execute(
        "INSERT INTO shots(id,episode_id,shot_no,duration_s,shot_size,camera_move,scene_setting,characters,action_desc,dialogues,transition,adopted_version_id,storyboard_artifact_id) "
        "VALUES('s','e',1,5,'中景','固定','夜，庭院','[]','角色抬头','[]','硬切','v',?)", (storyboard_id,),
    )
    technical = json.dumps({"passed": True, "issues": [], "evidence": {"duration_s": 5}}, ensure_ascii=False)
    conn.execute(
        "INSERT INTO shot_versions(id,shot_id,version_no,prompt_text,idem_key,status,video_path,qa_json,technical_validation_json,adoption_reason,artifact_id,created_at) "
        "VALUES('v','s',1,'prompt','key','succeeded',?,'{\"overall\":0.9}',?,'best candidate',?,0)",
        (str(video), technical, video_artifact_id),
    )
    conn.commit()

    readiness = delivery.delivery_readiness("e")
    assert readiness["ready"] is True and readiness["evidence_coverage"] == 1
    conn.execute(
        "UPDATE shot_versions SET qa_json=? WHERE id='v'",
        (json.dumps({"overall": 0.9, "failure_types": ["character_duplicate"]}),),
    )
    conn.commit()
    blocked = delivery.delivery_readiness("e")
    assert blocked["ready"] is False
    assert any(item["key"] == "fatal_video_quality" for item in blocked["blockers"])
    conn.execute(
        "UPDATE shot_versions SET qa_json=? WHERE id='v'",
        (json.dumps({"overall": 0.9}),),
    )
    conn.commit()
    draft = delivery.build_delivery_package(
        "e", package_id="delivery_crash_window", operation_started_at=42,
    )
    draft_manifest = Path(draft["package_path"], "manifest.json").read_bytes()
    draft_hash = repository.get_artifact(draft["artifact_id"])["content_hash"]
    # Simulate a hard exit after the immutable artifact commit but before the
    # delivery_packages pointer commit.  The stable operation must reconstruct
    # the pointer without changing evidence or duplicating its file evaluation.
    conn.execute("DELETE FROM delivery_packages WHERE id=?", (draft["package_id"],))
    conn.commit()
    recovered_draft = delivery.build_delivery_package(
        "e", package_id="delivery_crash_window", operation_started_at=42,
    )
    assert recovered_draft["artifact_id"] == draft["artifact_id"]
    assert repository.get_artifact(draft["artifact_id"])["content_hash"] == draft_hash
    assert len(repository.get_evaluations(draft["artifact_id"])) == 1
    package = delivery.approve_delivery(
        "e", decided_by="reviewer", decision="approve", reason="复验通过",
    )
    assert package["package_id"] != draft["package_id"]
    assert package["artifact_id"] != draft["artifact_id"]
    assert Path(draft["package_path"], "manifest.json").read_bytes() == draft_manifest
    assert repository.get_artifact(draft["artifact_id"])["status"] == "superseded"
    assert package["trust_level"] == "T5" and Path(package["archive_path"]).is_file()
    report_text = Path(package["package_path"], "quality-report.json").read_text(encoding="utf-8")
    assert str(tmp_path) not in report_text
    shot_entry = next(item for item in package["manifest"]["files"] if item["role"] == "shot_video")
    assert shot_entry["sha256"] == hashlib.sha256(video.read_bytes()).hexdigest()
    artifact_before = repository.get_artifact(package["artifact_id"])

    feedback = delivery.add_customer_feedback(
        "e", message="第二镜节奏可更紧", created_by="customer", rating=3, request_revision=True,
    )
    assert feedback["revision_run_id"]
    assert repository.get_artifact(package["artifact_id"])["content_hash"] == artifact_before["content_hash"]
    assert any(item["evaluator_name"] == "customer_feedback" for item in repository.get_evaluations(package["artifact_id"]))


def test_delivery_package_id_rejects_path_traversal() -> None:
    import pytest

    with pytest.raises(ValueError, match="非法的 package_id"):
        delivery.validate_package_id("../../etc/passwd")
    with pytest.raises(ValueError, match="非法的 package_id"):
        delivery.validate_package_id("delivery_../escape")
