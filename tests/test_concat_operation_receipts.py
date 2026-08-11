from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest

from app import db, worker
from app.capabilities import inputs as I
from app.capabilities.handlers import delivery as delivery_handler


def _memory_database() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    conn.execute("INSERT INTO projects(id,name,created_at) VALUES('p','P',0)")
    conn.execute(
        """INSERT INTO episodes(id,project_id,episode_no,title,status,created_at)
           VALUES('e','p',1,'E','confirmed',0)"""
    )
    conn.commit()
    return conn


def test_concat_publish_receipt_replays_after_handler_crash_without_rewrite(
    tmp_path, monkeypatch,
) -> None:
    from app import downstream_authority

    conn = _memory_database()
    projects = tmp_path / "projects"
    final_path = projects / "p" / "episodes" / "1" / "final" / "episode.mp4"
    final_path.parent.mkdir(parents=True)
    candidate = tmp_path / "candidate.mp4"
    candidate.write_bytes(b"durable-final-video")
    release_authority = {
        "published_storyboard_artifact_id": "storyboard-1",
        "release_qualification_hash": "release-hash-1",
    }
    manifest = {"manifest_hash": "video-manifest-1", "items": []}
    current = {"release": release_authority, "manifest": manifest}
    monkeypatch.setattr(worker, "get_conn", lambda: conn)
    monkeypatch.setattr(worker.config, "PROJECTS_DIR", projects)
    monkeypatch.setattr(
        downstream_authority,
        "verify_current_storyboard_release_authority",
        lambda episode_id, conn=None: current["release"],
    )
    monkeypatch.setattr(
        downstream_authority,
        "current_adopted_video_delivery_manifest",
        lambda episode_id, conn=None: current["manifest"],
    )

    calls = 0
    exact_result: dict = {}

    def publish_then_lose_response(episode_id: str, **operation) -> dict:
        nonlocal calls, exact_result
        calls += 1
        report = {
            "mode": "draft_concat",
            "video_delivery_manifest_hash": manifest["manifest_hash"],
        }
        result = {
            "total_duration_s": 12.5,
            "storyboard_release_authority": release_authority,
            "video_delivery_manifest": manifest,
            "final_edit": report,
        }
        worker._publish_concat_output(
            conn,
            episode_id=episode_id,
            candidate_path=candidate,
            final_path=final_path,
            report=report,
            result=result,
            release_authority=release_authority,
            video_delivery_manifest=manifest,
            operation_idempotency_key=operation["operation_idempotency_key"],
            operation_request_fingerprint=operation["operation_request_fingerprint"],
            operation_claim_token=operation["operation_claim_token"],
        )
        exact_result = json.loads(json.dumps(result))
        # Models the process dying after the domain publish commit and before
        # CommandBus can store its generic result.
        raise RuntimeError("response lost after publish")

    monkeypatch.setattr(worker, "concatenate_episode", publish_then_lose_response)
    args = I.EpisodeScopedInput(episode_id="e", idempotency_key="concat-once")
    with pytest.raises(RuntimeError, match="response lost"):
        asyncio.run(delivery_handler.concatenate(args))

    first_video_stat = final_path.stat()
    first_report_stat = worker._edit_report_path(final_path).stat()
    row = conn.execute(
        "SELECT * FROM concat_operation_receipts WHERE operation_key=?",
        ("delivery.concatenate:concat-once",),
    ).fetchone()
    assert row["status"] == "succeeded"
    assert row["final_sha256"] == exact_result["final_video_sha256"]
    assert row["report_sha256"] == exact_result["edit_report_sha256"]

    # A completed receipt is immutable historical evidence. Even if authority
    # moves before the HTTP retry, return that exact success rather than render
    # new sources under the old key.
    current["release"] = {"release": "new"}
    current["manifest"] = {"manifest_hash": "new", "items": []}
    replay = asyncio.run(delivery_handler.concatenate(args))
    assert replay.status.value == "succeeded"
    assert replay.data == exact_result
    assert calls == 1
    assert final_path.stat().st_mtime_ns == first_video_stat.st_mtime_ns
    assert worker._edit_report_path(final_path).stat().st_mtime_ns == first_report_stat.st_mtime_ns

    mismatch = asyncio.run(delivery_handler.concatenate(args.model_copy(update={"reason": "different"})))
    assert mismatch.status.value == "failed"
    assert mismatch.error_code == "idempotency_request_mismatch"
    assert calls == 1


