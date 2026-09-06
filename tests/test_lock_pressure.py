"""SQLite 写锁争用作为机器水位信号：60 秒内 ≥5 次争用就不放新调用（2026-09-06 第 13 轮写锁风暴）。"""
from __future__ import annotations

import asyncio
import sqlite3

from app import db
from app.observability import lock_pressure, machine_watermark


def test_threshold_and_window(monkeypatch) -> None:
    monkeypatch.setattr(lock_pressure, "_events", type(lock_pressure._events)(maxlen=512))
    now = [1000.0]
    monkeypatch.setattr(lock_pressure.time, "monotonic", lambda: now[0])
    assert lock_pressure.lock_pressure_reason() is None
    for _ in range(4):
        lock_pressure.note_lock_contention()
    assert lock_pressure.lock_pressure_reason() is None
    lock_pressure.note_lock_contention()
    assert "5 次" in (lock_pressure.lock_pressure_reason() or "")
    now[0] += 61
    assert lock_pressure.lock_pressure_reason() is None  # 退潮后自动放开


def test_watermark_reports_lock_pressure(monkeypatch) -> None:
    monkeypatch.setattr(lock_pressure, "lock_pressure_reason", lambda: "SQLite 写锁争用 9 次/60s ≥ 5")
    wm = machine_watermark.Watermark(sampled_at=0.0)  # 直接走判据函数，绕开 conftest 对 throttle_reason 的全局桩与 2 秒采样缓存
    reasons = machine_watermark.MachineWatermark._reasons(wm)
    assert any("写锁争用" in r for r in reasons)


def test_write_transaction_start_failure_counts_once_per_wait(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(lock_pressure, "_events", type(lock_pressure._events)(maxlen=512))
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "locked.db")
    monkeypatch.setattr(db, "WRITE_TXN_BUSY_TIMEOUT_S", 0.05)
    db.init_db()
    holder = sqlite3.connect(str(tmp_path / "locked.db"))
    holder.execute("BEGIN IMMEDIATE")  # 另一条连接握着写锁，BEGIN IMMEDIATE 等满 busy_timeout 后失败
    try:
        try:
            asyncio.run(db.run_write_transaction(lambda conn: None, retry_delays=(0.0,)))
        except sqlite3.OperationalError:
            pass
        else:
            raise AssertionError("expected database is locked after retries")
    finally:
        holder.rollback()
    assert lock_pressure.recent_contention_count() == 2  # 首次 + 一次重试，各等满一次；run_write_transaction 不再重复计
