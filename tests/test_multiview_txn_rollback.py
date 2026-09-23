"""app.multiview 写事务安全网：异常传播时不得留下未提交事务。

背景（见派单/CLAUDE.md「Ownership Must Be Explicit」）：``ensure_character_
multiview_pack``/``ensure_scene_multiview_pack``/``regenerate_character_view``/
``regenerate_scene_view`` 全程穿插十余次 ``conn.commit()``，此前零 try/except/
rollback——upsert 写入和紧随其后的 ``conn.commit()`` 之间一旦抛异常，连接上就
留下未提交事务；调用方随后可能带着它去 await 长等待（出图信号量），写锁在此
期间不释放（2026-09-05 实测：9 个并行映射台全部 database is locked）。

本文件直接打桩让 upsert 之后的下一步抛错，断言：异常照常向外传播（不吞）、
``conn.in_transaction`` 变回 False、且半截写入没有真的留在库里。断言全部在
被测函数内部实际使用的**同一个 asyncio task-local 连接**上做（``app.db.
get_conn()`` 按 task 派发连接，测试断言代码与被测函数必须处在同一个 task 里
才能拿到同一个连接对象——见 ``_run_scene_case``/``_run_character_case`` 的
写法），不是拿测试夹具自己的线程局部连接去看一个无关对象。
"""
from __future__ import annotations

import asyncio
import base64
import logging
import sqlite3
import threading
from typing import Any

import pytest

from app import config, db, multiview
from app.evidence.txn_guard import rollback_uncommitted_on_error

ENCODED_IMAGE = base64.b64encode(b"test-image-bytes").decode("ascii")


@pytest.fixture
def asset_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "assets.db")
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "_local", threading.local())
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    db.init_db()
    yield db.get_conn(), tmp_path
    db.get_conn().close()


def _seed_project(conn, project_id: str) -> None:
    conn.execute(
        "INSERT INTO projects(id, name, status, bible_json, bible_version, created_at) "
        "VALUES(?, ?, 'bible_ready', '{}', 1, 1)",
        (project_id, project_id),
    )
    conn.commit()


def _seed_scene(conn, *, project_id: str, scene_id: str, image_path: str | None) -> None:
    conn.execute(
        """INSERT INTO scene_references(
               id, project_id, scene_name, ep_start, ep_end, scene_canonical,
               prompt, image_path, qa_json, base_scene_id, bible_version, artifact_id, created_at
           ) VALUES(?, ?, 'Courtyard', 1, NULL, 'stone courtyard at dawn', NULL, ?, NULL, NULL, 1, NULL, 1)""",
        (scene_id, project_id, image_path),
    )
    conn.commit()


def _seed_scene_view(conn, *, scene_id: str, view_role: str, image_path: str) -> None:
    conn.execute(
        """INSERT INTO scene_reference_views(
               id, scene_reference_id, view_role, camera_axis, image_path, prompt, qa_json,
               artifact_id, base_view_id, status, selected, input_fingerprint, created_at
           ) VALUES(?, ?, ?, ?, ?, 'seed prompt', NULL, NULL, NULL, 'ready', 1, 'seed-fp', 1)""",
        (f"{scene_id}_{view_role}", scene_id, view_role, view_role, image_path),
    )
    conn.commit()


def _seed_portrait(conn, *, project_id: str, portrait_id: str, image_path: str | None) -> None:
    conn.execute(
        """INSERT INTO character_portraits(
               id, project_id, character_name, ep_start, ep_end, appearance, prompt, image_path,
               base_portrait_id, bible_version, artifact_id, created_at
           ) VALUES(?, ?, 'Hero', 1, NULL, 'young hero, short black hair', NULL, ?, NULL, 1, NULL, 1)""",
        (portrait_id, project_id, image_path),
    )
    conn.commit()


def _seed_portrait_view(conn, *, portrait_id: str, view_role: str, image_path: str) -> None:
    conn.execute(
        """INSERT INTO character_portrait_views(
               id, portrait_id, view_role, framing, image_path, prompt, qa_json,
               artifact_id, base_view_id, status, selected, input_fingerprint, created_at
           ) VALUES(?, ?, ?, 'full_body', ?, 'seed prompt', NULL, NULL, NULL, 'ready', 1, 'seed-fp', 1)""",
        (f"{portrait_id}_{view_role}", portrait_id, view_role, image_path),
    )
    conn.commit()


