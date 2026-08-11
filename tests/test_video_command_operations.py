from __future__ import annotations

import sqlite3

import pytest

from app import video_command_operations as operations


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return conn


def test_paid_shot_receipt_recovers_exact_domain_binding_after_restart(monkeypatch) -> None:
    conn = _conn()
    monkeypatch.setattr(operations, "get_conn", lambda: conn)

    owner, recovered = operations.claim_video_command_operation(
        command="video.generate_shot",
        idempotency_key="stable-shot-op",
        request_fingerprint="fp-a",
        scope_type="shot",
        scope_id="shot-1",
    )
    assert owner and recovered is None
    exact_result = {
        "reused": False,
        "plan_id": "plan-a",
        "version_id": "version-a",
        "job_id": "job-a",
        "paused_budget": True,
        "task_accepted": True,
    }
    operations.bind_video_command_operation(
        command="video.generate_shot",
        idempotency_key="stable-shot-op",
        request_fingerprint="fp-a",
        claim_token=owner,
        binding={
            "plan_id": "plan-a",
            "version_id": "version-a",
            "job_id": "job-a",
            "provider_operation_id": "provider-a",
            "result": exact_result,
        },
        conn=conn,
    )
    conn.commit()

    # Startup fences the dead process owner. Recovery returns the immutable
    # binding even if the job later became paused/failed/stale.
    conn.execute(
        "UPDATE video_command_operation_receipts SET lease_expires_at=0"
    )
    conn.commit()
    retry_owner, retry_result = operations.claim_video_command_operation(
        command="video.generate_shot",
        idempotency_key="stable-shot-op",
        request_fingerprint="fp-a",
        scope_type="shot",
        scope_id="shot-1",
    )
    assert retry_owner is None
    assert retry_result == exact_result
    row = conn.execute(
        "SELECT status,binding_json FROM video_command_operation_receipts"
    ).fetchone()
    assert row["status"] == "succeeded"

    with pytest.raises(operations.VideoCommandOperationConflict):
        operations.claim_video_command_operation(
            command="video.generate_shot",
            idempotency_key="stable-shot-op",
            request_fingerprint="fp-b",
            scope_type="shot",
            scope_id="shot-1",
        )


def test_paid_operation_live_owner_cannot_be_reexecuted(monkeypatch) -> None:
    conn = _conn()
    monkeypatch.setattr(operations, "get_conn", lambda: conn)
    owner, _ = operations.claim_video_command_operation(
        command="video.generate_episode",
        idempotency_key="stable-episode-op",
        request_fingerprint="fp",
        scope_type="episode",
        scope_id="episode-1",
    )
    assert owner
    with pytest.raises(operations.VideoCommandOperationInProgress):
        operations.claim_video_command_operation(
            command="video.generate_episode",
            idempotency_key="stable-episode-op",
            request_fingerprint="fp",
            scope_type="episode",
            scope_id="episode-1",
        )


def test_episode_receipt_freezes_selection_and_skips_bound_failed_jobs(monkeypatch) -> None:
    conn = _conn()
    monkeypatch.setattr(operations, "get_conn", lambda: conn)
    owner, _ = operations.claim_video_command_operation(
        command="video.generate_episode",
        idempotency_key="episode-batch",
        request_fingerprint="episode-fp",
        scope_type="episode",
        scope_id="episode-1",
    )
    assert owner
    operations.bind_video_command_operation(
        command="video.generate_episode",
        idempotency_key="episode-batch",
        request_fingerprint="episode-fp",
        claim_token=owner,
        binding={
            "plan_id": "plan-1",
            "plan_revision": 7,
            "selected_shot_ids": ["s1", "s2", "s3"],
            "enqueued": [],
        },
        conn=conn,
        merge=True,
    )
    operations.bind_video_command_operation(
        command="video.generate_episode",
        idempotency_key="episode-batch",
        request_fingerprint="episode-fp",
        claim_token=owner,
        binding={
            "append_enqueued": {
                "shot_id": "s1",
                "version_id": "v1",
                "job_id": "j1",
                "task_accepted": True,
            },
        },
        conn=conn,
        merge=True,
    )
    conn.commit()

    # A crash occurs after s1 is durable; its later failed/stale status is not
    # consulted by receipt recovery. The new owner gets the frozen set and exact
    # IDs, and only s2/s3 remain eligible for enqueue.
    conn.execute(
        "UPDATE video_command_operation_receipts SET lease_expires_at=0"
    )
    conn.commit()
    replacement_owner, recovered = operations.claim_video_command_operation(
        command="video.generate_episode",
        idempotency_key="episode-batch",
        request_fingerprint="episode-fp",
        scope_type="episode",
        scope_id="episode-1",
    )
    assert replacement_owner and recovered is None
    binding = operations.read_video_command_operation_binding(
        command="video.generate_episode",
        idempotency_key="episode-batch",
        request_fingerprint="episode-fp",
    )
    assert binding["selected_shot_ids"] == ["s1", "s2", "s3"]
    assert binding["enqueued"] == [{
        "shot_id": "s1",
        "version_id": "v1",
        "job_id": "j1",
        "task_accepted": True,
    }]
    assert [shot for shot in binding["selected_shot_ids"] if shot not in {
        item["shot_id"] for item in binding["enqueued"]
    }] == ["s2", "s3"]

    with pytest.raises(operations.VideoCommandOperationConflict):
        operations.bind_video_command_operation(
            command="video.generate_episode",
            idempotency_key="episode-batch",
            request_fingerprint="episode-fp",
            claim_token=owner,
            binding={"append_enqueued": {"shot_id": "s2", "job_id": "late-old"}},
            conn=conn,
            merge=True,
        )
