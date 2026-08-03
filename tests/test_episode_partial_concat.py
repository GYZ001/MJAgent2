import sqlite3
import subprocess
from types import SimpleNamespace
from pathlib import Path

from app import api, artifacts, db, worker
from app.video_playback import normalize_playback_rate


def _database(shot_nos: tuple[int, ...] = (1, 2, 3)) -> sqlite3.Connection:
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
        "INSERT INTO episodes(id,project_id,episode_no,title,status,created_at) "
        "VALUES('e','p',1,'E','confirmed',0)"
    )
    for shot_no in shot_nos:
        conn.execute(
            "INSERT INTO shots(id,episode_id,shot_no,duration_s) VALUES(?,?,?,?)",
            (f"s{shot_no}", "e", shot_no, 5 + shot_no),
        )
    conn.commit()
    return conn


def _version(conn: sqlite3.Connection, *, shot_no: int, path: Path, adopted: bool) -> None:
    version_id = f"v{shot_no}"
    conn.execute(
        """INSERT INTO shot_versions(
               id,shot_id,version_no,prompt_text,idem_key,status,video_path,created_at
           ) VALUES(?,?,?,?,?,'succeeded',?,0)""",
        (version_id, f"s{shot_no}", 1, "prompt", f"key-{shot_no}", str(path)),
    )
    if adopted:
        conn.execute(
            "UPDATE shots SET adopted_version_id=? WHERE id=?",
            (version_id, f"s{shot_no}"),
        )


def test_partial_episode_is_ready_when_any_real_video_exists_but_ffmpeg_is_still_required(
    tmp_path, monkeypatch,
) -> None:
    conn = _database()
    project_root = tmp_path / "projects"
    shot_dir = project_root / "p" / "episodes" / "1" / "shots"
    shot_dir.mkdir(parents=True)
    adopted_path = shot_dir / "shot-1.mp4"
    unadopted_path = shot_dir / "shot-2.mp4"
    missing_path = shot_dir / "shot-3-missing.mp4"
    adopted_path.write_bytes(b"adopted")
    unadopted_path.write_bytes(b"unadopted")
    _version(conn, shot_no=1, path=adopted_path, adopted=True)
    _version(conn, shot_no=2, path=unadopted_path, adopted=False)
    _version(conn, shot_no=3, path=missing_path, adopted=True)
    conn.commit()

    monkeypatch.setattr(worker, "get_conn", lambda: conn)
    monkeypatch.setattr(artifacts, "get_conn", lambda: conn)
    monkeypatch.setattr(worker.config, "PROJECTS_DIR", project_root)
    monkeypatch.setattr(worker.shutil, "which", lambda _name: None)

    status = worker.episode_mix_status("e")

    assert status["ready"] is True
    assert status["all_ready"] is False
    assert status["shots_ready"] == 2
    assert status["shots_skipped"] == 1
    assert status["skipped_shot_nos"] == [3]
    assert [item["shot_no"] for item in status["shots"] if item["has_adopted"]] == [1]

    try:
        worker.concatenate_episode("e")
    except ValueError as exc:
        assert "未找到视频合成组件 ffmpeg" in str(exc)
        assert "本次未生成成片" in str(exc)
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("缺少 ffmpeg 时不得把首个片段冒充最终成片")


