"""连播队列（``app.domain.series_ops.queue``）的串行 runner 回归。

内存 sqlite + monkeypatch 各处 ``get_conn``（同 test_series_film_orchestrator.py
的写法），绕开真实剧本/分镜/视频生成，只验证队列自身的调度语义：
- 严格串行（一个任务跑完才跑下一个）；
- 单任务失败标 failed 并继续，连续 3 个失败自动暂停整队且 stop_reason 非空；
- 暂停/取消单个任务时进度保留/清空的语义（queued vs idle）；
- 开机恢复把 running 复位为 queued、遗留 series_film 运行标 CANCELLED。

用 ``task_registry.get(queue.TASK_KIND, project_id)`` 拿到真实 asyncio.Task 再
``await`` 它，而不是依赖测试客户端的隐式事件循环 pump 时机——这是本文件相对
tests/test_series_tasks_api.py 的关键差异：那边测 HTTP 契约，这里测调度本身，
需要确定性的同步点。
"""
from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest

from app import db, task_registry
from app.domain.series_ops import merge, queue, recovery, tasks
from app.domain.series_ops import stages as series_stages


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
    conn.commit()
    return conn


def _patch_conn(monkeypatch, conn: sqlite3.Connection) -> None:
    import app.evidence.repository as evidence_repository
    import app.orchestration.engine as orchestration_engine
    import app.orchestration.state_machine as state_machine
    from app.domain.series_ops import orchestrator, state

    # tasks.py 不持有 get_conn：全部函数显式接收 conn 参数（所有权显式，
    # CLAUDE.md「Ownership Must Be Explicit」），没有 ambient get_conn() 调用点。
    for module in (
        evidence_repository, orchestration_engine, state_machine,
        orchestrator, state, queue, recovery,
    ):
        monkeypatch.setattr(module, "get_conn", lambda: conn)
    # 既有用例写的是串行语义；并发度改由 settings 决定后，这里固定成 1，
    # 并发行为由文末专门的用例覆盖（它们自己再改成 2）。
    monkeypatch.setattr(queue, "queue_concurrency", lambda: 1)


def _seed_task(conn: sqlite3.Connection, task_id: str, episode_nos: list[int]) -> None:
    for no in episode_nos:
        conn.execute(
            "INSERT OR IGNORE INTO episodes(id,project_id,episode_no,status,created_at) "
            "VALUES(?,?,?,?,0)",
            (f"e{no}", "p", no, "planned"),
        )
    conn.execute(
        """INSERT INTO series_tasks(
               id, project_id, title, episode_from, episode_to, status,
               progress_json, created_at, updated_at
           ) VALUES (?,'p','',?,?,'idle','{}',0,0)""",
        (task_id, episode_nos[0], episode_nos[-1]),
    )
    conn.commit()


async def _await_runner(project_id: str, timeout: float = 5.0) -> None:
    task = task_registry.get(queue.TASK_KIND, project_id)
    if task is not None:
        await asyncio.wait_for(asyncio.shield(task), timeout=timeout)


def _task_row(conn: sqlite3.Connection, task_id: str) -> dict:
    return dict(conn.execute("SELECT * FROM series_tasks WHERE id=?", (task_id,)).fetchone())


# --------------------------------------------------------------------- 串行

@pytest.mark.asyncio
async def test_queue_runs_two_tasks_strictly_serially(monkeypatch) -> None:
    conn = _conn()
    _patch_conn(monkeypatch, conn)
    _seed_task(conn, "st_a", [1])
    _seed_task(conn, "st_b", [2])

    order: list[str] = []
    done_pairs: set[tuple[str, str]] = set()

    def fake_complete(stage, _conn, episode_id):
        return (stage, episode_id) in done_pairs

    async def fake_run_stage(stage, episode_id, _run_id):
        order.append(f"{episode_id}:{stage}:start")
        await asyncio.sleep(0)  # 给调度器一个切换的机会，验证不会被第二个任务插队
        order.append(f"{episode_id}:{stage}:end")
        done_pairs.add((stage, episode_id))

    monkeypatch.setattr(series_stages, "stage_is_complete", fake_complete)
    monkeypatch.setattr(series_stages, "run_stage", fake_run_stage)
    # merge_is_current 必须是 False：True 会让 enqueue 的“已完成默认跳过”判据
    # 直接把两个任务都打进 skipped，永远轮不到 runner 跑；build_series_film
    # 桩成空操作，orchestrator 走到 merge 步骤时不会真跑 ffmpeg。
    monkeypatch.setattr(merge, "merge_is_current", lambda *_a: False)
    monkeypatch.setattr(merge, "build_series_film", lambda *_a: {})

    result = await queue.enqueue("p", ["st_a", "st_b"], False)
    assert result["enqueued"] == 2
    await _await_runner("p")

    assert _task_row(conn, "st_a")["status"] == "succeeded"
    assert _task_row(conn, "st_b")["status"] == "succeeded"
    # e1 的全部 5 步必须先于 e2 的任何一步完成。
    last_e1 = max(i for i, ev in enumerate(order) if ev.startswith("e1:"))
    first_e2 = min(i for i, ev in enumerate(order) if ev.startswith("e2:"))
    assert last_e1 < first_e2


