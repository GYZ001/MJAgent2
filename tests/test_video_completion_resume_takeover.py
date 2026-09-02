"""覆盖 `_complete_episode_core` 里 resume 模式接管 owner-pointer 的判据。

背景：`active_video_run_id` 指向的旧运行只要处于 CREATED/RUNNING/WAITING_*/
PAUSED_EXTERNAL 之一，且不是同一 operation_key 的可复用运行，就会被判
409 VIDEO_COMPLETION_ALREADY_ACTIVE——这个判据原来不分 mode，于是带着正确
grant 的 resume（生成台「继续补齐」、连播台对 PAUSED_EXTERNAL 的自动唤醒）
也被顶回，而 resume 存在的意义正是越过这些等待类状态继续。见
`app/domain/video_ops/completion_core.py::_assert_resume_may_take_over`。
"""
from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import api, db
from tests.conftest import (
    patch_api_everywhere,
    patch_completion_grant_everywhere,
    patch_video_supervisor_everywhere,
)


def _published_screenplay_json() -> str:
    from tests.test_screenplay_edit_save import _valid_script

    return _valid_script().model_dump_json()


def _conn() -> sqlite3.Connection:
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
        """INSERT INTO episodes(
               id,project_id,episode_no,title,status,screenplay_status,
               screenplay_json,
               screenplay_artifact_id,storyboard_artifact_id,
               published_screenplay_artifact_id,published_storyboard_artifact_id,created_at
           ) VALUES('e','p',1,'E','confirmed','ready',?,'screenplay-1','board-1','screenplay-1','board-1',0)""",
        (_published_screenplay_json(),),
    )
    conn.execute(
        """INSERT INTO shots(
               id,episode_id,shot_no,duration_s,shot_size,camera_move,
               scene_setting,action_desc,characters,dialogues,storyboard_artifact_id
           ) VALUES('s1','e',1,5,'中景','固定','日，测试室内场景',
                    'action','[]','[]','board-1')"""
    )
    conn.commit()
    return conn


def _seed_old_run(conn: sqlite3.Connection, status: str) -> None:
    conn.execute(
        """INSERT INTO workflow_runs(
               id, workflow_type, scope_type, scope_id, status,
               input_fingerprint, updated_at
           ) VALUES('run-old','episode_video_completion','episode','e',?,'fp',1)""",
        (status,),
    )
    conn.execute("UPDATE episodes SET active_video_run_id='run-old' WHERE id='e'")
    conn.commit()


def _patch_conn_everywhere(monkeypatch, conn: sqlite3.Connection) -> None:
    """让 `_complete_episode_core` 触达的每一处 get_conn（含拆包后的子模块
    各自绑定的副本）都读写同一份内存连接，复用 `test_review_wall_prd.py`
    里已验证过的组合。"""
    import app.evidence.repository as evidence_repository
    import app.orchestration.engine as orchestration_engine
    import app.orchestration.state_machine as state_machine

    patch_completion_grant_everywhere(monkeypatch, "get_conn", lambda: conn)
    for module in (evidence_repository, orchestration_engine, state_machine):
        monkeypatch.setattr(module, "get_conn", lambda: conn)
    patch_api_everywhere(monkeypatch, "get_conn", lambda: conn)


def _patch_common(monkeypatch, conn: sqlite3.Connection) -> list:
    """公共打桩：连接、任务注册表、分镜门禁、grant 校验、checkpoint、spawn。

    返回 ``spawned`` 列表，供调用方断言补齐协程有没有被真正 spawn。
    """
    _patch_conn_everywhere(monkeypatch, conn)
    monkeypatch.setattr(api.task_registry, "active", lambda *_args: False)
    patch_api_everywhere(monkeypatch, "_assert_storyboard_generation_gate", lambda *_args: None)
    patch_completion_grant_everywhere(
        monkeypatch,
        "validate_video_grant",
        lambda *_a, **_kw: SimpleNamespace(
            grant_id="grant-1", wall_clock_cap_s=3600.0, max_fallback_shots=2,
            allow_fallback_adopt=True, allow_storyboard_edit=False,
        ),
    )
    patch_video_supervisor_everywhere(
        monkeypatch,
        "load_latest_checkpoint",
        lambda _episode_id: SimpleNamespace(grant_id="grant-1", run_id="run-old"),
    )
    spawned: list = []

    def capture_spawn(kind, key, coro, *, project_id=None):
        spawned.append((kind, key, project_id))
        coro.close()
        return None

    monkeypatch.setattr(api.task_registry, "spawn", capture_spawn)
    return spawned