def test_concat_operation_live_owner_is_fenced(monkeypatch) -> None:
    conn = _memory_database()
    monkeypatch.setattr(worker, "get_conn", lambda: conn)
    owner, replay = worker.claim_concat_operation(
        idempotency_key="live-concat",
        request_fingerprint="fp-1",
        episode_id="e",
        release_authority={"release": "one"},
        video_delivery_manifest={"manifest_hash": "one"},
    )
    assert owner and replay is None
    with pytest.raises(worker.ConcatOperationInProgress):
        worker.claim_concat_operation(
            idempotency_key="live-concat",
            request_fingerprint="fp-1",
            episode_id="e",
            release_authority={"release": "one"},
            video_delivery_manifest={"manifest_hash": "one"},
        )
    with pytest.raises(worker.ConcatOperationConflict):
        worker.claim_concat_operation(
            idempotency_key="live-concat",
            request_fingerprint="fp-other",
            episode_id="e",
            release_authority={"release": "one"},
            video_delivery_manifest={"manifest_hash": "one"},
        )


def test_concat_claim_freezes_authority_and_manifest_across_restart(monkeypatch) -> None:
    conn = _memory_database()
    monkeypatch.setattr(worker, "get_conn", lambda: conn)
    owner, replay = worker.claim_concat_operation(
        idempotency_key="frozen-concat",
        request_fingerprint="same-request",
        episode_id="e",
        release_authority={"release": "old"},
        video_delivery_manifest={"manifest_hash": "old"},
    )
    assert owner and replay is None
    worker.release_concat_operation(
        idempotency_key="frozen-concat",
        request_fingerprint="same-request",
        claim_token=owner,
    )

    with pytest.raises(worker.ConcatOperationConflict, match="冻结"):
        worker.claim_concat_operation(
            idempotency_key="frozen-concat",
            request_fingerprint="same-request",
            episode_id="e",
            release_authority={"release": "new"},
            video_delivery_manifest={"manifest_hash": "new"},
        )
    row = conn.execute(
        "SELECT release_authority_json,video_manifest_json FROM concat_operation_receipts"
    ).fetchone()
    assert json.loads(row["release_authority_json"]) == {"release": "old"}
    assert json.loads(row["video_manifest_json"]) == {"manifest_hash": "old"}