def test_concat_uses_completed_real_videos_while_other_shots_are_still_generating(
    tmp_path, monkeypatch,
) -> None:
    from app.evidence import media as media_evidence

    conn = _database((1, 2))
    project_root = tmp_path / "projects"
    completed_path = project_root / "p" / "episodes" / "1" / "shots" / "shot-1.mp4"
    completed_path.parent.mkdir(parents=True)
    completed_path.write_bytes(b"completed-real-video")
    _version(conn, shot_no=1, path=completed_path, adopted=True)
    conn.execute(
        """INSERT INTO shot_versions(
               id,shot_id,version_no,prompt_text,idem_key,status,created_at
           ) VALUES('v_running','s2',1,'prompt','running-key','running',0)"""
    )
    conn.execute(
        """INSERT INTO jobs(
               id,kind,shot_id,version_id,episode_id,project_id,status,created_at,updated_at
           ) VALUES('j_running','video','s2','v_running','e','p','waiting_provider',0,0)"""
    )
    conn.commit()
    monkeypatch.setattr(worker, "get_conn", lambda: conn)
    monkeypatch.setattr(artifacts, "get_conn", lambda: conn)
    monkeypatch.setattr(media_evidence, "get_conn", lambda: conn)
    monkeypatch.setattr(worker.config, "PROJECTS_DIR", project_root)
    monkeypatch.setattr(worker.shutil, "which", lambda _name: "/usr/bin/ffmpeg")

    def successful_run(command, **_kwargs):
        if command[0] == "ffprobe":
            return SimpleNamespace(stdout="5.0")
        Path(command[-1]).write_bytes(b"partial-final")
        return SimpleNamespace(stdout="", stderr=b"")

    monkeypatch.setattr(worker.subprocess, "run", successful_run)

    status = worker.episode_mix_status("e")

    assert status["ready"] is True
    assert status["shots_ready"] == 1
    assert status["generation_active"] is True
    assert status["active_shot_nos"] == [2]

    result = worker.concatenate_episode("e")

    assert result["shots"] == 1
    assert result["partial"] is True
    assert result["included_shot_nos"] == [1]
    assert result["skipped_shot_nos"] == [2]
    assert conn.execute("SELECT COUNT(*) FROM shot_versions").fetchone()[0] == 2


def test_episode_without_adoption_selects_existing_model_videos_before_mix(tmp_path, monkeypatch) -> None:
    from app.evidence import media as media_evidence

    conn = _database()
    project_root = tmp_path / "projects"
    for shot_no in (1, 2, 3):
        path = project_root / "p" / "episodes" / "1" / "shots" / f"shot-{shot_no}.mp4"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"model-video-{shot_no}".encode())
        _version(conn, shot_no=shot_no, path=path, adopted=False)
        conn.execute(
            "UPDATE shot_versions SET technical_validation_json=?,qa_json=? WHERE id=?",
            ('{"passed":true}', '{"overall":0.2,"failure_types":[]}', f"v{shot_no}"),
        )
    conn.commit()
    monkeypatch.setattr(worker, "get_conn", lambda: conn)
    monkeypatch.setattr(artifacts, "get_conn", lambda: conn)
    monkeypatch.setattr(media_evidence, "get_conn", lambda: conn)
    monkeypatch.setattr(worker.config, "PROJECTS_DIR", project_root)
    monkeypatch.setattr(worker.shutil, "which", lambda _name: "/usr/bin/ffmpeg")

    def successful_run(command, **_kwargs):
        if command[0] == "ffprobe":
            return SimpleNamespace(stdout="5.0")
        Path(command[-1]).write_bytes(b"generated-video")
        return SimpleNamespace(stdout="", stderr=b"")

    monkeypatch.setattr(worker.subprocess, "run", successful_run)

    status = worker.episode_mix_status("e")
    assert status["ready"] is True
    assert status["shots_ready"] == 3

    result = worker.concatenate_episode("e")

    assert result["shots"] == 3
    assert result["shots_skipped"] == 0
    assert result["fallback_shots_created"] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM shots WHERE episode_id='e' AND adopted_version_id IS NOT NULL"
    ).fetchone()[0] == 3