@pytest.mark.asyncio
async def test_resume_takes_over_paused_external_run(monkeypatch) -> None:
    conn = _conn()
    _seed_old_run(conn, "PAUSED_EXTERNAL")
    spawned = _patch_common(monkeypatch, conn)

    result = await api._complete_episode_core(
        "e", {"mode": "resume", "completion_grant_id": "grant-1"},
    )

    new_run_id = result["run_id"]
    assert new_run_id != "run-old"
    assert spawned == [("video_completion", "e", "p")]
    old_run = conn.execute(
        "SELECT recovered_by_run_id FROM workflow_runs WHERE id='run-old'"
    ).fetchone()
    assert old_run["recovered_by_run_id"] == new_run_id
    episode = conn.execute(
        "SELECT active_video_run_id FROM episodes WHERE id='e'"
    ).fetchone()
    assert episode["active_video_run_id"] == new_run_id
    assert conn.execute(
        "SELECT COUNT(*) FROM workflow_runs WHERE scope_id='e'"
    ).fetchone()[0] == 2


@pytest.mark.asyncio
async def test_resume_still_blocked_when_old_run_is_running(monkeypatch) -> None:
    conn = _conn()
    _seed_old_run(conn, "RUNNING")
    _patch_common(monkeypatch, conn)

    with pytest.raises(HTTPException) as rejected:
        await api._complete_episode_core(
            "e", {"mode": "resume", "completion_grant_id": "grant-1"},
        )

    assert rejected.value.status_code == 409
    assert rejected.value.detail["code"] == "VIDEO_COMPLETION_ALREADY_ACTIVE"
    episode = conn.execute(
        "SELECT active_video_run_id FROM episodes WHERE id='e'"
    ).fetchone()
    assert episode["active_video_run_id"] == "run-old"
    assert conn.execute(
        "SELECT COUNT(*) FROM workflow_runs WHERE scope_id='e'"
    ).fetchone()[0] == 1


@pytest.mark.asyncio
async def test_resume_blocked_when_checkpoint_grant_mismatches(monkeypatch) -> None:
    conn = _conn()
    _seed_old_run(conn, "PAUSED_EXTERNAL")
    _patch_conn_everywhere(monkeypatch, conn)
    monkeypatch.setattr(api.task_registry, "active", lambda *_args: False)
    patch_api_everywhere(monkeypatch, "_assert_storyboard_generation_gate", lambda *_args: None)
    patch_completion_grant_everywhere(
        monkeypatch,
        "validate_video_grant",
        lambda *_a, **_kw: SimpleNamespace(
            grant_id="grant-1", wall_clock_cap_s=3600.0, max_fallback_shots=2,
            allow_fallback_adopt=True, allow_storyboard_edit=False,
        ),
    )
    # checkpoint 指向另一条运行，说明这份 grant 不是 run-old 当初持有的那份。
    patch_video_supervisor_everywhere(
        monkeypatch,
        "load_latest_checkpoint",
        lambda _episode_id: SimpleNamespace(grant_id="grant-1", run_id="run-other"),
    )

    with pytest.raises(HTTPException) as rejected:
        await api._complete_episode_core(
            "e", {"mode": "resume", "completion_grant_id": "grant-1"},
        )

    assert rejected.value.status_code == 409
    assert rejected.value.detail["code"] == "VIDEO_COMPLETION_RESUME_GRANT_MISMATCH"
    assert rejected.value.detail["action"] == "start_fresh"
    episode = conn.execute(
        "SELECT active_video_run_id FROM episodes WHERE id='e'"
    ).fetchone()
    assert episode["active_video_run_id"] == "run-old"


@pytest.mark.asyncio
async def test_fresh_mode_still_rejected_by_paused_external_run(monkeypatch) -> None:
    """回归网：mode=fresh 不受本次改动影响，撞见 PAUSED_EXTERNAL 仍然 409。"""
    conn = _conn()
    _seed_old_run(conn, "PAUSED_EXTERNAL")
    _patch_common(monkeypatch, conn)

    with pytest.raises(HTTPException) as rejected:
        await api._complete_episode_core("e", {"mode": "fresh"})

    assert rejected.value.status_code == 409
    assert rejected.value.detail["code"] == "VIDEO_COMPLETION_ALREADY_ACTIVE"
    episode = conn.execute(
        "SELECT active_video_run_id FROM episodes WHERE id='e'"
    ).fetchone()
    assert episode["active_video_run_id"] == "run-old"