@pytest.mark.parametrize(
    ("crash_phase", "durable_phase"),
    [
        ("after_final_copy", "staged"),
        ("after_report_write", "final_promoted"),
        ("before_finalize_commit", "report_promoted"),
    ],
)
def test_concat_promotion_crash_resumes_exact_stage_without_rendering_again(
    tmp_path, monkeypatch, crash_phase: str, durable_phase: str,
) -> None:
    from app import downstream_authority

    conn = _memory_database()
    projects = tmp_path / "projects"
    final_path = projects / "p" / "episodes" / "1" / "final" / "episode.mp4"
    final_path.parent.mkdir(parents=True)
    candidate = tmp_path / "candidate.mp4"
    candidate.write_bytes(b"one-render-only")
    release = {"release": "frozen"}
    manifest = {"manifest_hash": "frozen", "items": []}
    monkeypatch.setattr(worker, "get_conn", lambda: conn)
    monkeypatch.setattr(worker.config, "PROJECTS_DIR", projects)
    monkeypatch.setattr(
        downstream_authority,
        "verify_current_storyboard_release_authority",
        lambda episode_id, conn=None: release,
    )
    monkeypatch.setattr(
        downstream_authority,
        "current_adopted_video_delivery_manifest",
        lambda episode_id, conn=None: manifest,
    )
    renders = 0

    def render_once(episode_id: str, **operation) -> dict:
        nonlocal renders
        renders += 1
        report = {"mode": "draft_concat"}
        result = {"total_duration_s": 8.0, "final_edit": report}
        return worker._publish_concat_output(
            conn,
            episode_id=episode_id,
            candidate_path=candidate,
            final_path=final_path,
            report=report,
            result=result,
            release_authority=release,
            video_delivery_manifest=manifest,
            operation_idempotency_key=operation["operation_idempotency_key"],
            operation_request_fingerprint=operation["operation_request_fingerprint"],
            operation_claim_token=operation["operation_claim_token"],
        )

    def crash_at(phase: str) -> None:
        if phase == crash_phase:
            raise RuntimeError(f"crash:{phase}")

    monkeypatch.setattr(worker, "concatenate_episode", render_once)
    monkeypatch.setattr(worker, "_concat_promotion_checkpoint", crash_at)
    args = I.EpisodeScopedInput(
        episode_id="e", idempotency_key=f"crash-{crash_phase}",
    )
    with pytest.raises(RuntimeError, match=f"crash:{crash_phase}"):
        asyncio.run(delivery_handler.concatenate(args))
    row = conn.execute(
        "SELECT * FROM concat_operation_receipts WHERE operation_key=?",
        (f"delivery.concatenate:crash-{crash_phase}",),
    ).fetchone()
    assert row["promotion_phase"] == durable_phase
    expected_result = json.loads(row["result_json"])
    expected_video_hash = row["final_sha256"]
    expected_report_hash = row["report_sha256"]

    conn.execute(
        "UPDATE concat_operation_receipts SET lease_expires_at=0 WHERE operation_key=?",
        (f"delivery.concatenate:crash-{crash_phase}",),
    )
    conn.commit()
    monkeypatch.setattr(worker, "_concat_promotion_checkpoint", lambda _phase: None)
    replay = asyncio.run(delivery_handler.concatenate(args))

    assert replay.status.value == "succeeded"
    assert replay.data == expected_result
    assert renders == 1
    assert worker._media_sha256(final_path) == expected_video_hash
    assert worker._media_sha256(worker._edit_report_path(final_path)) == expected_report_hash
    assert conn.execute(
        "SELECT status FROM concat_operation_receipts WHERE operation_key=?",
        (f"delivery.concatenate:crash-{crash_phase}",),
    ).fetchone()[0] == "succeeded"


def test_concat_startup_recovery_fences_only_when_explicit(
    tmp_path, monkeypatch,
) -> None:
    database = tmp_path / "concat-restart.db"
    monkeypatch.setattr(db, "DB_PATH", database)
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db(reconcile_interrupted=False)
    conn = db.get_conn()
    conn.execute("INSERT INTO projects(id,name,created_at) VALUES('p','P',0)")
    conn.execute(
        """INSERT INTO episodes(id,project_id,episode_no,title,status,created_at)
           VALUES('e','p',1,'E','confirmed',0)"""
    )
    conn.commit()
    owner, replay = worker.claim_concat_operation(
        idempotency_key="restart-concat",
        request_fingerprint="restart-fp",
        episode_id="e",
        release_authority={"release": "one"},
        video_delivery_manifest={"manifest_hash": "one"},
    )
    assert owner and replay is None
    lease_before = conn.execute(
        "SELECT lease_expires_at FROM concat_operation_receipts"
    ).fetchone()[0]

    db.init_db(reconcile_interrupted=False)
    assert conn.execute(
        "SELECT lease_expires_at FROM concat_operation_receipts"
    ).fetchone()[0] == lease_before
    with pytest.raises(worker.ConcatOperationInProgress):
        worker.claim_concat_operation(
            idempotency_key="restart-concat",
            request_fingerprint="restart-fp",
            episode_id="e",
            release_authority={"release": "one"},
            video_delivery_manifest={"manifest_hash": "one"},
        )

    db.init_db(reconcile_interrupted=True)
    assert conn.execute(
        "SELECT lease_expires_at FROM concat_operation_receipts"
    ).fetchone()[0] == 0
    replacement, replay = worker.claim_concat_operation(
        idempotency_key="restart-concat",
        request_fingerprint="restart-fp",
        episode_id="e",
        release_authority={"release": "one"},
        video_delivery_manifest={"manifest_hash": "one"},
    )
    assert replacement and replacement != owner and replay is None
