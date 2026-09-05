"""清库脚本的项目级状态复位必须与真实 schema 兼容（2026-09-05 refs_resume NOT NULL 让整次重置中断）。"""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

from app import db as db_mod


def _load_reset_module():
    spec = importlib.util.spec_from_file_location(
        "reset_project_episodes", Path(__file__).resolve().parents[1] / "scripts" / "reset_project_episodes.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_project_status_reset_applies_on_real_schema():
    reset = _load_reset_module()
    conn = sqlite3.connect(":memory:")
    conn.executescript(db_mod.SCHEMA)
    for statement in db_mod.MIGRATIONS:
        try:
            conn.execute(statement)
        except sqlite3.OperationalError:
            pass
    conn.execute("INSERT INTO projects(id,name,created_at) VALUES('p','P',0)")
    conn.execute("UPDATE projects SET refs_batch_started_at=123.0, refs_status='failed', refs_resume=1 WHERE id='p'")
    sets = ", ".join(f"{k}=?" for k in reset.PROJECT_STATUS_RESET)
    conn.execute(f"UPDATE projects SET {sets} WHERE id=?", (*reset.PROJECT_STATUS_RESET.values(), "p"))
    row = conn.execute("SELECT refs_batch_started_at, refs_status, refs_resume FROM projects WHERE id='p'").fetchone()
    assert row[0] is None and row[1] == "idle"
    conn.execute("INSERT INTO provider_calls(ts,kind,model,status,http_status,latency_ms,project_id) VALUES(1,'image_generate','m','OK',200,1,'p')")
    conn.execute(reset.PROVIDER_CALLS_UNREUSABLE_SQL, ("p",))
    assert conn.execute("SELECT recovery_disposition FROM provider_calls").fetchone()[0] == "RESET_PURGED"
