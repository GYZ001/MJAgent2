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