# ----------------------------------------------------------- 失败继续/连续停队

@pytest.mark.asyncio
async def test_single_task_failure_marks_failed_and_continues_to_next(monkeypatch) -> None:
    conn = _conn()
    _patch_conn(monkeypatch, conn)
    _seed_task(conn, "st_bad", [1])
    _seed_task(conn, "st_good", [2])

    done_pairs: set[tuple[str, str]] = set()
    monkeypatch.setattr(
        series_stages, "stage_is_complete", lambda stage, _c, eid: (stage, eid) in done_pairs,
    )

    async def fake_run_stage(stage, episode_id, _run_id):
        if episode_id == "e1":
            raise RuntimeError("分镜炸了")
        done_pairs.add((stage, episode_id))  # e2 每一步都真的做完

    monkeypatch.setattr(series_stages, "run_stage", fake_run_stage)
    monkeypatch.setattr(merge, "merge_is_current", lambda *_a: False)
    monkeypatch.setattr(merge, "build_series_film", lambda *_a: {})

    await queue.enqueue("p", ["st_bad", "st_good"], False)
    await _await_runner("p")

    bad = _task_row(conn, "st_bad")
    good = _task_row(conn, "st_good")
    assert bad["status"] == "failed"
    assert bad["error"]
    assert good["status"] == "succeeded"


@pytest.mark.asyncio
async def test_three_consecutive_failures_auto_pause_queue(monkeypatch) -> None:
    conn = _conn()
    _patch_conn(monkeypatch, conn)
    for i in range(1, 5):
        _seed_task(conn, f"st_{i}", [i])

    monkeypatch.setattr(series_stages, "stage_is_complete", lambda *_a: False)

    async def always_fail(_stage, _episode_id, _run_id):
        raise RuntimeError("供应商故障")

    monkeypatch.setattr(series_stages, "run_stage", always_fail)

    await queue.enqueue("p", ["st_1", "st_2", "st_3", "st_4"], False)
    await _await_runner("p")

    for i in (1, 2, 3):
        assert _task_row(conn, f"st_{i}")["status"] == "failed"
    # 第 4 个任务从没被跑到——连续 3 次失败后队列已经自动暂停。
    assert _task_row(conn, "st_4")["status"] == "queued"

    snapshot = tasks.queue_snapshot(conn, "p")
    assert snapshot["paused"] is True
    assert snapshot["stop_reason"]
    assert "连续" in snapshot["stop_reason"]


# ------------------------------------------------------- 已完成默认跳过/force

def test_enqueue_skips_already_current_film_unless_forced(monkeypatch) -> None:
    conn = _conn()
    _patch_conn(monkeypatch, conn)
    _seed_task(conn, "st_a", [1])
    monkeypatch.setattr(merge, "merge_is_current", lambda *_a: True)  # 成片未过期

    accepted, skipped = tasks.enqueue_many(conn, "p", ["st_a"], force=False)
    assert accepted == []
    assert skipped == [{"task_id": "st_a", "reason": "已完成，成片未过期"}]
    assert _task_row(conn, "st_a")["status"] == "idle"  # 不静默跳过：状态原地不动

    accepted, skipped = tasks.enqueue_many(conn, "p", ["st_a"], force=True)
    assert accepted == ["st_a"]
    assert skipped == []
    assert _task_row(conn, "st_a")["status"] == "queued"


# --------------------------------------------------------------- 暂停/取消