async def _fake_generate_image(*_args: Any, **_kwargs: Any) -> dict[str, str]:
    return {"b64_json": ENCODED_IMAGE}


def _boom_after_real_write(monkeypatch, target_name: str) -> None:
    """打桩：真正执行一次 upsert（模拟"写已经落到连接上"），再抛异常（模拟
    upsert 之后、commit 之前的下一步失败）——比直接跳过写入更贴近真实故障：
    真实场景里失败的是 upsert 后面那条裸 ``conn.execute``，不是 upsert 本身。"""
    real_fn = getattr(multiview, target_name)

    def _stub(conn, **kwargs):
        real_fn(conn, **kwargs)
        raise RuntimeError("boom-after-upsert")

    monkeypatch.setattr(multiview, target_name, _stub)


def test_ensure_scene_multiview_pack_rolls_back_pending_write_on_error(
    asset_db, monkeypatch,
) -> None:
    conn, _ = asset_db
    _seed_project(conn, "proj_scene")
    _seed_scene(conn, project_id="proj_scene", scene_id="scene_1", image_path=None)
    monkeypatch.setattr(multiview, "_generate_image", _fake_generate_image)
    _boom_after_real_write(monkeypatch, "_upsert_scene_view")

    async def _run() -> None:
        task_conn = db.get_conn()
        with pytest.raises(RuntimeError, match="boom-after-upsert"):
            await multiview.ensure_scene_multiview_pack(
                project_id="proj_scene",
                scene_reference_id="scene_1",
                scene_name="Courtyard",
                scene_canonical="stone courtyard at dawn",
                visual_style="cinematic animation",
                ep_start=1,
            )
        assert task_conn.in_transaction is False
        count = task_conn.execute(
            "SELECT COUNT(*) FROM scene_reference_views "
            "WHERE scene_reference_id='scene_1' AND view_role='establishing'",
        ).fetchone()[0]
        assert count == 0

    asyncio.run(_run())


def test_ensure_character_multiview_pack_rolls_back_pending_write_on_error(
    asset_db, monkeypatch,
) -> None:
    conn, _ = asset_db
    _seed_project(conn, "proj_char")
    _seed_portrait(conn, project_id="proj_char", portrait_id="portrait_1", image_path=None)
    monkeypatch.setattr(multiview, "_generate_image", _fake_generate_image)
    _boom_after_real_write(monkeypatch, "_upsert_character_view")

    async def _run() -> None:
        task_conn = db.get_conn()
        with pytest.raises(RuntimeError, match="boom-after-upsert"):
            await multiview.ensure_character_multiview_pack(
                project_id="proj_char",
                portrait_id="portrait_1",
                character_name="Hero",
                appearance="young hero, short black hair",
                visual_style="cinematic animation",
                ep_start=1,
            )
        assert task_conn.in_transaction is False
        count = task_conn.execute(
            "SELECT COUNT(*) FROM character_portrait_views "
            "WHERE portrait_id='portrait_1' AND view_role='front_full'",
        ).fetchone()[0]
        assert count == 0

    asyncio.run(_run())


def test_rollback_uncommitted_on_error_rolls_back_and_reraises() -> None:
    """工具本身的独立单测：不经过 multiview，只验证上下文管理器自己的契约。"""
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT)")
        conn.commit()
        with pytest.raises(ValueError, match="kaboom"):
            with rollback_uncommitted_on_error(conn, where="unit_test"):
                conn.execute("INSERT INTO t(v) VALUES('x')")
                assert conn.in_transaction is True
                raise ValueError("kaboom")
        assert conn.in_transaction is False
        assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 0
    finally:
        conn.close()


def test_rollback_uncommitted_on_error_rolls_back_on_cancelled_error() -> None:
    """``asyncio.CancelledError`` 从 Python 3.8 起是 ``BaseException`` 的子类，
    不是 ``Exception`` 的子类。这四个函数跨多个 await（出图信号量、存图），用户
    取消映射台/场景库任务时，取消异常正是在某个 await 点抛出——如果安全网只接
    ``except Exception``，恰好接不住它，连接带着未提交写挂起，复现 2026-09-05
    那类「别人全部 database is locked」。"""
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT)")
        conn.commit()
        with pytest.raises(asyncio.CancelledError):
            with rollback_uncommitted_on_error(conn, where="unit_test_cancel"):
                conn.execute("INSERT INTO t(v) VALUES('x')")
                assert conn.in_transaction is True
                raise asyncio.CancelledError()
        assert conn.in_transaction is False
        assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 0
    finally:
        conn.close()


