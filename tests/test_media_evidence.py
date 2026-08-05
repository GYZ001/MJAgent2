import json
import sqlite3

from app.evidence import media


def test_video_file_gate_accepts_mp4_signature_but_marks_unverified_duration(tmp_path, monkeypatch) -> None:
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"x" * 64)
    monkeypatch.setattr(media.shutil, "which", lambda _: None)
    result = media.validate_video_file(str(path), expected_duration_s=5)
    assert result["passed"] is True
    assert result["evidence"]["container_signature"] == "mp4"
    assert [issue.code for issue in result["issues"]] == ["VIDEO_DURATION_UNVERIFIED"]


def test_video_file_gate_rejects_unknown_container(tmp_path, monkeypatch) -> None:
    path = tmp_path / "bad.mp4"
    path.write_bytes(b"not-an-mp4")
    monkeypatch.setattr(media.shutil, "which", lambda _: None)
    result = media.validate_video_file(str(path))
    assert result["passed"] is False
    assert "VIDEO_CONTAINER_INVALID" in {issue.code for issue in result["issues"]}


def test_candidate_selection_uses_first_technical_version_and_records_reason(monkeypatch) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
      CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT);
      CREATE TABLE shots(id TEXT PRIMARY KEY,episode_id TEXT,adopted_version_id TEXT);
      CREATE TABLE shot_versions(id TEXT PRIMARY KEY,shot_id TEXT,version_no INTEGER,status TEXT,
        technical_validation_json TEXT,qa_json TEXT,adoption_reason TEXT);
      INSERT INTO shots VALUES('s','e',NULL);
    """)
    technical = json.dumps({"passed": True})
    conn.execute("INSERT INTO shot_versions VALUES('v1','s',1,'succeeded',?,?,NULL)", (technical, json.dumps({"overall": .7})))
    conn.execute("INSERT INTO shot_versions VALUES('v2','s',2,'succeeded',?,?,NULL)", (technical, json.dumps({"overall": .9})))
    conn.execute("INSERT INTO shot_versions VALUES('v3','s',3,'succeeded',?,?,NULL)", (technical, json.dumps({"overall": 1, "qa_recovered": True})))
    conn.commit()
    monkeypatch.setattr(media, "get_conn", lambda: conn)
    import app.artifacts
    monkeypatch.setattr(app.artifacts, "invalidate_episode_final", lambda _: False)
    selected = media.select_best_video_candidate("s")
    assert selected and selected["version_id"] == "v1"
    assert "首个技术有效视频" in selected["reason"]
    assert conn.execute("SELECT adopted_version_id FROM shots WHERE id='s'").fetchone()[0] == "v1"
