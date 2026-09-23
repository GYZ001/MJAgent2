"""回归：项目彻底清理与重新分集的同步重活必须离开事件循环线程执行。

背景：2026-09-15 成片合成焊在事件循环线程上把整个后端冻结了 122 秒（健康检查
都超时），修复是把合成挪进 ``asyncio.to_thread``（见
``app/capabilities/handlers/delivery.py::_concatenate_in_thread``）。同类风险
在 ``app/domain/projects/lifecycle.py::_purge_project_core``（整项目 DB 删除
事务 + ``shutil.rmtree`` + 逐镜头文件删除）与 ``app/planning.py::run_regex_plan``
（``async def`` 但体内此前一次 ``await`` 都没有——协程一旦被
``task_registry.spawn`` 调度就会不间断跑在事件循环线程上）里原样存在，且后者
没人手动操作也会被 ``app.recovery.project_recycle_bin_sweep_loop`` 周期性触发。

不用计时断言——多快都证明不了「离开了事件循环线程」，只能证明「没超时」，
在并行开发共享的这台机器上尤其不可靠。改用确定性判据：打桩记录同步重活实际
跑在哪个 ``threading.Thread`` 上，与事件循环所在线程（这里用 ``asyncio.run``
在当前线程起循环，即 ``threading.main_thread()``）比较——``asyncio.to_thread``
底层用 ``ThreadPoolExecutor``，保证工作项跑在一个新派生的线程上、绝不复用调用
方所在的线程，这个判据不依赖时序、不会偶发抖动。
"""
from __future__ import annotations

import asyncio
import sqlite3
import threading

from app import config, db, planning
from app.domain.projects import lifecycle


def test_purge_project_sync_runs_off_event_loop_thread(tmp_path, monkeypatch) -> None:
    """``_purge_project_core`` 的数据库删除事务 + ``shutil.rmtree`` 必须不在
    事件循环线程上执行；连接归属说明见
    ``app/domain/projects/lifecycle.py::_purge_project_sync`` 的 docstring。"""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "purge-thread.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()
    conn = db.get_conn()
    project_root = tmp_path / "projects"
    monkeypatch.setattr(config, "PROJECTS_DIR", project_root)
    media = project_root / "p1" / "scene_refs" / "scene.jpg"
    media.parent.mkdir(parents=True)
    media.write_bytes(b"x")
    conn.execute(
        "INSERT INTO projects(id,name,status,created_at) VALUES('p1','P','planned',1)"
    )
    conn.execute("UPDATE projects SET deleted_at=? WHERE id='p1'", (db.now(),))
    conn.commit()

    observed_threads: list[threading.Thread] = []
    real_purge_sync = lifecycle._purge_project_sync

    def _observing_purge_sync(project_id: str):
        observed_threads.append(threading.current_thread())
        return real_purge_sync(project_id)

    monkeypatch.setattr(lifecycle, "_purge_project_sync", _observing_purge_sync)

    result = asyncio.run(lifecycle._purge_project_core("p1"))

    assert result["purged"] == "p1"
    assert isinstance(result["evidence_removed"], dict)
    # 确定性判据：同步重活确实跑过（len==1），且不在事件循环所在的主线程上。
    assert len(observed_threads) == 1
    assert observed_threads[0] is not threading.main_thread()

    # 独立观察点：不复用被测代码用过的任何连接，另开一条连接核实磁盘/数据库。
    verify_conn = sqlite3.connect(db.DB_PATH)
    verify_conn.row_factory = sqlite3.Row
    row = verify_conn.execute("SELECT COUNT(*) c FROM projects WHERE id='p1'").fetchone()
    assert row["c"] == 0
    verify_conn.close()
    assert not (project_root / "p1").exists()


def test_run_regex_plan_sync_runs_off_event_loop_thread(tmp_path, monkeypatch) -> None:
    """``run_regex_plan`` 整段此前是没有任何 ``await`` 的 ``async def``——协程
    一旦被 ``task_registry.spawn`` 调度就会不间断地跑在调用方所在线程（事件
    循环线程）上。现在同步重活（分集替换事务 + 旧集目录 rmtree）必须离开该
    线程；连接归属说明见 ``app/planning.py::_run_regex_plan_sync`` 的
    docstring。"""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "plan-thread.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()
    conn = db.get_conn()
    project_root = tmp_path / "projects"
    monkeypatch.setattr(planning.config, "PROJECTS_DIR", project_root)
    conn.execute(
        "INSERT INTO projects(id,name,status,plan_status,created_at) "
        "VALUES('p1','P','planned','running',1)"
    )
    conn.execute(
        "INSERT INTO chapters(project_id,idx,title,content) "
        "VALUES('p1',1,'第一章','第一章 正文')"
    )
    conn.commit()

    observed_threads: list[threading.Thread] = []
    real_sync = planning._run_regex_plan_sync

    def _observing_sync(project_id: str) -> None:
        observed_threads.append(threading.current_thread())
        real_sync(project_id)

    monkeypatch.setattr(planning, "_run_regex_plan_sync", _observing_sync)

    asyncio.run(planning.run_regex_plan("p1"))

    assert len(observed_threads) == 1
    assert observed_threads[0] is not threading.main_thread()

    verify_conn = sqlite3.connect(db.DB_PATH)
    verify_conn.row_factory = sqlite3.Row
    project_row = verify_conn.execute(
        "SELECT plan_status FROM projects WHERE id='p1'"
    ).fetchone()
    assert project_row["plan_status"] == "ready"
    ep_count = verify_conn.execute(
        "SELECT COUNT(*) c FROM episodes WHERE project_id='p1'"
    ).fetchone()["c"]
    assert ep_count == 1
    verify_conn.close()