def test_rollback_uncommitted_on_error_normal_exit_preserves_pending_commit() -> None:
    """正常退出（不抛异常）不得改变调用方自己逐段 commit 的既有结构：不自动
    commit、也不动调用方尚未提交的写。"""
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT)")
        conn.commit()
        with rollback_uncommitted_on_error(conn, where="unit_test"):
            conn.execute("INSERT INTO t(v) VALUES('x')")
        assert conn.in_transaction is True
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 1
    finally:
        conn.close()


def test_rollback_uncommitted_on_error_does_not_touch_preexisting_open_transaction(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """进入时连接已经带着未提交事务（按约定不应发生）：只记告警，不代为回滚/提交。"""
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT)")
        conn.commit()
        conn.execute("INSERT INTO t(v) VALUES('caller-pending')")
        assert conn.in_transaction is True

        with caplog.at_level(logging.WARNING, logger="app.evidence.txn_guard"):
            with rollback_uncommitted_on_error(conn, where="unit_test_entry"):
                pass

        assert conn.in_transaction is True
        assert any(
            "进入时连接已带着未提交事务" in record.message for record in caplog.records
        )
        conn.rollback()
    finally:
        conn.close()


def test_regenerate_character_view_rolls_back_pending_write_on_error(
    asset_db, monkeypatch,
) -> None:
    conn, tmp_path = asset_db
    _seed_project(conn, "proj_char2")
    front_path = tmp_path / "front.jpg"
    front_path.write_bytes(b"front-bytes")
    profile_path = tmp_path / "profile.jpg"
    profile_path.write_bytes(b"profile-bytes")
    _seed_portrait(conn, project_id="proj_char2", portrait_id="portrait_2", image_path=str(front_path))
    _seed_portrait_view(conn, portrait_id="portrait_2", view_role="front_full", image_path=str(front_path))
    _seed_portrait_view(conn, portrait_id="portrait_2", view_role="profile", image_path=str(profile_path))
    monkeypatch.setattr(multiview, "_generate_image", _fake_generate_image)
    _boom_after_real_write(monkeypatch, "_upsert_character_view")

    async def _run() -> None:
        task_conn = db.get_conn()
        with pytest.raises(RuntimeError, match="boom-after-upsert"):
            await multiview.regenerate_character_view(
                project_id="proj_char2", portrait_id="portrait_2", view_role="three_quarter",
            )
        assert task_conn.in_transaction is False
        count = task_conn.execute(
            "SELECT COUNT(*) FROM character_portrait_views "
            "WHERE portrait_id='portrait_2' AND view_role='three_quarter'",
        ).fetchone()[0]
        assert count == 0

    asyncio.run(_run())


def test_regenerate_scene_view_rolls_back_pending_write_on_error(
    asset_db, monkeypatch,
) -> None:
    conn, tmp_path = asset_db
    _seed_project(conn, "proj_scene2")
    est_path = tmp_path / "establishing.jpg"
    est_path.write_bytes(b"establishing-bytes")
    _seed_scene(conn, project_id="proj_scene2", scene_id="scene_2", image_path=str(est_path))
    _seed_scene_view(conn, scene_id="scene_2", view_role="establishing", image_path=str(est_path))
    monkeypatch.setattr(multiview, "_generate_image", _fake_generate_image)
    _boom_after_real_write(monkeypatch, "_upsert_scene_view")

    async def _run() -> None:
        task_conn = db.get_conn()
        with pytest.raises(RuntimeError, match="boom-after-upsert"):
            await multiview.regenerate_scene_view(
                project_id="proj_scene2", scene_reference_id="scene_2", view_role="reverse_angle",
            )
        assert task_conn.in_transaction is False
        count = task_conn.execute(
            "SELECT COUNT(*) FROM scene_reference_views "
            "WHERE scene_reference_id='scene_2' AND view_role='reverse_angle'",
        ).fetchone()[0]
        assert count == 0

    asyncio.run(_run())
