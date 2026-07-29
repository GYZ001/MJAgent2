import sqlite3
import subprocess
from types import SimpleNamespace
from pathlib import Path

from app import api, artifacts, db, worker
from app.video_playback import normalize_playback_rate


def _database() -> sqlite3.Connection:
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
    for shot_no in (1, 2, 3):
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


def test_partial_episode_can_mix_one_adopted_video_and_skip_other_shots(
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
    assert status["shots_ready"] == 1
    assert status["shots_skipped"] == 2
    assert status["skipped_shot_nos"] == [2, 3]
    assert [item["shot_no"] for item in status["shots"] if item["has_adopted"]] == [1]

    result = worker.concatenate_episode("e")

    assert result["shots"] == 1
    assert result["shots_skipped"] == 2
    assert result["skipped_shot_nos"] == [2, 3]
    assert result["total_duration_s"] == 6
    assert result["video_url"].endswith("/shot-1.mp4")


def test_episode_without_adopted_video_still_cannot_mix(tmp_path, monkeypatch) -> None:
    conn = _database()
    monkeypatch.setattr(worker, "get_conn", lambda: conn)
    monkeypatch.setattr(artifacts, "get_conn", lambda: conn)
    monkeypatch.setattr(worker.config, "PROJECTS_DIR", tmp_path / "projects")

    status = worker.episode_mix_status("e")
    assert status["ready"] is False
    assert status["shots_ready"] == 0

    try:
        worker.concatenate_episode("e")
    except ValueError as exc:
        assert "没有任何已采用的视频片段" in str(exc)
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("没有采纳片段时不应进入合成")


def test_concat_timeout_preserves_previous_final_video(tmp_path, monkeypatch) -> None:
    conn = _database()
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


def test_cancel_adoption_keeps_candidate_and_marks_shot_to_skip(tmp_path, monkeypatch) -> None:
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


def test_cancel_storyboard_adoption_hides_shot_from_generation_and_cinema(
    tmp_path, monkeypatch,
) -> None:
    conn = _database()
    project_root = tmp_path / "projects"
    piece = project_root / "p" / "episodes" / "1" / "shots" / "shot-1.mp4"
    piece.parent.mkdir(parents=True)
    piece.write_bytes(b"video")
    _version(conn, shot_no=1, path=piece, adopted=True)
    conn.commit()
    invalidated: list[str] = []
    stopped: list[str] = []

    monkeypatch.setattr(api, "get_conn", lambda: conn)
    monkeypatch.setattr(worker, "get_conn", lambda: conn)
    monkeypatch.setattr(artifacts, "get_conn", lambda: conn)
    monkeypatch.setattr(worker.config, "PROJECTS_DIR", project_root)
    monkeypatch.setattr(api.worker, "invalidate_episode_final", invalidated.append)
    monkeypatch.setattr(
        api.worker,
        "stop_shot_video_tasks",
        lambda shot_id: stopped.append(shot_id) or {"shot_id": shot_id},
    )
    monkeypatch.setattr(api, "log_provider_call", lambda *_args, **_kwargs: None)

    result = api._set_storyboard_shot_adoption_core(
        "s1", {"adopted": False, "reason": "本镜不进入成片"},
    )

    assert result["storyboard_adopted"] is False
    assert result["candidate_media_preserved"] is True
    assert conn.execute("SELECT COUNT(*) FROM shot_versions WHERE id='v1'").fetchone()[0] == 1
    assert stopped == ["s1"]
    assert invalidated == ["e"]
    assert worker.episode_mix_status("e")["shots_total"] == 2
    try:
        worker.enqueue_shot("s1")
    except ValueError as exc:
        assert "取消采纳" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("未采纳分镜不得进入生成队列")


def test_new_storyboard_shots_are_adopted_by_default() -> None:
    conn = _database()
    values = conn.execute(
        "SELECT storyboard_adopted FROM shots ORDER BY shot_no",
    ).fetchall()
    assert [row[0] for row in values] == [1, 1, 1]


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
    conn = _database()
    project_root = tmp_path / "projects"
    piece = project_root / "p" / "episodes" / "1" / "shots" / "shot-1.mp4"
    piece.parent.mkdir(parents=True)
    piece.write_bytes(b"source-video")
    _version(conn, shot_no=1, path=piece, adopted=True)
    conn.execute("UPDATE shot_versions SET playback_rate=2.0 WHERE id='v1'")
    conn.commit()
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

    assert status["shots"][0]["playback_rate"] == 2.0
    assert status["shots"][0]["effective_duration_s"] == 3.0
    assert any("setpts=PTS/2.000000" in command for command in commands)
    assert any("atempo=2.000000" in command for command in commands)
    assert result["playback_rates"] == {"1": 2.0}
    assert result["total_duration_s"] == 4.0


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
