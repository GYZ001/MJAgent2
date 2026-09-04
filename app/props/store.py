"""``prop_references`` 的表结构与读写原语（与 ``scene_references`` 同构）。

``app.db`` 扇入 254、不许再加职责（CLAUDE.md「扇入 >100 的模块不得再加职责」），
所以这张新表不进 ``app/db.py`` 的核心 schema，改由本模块自己 lazy 建表——照
``app/audit/store.py`` 的先例：``ensure_schema()`` 按当前 ``db.DB_PATH`` 幂等
记忆，DDL 走独立连接（``db._run_write_transaction_once``），不占用调用方的
业务事务。

但业务数据的读写（登记一条道具参考图、按集查询）不是审计那种"失败静默降级"
的诊断写入——它们是正常状态转移的一部分，必须显式用调用方传入的 ``conn``
（CLAUDE.md「不得在调用方的连接上隐式提交」的另一面：这里的 commit 就是这次
状态转移本身，不是借道），与 ``app.scenes.register_initial_scene_ref``/
``app.multiview.scene_row_for_episode`` 同一分工。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from app import config, db
from app.refs import _safe_name

_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS prop_references (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  prop_name TEXT NOT NULL,
  ep_start INTEGER NOT NULL,
  ep_end INTEGER,
  appearance TEXT,
  image_path TEXT,
  prompt TEXT,
  status TEXT NOT NULL,
  qa_json TEXT,
  created_at REAL NOT NULL,
  UNIQUE(project_id, prop_name, ep_start)
);
CREATE INDEX IF NOT EXISTS idx_prop_refs_proj_name
  ON prop_references(project_id, prop_name, ep_start);
"""

PROP_REFERENCE_STATUSES = ("ready", "failed", "generating")

_ensured_paths: set[str] = set()


def ensure_schema() -> None:
    """幂等建表；按当前 ``db.DB_PATH`` 记忆已建，避免每次调用都重跑 DDL。

    键上 ``db.DB_PATH`` 而不是任何静态路径：测试隔离下每个测试用例都会切到
    独立的新库（见 tests/conftest.py ``_restore_isolated_runtime``），键上它
    才能保证每个测试独占的新库都会重新建表。
    """
    key = str(db.DB_PATH)
    if key in _ensured_paths:
        return

    def operation(conn: sqlite3.Connection) -> None:
        conn.executescript(_SCHEMA_DDL)

    try:
        db._run_write_transaction_once(operation)
    except Exception:  # noqa: BLE001 建表失败留到下一次调用重试，不阻塞调用方
        return
    _ensured_paths.add(key)


def prop_ref_dir(project_id: str) -> Path:
    d = config.PROJECTS_DIR / project_id / "prop_refs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def prop_ref_path(project_id: str, prop_name: str) -> str:
    return str(prop_ref_dir(project_id) / f"{_safe_name(prop_name)}.png")


def upsert_prop_reference(
    conn: sqlite3.Connection, project_id: str, prop_name: str, episode_no: int,
    *, appearance: str, image_path: str | None, prompt: str, status: str, qa: dict[str, Any],
) -> str:
    """登记/覆盖一条道具参考图（适用集 ``episode_no`` 起，开区间）。

    覆盖式：先清掉该道具全部旧分段——道具库目前没有场景那种"跨集演化出多个
    分段区间"的需求（用户拍板：道具形态本就该稳定不漂移，变了就是新道具），
    与 ``register_initial_scene_ref`` 的覆盖式登记同一处置。
    """
    import json

    ensure_schema()
    conn.execute(
        "DELETE FROM prop_references WHERE project_id=? AND prop_name=?",
        (project_id, prop_name),
    )
    ref_id = db.new_id("prop")
    conn.execute(
        "INSERT INTO prop_references(id, project_id, prop_name, ep_start, ep_end, "
        "appearance, image_path, prompt, status, qa_json, created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            ref_id, project_id, prop_name, episode_no, None,
            appearance, image_path, prompt, status,
            json.dumps(qa, ensure_ascii=False), db.now(),
        ),
    )
    return ref_id


def prop_reference_for_episode(
    conn: sqlite3.Connection, project_id: str, name: str, episode_no: int | None,
):
    """返回覆盖该集的道具参考图行；未命中返回 None（区间语义同
    ``app.multiview.scene_row_for_episode``）。"""
    if episode_no is None:
        return None
    ensure_schema()
    try:
        return conn.execute(
            "SELECT * FROM prop_references "
            "WHERE project_id=? AND prop_name=? AND ep_start<=? AND (ep_end IS NULL OR ep_end>=?) "
            "ORDER BY ep_start DESC LIMIT 1",
            (project_id, name, episode_no, episode_no),
        ).fetchone()
    except Exception:  # noqa: BLE001 与 scene_row_for_episode 同一容错口径
        return None


def latest_prop_reference_status(conn: sqlite3.Connection, project_id: str, name: str):
    ensure_schema()
    try:
        return conn.execute(
            "SELECT * FROM prop_references WHERE project_id=? AND prop_name=? "
            "ORDER BY ep_start DESC LIMIT 1",
            (project_id, name),
        ).fetchone()
    except Exception:  # noqa: BLE001
        return None
