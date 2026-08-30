"""monitor_audit 独立连接写锁竞争时不再直接丢行——落本地缓冲，由
app.recovery.monitor_audit_flush_loop 定期补写回库。

回归背景：app.db.insert_monitor_audit 用独立连接 + BEGIN IMMEDIATE + timeout=0
写审计行；调用方（app.domain.common._principal_access_check）持有未提交写事务
时，这个独立连接抢不到写锁会直接失败——旧版本失败就只留一行 WARNING，行永久
丢失。这里验证：(1) 写锁竞争下审计行最终仍然落库；(2) 重放不产生重复行；
(3) 审计失败（含缓冲自身失败）不影响业务请求。

用第二条独立 sqlite3 连接模拟调用方持有的未提交写锁（app/test_db_task_
connections.py 已有的标准手法），用第三条独立连接读盘验证，不复用被测代码的
连接（CLAUDE.md「验证要有独立观察点」）。
"""
import json
import sqlite3

from app import config as app_config, db, monitor_audit_buffer


def _reset_buffer() -> None:
    path = monitor_audit_buffer._buffer_path()
    if path.exists():
        path.unlink()


def _count_audit_rows(object_id: str) -> int:
    """独立连接读盘计数，不复用被测代码用过的任何连接。"""
    conn = sqlite3.connect(app_config.DB_PATH)
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM monitor_audit WHERE object_id=?", (object_id,),
        ).fetchone()
        return row[0]
    finally:
        conn.close()


def test_audit_row_survives_write_lock_contention_via_buffer_and_flush():
    _reset_buffer()
    object_id = "proj-lock-contention"
    holder = sqlite3.connect(app_config.DB_PATH, timeout=0)
    holder.execute("BEGIN IMMEDIATE")
    try:
        # 调用方（这里用 holder 模拟）正持有未提交写事务：insert_monitor_audit
        # 的独立连接抢不到锁——但业务调用本身不能跟着炸。
        db.insert_monitor_audit(
            action="view", object_type="project", object_id=object_id,
            outcome="allowed", detail={"note": "lock contention"},
        )
    finally:
        holder.rollback()
        holder.close()

    # 直接写失败：这一刻库里还没有这行。
    assert _count_audit_rows(object_id) == 0
    # 但没有丢：本地缓冲里有这一行。
    buffered = monitor_audit_buffer._buffer_path().read_text(encoding="utf-8")
    rows = [json.loads(line) for line in buffered.splitlines() if line.strip()]
    assert any(r["object_id"] == object_id and r["action"] == "view" for r in rows)

    flushed = monitor_audit_buffer.flush()

    assert flushed == len(rows)
    assert _count_audit_rows(object_id) == 1
    # flush 成功后缓冲文件已清空，不会被重复补写。
    assert monitor_audit_buffer._buffer_path().read_text(encoding="utf-8").strip() == ""


def test_flush_replay_of_the_same_buffered_row_does_not_duplicate():
    _reset_buffer()
    object_id = "proj-replay"
    monitor_audit_buffer.append(
        "audit-fixed-id-for-replay-test", 1000.0, "view", "project", object_id,
        "allowed", "{}",
    )

    first = monitor_audit_buffer.flush()
    assert first == 1
    assert _count_audit_rows(object_id) == 1

    # 模拟"DB 已提交但文件尚未截断"崩溃后的重放：同一行（同一个 id）再次出现
    # 在缓冲里，flush 再跑一轮。
    monitor_audit_buffer.append(
        "audit-fixed-id-for-replay-test", 1000.0, "view", "project", object_id,
        "allowed", "{}",
    )
    second = monitor_audit_buffer.flush()

    assert second == 1  # 补写尝试过，但…
    assert _count_audit_rows(object_id) == 1  # …INSERT OR IGNORE 撞主键，没有变成 2。


def test_insert_monitor_audit_never_raises_even_when_buffer_also_fails(monkeypatch):
    _reset_buffer()
    monkeypatch.setattr(
        monitor_audit_buffer, "_buffer_path",
        lambda: (_ for _ in ()).throw(OSError("simulated unwritable buffer")),
    )
    holder = sqlite3.connect(app_config.DB_PATH, timeout=0)
    holder.execute("BEGIN IMMEDIATE")
    try:
        # 直接写失败（锁竞争）且缓冲兜底自身也失败——业务调用依然不能抛，
        # 这次调用没有因为 db.insert_monitor_audit 抛异常而失败就是通过。
        db.insert_monitor_audit(
            action="view", object_type="project", object_id="proj-total-failure",
            outcome="allowed",
        )
    finally:
        holder.rollback()
        holder.close()
