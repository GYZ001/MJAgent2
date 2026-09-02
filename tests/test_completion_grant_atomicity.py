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
        wall_clock_cap_s=3600,
        shots_total=2,
        **kwargs,
    )


def test_issue_rolls_back_grant_when_audit_ledger_write_fails(
    grant_db,
    monkeypatch,
) -> None:
    """成本预算拦截体系退场（2026-09-01）：``authorize_episode_video_budget_
    absolute`` 已整体删除。签发仍在同一个事务里写 ``video_budget_authority_
    ledger``（幂等去重职责，见 grants_issue.py 说明）——注入失败到这个剩余的
    写点上，证明原子性回滚依然成立。"""
    write_event = completion_grant._record_video_budget_authority_event

    def fail_event(*args, **kwargs):
        write_event(*args, **kwargs)
        raise RuntimeError("injected ledger failure")

    patch_completion_grant_everywhere(
        monkeypatch,
        "_record_video_budget_authority_event",
        fail_event,
    )

    with pytest.raises(RuntimeError, match="injected ledger failure"):
        _issue()

    assert grant_db.execute(
        "SELECT COUNT(*) FROM completion_grants"
    ).fetchone()[0] == 0
    assert grant_db.execute(
        "SELECT COUNT(*) FROM video_budget_authority_ledger"
    ).fetchone()[0] == 0


def test_topup_rolls_back_grant_when_audit_ledger_write_fails(
    grant_db,
    monkeypatch,
) -> None:
    grant, _token = _issue()
    before_wall = grant_db.execute(
        "SELECT wall_clock_cap_s FROM completion_grants WHERE id=?",
        (grant.grant_id,),
    ).fetchone()["wall_clock_cap_s"]
    write_event = completion_grant._record_video_budget_authority_event

    def fail_event(*args, **kwargs):
        write_event(*args, **kwargs)
        raise RuntimeError("injected topup ledger failure")

    patch_completion_grant_everywhere(
        monkeypatch,
        "_record_video_budget_authority_event",
        fail_event,
    )

    with pytest.raises(RuntimeError, match="injected topup ledger failure"):
        completion_grant.bump_video_grant_wall_clock(grant.grant_id, add_wall_s=600)

    stored_grant = grant_db.execute(
        "SELECT wall_clock_cap_s FROM completion_grants WHERE id=?",
        (grant.grant_id,),
    ).fetchone()
    assert stored_grant["wall_clock_cap_s"] == before_wall


def test_issue_retry_reuses_one_grant_and_one_audit_event(grant_db) -> None:
    first, first_token = _issue(idempotency_key="issue-request-1")
    replay, replay_token = _issue(idempotency_key="issue-request-1")

    assert replay.grant_id == first.grant_id
    assert first_token
    assert replay_token == ""
    assert grant_db.execute(
        "SELECT COUNT(*) FROM completion_grants"
    ).fetchone()[0] == 1
    # 金额字段固定写 0（见 grants_issue.py 说明）：这张台账现在只承担幂等
    # 去重职责，不再是金额审计。
    events = grant_db.execute(
        """SELECT event_type,grant_cap_cny,authority_cap_cny
           FROM video_budget_authority_ledger ORDER BY created_at,id"""
    ).fetchall()
    assert [tuple(row) for row in events] == [
        ("grant_issued", 0.0, 0.0),
    ]


def test_topup_retry_applies_once_and_preserves_audit_history(grant_db) -> None:
    grant, _token = _issue(idempotency_key="issue-request-2")

    first = completion_grant.bump_video_grant_wall_clock(
        grant.grant_id,
        add_wall_s=600,
        idempotency_key="topup-request-1",
    )
    replay = completion_grant.bump_video_grant_wall_clock(
        grant.grant_id,
        add_wall_s=600,
        idempotency_key="topup-request-1",
    )

    assert first.wall_clock_cap_s == 4200
    assert replay.wall_clock_cap_s == 4200
    events = grant_db.execute(
        """SELECT event_type,prior_wall_clock_cap_s,wall_clock_cap_s
           FROM video_budget_authority_ledger ORDER BY created_at,id"""
    ).fetchall()
    assert [tuple(row) for row in events] == [
        ("grant_issued", None, 3600.0),
        ("grant_topped_up", 3600.0, 4200.0),
    ]


def test_concurrent_distinct_topups_are_all_applied(grant_db) -> None:
    grant, _token = _issue(idempotency_key="issue-request-3")
    workers = 6
    barrier = threading.Barrier(workers)

    def topup(index: int) -> float:
        barrier.wait()
        updated = completion_grant.bump_video_grant_wall_clock(
            grant.grant_id,
            add_wall_s=60,
            idempotency_key=f"topup-concurrent-{index}",
        )
        return updated.wall_clock_cap_s

    with ThreadPoolExecutor(max_workers=workers) as executor:
        results = list(executor.map(topup, range(workers)))

    assert max(results) == 3960
    assert completion_grant.get_video_grant(
        grant.grant_id
    ).wall_clock_cap_s == 3960
    assert grant_db.execute(
        "SELECT COUNT(*) FROM video_budget_authority_ledger"
    ).fetchone()[0] == workers + 1


def test_concurrent_same_topup_key_is_applied_once(grant_db) -> None:
    grant, _token = _issue(idempotency_key="issue-request-4")
    workers = 6
    barrier = threading.Barrier(workers)

    def topup(_index: int) -> float:
        barrier.wait()
        updated = completion_grant.bump_video_grant_wall_clock(
            grant.grant_id,
            add_wall_s=300,
            idempotency_key="topup-concurrent-retry",
        )
        return updated.wall_clock_cap_s

    with ThreadPoolExecutor(max_workers=workers) as executor:
        results = list(executor.map(topup, range(workers)))

    assert results == [3900] * workers
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
        "bump_video_grant_wall_clock",
        stop_after_capture,
    )

    with pytest.raises(RuntimeError, match="stop after topup capture"):
        await api._complete_episode_core(
            "grant-episode",
            {
                "mode": "resume",
                "completion_grant_id": "grant-existing",
                "add_wall_clock_s": 300,
                "idempotency_key": "episode-topup-request",
            },
        )

    assert captured == {
        "grant_id": "grant-existing",
        "add_wall_s": 300.0,
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
            "wall_clock_cap_s": 3600,
            "idempotency_key": "project-request",
        },
    )

    assert captured["episode_id"] == "grant-episode"
    assert captured["body"]["idempotency_key"] == (
        "project-request:episode:grant-episode"
    )


def test_issue_does_not_write_completion_grants_budget_cap_cny(grant_db) -> None:
    """成本预算拦截体系退场（2026-09-01）：签发不再计算或写入任何金额上限。

    ``completion_grants.budget_cap_cny`` 是历史列，无写入者；
    ``episode_video_budget_authorities``/``_episode_video_budget_floor``/
    ``authorize_episode_video_budget_increment`` 这条金额上限计算链路已随
    A2 整体删除——见 CLAUDE.md「Retiring Features」。"""
    grant, _token = _issue(idempotency_key="no-budget-write")

    stored = grant_db.execute(
        "SELECT budget_cap_cny FROM completion_grants WHERE id=?",
        (grant.grant_id,),
    ).fetchone()
    assert stored["budget_cap_cny"] is None
    assert not hasattr(grant, "budget_cap_cny")