@pytest.mark.asyncio
async def test_pause_returns_running_task_to_queued_with_progress_kept(monkeypatch) -> None:
    conn = _conn()
    _patch_conn(monkeypatch, conn)
    _seed_task(conn, "st_a", [1])

    def fake_complete(stage, _conn, episode_id):
        return stage == "screenplay"  # 第一步已经做完，第二步还没

    block = asyncio.Event()

    async def fake_run_stage(stage, _episode_id, _run_id):
        assert stage == "storyboard"
        await block.wait()

    monkeypatch.setattr(series_stages, "stage_is_complete", fake_complete)
    monkeypatch.setattr(series_stages, "run_stage", fake_run_stage)

    await queue.enqueue("p", ["st_a"], False)
    # 让 runner 真正跑起来、卡在 storyboard 的 run_stage 上。
    for _ in range(3):
        await asyncio.sleep(0)

    row = _task_row(conn, "st_a")
    assert row["status"] == "running"

    result = await queue.pause("p")
    assert result["status"] == "paused"

    row = _task_row(conn, "st_a")
    assert row["status"] == "queued"  # 不是 idle：暂停要保留进度、原地排队
    assert row["queue_seq"] is not None
    progress = tasks.task_detail(conn, "p", "st_a")
    assert progress["episodes"][0]["stages"]["screenplay"] == "skipped"


@pytest.mark.asyncio
async def test_cancel_running_task_goes_idle_and_queue_continues(monkeypatch) -> None:
    conn = _conn()
    _patch_conn(monkeypatch, conn)
    _seed_task(conn, "st_a", [1])
    _seed_task(conn, "st_b", [2])

    monkeypatch.setattr(series_stages, "stage_is_complete", lambda *_a: False)
    block = asyncio.Event()

    async def fake_run_stage(_stage, episode_id, _run_id):
        if episode_id == "e1":
            await block.wait()
        raise RuntimeError("st_b 故意跑不完，用来确认队列真的往下推进了")

    monkeypatch.setattr(series_stages, "run_stage", fake_run_stage)

    await queue.enqueue("p", ["st_a", "st_b"], False)
    for _ in range(3):
        await asyncio.sleep(0)
    assert _task_row(conn, "st_a")["status"] == "running"

    result = await queue.cancel("p", ["st_a"])
    assert "st_a" in result["cancelled"]
    assert _task_row(conn, "st_a")["status"] == "idle"
    assert _task_row(conn, "st_a")["queue_seq"] is None

    # 取消命中正在跑的任务后，队列必须自动继续跑后面排队的 st_b——
    # 等新 runner 跑到终态（st_b 会失败，但重点是它确实被跑到了）。
    await _await_runner("p")
    assert _task_row(conn, "st_b")["status"] == "failed"


# --------------------------------------------------------------------- 恢复

@pytest.mark.asyncio
async def test_recovery_resets_running_tasks_and_cancels_legacy_runs(monkeypatch) -> None:
    conn = _conn()
    _patch_conn(monkeypatch, conn)
    _seed_task(conn, "st_a", [1])
    conn.execute("UPDATE series_tasks SET status='running' WHERE id='st_a'")
    conn.execute(
        """INSERT INTO workflow_runs(
               id,workflow_type,scope_type,scope_id,status,input_fingerprint,updated_at
           ) VALUES('legacy-run','series_film','project','p','RUNNING','fp',1)"""
    )
    conn.commit()

    restarted: list[str] = []
    monkeypatch.setattr(queue, "_ensure_runner", lambda project_id: restarted.append(project_id))

    resumed = recovery.recover_series_film_runs()

    assert _task_row(conn, "st_a")["status"] == "queued"
    legacy = dict(conn.execute("SELECT * FROM workflow_runs WHERE id='legacy-run'").fetchone())
    assert legacy["status"] == "CANCELLED"
    assert legacy["failure_code"] == "SERIES_TASK_MIGRATION"
    assert restarted == ["p"]
    assert resumed == 1


@pytest.mark.asyncio
async def test_recovery_does_not_restart_a_paused_project_queue(monkeypatch) -> None:
    conn = _conn()
    _patch_conn(monkeypatch, conn)
    _seed_task(conn, "st_a", [1])
    conn.execute("UPDATE series_tasks SET status='running' WHERE id='st_a'")
    conn.execute(
        "INSERT INTO series_queue_state(project_id,paused,stop_reason,updated_at) VALUES('p',1,'手动暂停',0)"
    )
    conn.commit()

    restarted: list[str] = []
    monkeypatch.setattr(queue, "_ensure_runner", lambda project_id: restarted.append(project_id))

    resumed = recovery.recover_series_film_runs()

    assert _task_row(conn, "st_a")["status"] == "queued"  # 仍然复位，但不重启 runner
    assert restarted == []
    assert resumed == 0


