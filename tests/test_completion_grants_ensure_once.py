"""completion_grants 的建表/补列/清理迁移一个库文件每进程只跑一次（2026-09-06 第 13 轮写锁风暴第二来源）。"""
from __future__ import annotations

import sqlite3

from app.completion_grant import grants_issue


def test_file_database_migrates_once_per_process(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(grants_issue, "_ENSURED_DB_PATHS", set())
    conn = sqlite3.connect(str(tmp_path / "a.db"))
    grants_issue.ensure_completion_grants_table(conn)
    conn.execute(
        "INSERT INTO completion_grants(id, episode_id, project_id, screenplay_artifact_id, permission, token_hash, "
        "issued_by, issued_at, expires_at, kind) VALUES('g1','e','p','s','storyboard.generate_and_confirm','h','u',0,0,'storyboard')"
    )
    conn.commit()
    grants_issue.ensure_completion_grants_table(conn)  # 第二次不再执行清理 DELETE
    assert conn.execute("SELECT COUNT(*) FROM completion_grants").fetchone()[0] == 1
    other = sqlite3.connect(str(tmp_path / "b.db"))
    grants_issue.ensure_completion_grants_table(other)  # 另一个库文件照常建表
    assert other.execute("SELECT COUNT(*) FROM completion_grants").fetchone()[0] == 0


def test_memory_database_is_never_cached(monkeypatch) -> None:
    monkeypatch.setattr(grants_issue, "_ENSURED_DB_PATHS", set())
    for _ in range(2):
        conn = sqlite3.connect(":memory:")
        grants_issue.ensure_completion_grants_table(conn)
        assert conn.execute("SELECT COUNT(*) FROM completion_grants").fetchone()[0] == 0
    assert grants_issue._ENSURED_DB_PATHS == set()
