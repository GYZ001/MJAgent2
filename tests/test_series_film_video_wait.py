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
在 ``stages.py`` 里是函数体内的惰性 import（``from app.domain.video_ops
import _complete_episode_core``），打包属性本可命中；但仓库的 AST 守卫
（``tests/test_api_monkeypatch_guard.py`` / ``test_video_supervisor_monkeypatch_guard.py``）
不区分这种形态，一律要求走 ``patch_api_everywhere`` /
``patch_video_supervisor_everywhere``——它们同样会打到包属性，对本测试等价，
且守卫能把「漏改」从静默变成 CI 立刻报错（CLAUDE.md「拆包会静默废掉 monkeypatch」）。
"""
from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import db
from app.domain.series_ops import stages as series_stages
from tests.conftest import patch_api_everywhere, patch_video_supervisor_everywhere


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
    patch_video_supervisor_everywhere(
        monkeypatch,
        "load_latest_checkpoint",
        lambda _eid: SimpleNamespace(
            run_id="run-old", grant_id="grant-1", phase="DISPATCHING", outcome=None,
        ),
    )
    captured: dict = {}

    async def fake_complete(episode_id, body, **_kwargs):
        captured.update({"episode_id": episode_id, "body": body})
        return {"run_id": "run-new"}

    patch_api_everywhere(monkeypatch, "_complete_episode_core", fake_complete)

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
    patch_video_supervisor_everywhere(
        monkeypatch,
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

    patch_api_everywhere(monkeypatch, "_complete_episode_core", fail_if_called)

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

    patch_api_everywhere(monkeypatch, "_complete_episode_core", fake_complete)

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

    patch_api_everywhere(monkeypatch, "_complete_episode_core", conflict)

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
    patch_video_supervisor_everywhere(
        monkeypatch,
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

    patch_api_everywhere(monkeypatch, "_complete_episode_core", already_active)

    with pytest.raises(RuntimeError) as exc:
        await series_stages._kick_video_completion("e", "series-run-1")

    message = str(exc.value)
    assert "自动恢复未成功" in message
    assert "全片补齐任务已在启动或运行" in message


def _insert_minimal(conn: sqlite3.Connection, table: str, **values) -> None:
    """按 pragma 把 NOT NULL 且无默认值的列补上占位，只关心测试点名的列。"""
    cols = conn.execute(f"pragma table_info({table})").fetchall()
    row = dict(values)
    for col in cols:
        name, ctype, notnull, default = col[1], (col[2] or "").upper(), col[3], col[4]
        if name in row or not notnull or default is not None:
            continue
        row[name] = 0 if ("INT" in ctype or "REAL" in ctype) else f"{name}-x"
    conn.execute(
        f"INSERT INTO {table}({','.join(row)}) VALUES({','.join('?' for _ in row)})",
        tuple(row.values()),
    )


def test_stalled_reason_names_the_blocked_shot_with_provider_words(monkeypatch) -> None:
    """运行级结论只说「需人工」；连播台必须把是哪一镜、供应商说了什么带出来。"""
    conn = _conn("run-1")
    _insert_run(conn, "run-1", "PARTIAL")
    conn.execute("UPDATE workflow_runs SET failure_message='外部终态或不可自动修复问题，已停止自动重试，需人工处理' WHERE id='run-1'")
    _insert_minimal(conn, "shots", id="s1", episode_id="e", shot_no=1)
    _insert_minimal(conn, "jobs", id="j1", project_id="p", episode_id="e", shot_id="s1", kind="video",
                    status="waiting_human", error="请求被拒绝（HTTP 400）：不符合安全合规要求")
    conn.commit()
    monkeypatch.setattr(series_stages, "get_conn", lambda: conn)
    reason = series_stages._stalled_video_reason("e")
    assert "需人工处理" in reason
    assert "第1镜" in reason and "不符合安全合规要求" in reason



@pytest.mark.asyncio
async def test_waiting_authorization_after_storyboard_change_restarts_fresh_completion(monkeypatch) -> None:
    """旧授权因分镜重做失效（UPSTREAM_VERSION_CHANGED）是流程自己造成的：连播台自己重新发起
    fresh 补齐，不把人晾到生成台（2026-09-05 产品复盘，我欲封天第 10 集）。"""
    conn = _conn("run-old")
    _insert_run(conn, "run-old", "RUNNING")
    monkeypatch.setattr(series_stages, "get_conn", lambda: conn)
    patch_video_supervisor_everywhere(
        monkeypatch,
        "load_latest_checkpoint",
        lambda _eid: SimpleNamespace(
            run_id="run-old", grant_id="grant-1", phase="WAITING_AUTHORIZATION",
            outcome="UPSTREAM_VERSION_CHANGED",
        ),
    )
    captured: dict = {}

    async def fake_complete(episode_id, body, **_kwargs):
        captured.update({"episode_id": episode_id, "body": body})
        return {"run_id": "run-new"}

    patch_api_everywhere(monkeypatch, "_complete_episode_core", fake_complete)
    await series_stages._kick_video_completion("e", "series-run-1")
    assert captured["body"]["mode"] == "fresh"


def test_screenplay_complete_requires_projection_not_just_status_column(monkeypatch) -> None:
    """重置后状态列残留 ready 而 screenplay_json 已清：连播台不得把映射台判成已完成。"""
    conn = _conn("run-x")
    conn.execute("UPDATE episodes SET screenplay_status='ready', screenplay_json=NULL WHERE id='e'")
    conn.commit()
    assert series_stages.screenplay_complete(conn, "e") is False
    patch_api_everywhere(monkeypatch, "_screenplay_ready", lambda ep: True)
    assert series_stages.screenplay_complete(conn, "e") is True


@pytest.mark.asyncio
async def test_paused_run_without_checkpoint_is_cancelled_and_restarted_fresh(monkeypatch) -> None:
    """2026-09-05 我欲封天第 13/14 集：服务重启把运行标成 PAUSED_EXTERNAL，但它还没写下
    检查点——开机恢复不接管、续跑无从续起、fresh 又被它挡成 ALREADY_ACTIVE，连播台只能
    判「未能补齐全部镜头」。现在连播台把这种孤儿运行取消掉，按当前分镜重新发起。"""
    conn = _conn("run-orphan")
    _insert_run(conn, "run-orphan", "PAUSED_EXTERNAL")
    monkeypatch.setattr(series_stages, "get_conn", lambda: conn)
    patch_video_supervisor_everywhere(monkeypatch, "load_latest_checkpoint", lambda _eid: None)
    cancelled: list[tuple[str, str]] = []

    class FakeRecorder:
        def __init__(self, run_id: str) -> None:
            self.run_id = run_id

        def cancel(self, message: str = "", *, conn=None) -> None:
            cancelled.append((self.run_id, message))

    from app.orchestration import engine as engine_mod
    monkeypatch.setattr(engine_mod, "WorkflowRecorder", FakeRecorder)
    captured: dict = {}

    async def fake_complete(episode_id, body, **_kwargs):
        captured.update({"episode_id": episode_id, "body": body})
        return {"run_id": "run-new"}

    patch_api_everywhere(monkeypatch, "_complete_episode_core", fake_complete)
    await series_stages._kick_video_completion("e", "series-run-1")
    assert cancelled and cancelled[0][0] == "run-orphan"
    assert "检查点" in cancelled[0][1]
    assert captured["body"]["mode"] == "fresh", "孤儿运行收尾后必须按当前分镜重新发起"


@pytest.mark.asyncio
async def test_paused_run_with_matching_checkpoint_is_not_treated_as_orphan(monkeypatch) -> None:
    conn = _conn("run-old")
    _insert_run(conn, "run-old", "PAUSED_EXTERNAL")
    monkeypatch.setattr(series_stages, "get_conn", lambda: conn)
    patch_video_supervisor_everywhere(
        monkeypatch, "load_latest_checkpoint",
        lambda _eid: SimpleNamespace(run_id="run-old", grant_id="grant-1", phase="DISPATCHING", outcome=None),
    )
    from app.orchestration import engine as engine_mod

    class Boom:
        def __init__(self, run_id: str) -> None:
            raise AssertionError("有检查点的暂停运行不得被当成孤儿取消")

    monkeypatch.setattr(engine_mod, "WorkflowRecorder", Boom)
    captured: dict = {}

    async def fake_complete(episode_id, body, **_kwargs):
        captured.update(body)
        return {}

    patch_api_everywhere(monkeypatch, "_complete_episode_core", fake_complete)
    await series_stages._kick_video_completion("e", "series-run-1")
    assert captured["mode"] == "resume"