# ------------------------------------------------------------- 开跑时占用等待
@pytest.mark.asyncio
async def test_task_waits_for_busy_episode_then_continues(monkeypatch) -> None:
    """区间内某一集正被单集任务占用时，任务不判失败：等占用释放后自动继续。
    重启后平台会把上一轮的映射台运行恢复起来，占用者就是本任务自己的产出，
    判失败等于每次重启都制造一次假失败（2026-09-04 B 上实测）。"""
    conn = _conn()
    _patch_conn(monkeypatch, conn)
    _seed_task(conn, "st_busy", [1])
    busy = {"storyboard": True}
    real_active = task_registry.active
    monkeypatch.setattr(
        task_registry, "active",
        lambda kind, key: busy.get(kind, False) if key == "e1" else real_active(kind, key),
    )
    sleeps: list[float] = []

    async def fast_sleep(seconds):
        sleeps.append(seconds)
        busy["storyboard"] = False  # 第一次等待后占用释放
    monkeypatch.setattr(asyncio, "sleep", fast_sleep)  # 编排器等占用用的就是 asyncio.sleep
    ran: list[str] = []

    async def fake_run_stage(stage, _episode_id, _run_id):
        ran.append(stage)
    monkeypatch.setattr(series_stages, "stage_is_complete", lambda stage, _c, eid: stage in ran)
    monkeypatch.setattr(series_stages, "run_stage", fake_run_stage)
    monkeypatch.setattr(merge, "merge_is_current", lambda *_a: False)
    monkeypatch.setattr(merge, "build_series_film", lambda *_a: {})
    await queue.enqueue("p", ["st_busy"], False)
    await _await_runner("p")
    row = _task_row(conn, "st_busy")
    assert row["status"] == "succeeded", row["error"]
    assert sleeps and ran[0] == "screenplay"
    progress = json.loads(row["progress_json"])
    assert progress["note"] is None and progress["episodes"][0]["waiting"] is None


@pytest.mark.asyncio
async def test_child_crash_before_terminal_state_marks_task_failed(monkeypatch) -> None:
    """子任务在落终态前异常退出（如启动期 database is locked）必须落 failed，
    否则调度器会对同一条 queued 行反复重派、空转。"""
    conn = _conn()
    _patch_conn(monkeypatch, conn)
    _seed_task(conn, "st_crash", [1])

    async def boom(_project_id, _row):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(queue, "_run_one_task", boom)

    class _Rec:
        error_id = "ERR-TEST"
    import app.errors as errors_mod
    monkeypatch.setattr(errors_mod, "log_error", lambda *_a, **_k: _Rec())
    await queue.enqueue("p", ["st_crash"], False)
    await _await_runner("p")
    row = _task_row(conn, "st_crash")
    assert row["status"] == "failed" and "database is locked" in row["error"]


# --------------------------------------------------------------------- 并行
@pytest.mark.asyncio
async def test_queue_runs_tasks_concurrently_up_to_limit(monkeypatch) -> None:
    """并发度 2、三个任务：前两个必须交错运行，第三个只能在某个任务整体结束后开跑。"""
    conn = _conn()
    _patch_conn(monkeypatch, conn)
    monkeypatch.setattr(queue, "queue_concurrency", lambda: 2)
    for tid, no in (("st_a", 1), ("st_b", 2), ("st_c", 3)):
        _seed_task(conn, tid, [no])
    order: list[str] = []
    done_pairs: set[tuple[str, str]] = set()

    def fake_complete(stage, _conn, episode_id):
        return (stage, episode_id) in done_pairs

    async def fake_run_stage(stage, episode_id, _run_id):
        order.append(f"{episode_id}:{stage}:start")
        await asyncio.sleep(0.005)
        order.append(f"{episode_id}:{stage}:end")
        done_pairs.add((stage, episode_id))

    monkeypatch.setattr(series_stages, "stage_is_complete", fake_complete)
    monkeypatch.setattr(series_stages, "run_stage", fake_run_stage)
    monkeypatch.setattr(merge, "merge_is_current", lambda *_a: False)
    monkeypatch.setattr(merge, "build_series_film", lambda *_a: {})

    result = await queue.enqueue("p", ["st_a", "st_b", "st_c"], False)
    assert result["enqueued"] == 3 and result["queue"]["concurrency"] == 2
    await _await_runner("p", timeout=10.0)
    assert all(_task_row(conn, tid)["status"] == "succeeded" for tid in ("st_a", "st_b", "st_c"))
    first_e2 = min(i for i, ev in enumerate(order) if ev.startswith("e2:"))
    last_e1 = max(i for i, ev in enumerate(order) if ev.startswith("e1:"))
    assert first_e2 < last_e1, "e1/e2 必须交错（并行），不是串行"
    first_e3 = min(i for i, ev in enumerate(order) if ev.startswith("e3:"))
    last_e2 = max(i for i, ev in enumerate(order) if ev.startswith("e2:"))
    assert first_e3 > min(last_e1, last_e2), "e3 只能在某个任务整体结束后开跑（并发上限 2）"