def test_concat_refuses_only_when_no_real_video_exists_and_never_creates_image_fallback(
    tmp_path, monkeypatch,
) -> None:
    conn = _database((1,))
    monkeypatch.setattr(worker, "get_conn", lambda: conn)
    monkeypatch.setattr(artifacts, "get_conn", lambda: conn)
    monkeypatch.setattr(worker.config, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setattr(worker.shutil, "which", lambda _name: "/usr/bin/ffmpeg")

    status = worker.episode_mix_status("e")
    assert status["ready"] is False

    try:
        worker.concatenate_episode("e")
    except ValueError as exc:
        assert "真实模型视频" in str(exc)
        assert "不会使用静态图片" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("缺少真实模型视频时不得创建图片兜底")

    assert conn.execute("SELECT COUNT(*) FROM shot_versions").fetchone()[0] == 0
    assert conn.execute("SELECT adopted_version_id FROM shots WHERE id='s1'").fetchone()[0] is None


def test_legacy_image_fallback_cannot_outrank_or_grade_over_real_video(
    tmp_path, monkeypatch,
) -> None:
    from app.evidence import media as media_evidence

    conn = _database((1,))
    project_root = tmp_path / "projects"
    real_path = project_root / "p" / "episodes" / "1" / "shots" / "real-v1.mp4"
    fallback_path = project_root / "p" / "episodes" / "1" / "shots" / "fallback-v2.mp4"
    real_path.parent.mkdir(parents=True)
    real_path.write_bytes(b"real-model-video")
    fallback_path.write_bytes(b"static-silent-placeholder")
    _version(conn, shot_no=1, path=real_path, adopted=False)
    conn.execute(
        "UPDATE shot_versions SET technical_validation_json=?,qa_json=? WHERE id='v1'",
        ('{"passed":true}', '{"overall":0.18,"failure_types":["action_missing"]}'),
    )
    conn.execute(
        """INSERT INTO shot_versions(
               id,shot_id,version_no,prompt_text,idem_key,status,video_path,image_inputs,
               technical_validation_json,qa_json,created_at
           ) VALUES('fallback-v2','s1',2,'fallback','fallback-key','succeeded',?,?,?, ?,0)""",
        (
            str(fallback_path),
            '{"delivery_fallback":true}',
            '{"passed":true}',
            '{"overall":1.0,"failure_types":[]}',
        ),
    )
    conn.execute("UPDATE shots SET adopted_version_id='fallback-v2' WHERE id='s1'")
    conn.commit()

    monkeypatch.setattr(worker, "get_conn", lambda: conn)
    monkeypatch.setattr(artifacts, "get_conn", lambda: conn)
    monkeypatch.setattr(media_evidence, "get_conn", lambda: conn)
    monkeypatch.setattr(worker.config, "PROJECTS_DIR", project_root)

    before = worker.episode_mix_status("e")
    assert before["shots_ready"] == 1
    assert before["shots"][0]["has_model_candidate"] is True
    assert media_evidence.grade_shot_video("s1")["version_id"] == "v1"

    selected = media_evidence.select_best_video_candidate("s1", force_best=True)

    assert selected is not None
    assert selected["version_id"] == "v1"
    assert conn.execute(
        "SELECT adopted_version_id FROM shots WHERE id='s1'"
    ).fetchone()[0] == "v1"


def test_database_startup_quarantines_legacy_static_fallback_without_deleting_it() -> None:
    conn = _database((1,))
    conn.execute(
        """INSERT INTO shot_versions(
               id,shot_id,version_no,prompt_text,idem_key,status,image_inputs,created_at
           ) VALUES('fallback-v1','s1',1,'fallback','fallback-key','succeeded',?,0)""",
        ('{"delivery_fallback":true}',),
    )
    conn.execute("UPDATE shots SET adopted_version_id='fallback-v1' WHERE id='s1'")
    conn.commit()

    changed = db._quarantine_static_delivery_fallbacks(conn)
    conn.commit()

    assert changed == 1
    assert conn.execute(
        "SELECT adopted_version_id FROM shots WHERE id='s1'"
    ).fetchone()[0] is None
    row = conn.execute(
        "SELECT status,error FROM shot_versions WHERE id='fallback-v1'"
    ).fetchone()
    assert row["status"] == "rejected_static_fallback"
    assert "不具备视频资格" in row["error"]
    assert conn.execute(
        "SELECT COUNT(*) FROM shot_versions WHERE id='fallback-v1'"
    ).fetchone()[0] == 1


def test_outdated_final_video_is_preserved_and_remains_visible(tmp_path, monkeypatch) -> None:
    conn = _database()
    project_root = tmp_path / "projects"
    final_path = project_root / "p" / "episodes" / "1" / "final" / "episode.mp4"
    final_path.parent.mkdir(parents=True)
    final_path.write_bytes(b"existing-final")

    monkeypatch.setattr(worker, "get_conn", lambda: conn)
    monkeypatch.setattr(artifacts, "get_conn", lambda: conn)
    monkeypatch.setattr(worker.config, "PROJECTS_DIR", project_root)
    monkeypatch.setattr(artifacts.config, "PROJECTS_DIR", project_root)

    assert artifacts.invalidate_episode_final("e") is True

    status = worker.episode_mix_status("e")
    assert final_path.read_bytes() == b"existing-final"
    assert final_path.with_suffix(".stale").is_file()
    assert status["final_video_url"].startswith(
        "/media/p/episodes/1/final/episode.mp4?v="
    )
    assert status["final_video_stale"] is True


def test_concat_timeout_preserves_previous_final_video(tmp_path, monkeypatch) -> None:
    conn = _database((1,))
    project_root = tmp_path / "projects"
    piece = project_root / "p" / "episodes" / "1" / "shots" / "shot-1.mp4"
    piece.parent.mkdir(parents=True)
    piece.write_bytes(b"new-piece")
    _version(conn, shot_no=1, path=piece, adopted=True)
    conn.commit()
    final_path = project_root / "p" / "episodes" / "1" / "final" / "episode.mp4"
    final_path.parent.mkdir(parents=True)
    final_path.write_bytes(b"previous-final")

    monkeypatch.setattr(worker, "get_conn", lambda: conn)
    monkeypatch.setattr(artifacts, "get_conn", lambda: conn)
    monkeypatch.setattr(worker.config, "PROJECTS_DIR", project_root)
    monkeypatch.setattr(worker.shutil, "which", lambda _name: "/usr/bin/ffmpeg")

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs.get("timeout", 1))

    monkeypatch.setattr(worker.subprocess, "run", timeout)

    try:
        worker.concatenate_episode("e")
    except ValueError as exc:
        assert "上一版成片仍保留" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("合成超时必须返回可重试失败")
    assert final_path.read_bytes() == b"previous-final"


