"""定妆包已就绪且为开区间时，自动补齐重试必须纯复用、不写库（ERR-20260902-30223f）。

《三国演义_白话文版》第一回：刘备的定妆包三视角齐全、pack_status=ready，人物谱变更记录却还停在
「资产待补」，分镜前的补齐重试因此再次调用 _generate_discovered_character_portrait。旧实现在复用
路径上仍要写 character_portraits 与 bible_json，撞上并发写锁后被外层当成「自动定妆包生成失败」
——而错误记录本身也因同一把锁没写进去。复用一份已经就绪的包不该产生任何写入。
"""
from __future__ import annotations

import asyncio
import sqlite3

from app.portraits.portrait_io import _generate_discovered_character_portrait
from tests.conftest import patch_portraits_everywhere


class _SpyConn:
    """记录所有写语句的连接包装；读语句透传。"""

    def __init__(self, inner: sqlite3.Connection) -> None:
        self._inner = inner
        self.writes: list[str] = []

    def execute(self, sql: str, params=()):
        head = sql.lstrip()[:6].upper()
        if head in {"UPDATE", "INSERT", "DELETE"}:
            self.writes.append(sql.strip().split("\n")[0][:60])
        return self._inner.execute(sql, params)

    def commit(self) -> None:
        self._inner.commit()

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _conn(tmp_path, *, ep_end):
    image = tmp_path / "刘备__candidate.jpg"
    image.write_bytes(b"jpg")
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE projects(id TEXT PRIMARY KEY, bible_json TEXT, bible_version INTEGER, bible_artifact_id TEXT)")
    conn.execute(
        "CREATE TABLE character_portraits(id TEXT PRIMARY KEY, project_id TEXT, character_name TEXT, ep_start INTEGER, "
        "ep_end INTEGER, appearance TEXT, prompt TEXT, image_path TEXT, base_portrait_id TEXT, bible_version INTEGER, "
        "artifact_id TEXT, pack_status TEXT, created_at REAL)"
    )
    conn.execute("INSERT INTO projects VALUES('p1', '{\"characters\": [], \"scenes\": [], \"world\": {}}', 3, NULL)")
    conn.execute(
        "INSERT INTO character_portraits VALUES('portrait_1','p1','刘备',1,?, '汉代束发高冠', 'prompt', ?, NULL, 3, NULL, 'ready', 1.0)",
        (ep_end, str(image)),
    )
    conn.commit()
    return _SpyConn(conn)


def test_ready_open_pack_is_reused_without_any_write(monkeypatch, tmp_path) -> None:
    spy = _conn(tmp_path, ep_end=None)
    patch_portraits_everywhere(monkeypatch, "get_conn", lambda: spy)

    result = asyncio.run(_generate_discovered_character_portrait(
        "p1", "刘备", "国漫风格", "汉代束发高冠", ep_start=1, bible_version=3,
    ))

    assert result["portrait_id"] == "portrait_1"
    assert result["pack_status"] == "ready" and result["reused"] is True
    assert spy.writes == [], spy.writes


def test_ready_but_closed_interval_pack_still_reopens(monkeypatch, tmp_path) -> None:
    """服务重启恢复形态：候选行仍是闭区间（ep_end=ep_start），必须照旧原子切换为开区间。"""
    spy = _conn(tmp_path, ep_end=1)
    patch_portraits_everywhere(monkeypatch, "get_conn", lambda: spy)

    result = asyncio.run(_generate_discovered_character_portrait(
        "p1", "刘备", "国漫风格", "汉代束发高冠", ep_start=1, bible_version=3,
    ))

    assert result["portrait_id"] == "portrait_1"
    assert any(w.startswith("UPDATE character_portraits SET ep_end=NULL") for w in spy.writes), spy.writes
    row = spy.execute("SELECT ep_end, pack_status FROM character_portraits WHERE id='portrait_1'").fetchone()
    assert row["ep_end"] is None and row["pack_status"] == "ready"
