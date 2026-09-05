"""projects.bible_json 的唯一合法写法：读-改-写 + bible_version 乐观锁（app.bible_store）。

2026-09-02《神墓》丢更新事故之后，十余处「整份读→改一处→整份写回」的写入点统一收口到
mutate_bible_json：无改动不写、版本被推进就重读重做、重试耗尽失败关闭。本文件钉住这三条。
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from app.bible_store import BIBLE_JSON_CAS_ATTEMPTS, BibleJsonConflict, mutate_bible_json


def _conn(version: int = 3) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE projects(id TEXT PRIMARY KEY, bible_json TEXT, bible_version INTEGER)")
    conn.execute(
        "INSERT INTO projects VALUES('p1', ?, ?)",
        (json.dumps({"characters": [{"name": "辰南", "ref_image_path": None}], "scenes": []}), version),
    )
    conn.commit()
    return conn


def _row(conn):
    return conn.execute("SELECT bible_json, bible_version FROM projects WHERE id='p1'").fetchone()


def test_mutation_is_written_with_version_bump() -> None:
    conn = _conn(version=3)

    def set_ref(data: dict) -> bool:
        data["characters"][0]["ref_image_path"] = "/refs/辰南.jpg"
        return True

    assert mutate_bible_json(conn, "p1", set_ref) is True
    row = _row(conn)
    assert json.loads(row["bible_json"])["characters"][0]["ref_image_path"] == "/refs/辰南.jpg"
    assert row["bible_version"] == 4


def test_no_change_means_no_write_and_no_version_bump() -> None:
    conn = _conn(version=3)
    assert mutate_bible_json(conn, "p1", lambda data: False) is False
    assert _row(conn)["bible_version"] == 3


def test_missing_project_or_empty_bible_returns_false() -> None:
    conn = _conn()
    assert mutate_bible_json(conn, "nope", lambda data: True) is False
    conn.execute("UPDATE projects SET bible_json=NULL WHERE id='p1'")
    assert mutate_bible_json(conn, "p1", lambda data: True) is False


class _RacingConn:
    """每次写入前都有「别人」先推进版本：模拟并发写者。"""

    def __init__(self, inner: sqlite3.Connection, races: int) -> None:
        self._inner, self._races, self.updates = inner, races, 0

    def commit(self) -> None:  # mutate_bible_json 写成即提交
        pass

    def execute(self, sql: str, params=()):
        if sql.lstrip().upper().startswith("UPDATE") and self.updates < self._races:
            self.updates += 1
            self._inner.execute("UPDATE projects SET bible_version=bible_version+1 WHERE id='p1'")
        return self._inner.execute(sql, params)


def test_one_lost_race_is_retried_on_fresh_data() -> None:
    real = _conn(version=1)
    racing = _RacingConn(real, races=1)
    seen_versions: list[int] = []

    def mutate(data: dict) -> bool:
        seen_versions.append(len(data.setdefault("scenes", [])))
        data["scenes"].append({"name": "神魔陵园"})
        return True

    assert mutate_bible_json(racing, "p1", mutate) is True
    row = _row(real)
    assert [s["name"] for s in json.loads(row["bible_json"])["scenes"]] == ["神魔陵园"]  # 重做在新数据上，不叠加
    assert row["bible_version"] == 3  # 别人 +1，我们 +1


def test_exhausted_retries_fail_closed_without_writing() -> None:
    real = _conn(version=1)
    racing = _RacingConn(real, races=BIBLE_JSON_CAS_ATTEMPTS)
    with pytest.raises(BibleJsonConflict, match="并发修改"):
        mutate_bible_json(racing, "p1", lambda data: data.update(x=1) or True)
    assert "x" not in json.loads(_row(real)["bible_json"])


def test_mutate_exceptions_propagate_without_write() -> None:
    conn = _conn(version=3)

    def boom(data: dict) -> bool:
        raise ValueError("角色不存在")

    with pytest.raises(ValueError, match="角色不存在"):
        mutate_bible_json(conn, "p1", boom)
    assert _row(conn)["bible_version"] == 3
