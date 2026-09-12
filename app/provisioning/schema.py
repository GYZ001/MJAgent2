"""EP-03 第一阶段：``user_import_batches`` 表 + ``users`` 补列，lazy 建表（L2）。

``app/db.py`` 的 line_count 基线已零余量，新表不走它——照抄
``app/orgs/schema.py``/``app/audit/store.py`` 的手法：``ensure_schema()``
按当前 ``db.DB_PATH`` 幂等记忆；``app.provisioning.importer``/
``app.provisioning.handover`` 的每个读写函数各自兜底调用一次（多数测试不经
``app.main`` lifespan，必须靠这条兜底，与 ``app/orgs/store.py`` 同一惯例）。

``users`` 新增 ``email``/``employee_no`` 两列：与 ``app/orgs/schema.py`` 给
``users``/``projects`` 加 ``org_id`` 列的手法完全一致（``_add_column_if_missing``），
不碰 ``app/db.py``。**不**新增 PRD §3 草图里的 ``disabled_at``/
``handover_to_user_id`` 两列——``users.status='disabled'`` 已经是现成的可用
判据（``app/auth/admin_api.py::update_user``），移交动作走操作审计
（``operation_audit``，见 ``app.provisioning.handover`` 里对
``app.audit.recorder.record_command`` 的调用）留痕，不必再造一列专门存最近
一次移交对象——这是与 PRD 数据模型草图不完全一致之处，见交付报告。

``user_import_batches.report_json`` 存**脱敏后**的逐行报告（初始口令位置写
占位符，不写明文）——明文只活在 ``importer.py`` 的进程内存缓存里，见该模块
文档「一次性口令」一节；这张表因此任何时候读出来都不含明文，天然满足
「库中无明文」的验收项，不需要额外的读路径脱敏。
"""
from __future__ import annotations

import sqlite3

from app import db

_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS user_import_batches (
    id TEXT PRIMARY KEY,
    org_id TEXT,
    created_by TEXT,
    created_at REAL NOT NULL,
    filename TEXT,
    encoding TEXT,
    total_rows INTEGER NOT NULL DEFAULT 0,
    create_rows INTEGER NOT NULL DEFAULT 0,
    update_rows INTEGER NOT NULL DEFAULT 0,
    skip_rows INTEGER NOT NULL DEFAULT 0,
    error_rows INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'previewing',
    plan_json TEXT,
    report_json TEXT,
    applied_at REAL
);
CREATE INDEX IF NOT EXISTS idx_user_import_batches_org ON user_import_batches(org_id);
"""

_ensured_paths: set[str] = set()


def ensure_schema() -> None:
    """幂等建表 + ``users`` 补列；按当前 ``db.DB_PATH`` 记忆已建。"""
    key = str(db.DB_PATH)
    if key in _ensured_paths:
        return

    def operation(conn: sqlite3.Connection) -> None:
        conn.executescript(_SCHEMA_DDL)
        _add_column_if_missing(conn, "users", "email", "TEXT")
        _add_column_if_missing(conn, "users", "employee_no", "TEXT")

    try:
        db._run_write_transaction_once(operation)
    except Exception:  # noqa: BLE001 建表失败留到下一次调用重试，不阻塞调用方
        return
    _ensured_paths.add(key)


def _add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, coltype: str) -> None:
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
    except sqlite3.OperationalError as exc:
        # 与 app/db.py::init_db() 现有做法一致：只吞"列已存在"，别的一律抛出。
        if "duplicate column name" not in str(exc).lower():
            raise
