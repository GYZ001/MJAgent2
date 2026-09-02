"""error_logs 独立连接写锁竞争时不再丢记录——落本地缓冲，由 monitor_audit_flush_loop 定期补写。

回归背景（ERR-20260902-30223f，2026-09-02）：刘备定妆包重试的真实异常要写进 error_logs 时
撞上写锁（同一分钟 46 条审计写入也因 database is locked 失败），insert_error_log 只留了一行
进程 WARNING，排障时那条异常再也找不回来。与 monitor_audit 同一套缓冲：写失败落
error_log_pending.jsonl，flush 用 id 主键 INSERT OR IGNORE 幂等重放。
"""
import json
import sqlite3

from app import config as app_config, db, monitor_audit_buffer


def _reset_buffers() -> None:
    for path in (monitor_audit_buffer._buffer_path(), monitor_audit_buffer._error_log_buffer_path()):
        if path.exists():
            path.unlink()


def _count_error_rows(error_id: str) -> int:
    conn = sqlite3.connect(app_config.DB_PATH)
    try:
        return conn.execute("SELECT COUNT(*) FROM error_logs WHERE id=?", (error_id,)).fetchone()[0]
    finally:
        conn.close()


def _insert(error_id: str) -> None:
    db.insert_error_log(
        error_id, category="system", category_label="系统", code="SYS", is_technical=True,
        http_status=None, action="retry_auto_character_portrait", context={"name": "刘备"},
        message="database is locked", traceback_text="Traceback ...", exc_type="OperationalError",
    )


def test_error_row_survives_write_lock_contention_via_buffer_and_flush():
    _reset_buffers()
    error_id = "ERR-TEST-LOCKED-001"
    holder = sqlite3.connect(app_config.DB_PATH, timeout=0)
    holder.execute("BEGIN IMMEDIATE")
    try:
        _insert(error_id)  # 调用方持锁：独立连接抢不到锁，但记日志绝不能炸
    finally:
        holder.rollback()
        holder.close()

    assert _count_error_rows(error_id) == 0
    buffered = monitor_audit_buffer._error_log_buffer_path().read_text(encoding="utf-8")
    rows = [json.loads(line) for line in buffered.splitlines() if line.strip()]
    assert any(r["id"] == error_id and r["action"] == "retry_auto_character_portrait" for r in rows)

    flushed = monitor_audit_buffer.flush()

    assert flushed == len(rows)
    assert _count_error_rows(error_id) == 1
    assert monitor_audit_buffer._error_log_buffer_path().read_text(encoding="utf-8").strip() == ""
    row = sqlite3.connect(app_config.DB_PATH).execute(
        "SELECT context_json, exc_type FROM error_logs WHERE id=?", (error_id,)
    ).fetchone()
    assert json.loads(row[0]) == {"name": "刘备"} and row[1] == "OperationalError"


def test_flush_replay_does_not_duplicate_error_rows():
    _reset_buffers()
    error_id = "ERR-TEST-LOCKED-002"
    _insert(error_id)  # 无锁竞争：直接落库
    assert _count_error_rows(error_id) == 1
    monitor_audit_buffer.append_error_log({
        "id": error_id, "ts": 1.0, "category": "system", "category_label": "系统", "code": "SYS",
        "is_technical": 1, "http_status": None, "action": "x", "context_json": "{}", "message": "m",
        "traceback": None, "exc_type": "E", "meta_json": "{}",
    })
    assert monitor_audit_buffer.flush() == 1
    assert _count_error_rows(error_id) == 1


def test_insert_error_log_never_raises_even_when_buffer_also_fails(monkeypatch):
    monkeypatch.setattr(monitor_audit_buffer, "_error_log_buffer_path", lambda: sqlite3.connect(":memory:"))
    holder = sqlite3.connect(app_config.DB_PATH, timeout=0)
    holder.execute("BEGIN IMMEDIATE")
    try:
        _insert("ERR-TEST-LOCKED-003")  # 缓冲自身也失败：依旧不抛
    finally:
        holder.rollback()
        holder.close()
