from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import threading

import pytest

from app import completion_grant, db


@pytest.fixture
def grant_db(tmp_path, monkeypatch):
    existing = getattr(db._local, "conn", None)
    if existing is not None:
        existing.close()
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "completion-grant-atomicity.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()
    conn = db.get_conn()
    conn.execute(
        """INSERT INTO projects(id,name,status,created_at)
           VALUES('grant-project','Grant project','created',1)"""
    )
    conn.execute(
        """INSERT INTO episodes(
               id,project_id,episode_no,title,status,storyboard_artifact_id,
               created_at
           ) VALUES(
               'grant-episode','grant-project',1,'Episode','confirmed',
               'storyboard-artifact',1
           )"""
    )
    for shot_no in (1, 2):
        conn.execute(
            """INSERT INTO shots(
                   id,episode_id,shot_no,duration_s,shot_size,camera_move,
                   scene_setting,characters,action_desc,dialogues
               ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                f"grant-shot-{shot_no}",
                "grant-episode",
                shot_no,
                5,
                "medium",
                "locked",
                "interior",
                json.dumps(["A"]),
                f"action {shot_no}",
                "[]",
            ),
        )
    conn.commit()
    yield conn
    conn.close()
    db._local.conn = None


def _issue(*, idempotency_key: str | None = None):
    kwargs = {}
    if idempotency_key is not None:
        kwargs["idempotency_key"] = idempotency_key
    return completion_grant.issue_video_completion_grant(
        episode_id="grant-episode",
        project_id="grant-project",
        storyboard_artifact_id="storyboard-artifact",
        budget_cap_cny=50,
        shots_total=2,
        **kwargs,
    )


def test_issue_rolls_back_grant_when_budget_authority_write_fails(
    grant_db,
    monkeypatch,
) -> None:
    def fail_authority(*_args, **_kwargs):
        raise RuntimeError("injected authority failure")

    monkeypatch.setattr(
        completion_grant,
        "authorize_episode_video_budget_absolute",
        fail_authority,
    )

    with pytest.raises(RuntimeError, match="injected authority failure"):
        _issue()

    assert grant_db.execute(
        "SELECT COUNT(*) FROM completion_grants"
    ).fetchone()[0] == 0
    assert grant_db.execute(
        "SELECT COUNT(*) FROM episode_video_budget_authorities"
    ).fetchone()[0] == 0


def test_topup_rolls_back_grant_when_budget_authority_write_fails(
    grant_db,
    monkeypatch,
) -> None:
    grant, _token = _issue()
    before_authority = grant_db.execute(
        """SELECT cap_cny,source FROM episode_video_budget_authorities
           WHERE episode_id='grant-episode'"""
    ).fetchone()

    def fail_authority(*_args, **_kwargs):
        raise RuntimeError("injected topup authority failure")

    monkeypatch.setattr(
        completion_grant,
        "authorize_episode_video_budget_absolute",
        fail_authority,
    )

    with pytest.raises(RuntimeError, match="injected topup authority failure"):
        completion_grant.bump_video_grant_budget(grant.grant_id, add_cny=10)

    stored_grant = grant_db.execute(
        "SELECT budget_cap_cny FROM completion_grants WHERE id=?",
        (grant.grant_id,),
    ).fetchone()
    stored_authority = grant_db.execute(
        """SELECT cap_cny,source FROM episode_video_budget_authorities
           WHERE episode_id='grant-episode'"""
    ).fetchone()
    assert stored_grant["budget_cap_cny"] == 50
    assert tuple(stored_authority) == tuple(before_authority)


def test_issue_retry_reuses_one_grant_and_one_audit_event(grant_db) -> None:
    first, first_token = _issue(idempotency_key="issue-request-1")
    replay, replay_token = _issue(idempotency_key="issue-request-1")

    assert replay.grant_id == first.grant_id
    assert first_token
    assert replay_token == ""
    assert grant_db.execute(
        "SELECT COUNT(*) FROM completion_grants"
    ).fetchone()[0] == 1
    events = grant_db.execute(
        """SELECT event_type,grant_cap_cny,authority_cap_cny
           FROM video_budget_authority_ledger ORDER BY created_at,id"""
    ).fetchall()
    assert [tuple(row) for row in events] == [
        ("grant_issued", 50.0, 50.0),
    ]


def test_topup_retry_applies_once_and_preserves_audit_history(grant_db) -> None:
    grant, _token = _issue(idempotency_key="issue-request-2")

    first = completion_grant.bump_video_grant_budget(
        grant.grant_id,
        add_cny=10,
        idempotency_key="topup-request-1",
    )
    replay = completion_grant.bump_video_grant_budget(
        grant.grant_id,
        add_cny=10,
        idempotency_key="topup-request-1",
    )

    assert first.budget_cap_cny == 60
    assert replay.budget_cap_cny == 60
    assert grant_db.execute(
        """SELECT cap_cny FROM episode_video_budget_authorities
           WHERE episode_id='grant-episode'"""
    ).fetchone()["cap_cny"] == 60
    events = grant_db.execute(
        """SELECT event_type,prior_grant_cap_cny,grant_cap_cny,requested_add_cny
           FROM video_budget_authority_ledger ORDER BY created_at,id"""
    ).fetchall()
    assert [tuple(row) for row in events] == [
        ("grant_issued", None, 50.0, 50.0),
        ("grant_topped_up", 50.0, 60.0, 10.0),
    ]


def test_concurrent_distinct_topups_are_all_applied(grant_db) -> None:
    grant, _token = _issue(idempotency_key="issue-request-3")
    workers = 6
    barrier = threading.Barrier(workers)

    def topup(index: int) -> float:
        barrier.wait()
        updated = completion_grant.bump_video_grant_budget(
            grant.grant_id,
            add_cny=1,
            idempotency_key=f"topup-concurrent-{index}",
        )
        return updated.budget_cap_cny

    with ThreadPoolExecutor(max_workers=workers) as executor:
        results = list(executor.map(topup, range(workers)))

    assert max(results) == 56
    assert completion_grant.get_video_grant(
        grant.grant_id
    ).budget_cap_cny == 56
    assert grant_db.execute(
        """SELECT cap_cny FROM episode_video_budget_authorities
           WHERE episode_id='grant-episode'"""
    ).fetchone()["cap_cny"] == 56
    assert grant_db.execute(
        "SELECT COUNT(*) FROM video_budget_authority_ledger"
    ).fetchone()[0] == workers + 1


def test_concurrent_same_topup_key_is_applied_once(grant_db) -> None:
    grant, _token = _issue(idempotency_key="issue-request-4")
    workers = 6
    barrier = threading.Barrier(workers)

    def topup(_index: int) -> float:
        barrier.wait()
        updated = completion_grant.bump_video_grant_budget(
            grant.grant_id,
            add_cny=5,
            idempotency_key="topup-concurrent-retry",
        )
        return updated.budget_cap_cny

    with ThreadPoolExecutor(max_workers=workers) as executor:
        results = list(executor.map(topup, range(workers)))

    assert results == [55] * workers
    assert grant_db.execute(
        """SELECT cap_cny FROM episode_video_budget_authorities
           WHERE episode_id='grant-episode'"""
    ).fetchone()["cap_cny"] == 55
    assert grant_db.execute(
        """SELECT COUNT(*) FROM video_budget_authority_ledger
           WHERE event_type='grant_topped_up'"""
    ).fetchone()[0] == 1
