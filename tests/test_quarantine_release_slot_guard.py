"""隔离版本放行必须与 ``uq_versions_active_video_shot`` 同判据；reconcile 抛错时先回滚。

2026-09-02 计算服务器启动即整进程 ``database is locked`` 的两个根因，各守一条：
- 本镜有别的版本占着视频槽位（新一轮生成在排队/运行）时，放行旧隔离版会撞唯一索引；
- ``reconcile_stalled_video_jobs`` 在常驻连接上开着写事务时抛错，没回滚就把写锁留给了
  整个进程（错误记录走独立连接、``timeout=0``，先抢不到锁，随后所有写入者跟着卡死）。
"""
from __future__ import annotations

import sqlite3

import pytest

from app import db
from app.media_exec import job_recovery, quarantine_release
from tests.test_video_stall_recovery import _conn as _base_conn, _seed


def _conn() -> sqlite3.Connection:
    """复用 stall-recovery 测试的建库+种子（projects/episodes/shots s1），再叠上
    INTEGRITY_SCHEMA——槽位唯一索引与 shot_versions.shot_id 引用触发器都在那里，
    不在 SCHEMA/MIGRATIONS；缺了它们验证不到冲突。"""
    conn = _base_conn()
    _seed(conn)
    conn.executescript(db.INTEGRITY_SCHEMA)
    return conn


def _insert_version(conn, *, vid, shot_id, no, status, slot, video_path, created_at):
    conn.execute(
        """INSERT INTO shot_versions(id,shot_id,version_no,prompt_text,idem_key,status,
                                     video_slot_active,video_path,created_at)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        (vid, shot_id, no, "p", f"idem-{vid}", status, slot, video_path, created_at),
    )


def test_release_skips_shot_whose_slot_is_held_by_newer_version(tmp_path):
    conn = _conn()
    assert conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name='uq_versions_active_video_shot'"
    ).fetchone() is not None, "测试库必须带真实的槽位唯一索引，否则验证不到冲突"
    video = tmp_path / "v1.mp4"
    video.write_bytes(b"x")
    _insert_version(conn, vid="v1", shot_id="s1", no=1, status="quarantined", slot=0,
                    video_path=str(video), created_at=1.0)
    _insert_version(conn, vid="v2", shot_id="s1", no=2, status="queued", slot=1,
                    video_path=None, created_at=2.0)
    # 修复前：UPDATE 撞 uq_versions_active_video_shot → sqlite3.IntegrityError
    assert quarantine_release.release_orphan_quarantined_versions(conn, 50) == 0
    row = conn.execute("SELECT status, video_slot_active FROM shot_versions WHERE id='v1'").fetchone()
    assert (row["status"], row["video_slot_active"]) == ("quarantined", 0)


def test_release_still_frees_truly_orphaned_shot(tmp_path):
    conn = _conn()
    video = tmp_path / "v1.mp4"
    video.write_bytes(b"x")
    _insert_version(conn, vid="v1", shot_id="s1", no=1, status="quarantined", slot=0,
                    video_path=str(video), created_at=1.0)
    assert quarantine_release.release_orphan_quarantined_versions(conn, 50) == 1
    row = conn.execute("SELECT status, video_slot_active FROM shot_versions WHERE id='v1'").fetchone()
    assert (row["status"], row["video_slot_active"]) == ("succeeded", 1)


def test_reconcile_rolls_back_before_raising(monkeypatch):
    def explode(conn, limit):
        conn.execute("INSERT INTO settings(key,value) VALUES('__rollback_probe__','1')")
        raise RuntimeError("boom")

    monkeypatch.setattr(quarantine_release, "release_orphan_quarantined_versions", explode)
    with pytest.raises(RuntimeError, match="boom"):
        job_recovery.reconcile_stalled_video_jobs()
    conn = db.get_conn()
    assert conn.in_transaction is False
    assert conn.execute("SELECT 1 FROM settings WHERE key='__rollback_probe__'").fetchone() is None
    db_file = conn.execute("PRAGMA database_list").fetchone()[2]
    other = sqlite3.connect(db_file, timeout=0)
    try:
        other.execute("BEGIN IMMEDIATE")  # 修复前：database is locked
        other.execute("ROLLBACK")
    finally:
        other.close()