@pytest.mark.asyncio
async def test_cancel_one_running_task_keeps_the_other_running(monkeypatch) -> None:
    conn = _conn()
    _patch_conn(monkeypatch, conn)
    monkeypatch.setattr(queue, "queue_concurrency", lambda: 2)
    _seed_task(conn, "st_a", [1])
    _seed_task(conn, "st_b", [2])
    release = asyncio.Event()
    started: set[str] = set()
    done_pairs: set[tuple[str, str]] = set()

    async def fake_run_stage(stage, episode_id, _run_id):
        started.add(episode_id)
        await release.wait()
        done_pairs.add((stage, episode_id))

    monkeypatch.setattr(series_stages, "stage_is_complete", lambda stage, _c, eid: (stage, eid) in done_pairs)
    monkeypatch.setattr(series_stages, "run_stage", fake_run_stage)
    monkeypatch.setattr(merge, "merge_is_current", lambda *_a: False)
    monkeypatch.setattr(merge, "build_series_film", lambda *_a: {})

    await queue.enqueue("p", ["st_a", "st_b"], False)
    for _ in range(50):
        await asyncio.sleep(0.01)
        if started >= {"e1", "e2"}:
            break
    assert started >= {"e1", "e2"}
    snap = queue.queue_snapshot(conn, "p")
    assert set(snap["running_task_ids"]) == {"st_a", "st_b"}

    result = await queue.cancel("p", ["st_a"])
    assert result["cancelled"] == ["st_a"]
    assert _task_row(conn, "st_a")["status"] == "idle"
    assert _task_row(conn, "st_b")["status"] == "running"
    assert task_registry.active(queue.TASK_KIND, "p")

    release.set()
    await _await_runner("p", timeout=10.0)
    assert _task_row(conn, "st_b")["status"] == "succeeded"


# ------------------------------------------------------------- runner 韧性 / 残留标记
@pytest.mark.asyncio
async def test_runner_survives_transient_db_error_and_still_runs_the_task(monkeypatch) -> None:
    conn = _conn()
    _patch_conn(monkeypatch, conn)
    _seed_task(conn, "st_a", [1])
    real_next = tasks.next_queued_task
    calls = {"n": 0}

    def flaky_next(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return real_next(*args, **kwargs)
    monkeypatch.setattr(tasks, "next_queued_task", flaky_next)

    async def no_sleep(_s):
        return None
    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    import app.errors as errors_mod
    monkeypatch.setattr(errors_mod, "log_error", lambda *_a, **_k: None)
    done_pairs: set[tuple[str, str]] = set()

    async def fake_run_stage(stage, episode_id, _run_id):
        done_pairs.add((stage, episode_id))
    monkeypatch.setattr(series_stages, "stage_is_complete", lambda stage, _c, eid: (stage, eid) in done_pairs)
    monkeypatch.setattr(series_stages, "run_stage", fake_run_stage)
    monkeypatch.setattr(merge, "merge_is_current", lambda *_a: False)
    monkeypatch.setattr(merge, "build_series_film", lambda *_a: {})
    await queue.enqueue("p", ["st_a"], False)
    await _await_runner("p")
    assert calls["n"] >= 2
    assert _task_row(conn, "st_a")["status"] == "succeeded"


def test_init_progress_resets_stale_running_markers_from_previous_process() -> None:
    conn = _conn()
    _seed_task(conn, "st_a", [1, 2])
    stale = {
        "episodes": [
            {"episode_id": "e1", "episode_no": 1, "error": None, "waiting": "第1集正被映射台的任务占用",
             "stages": {"screenplay": "done", "storyboard": "running", "confirm": "pending", "video": "pending", "final": "pending"}},
            {"episode_id": "e2", "episode_no": 2, "error": None, "waiting": None,
             "stages": {"screenplay": "running", "storyboard": "pending", "confirm": "pending", "video": "pending", "final": "pending"}},
        ],
        "current_episode_no": 1, "current_stage": "storyboard", "running_episode_nos": [1, 2], "error": None,
    }
    conn.execute("UPDATE series_tasks SET progress_json=? WHERE id='st_a'", (json.dumps(stale),))
    conn.commit()
    row = dict(conn.execute("SELECT * FROM series_tasks WHERE id='st_a'").fetchone())
    progress = queue._init_progress(conn, "p", row)
    assert progress["episodes"][0]["stages"]["storyboard"] == "pending"
    assert progress["episodes"][0]["stages"]["screenplay"] == "done"  # 已完成的不动
    assert progress["episodes"][1]["stages"]["screenplay"] == "pending"
    assert all(e["waiting"] is None for e in progress["episodes"])
