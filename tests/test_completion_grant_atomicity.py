from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import threading

import pytest

from app import api, completion_grant, db
from tests.conftest import patch_completion_grant_everywhere, patch_video_supervisor_everywhere, patch_api_everywhere


@pytest.fixture
def grant_db(tmp_path, monkeypatch):
    existing = getattr(db._local, "conn", None)
    if existing is not None:
        existing.close()
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "completion-grant-atomicity.db")
    db._local.conn = None
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
    write_authority = completion_grant.authorize_episode_video_budget_absolute

    def fail_authority(*args, **kwargs):
        write_authority(*args, **kwargs)
        raise RuntimeError("injected authority failure")

    patch_completion_grant_everywhere(
        monkeypatch,
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
    write_authority = completion_grant.authorize_episode_video_budget_absolute

    def fail_authority(*args, **kwargs):
        write_authority(*args, **kwargs)
        raise RuntimeError("injected topup authority failure")

    patch_completion_grant_everywhere(
        monkeypatch,
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


def _isolate_completion_core(grant_db, monkeypatch) -> None:
    patch_api_everywhere(monkeypatch, "get_conn", lambda: grant_db)
    monkeypatch.setattr(api.task_registry, "active", lambda *_args: False)
    patch_api_everywhere(monkeypatch,
        "_review_assert_positive_action",
        lambda *_args, **_kwargs: None,
    )
    patch_api_everywhere(monkeypatch,
        "_assert_storyboard_generation_gate",
        lambda *_args, **_kwargs: None,
    )
    patch_api_everywhere(monkeypatch,
        "_ensure_video_episode_columns",
        lambda: None,
    )


@pytest.mark.asyncio
async def test_completion_core_passes_idempotency_key_to_grant_issue(
    grant_db,
    monkeypatch,
) -> None:
    _isolate_completion_core(grant_db, monkeypatch)
    captured = {}

    def stop_after_capture(**kwargs):
        captured.update(kwargs)
        raise RuntimeError("stop after issue capture")

    patch_completion_grant_everywhere(
        monkeypatch,
        "issue_video_completion_grant",
        stop_after_capture,
    )

    with pytest.raises(RuntimeError, match="stop after issue capture"):
        await api._complete_episode_core(
            "grant-episode",
            {
                "mode": "fresh",
                "budget_cap_cny": 50,
                "idempotency_key": "episode-fresh-request",
            },
        )

    assert captured["idempotency_key"] == "episode-fresh-request"


@pytest.mark.asyncio
async def test_completion_core_passes_idempotency_key_to_grant_topup(
    grant_db,
    monkeypatch,
) -> None:
    _isolate_completion_core(grant_db, monkeypatch)
    captured = {}
    patch_completion_grant_everywhere(
        monkeypatch,
        "validate_video_grant",
        lambda *_args, **_kwargs: object(),
    )

    def stop_after_capture(grant_id, **kwargs):
        captured.update({"grant_id": grant_id, **kwargs})
        raise RuntimeError("stop after topup capture")

    patch_completion_grant_everywhere(
        monkeypatch,
        "bump_video_grant_budget",
        stop_after_capture,
    )

    with pytest.raises(RuntimeError, match="stop after topup capture"):
        await api._complete_episode_core(
            "grant-episode",
            {
                "mode": "resume",
                "completion_grant_id": "grant-existing",
                "add_budget_cny": 5,
                "idempotency_key": "episode-topup-request",
            },
        )

    assert captured == {
        "grant_id": "grant-existing",
        "add_cny": 5.0,
        "add_wall_s": 0.0,
        "idempotency_key": "episode-topup-request",
    }


@pytest.mark.asyncio
async def test_project_completion_derives_stable_episode_grant_key(
    grant_db,
    monkeypatch,
) -> None:

    patch_api_everywhere(monkeypatch, "get_conn", lambda: grant_db)
    monkeypatch.setattr(api.task_registry, "active", lambda *_args: False)
    patch_video_supervisor_everywhere(
        monkeypatch,
        "rebuild_coverage_ledger",
        lambda _episode_id: type(
            "Ledger",
            (),
            {"covered_within_quota": lambda self: False},
        )(),
    )
    captured = {}

    async def capture_episode(episode_id, body, **_kwargs):
        captured.update({"episode_id": episode_id, "body": body})
        return {
            "run_id": "run-episode",
            "completion_grant_id": "grant-episode",
        }

    patch_api_everywhere(monkeypatch, "_complete_episode_core", capture_episode)

    await api._complete_project_videos_core(
        "grant-project",
        {
            "episode_ids": ["grant-episode"],
            "global_budget_cap_cny": 50,
            "per_episode_cap_cny": 50,
            "wall_clock_cap_s": 3600,
            "idempotency_key": "project-request",
        },
    )

    assert captured["episode_id"] == "grant-episode"
    assert captured["body"]["idempotency_key"] == (
        "project-request:episode:grant-episode"
    )


def _insert_claim(conn, *, operation_id: str, amount: float, status: str) -> None:
    completion_grant.ensure_video_budget_authority_tables(conn)
    conn.execute(
        """INSERT INTO provider_video_budget_claims(
               operation_id,project_id,episode_id,shot_id,job_id,version_id,
               origin_episode_id,origin_shot_id,origin_job_id,origin_version_id,
               amount_cny,status,created_at,updated_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            # job_id/version_id 置空，复刻生产形态：这两列的外键是
            # ON DELETE SET NULL，镜头版本被清掉后认领行留存、只剩 origin_*。
            operation_id, "grant-project", "grant-episode", "grant-shot-1",
            None, None, "grant-episode", "grant-shot-1", "job-1", "ver-1",
            amount, status, 1, 1,
        ),
    )
    conn.commit()


def test_budget_floor_counts_claims_when_authority_row_is_missing(grant_db) -> None:
    """已有认领、却没有 authority 行时，新批的上限不得低于已用额度。

    扣款侧一律按 ``used = baseline + claimed`` 判断，而
    ``_historical_video_liability`` 按设计只统计**没有 claim 归属**的遗留责任
    （避免与 claimed 重复计），所以有认领时它正确地返回 0。此前 floor 直接
    等于 baseline、把 claimed 丢掉，新批的 cap 一诞生就低于已用额度：实测
    ep_0a70ec56e8e9 已有 96 元 settled 认领，新授权拿到 cap=96 而 used 已是
    96，之后每次供应商调用都被判超限，8 个镜头全部 paused_budget，整集永久
    停在 WAITING_AUTHORIZATION。
    """
    conn = grant_db
    _insert_claim(conn, operation_id="op-old", amount=96.0, status="settled")
    conn.execute("DELETE FROM episode_video_budget_authorities")
    conn.commit()

    baseline, floor = completion_grant._episode_video_budget_floor(
        "grant-episode", conn=conn,
    )

    assert baseline == 0.0
    assert floor == 96.0


def test_released_claims_do_not_raise_the_floor(grant_db) -> None:
    """released 的认领代表从未真正计费，不构成已承诺责任，不得抬高下限。"""
    conn = grant_db
    _insert_claim(conn, operation_id="op-released", amount=96.0, status="released")
    conn.execute("DELETE FROM episode_video_budget_authorities")
    conn.commit()

    _baseline, floor = completion_grant._episode_video_budget_floor(
        "grant-episode", conn=conn,
    )

    assert floor == 0.0


def test_new_grant_after_historical_claims_can_still_reserve(grant_db) -> None:
    """端到端：有历史认领的分集重新发授权后，下一次供应商扣款必须还能通过。"""
    conn = grant_db
    _insert_claim(conn, operation_id="op-old", amount=96.0, status="settled")
    conn.execute("DELETE FROM episode_video_budget_authorities")
    conn.commit()

    completion_grant.authorize_episode_video_budget_increment(
        episode_id="grant-episode", increment_cny=96.0, source="test:regrant", conn=conn,
    )
    row = conn.execute(
        "SELECT baseline_cny,cap_cny FROM episode_video_budget_authorities"
        " WHERE episode_id='grant-episode'"
    ).fetchone()

    assert float(row["cap_cny"]) > 96.0
