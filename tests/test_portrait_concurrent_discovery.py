"""定妆照并发发现/重绘的冲突消解回归测试。

取证背景（生产项目 proj_c89e1d2fa4be《顾念长安》第 1 集）：女主温念的定妆照
在后续集并行映射时丢失，character_portraits 只剩 ep_start=-1 的历史槽位覆盖
不到任何真实集号。根因之一是 ``portrait_io._complete_candidate``（现拆为
``portrait_candidate_complete._resolve_candidate_current_conflict``）在候选行
与既有当前行冲突时无日志物理 DELETE。

本文件覆盖：
  1. 两个集号几乎同时为同一角色完成定妆候选（起始集更晚的先完成）时，不发生
     物理删除，两条候选都仍在库里，每一集都仍被某条 ``ep_start>=0`` 的行覆盖。
  2. 覆盖回归守护（``_warn_if_new_coverage_gap``）在这个合法的出场顺序下不误报。
  3. ``portrait_coverage_guard`` 三个底层函数的单元行为：该告警时告警、不该
     告警时不告警，物理删除路径同理。
  4. 原有按集漂移重绘（``_refresh_portrait_on_drift``）链式行为不回归。
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading

import pytest

from app import config, db
from app.portraits.current_ref import _current_portrait_row
from app.portraits.portrait_coverage_guard import (
    _close_portrait_segment,
    _delete_portrait_segment,
    _uncovered_episode_snapshot,
    _warn_if_new_coverage_gap,
)
from app.portraits.portrait_drift import _refresh_portrait_on_drift
from app.portraits.portrait_io import _generate_discovered_character_portrait
from tests.conftest import patch_portraits_everywhere

CHARACTER_NAME = "温念"
PROJECT_ID = "proj_concurrent"


@pytest.fixture
def portrait_db(tmp_path, monkeypatch):
    """真实 schema 的隔离测试库——避免手搓的 minimal schema 漏掉列，导致
    「测试没跑到真实代码路径」这类假绿（见 CLAUDE.md「验证要有独立观察点」）。
    """
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "assets.db")
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "_local", threading.local())
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    db.init_db()
    yield db.get_conn(), tmp_path
    db.get_conn().close()


def _seed_project(conn, *, episodes: int = 10) -> None:
    bible_json = '{"characters": [], "scenes": [], "world": {"visual_style_canonical": "国风"}}'
    conn.execute(
        "INSERT INTO projects(id, name, status, bible_json, bible_version, created_at) "
        "VALUES(?, ?, 'bible_ready', ?, 1, 1)",
        (PROJECT_ID, "顾念长安", bible_json),
    )
    for episode_no in range(1, episodes + 1):
        conn.execute(
            "INSERT INTO episodes(id, project_id, episode_no, title, status, created_at) "
            "VALUES(?, ?, ?, ?, 'planned', 1)",
            (f"ep{episode_no}", PROJECT_ID, episode_no, f"第{episode_no}集"),
        )
    conn.commit()


def _patch_fresh_portrait(monkeypatch, tmp_path) -> None:
    async def fake_generate_fresh_portrait(project_id, name, style, appearance, *, ep_start):
        # 真正交出一次控制权：两个并发任务都要先各自跑到这里再各自插入占位行，
        # 不能让创建顺序在前的任务一路同步跑到底、另一个永远抢不到执行机会
        # （见 asyncio 协作式调度：不挂起的 await 不会让出控制权）。
        await asyncio.sleep(0)
        path = tmp_path / f"{name}-ep{ep_start}.jpg"
        path.write_bytes(b"\xff\xd8\xff\xe0fake")
        return str(path), f"prompt-ep{ep_start}"

    patch_portraits_everywhere(monkeypatch, "_generate_fresh_portrait", fake_generate_fresh_portrait)


def _patch_record_reference_asset(monkeypatch) -> None:
    """真库自带 artifacts 表，_generate_discovered_character_portrait /
    _refresh_portrait_on_drift 都会走到 record_reference_asset；真实实现会
    对落盘文件做图片格式校验，测试写的假 jpg 过不了，直接打桩成「技术校验
    通过」，只验证本文件关心的分段冲突消解，不重复验证图片校验本身。"""

    def fake_record_reference_asset(*, asset_type, scope_id, file_path, content, **_kwargs):
        return {
            "id": f"artifact_{scope_id}", "type": asset_type, "scope_id": scope_id,
            "status": "approved", "file_path": file_path,
        }

    patch_portraits_everywhere(monkeypatch, "record_reference_asset", fake_record_reference_asset)


def _rows_by_ep_start(conn) -> dict:
    return {
        row["ep_start"]: row
        for row in conn.execute(
            "SELECT * FROM character_portraits WHERE project_id=? AND character_name=?",
            (PROJECT_ID, CHARACTER_NAME),
        ).fetchall()
    }


# ---------- 1. 端到端：起始集更晚的候选先完成 ----------

def test_out_of_order_discovery_candidates_do_not_delete_each_other(portrait_db, monkeypatch, caplog) -> None:
    """ep_start=5 的候选先完成、成为当前行；ep_start=3 的候选后完成，不该顶掉
    它，也不该被删除——退化为覆盖 [3,4] 的历史分段。"""
    conn, tmp_path = portrait_db
    _seed_project(conn)
    _patch_fresh_portrait(monkeypatch, tmp_path)
    _patch_record_reference_asset(monkeypatch)

    five_done = asyncio.Event()

    async def fake_pack(**kwargs):
        ep_start = kwargs["ep_start"]
        if ep_start == 3:
            await five_done.wait()  # 逼真正的乱序：3 先起步，5 先落地
        result = {"status": "ready", "portrait_id": kwargs["portrait_id"]}
        if ep_start == 5:
            five_done.set()
        return result

    monkeypatch.setattr("app.multiview.ensure_character_multiview_pack", fake_pack)
    caplog.set_level(logging.WARNING)

    async def run_both():
        task_three = asyncio.create_task(_generate_discovered_character_portrait(
            PROJECT_ID, CHARACTER_NAME, "国风", "外观锚点：淡绿襦裙", ep_start=3, bible_version=1,
        ))
        task_five = asyncio.create_task(_generate_discovered_character_portrait(
            PROJECT_ID, CHARACTER_NAME, "国风", "外观锚点：淡绿襦裙", ep_start=5, bible_version=1,
        ))
        return await asyncio.gather(task_three, task_five)

    result_three, result_five = asyncio.run(run_both())

    rows = _rows_by_ep_start(conn)
    assert set(rows) == {3, 5}, rows
    # 不物理删除：两条候选行的 id 都还在库里，且与调用方拿到的 portrait_id 一致。
    assert rows[3]["id"] == result_three["portrait_id"]
    assert rows[5]["id"] == result_five["portrait_id"]
    assert rows[5]["ep_end"] is None  # 先完成者仍是当前（开区间）行
    assert rows[3]["ep_end"] == 4  # 后完成者退化为历史分段，覆盖 [3,4]
    assert rows[3]["pack_status"] == "ready"  # 仍是完整可用的包，不是失败态

    # 每一集都仍被某条 ep_start>=0 的有效行覆盖，没有断档。
    for episode_no in range(3, 10):
        assert _current_portrait_row(PROJECT_ID, CHARACTER_NAME, episode_no, conn=conn) is not None, episode_no

    # 这是一次合法的出场顺序（不是真正的数据丢失），覆盖回归守护不应误报。
    coverage_warnings = [
        r.getMessage() for r in caplog.records
        if r.levelno >= logging.WARNING and ("覆盖" in r.getMessage())
    ]
    assert coverage_warnings == [], coverage_warnings


def test_out_of_order_reverse_order_also_keeps_both_rows(portrait_db, monkeypatch, caplog) -> None:
    """反过来：ep_start=3 先完成、成为当前行；ep_start=5 后完成时该正常接管
    （原有语义：更晚起始集的候选合法地收窄更早的当前行，不受本次修复影响）。

    两个协程真正并发发起（``asyncio.gather``）：``_patch_fresh_portrait`` 里的
    ``asyncio.sleep(0)`` 保证两边都先各自跑到「候选行已插入、尚未成为当前行」
    这一步，创建顺序在前的 ep_start=3 任务随后自然先完成、先成为当前行，
    ep_start=5 任务后完成时走到的正是原有「更晚起始集收窄更早当前行」分支。
    """
    conn, tmp_path = portrait_db
    _seed_project(conn)
    _patch_fresh_portrait(monkeypatch, tmp_path)
    _patch_record_reference_asset(monkeypatch)
    monkeypatch.setattr(
        "app.multiview.ensure_character_multiview_pack",
        lambda **kwargs: _ready_pack(kwargs),
    )
    caplog.set_level(logging.WARNING)

    async def run_both():
        task_three = asyncio.create_task(_generate_discovered_character_portrait(
            PROJECT_ID, CHARACTER_NAME, "国风", "外观锚点：淡绿襦裙", ep_start=3, bible_version=1,
        ))
        task_five = asyncio.create_task(_generate_discovered_character_portrait(
            PROJECT_ID, CHARACTER_NAME, "国风", "外观锚点：淡绿襦裙", ep_start=5, bible_version=1,
        ))
        return await asyncio.gather(task_three, task_five)

    result_three, result_five = asyncio.run(run_both())

    rows = _rows_by_ep_start(conn)
    assert set(rows) == {3, 5}, rows
    assert rows[3]["id"] == result_three["portrait_id"]
    assert rows[5]["id"] == result_five["portrait_id"]
    assert rows[3]["ep_end"] == 4  # 被更晚起始集的候选收窄
    assert rows[5]["ep_end"] is None
    for episode_no in range(3, 10):
        assert _current_portrait_row(PROJECT_ID, CHARACTER_NAME, episode_no, conn=conn) is not None, episode_no
    coverage_warnings = [
        r.getMessage() for r in caplog.records
        if r.levelno >= logging.WARNING and ("覆盖" in r.getMessage())
    ]
    assert coverage_warnings == [], coverage_warnings


async def _ready_pack(kwargs: dict) -> dict:
    return {"status": "ready", "portrait_id": kwargs["portrait_id"]}


# ---------- 2. 原有 drift 行为不回归 ----------

def test_drift_refresh_chain_reproduces_original_behavior(portrait_db, monkeypatch, caplog) -> None:
    """连续两次按集漂移重绘（ep7 再 ep9）仍正确收窄旧区间、开放新区间；
    正常路径不产生任何覆盖回归 WARNING。"""
    conn, tmp_path = portrait_db
    _seed_project(conn)
    _patch_fresh_portrait(monkeypatch, tmp_path)
    _patch_record_reference_asset(monkeypatch)

    async def fake_redraw_portrait(project_id, name, style, appearance, *, base_path, ep_start):
        path = tmp_path / f"{name}-drift-ep{ep_start}.jpg"
        path.write_bytes(b"\xff\xd8\xff\xe0fake-drift")
        return str(path), f"drift-prompt-ep{ep_start}"

    patch_portraits_everywhere(monkeypatch, "_redraw_portrait", fake_redraw_portrait)
    monkeypatch.setattr(
        "app.multiview.ensure_character_multiview_pack",
        lambda **kwargs: _ready_pack(kwargs),
    )

    initial = asyncio.run(_generate_discovered_character_portrait(
        PROJECT_ID, CHARACTER_NAME, "国风", "外观锚点：淡绿襦裙", ep_start=1, bible_version=1,
    ))
    assert initial["portrait_id"]

    caplog.set_level(logging.WARNING)

    async def run_chain():
        first = await _refresh_portrait_on_drift(
            PROJECT_ID, CHARACTER_NAME, 7, "外观锚点：藏青斗篷", "国风", 1,
            change_meta={"persistence": "persistent", "reason": "换装"},
        )
        second = await _refresh_portrait_on_drift(
            PROJECT_ID, CHARACTER_NAME, 9, "外观锚点：白狐裘", "国风", 1,
            change_meta={"persistence": "persistent", "reason": "再换装"},
        )
        return first, second

    result_ep7, result_ep9 = asyncio.run(run_chain())

    assert result_ep7 is not None and result_ep7["ep_start"] == 7
    assert result_ep9 is not None and result_ep9["ep_start"] == 9

    rows = _rows_by_ep_start(conn)
    assert set(rows) == {1, 7, 9}, rows
    assert rows[1]["ep_end"] == 6
    assert rows[7]["ep_end"] == 8
    assert rows[9]["ep_end"] is None

    for episode_no in range(1, 10):
        assert _current_portrait_row(PROJECT_ID, CHARACTER_NAME, episode_no, conn=conn) is not None, episode_no

    warning_records = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert warning_records == [], warning_records


# ---------- 3. portrait_coverage_guard 底层单元行为 ----------

def _guard_conn(tmp_path) -> sqlite3.Connection:
    """``_current_portrait_row``（``_uncovered_episode_snapshot`` 依赖它）额外要求
    ``image_path`` 指向一个真实存在的文件，否则命中行也判"未覆盖"——这里落一个
    真文件，避免测试自己踩中该判据本该拦的那条路。"""
    image = tmp_path / "row1.jpg"
    image.write_bytes(b"\xff\xd8\xff\xe0fake")
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE character_portraits(id TEXT PRIMARY KEY, project_id TEXT, character_name TEXT, "
        "ep_start INTEGER, ep_end INTEGER, pack_status TEXT, image_path TEXT)"
    )
    conn.execute("CREATE TABLE episodes(project_id TEXT, episode_no INTEGER)")
    conn.execute(
        "INSERT INTO character_portraits VALUES('row1','p1','角色甲',3,NULL,'ready',?)",
        (str(image),),
    )
    for episode_no in range(1, 6):
        conn.execute("INSERT INTO episodes VALUES('p1', ?)", (episode_no,))
    conn.commit()
    return conn


def test_close_portrait_segment_warns_when_row_stops_covering_anything(caplog, tmp_path) -> None:
    conn = _guard_conn(tmp_path)
    caplog.set_level(logging.WARNING)
    # 把唯一一条开区间行收窄到 ep_end < ep_start：不再覆盖任何集。
    _close_portrait_segment(
        conn, character_name="角色甲", portrait_id="row1", before_ep_start=3, before_ep_end=None,
        new_ep_end=2, caller_episode_no=3, reason="test_forced_gap")
    row = conn.execute("SELECT ep_end FROM character_portraits WHERE id='row1'").fetchone()
    assert row["ep_end"] == 2  # 写入照常发生，守护只记录不拦截
    messages = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("不再覆盖任何集" in m for m in messages), messages


def test_close_portrait_segment_silent_when_coverage_remains(caplog, tmp_path) -> None:
    conn = _guard_conn(tmp_path)
    caplog.set_level(logging.WARNING)
    _close_portrait_segment(
        conn, character_name="角色甲", portrait_id="row1", before_ep_start=3, before_ep_end=None,
        new_ep_end=4, caller_episode_no=5, reason="test_normal_close")
    messages = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert messages == [], messages


def test_delete_portrait_segment_silent_when_already_invalid(caplog, tmp_path) -> None:
    conn = _guard_conn(tmp_path)
    conn.execute("UPDATE character_portraits SET ep_end=1 WHERE id='row1'")  # ep_end<ep_start，早已无效
    conn.commit()
    caplog.set_level(logging.WARNING)
    _delete_portrait_segment(
        conn, character_name="角色甲", portrait_id="row1", before_ep_start=3, before_ep_end=1,
        caller_episode_no=3, reason="test_stale_cleanup")
    assert conn.execute("SELECT 1 FROM character_portraits WHERE id='row1'").fetchone() is None
    messages = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert messages == [], messages


def test_delete_portrait_segment_warns_when_row_was_valid(caplog, tmp_path) -> None:
    conn = _guard_conn(tmp_path)
    caplog.set_level(logging.WARNING)
    _delete_portrait_segment(
        conn, character_name="角色甲", portrait_id="row1", before_ep_start=3, before_ep_end=None,
        caller_episode_no=3, reason="test_bad_delete")
    assert conn.execute("SELECT 1 FROM character_portraits WHERE id='row1'").fetchone() is None
    messages = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("不再覆盖任何集" in m for m in messages), messages


def test_warn_if_new_coverage_gap_fires_only_on_regression(caplog, tmp_path) -> None:
    conn = _guard_conn(tmp_path)
    before = _uncovered_episode_snapshot(conn, "p1", "角色甲")
    assert before == {1, 2}  # row1 从 ep_start=3 起开放，1、2 集本来就没有覆盖

    caplog.set_level(logging.WARNING)
    conn.execute("UPDATE character_portraits SET ep_end=2 WHERE id='row1'")  # 人为制造回归：3~5 集也丢了覆盖
    conn.commit()
    _warn_if_new_coverage_gap(
        conn, project_id="p1", character_name="角色甲", caller_episode_no=3, before_uncovered=before)
    messages = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("覆盖出现回退" in m for m in messages), messages

    caplog.clear()
    _warn_if_new_coverage_gap(  # 再跑一次：这次集合没有变化（1、2 一直没覆盖），不应重复告警
        conn, project_id="p1", character_name="角色甲", caller_episode_no=3,
        before_uncovered=_uncovered_episode_snapshot(conn, "p1", "角色甲"))
    messages = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert messages == [], messages
