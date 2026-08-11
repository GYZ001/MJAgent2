import hashlib
import json
import sqlite3
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import db, delivery, downstream_authority
from app.evidence import repository
from app.production import screenplay_authority


class _InjectedPromotionCrash(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _seed_delivery_operation(tmp_path: Path, monkeypatch) -> tuple[sqlite3.Connection, dict]:
    database = tmp_path / "delivery-promotion.db"
    monkeypatch.setattr(db, "DB_PATH", database)
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    monkeypatch.setattr(delivery.config, "PROJECTS_DIR", tmp_path / "projects")
    db.init_db(reconcile_interrupted=False)
    conn = db.get_conn()

    conn.execute(
        "INSERT INTO projects(id,name,status,bible_json,created_at) "
        "VALUES('p','P','planned','{\"world\":{}}',0)"
    )
    conn.execute(
        """INSERT INTO episodes(
               id,project_id,episode_no,title,source_chapters,screenplay_json,
               screenplay_artifact_id,storyboard_artifact_id,
               published_storyboard_artifact_id,
               storyboard_production_revision_id,
               storyboard_completion_certificate_id,status,created_at
           ) VALUES(
               'e','p',1,'E','[1]','{}','screenplay-current','storyboard-current',
               'storyboard-current','revision-current','certificate-current','done',0
           )"""
    )
    conn.execute(
        "INSERT INTO chapters(project_id,idx,title,content) "
        "VALUES('p',1,'C1','chapter-one')"
    )
    conn.execute(
        """INSERT INTO shots(
               id,episode_id,shot_no,duration_s,shot_size,camera_move,
               scene_setting,characters,action_desc,dialogues,transition,
               storyboard_artifact_id
           ) VALUES(
               's','e',1,5,'medium','static','courtyard','[]','look up','[]',
               'cut','storyboard-current'
           )"""
    )
    conn.commit()

    final_dir = tmp_path / "projects" / "p" / "episodes" / "1" / "final"
    final_dir.mkdir(parents=True)
    final_video = final_dir / "episode.mp4"
    final_video.write_bytes(b"\x00\x00\x00\x18ftypmp42-final-delivery")
    (final_dir / "episode.edit-report.json").write_text(
        json.dumps({"ok": True, "final_video_sha256": _sha256(final_video)}),
        encoding="utf-8",
    )
    shot_video = tmp_path / "adopted.mp4"
    shot_video.write_bytes(b"\x00\x00\x00\x18ftypmp42-adopted-shot")

    release = {
        "published_storyboard_artifact_id": "storyboard-current",
        "storyboard_production_revision_id": "revision-current",
        "storyboard_completion_certificate_id": "certificate-current",
        "release_qualification_hash": "qualification-current",
    }
    video_manifest = {
        "episode_id": "e",
        "items": [{
            "shot_id": "s",
            "shot_no": 1,
            "adopted_version_id": "version-current",
            "artifact_id": "video-current",
            "video_sha256": _sha256(shot_video),
        }],
        "manifest_hash": "video-manifest-current",
    }
    readiness = {
        "episode_id": "e",
        "project_id": "p",
        "episode_no": 1,
        "title": "E",
        "ready": True,
        "blockers": [],
        "checks": [{"key": "delivery", "passed": True, "message": "ready"}],
        "warnings": [],
        "evidence_coverage": 1.0,
        "source_artifacts": [
            {"id": "bible-current", "status": "validated"},
            {"id": "screenplay-current", "status": "validated"},
            {"id": "storyboard-current", "status": "validated"},
        ],
        "source_chapters": [{
            "chapter_id": 1,
            "project_id": "p",
            "chapter_idx": 1,
            "title": "C1",
            "content_sha256": hashlib.sha256(b"chapter-one").hexdigest(),
        }],
        "videos": [{
            "shot_id": "s",
            "shot_no": 1,
            "version_id": "version-current",
            "artifact_id": "video-current",
            "path": str(shot_video),
            "ready": True,
        }],
        "final_edit_report": {"ok": True},
        "storyboard_release_authority": release,
        "video_delivery_manifest": video_manifest,
    }
    monkeypatch.setattr(delivery, "delivery_readiness", lambda _episode_id: readiness)
    monkeypatch.setattr(
        downstream_authority,
        "verify_current_storyboard_release_authority",
        lambda _episode_id, conn=None: release,
    )
    monkeypatch.setattr(
        downstream_authority,
        "current_adopted_video_delivery_manifest",
        lambda _episode_id, conn=None: video_manifest,
    )
    monkeypatch.setattr(
        screenplay_authority,
        "resolve_downstream_screenplay",
        lambda _episode_id, conn=None: SimpleNamespace(
            screenplay=SimpleNamespace(model_dump=lambda mode="json": {"scenes": []})
        ),
    )
    return conn, readiness


def _inject_crash(
    crash_point: str,
    *,
    package_id: str,
    conn: sqlite3.Connection,
    monkeypatch,
) -> None:
    if crash_point in {"ready", "directory_promoted"}:
        original_rename = Path.rename

        def crashing_rename(source: Path, target: Path):
            target_path = Path(target)
            if crash_point == "ready" and target_path.name == package_id:
                raise _InjectedPromotionCrash("crash after ready")
            if (
                crash_point == "directory_promoted"
                and target_path.name == f"{package_id}.zip"
            ):
                raise _InjectedPromotionCrash("crash after directory promotion")
            return original_rename(source, target)

        monkeypatch.setattr(Path, "rename", crashing_rename)
        return

    if crash_point == "promoted":
        original_content_hash = repository.content_hash

        def crashing_content_hash(content, file_path=None):
            if isinstance(content, dict) and content.get("package_id") == package_id:
                raise _InjectedPromotionCrash("crash after archive promotion")
            return original_content_hash(content, file_path)

        monkeypatch.setattr(repository, "content_hash", crashing_content_hash)
        return

    if crash_point == "artifact_uncommitted":
        original_new_id = delivery.new_id

        def crashing_new_id(prefix: str) -> str:
            if prefix == "eval":
                raise _InjectedPromotionCrash("crash after uncommitted artifact insert")
            return original_new_id(prefix)

        monkeypatch.setattr(delivery, "new_id", crashing_new_id)
        return

    if crash_point == "pointer_uncommitted":
        conn.execute(
            """CREATE TRIGGER inject_delivery_pointer_crash
               BEFORE UPDATE OF delivery_artifact_id ON episodes
               BEGIN
                   SELECT RAISE(ABORT, 'crash before pointer commit');
               END"""
        )
        conn.commit()
        return

    raise AssertionError(f"unknown crash point: {crash_point}")


def _assert_package_is_single_source(
    conn: sqlite3.Connection,
    result: dict,
    *,
    package_id: str,
) -> None:
    package_dir = Path(result["package_path"])
    archive_path = Path(result["archive_path"])
    manifest_path = package_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    row = conn.execute(
        "SELECT * FROM delivery_packages WHERE id=?", (package_id,)
    ).fetchone()

    assert package_dir.is_dir()
    assert delivery._archive_matches_directory(archive_path, package_dir)
    assert json.loads(row["manifest_json"]) == manifest == result["manifest"]
    for item in manifest["files"]:
        source = package_dir / item["path"]
        assert source.stat().st_size == item["size_bytes"]
        assert _sha256(source) == item["sha256"]
    with zipfile.ZipFile(archive_path) as archive:
        for item in manifest["files"]:
            assert hashlib.sha256(archive.read(item["path"])).hexdigest() == item["sha256"]

    artifact_id = f"art_delivery_{package_id.removeprefix('delivery_')}"
    artifact = repository.get_artifact(artifact_id, conn=conn)
    assert artifact["content"] == {
        "package_id": package_id,
        "manifest": manifest,
        "quality_report": json.loads(row["quality_report_json"]),
    }
    assert Path(artifact["file_path"]).resolve() == manifest_path.resolve()
    assert repository.content_hash(
        artifact["content"], artifact["file_path"]
    ) == artifact["content_hash"]
    assert conn.execute(
        "SELECT COUNT(*) FROM delivery_packages WHERE id=?", (package_id,)
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM artifacts WHERE id=?", (artifact_id,)
    ).fetchone()[0] == 1
    assert conn.execute(
        """SELECT COUNT(*) FROM evaluations
             WHERE artifact_id=? AND evaluator_name='delivery_manifest_validator'""",
        (artifact_id,),
    ).fetchone()[0] == 1
    episode = conn.execute(
        "SELECT delivery_artifact_id,delivery_status FROM episodes WHERE id='e'"
    ).fetchone()
    assert dict(episode) == {
        "delivery_artifact_id": artifact_id,
        "delivery_status": "waiting_human",
    }


@pytest.mark.parametrize(
    ("crash_point", "expected_phase"),
    [
        ("ready", "ready"),
        ("directory_promoted", "directory_promoted"),
        ("promoted", "promoted"),
        ("artifact_uncommitted", "promoted"),
        ("pointer_uncommitted", "promoted"),
    ],
)
def test_delivery_promotion_crash_startup_takeover_recovers_exactly_once(
    tmp_path,
    monkeypatch,
    crash_point: str,
    expected_phase: str,
) -> None:
    conn, _readiness = _seed_delivery_operation(tmp_path, monkeypatch)
    package_id = f"delivery_crash_{crash_point}"
    request_fingerprint = f"fp-{crash_point}"
    old_owner, replay = delivery.claim_delivery_package_operation(
        package_id=package_id,
        episode_id="e",
        request_fingerprint=request_fingerprint,
        conn=conn,
    )
    assert old_owner and replay is None

    with monkeypatch.context() as fault:
        _inject_crash(
            crash_point,
            package_id=package_id,
            conn=conn,
            monkeypatch=fault,
        )
        with pytest.raises((_InjectedPromotionCrash, sqlite3.IntegrityError)):
            delivery.build_delivery_package(
                "e",
                package_id=package_id,
                operation_started_at=42,
                operation_request_fingerprint=request_fingerprint,
                operation_lease_owner=old_owner,
            )

    receipt = conn.execute(
        "SELECT * FROM delivery_operation_receipts WHERE package_id=?",
        (package_id,),
    ).fetchone()
    old_workspace = Path(receipt["workspace_path"])
    assert receipt["status"] == "running"
    assert receipt["promotion_phase"] == expected_phase
    assert conn.execute(
        "SELECT COUNT(*) FROM artifacts WHERE id=?",
        (f"art_delivery_{package_id.removeprefix('delivery_')}",),
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM delivery_packages WHERE id=?", (package_id,)
    ).fetchone()[0] == 0

    if crash_point == "pointer_uncommitted":
        conn.execute("DROP TRIGGER inject_delivery_pointer_crash")
        conn.commit()
    conn.close()
    db._local.conn = None

    # A process restart is the proof that fences the old live lease.  The new
    # owner must preserve the exact abandoned phase/path, build in a distinct
    # workspace, and may remove only the fenced remnants.
    db.init_db(reconcile_interrupted=True)
    recovered_conn = db.get_conn()
    new_owner, replay = delivery.claim_delivery_package_operation(
        package_id=package_id,
        episode_id="e",
        request_fingerprint=request_fingerprint,
        allow_interrupted_takeover=True,
        conn=recovered_conn,
    )
    assert new_owner and new_owner != old_owner and replay is None
    takeover = recovered_conn.execute(
        "SELECT * FROM delivery_operation_receipts WHERE package_id=?",
        (package_id,),
    ).fetchone()
    assert takeover["abandoned_workspace_path"] == str(old_workspace)
    assert takeover["abandoned_promotion_phase"] == expected_phase
    assert takeover["recovery_fenced_owner"] == new_owner

    result = delivery.build_delivery_package(
        "e",
        package_id=package_id,
        operation_started_at=42,
        operation_request_fingerprint=request_fingerprint,
        operation_lease_owner=new_owner,
    )
    delivery.finish_delivery_package_operation(
        package_id=package_id,
        request_fingerprint=request_fingerprint,
        lease_owner=new_owner,
        result=result,
        succeeded=True,
        conn=recovered_conn,
    )
    _assert_package_is_single_source(recovered_conn, result, package_id=package_id)

    final_dir = Path(result["package_path"])
    final_archive = Path(result["archive_path"])
    final_snapshot = {
        path.relative_to(final_dir).as_posix(): _sha256(path)
        for path in final_dir.rglob("*")
        if path.is_file()
    }
    final_archive_hash = _sha256(final_archive)
    assert not old_workspace.exists()
    assert not Path(str(old_workspace) + ".zip").exists()

    # A late callback from the previous owner is fenced before it can delete,
    # replace, or mix bytes into the recovered package.
    with pytest.raises(ValueError, match="lease"):
        delivery.build_delivery_package(
            "e",
            package_id=package_id,
            operation_started_at=42,
            operation_request_fingerprint=request_fingerprint,
            operation_lease_owner=old_owner,
        )
    assert final_snapshot == {
        path.relative_to(final_dir).as_posix(): _sha256(path)
        for path in final_dir.rglob("*")
        if path.is_file()
    }
    assert _sha256(final_archive) == final_archive_hash

    replay_owner, replay = delivery.claim_delivery_package_operation(
        package_id=package_id,
        episode_id="e",
        request_fingerprint=request_fingerprint,
        conn=recovered_conn,
    )
    assert replay_owner is None
    assert replay == result
    _assert_package_is_single_source(recovered_conn, replay, package_id=package_id)

    recovered_conn.close()
    db._local.conn = None