def test_cancel_video_adoption_keeps_candidate_and_marks_shot_pending(tmp_path, monkeypatch) -> None:
    conn = _database()
    video_path = tmp_path / "candidate.mp4"
    video_path.write_bytes(b"video")
    _version(conn, shot_no=1, path=video_path, adopted=True)
    conn.commit()
    invalidated: list[str] = []
    audits: list[tuple] = []
    monkeypatch.setattr(api, "get_conn", lambda: conn)
    monkeypatch.setattr(
        api.worker,
        "invalidate_episode_final",
        lambda episode_id: invalidated.append(episode_id),
    )
    monkeypatch.setattr(api, "_review_write_audit", lambda *args, **kwargs: audits.append((args, kwargs)))

    result = api._cancel_shot_adoption_core("s1")

    assert result["previous_adopted_version_id"] == "v1"
    assert conn.execute(
        "SELECT adopted_version_id FROM shots WHERE id='s1'",
    ).fetchone()[0] is None
    assert conn.execute("SELECT COUNT(*) FROM shot_versions WHERE id='v1'").fetchone()[0] == 1
    assert video_path.exists()
    assert invalidated == ["e"]
    assert audits


def test_adopt_version_persists_playback_rate_and_invalidates_previous_mix(
    tmp_path, monkeypatch,
) -> None:
    conn = _database()
    video_path = tmp_path / "candidate.mp4"
    video_path.write_bytes(b"video")
    _version(conn, shot_no=1, path=video_path, adopted=True)
    conn.execute(
        "UPDATE shot_versions SET technical_validation_json=? WHERE id='v1'",
        ('{"passed": true}',),
    )
    conn.commit()
    invalidated: list[str] = []

    monkeypatch.setattr(api, "get_conn", lambda: conn)
    monkeypatch.setattr(api, "_review_assert_shot_positive", lambda *_args: None)
    monkeypatch.setattr(api.evidence_repository, "commit_artifact", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(api, "_review_write_audit", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(api.worker, "invalidate_episode_final", invalidated.append)
    from app.evidence import media as media_evidence
    monkeypatch.setattr(media_evidence, "record_video_candidate", lambda *_args, **_kwargs: {"id": "art-v1"})

    result = api._adopt_version_core("s1", {
        "version_id": "v1",
        "reason": "调整节奏后定稿",
        "playback_rate": 1.5,
    })

    assert result["playback_rate"] == 1.5
    assert conn.execute("SELECT playback_rate FROM shot_versions WHERE id='v1'").fetchone()[0] == 1.5
    assert invalidated == ["e"]


def test_mix_applies_each_adopted_versions_finalized_playback_rate(
    tmp_path, monkeypatch,
) -> None:
    conn = _database((1,))
    project_root = tmp_path / "projects"
    piece = project_root / "p" / "episodes" / "1" / "shots" / "shot-1.mp4"
    piece.parent.mkdir(parents=True)
    piece.write_bytes(b"source-video")
    _version(conn, shot_no=1, path=piece, adopted=True)
    conn.execute("UPDATE shot_versions SET playback_rate=2.0 WHERE id='v1'")
    conn.commit()
    final_path = project_root / "p" / "episodes" / "1" / "final" / "episode.mp4"
    final_path.parent.mkdir(parents=True)
    final_path.write_bytes(b"old-final")
    final_path.with_suffix(".stale").write_text("outdated\n", encoding="utf-8")
    commands: list[list[str]] = []

    monkeypatch.setattr(worker, "get_conn", lambda: conn)
    monkeypatch.setattr(artifacts, "get_conn", lambda: conn)
    monkeypatch.setattr(worker.config, "PROJECTS_DIR", project_root)
    monkeypatch.setattr(worker.shutil, "which", lambda _name: "/usr/bin/ffmpeg")

    def successful_run(command, **_kwargs):
        commands.append(command)
        if command[0] == "ffprobe":
            return SimpleNamespace(stdout="8.0")
        Path(command[-1]).write_bytes(b"generated-video")
        return SimpleNamespace(stdout="", stderr=b"")

    monkeypatch.setattr(worker.subprocess, "run", successful_run)

    status = worker.episode_mix_status("e")
    result = worker.concatenate_episode("e")
    status_after = worker.episode_mix_status("e")

    assert status["shots"][0]["playback_rate"] == 2.0
    assert status["shots"][0]["effective_duration_s"] == 3.0
    assert any("setpts=PTS/2.000000" in command for command in commands)
    assert any("atempo=2.000000" in command for command in commands)
    assert result["playback_rates"] == {"1": 2.0}
    assert result["total_duration_s"] == 4.0
    assert result["final_edit"]["ok"] is False
    assert result["final_edit"]["runtime_blocking"] is False
    assert status_after["final_edit_report"]["fallback"] == "draft_concat"
    assert final_path.read_bytes() == b"generated-video"
    assert not final_path.with_suffix(".stale").exists()
    assert result["video_url"].startswith(
        "/media/p/episodes/1/final/episode.mp4?v="
    )
    assert result["video_url"] != status["final_video_url"]


def test_playback_rate_contract_rejects_unsafe_values() -> None:
    assert normalize_playback_rate(None) == 1.0
    assert normalize_playback_rate("1.25") == 1.25
    for value in (0.49, 2.01, float("nan"), float("inf"), "fast"):
        try:
            normalize_playback_rate(value)
        except ValueError:
            pass
        else:  # pragma: no cover
            raise AssertionError(f"非法倍速未被拒绝：{value!r}")
