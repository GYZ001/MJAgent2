"""连播台生成台步骤遇到补齐 Supervisor 等待态时的分支行为。

``app.domain.series_ops.stages._kick_video_completion`` 是 ``_run_video`` 在
「上次补齐运行既不活跃、也没结束」时的决策点：服务重启导致的 PAUSED_EXTERNAL
要唤醒原运行，WAITING_AUTHORIZATION/WAITING_HUMAN/WAITING_RETRY（以及非服务
重启的 PAUSED_EXTERNAL checkpoint phase）要停下来讲清楚出路，其余情况维持原有
的「fresh + 吞掉 VIDEO_COMPLETION_ALREADY_ACTIVE」逻辑不变。

直接单测 ``_kick_video_completion``（私有函数），照
``tests/test_series_film_orchestrator.py`` 直测
``orchestrator._exception_message`` 的先例——这是一个纯决策函数，不需要经过
整个 ``run_series_film`` 编排跑一遍。``app.domain.video_ops._complete_episode_core``
在 ``stages.py`` 里永远是函数体内的惰性 import（``from app.domain.video_ops
import _complete_episode_core``），每次调用都在包对象上现查，所以直接
monkeypatch 包属性即可命中，不需要 ``patch_video_ops_everywhere`` 之类的
「everywhere」helper（那是给顶层缓存 import 准备的，参见
``tests/test_stages_monkeypatch_guard.py`` 的说明）。同理
``app.video_supervisor.load_latest_checkpoint``。
"""
from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import app.domain.video_ops as video_ops
import app.video_supervisor as video_supervisor
from app import db
from app.domain.series_ops import stages as series_stages


def _conn(active_video_run_id: str | None) -> sqlite3.Connection:
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
               id,project_id,episode_no,status,active_video_run_id,created_at
           ) VALUES('e','p',1,'generating',?,0)""",
        (active_video_run_id,),
    )
    conn.commit()
    return conn


def _insert_run(conn: sqlite3.Connection, run_id: str, status: str) -> None:
    conn.execute(
        """INSERT INTO workflow_runs(
               id,workflow_type,scope_type,scope_id,status,input_fingerprint,updated_at
           ) VALUES(?,'episode_video_completion','episode','e',?,'fp',1)""",
        (run_id, status),
    )
    conn.commit()


@pytest.mark.asyncio
async def test_paused_external_service_restart_resumes_with_original_grant(
    monkeypatch,
) -> None:
    conn = _conn("run-old")
    _insert_run(conn, "run-old", "PAUSED_EXTERNAL")
    monkeypatch.setattr(series_stages, "get_conn", lambda: conn)
    monkeypatch.setattr(
        video_supervisor,
        "load_latest_checkpoint",
        lambda _eid: SimpleNamespace(
            run_id="run-old", grant_id="grant-1", phase="DISPATCHING", outcome=None,
        ),
    )
    captured: dict = {}

    async def fake_complete(episode_id, body, **_kwargs):
        captured.update({"episode_id": episode_id, "body": body})
        return {"run_id": "run-new"}

    monkeypatch.setattr(video_ops, "_complete_episode_core", fake_complete)

    await series_stages._kick_video_completion("e", "series-run-1")

    assert captured["episode_id"] == "e"
    assert captured["body"]["mode"] == "resume"
    assert captured["body"]["completion_grant_id"] == "grant-1"


@pytest.mark.asyncio
async def test_waiting_authorization_stops_with_actionable_message(monkeypatch) -> None:
    """真实字面值：checkpoint phase 才是 WAITING_AUTHORIZATION 的唯一来源，
    workflow_runs.status 落的是 PARTIAL（见 completion_core.py 的
    recorder.partial()），不是字面量 WAITING_AUTHORIZATION——两者都要覆盖到。
    """
    conn = _conn("run-old")
    _insert_run(conn, "run-old", "PARTIAL")
    monkeypatch.setattr(series_stages, "get_conn", lambda: conn)
    monkeypatch.setattr(
        video_supervisor,
        "load_latest_checkpoint",
        lambda _eid: SimpleNamespace(
            run_id="run-old",
            grant_id="grant-1",
            phase="WAITING_AUTHORIZATION",
            outcome="STORYBOARD_REPAIR_PROPOSAL_NOT_AUTHORIZED",
        ),
    )

    async def fail_if_called(*_a, **_k):
        raise AssertionError("等待人工处理时不应该发起新的补齐尝试")

    monkeypatch.setattr(video_ops, "_complete_episode_core", fail_if_called)

    with pytest.raises(RuntimeError) as exc:
        await series_stages._kick_video_completion("e", "series-run-1")

    message = str(exc.value)
    assert "生成台" in message
    assert "继续" in message
    assert "AI 提议修改分镜以补齐镜头" in message


@pytest.mark.asyncio
async def test_fresh_mode_unaffected_when_no_active_run(monkeypatch) -> None:
    conn = _conn(None)
    monkeypatch.setattr(series_stages, "get_conn", lambda: conn)
    captured: dict = {}

    async def fake_complete(episode_id, body, **_kwargs):
        captured.update({"episode_id": episode_id, "body": body})
        return {}

    monkeypatch.setattr(video_ops, "_complete_episode_core", fake_complete)

    await series_stages._kick_video_completion("e", "series-run-1")

    assert captured["episode_id"] == "e"
    assert captured["body"]["mode"] == "fresh"


@pytest.mark.asyncio
async def test_fresh_still_swallows_already_active_conflict(monkeypatch) -> None:
    """回归网：重构前 _run_video 对 fresh 尝试吞掉 409 的行为必须原样保留。"""
    conn = _conn(None)
    monkeypatch.setattr(series_stages, "get_conn", lambda: conn)

    async def conflict(*_a, **_k):
        raise HTTPException(409, {"code": "VIDEO_COMPLETION_ALREADY_ACTIVE"})

    monkeypatch.setattr(video_ops, "_complete_episode_core", conflict)

    await series_stages._kick_video_completion("e", "series-run-1")  # 不应该抛出


@pytest.mark.asyncio
async def test_paused_external_resume_conflict_falls_back_to_wait_message(
    monkeypatch,
) -> None:
    """自动唤醒本身 409/422 时不能悄悄退回 fresh（会丢弃 checkpoint 进度），
    必须给出诚实的等待文案。"""
    conn = _conn("run-old")
    _insert_run(conn, "run-old", "PAUSED_EXTERNAL")
    monkeypatch.setattr(series_stages, "get_conn", lambda: conn)
    monkeypatch.setattr(
        video_supervisor,
        "load_latest_checkpoint",
        lambda _eid: SimpleNamespace(
            run_id="run-old", grant_id="grant-1", phase="DISPATCHING", outcome=None,
        ),
    )

    async def already_active(*_a, **_k):
        raise HTTPException(409, {
            "code": "VIDEO_COMPLETION_ALREADY_ACTIVE",
            "message": "全片补齐任务已在启动或运行，请勿重复提交",
        })

    monkeypatch.setattr(video_ops, "_complete_episode_core", already_active)

    with pytest.raises(RuntimeError) as exc:
        await series_stages._kick_video_completion("e", "series-run-1")

    message = str(exc.value)
    assert "自动恢复未成功" in message
    assert "全片补齐任务已在启动或运行" in message
