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

``ensure_tables_on_connection()`` 是 ``ensure_schema()`` 之外单独留的第二个
入口（同连接、不开新连接、不申请新锁，与 ``app/orgs/schema.py``/
``app/sso/schema.py``/``app/quota_policy/schema.py`` 同一手法），供未来任何
"接受调用方 conn"的调用点使用。本包当前 ``importer.py``/``handover.py`` 的
函数都不接受外部 conn（各自 ``get_conn()`` 自己开、且都在自己第一条语句就
调用 ``ensure_schema()``——此时连接上还没执行过任何写操作，不持有写锁，独立
连接版不会跟自己抢锁），实测未处在这条风险路径上，仍继续用
``ensure_schema()``；这里先补上同连接入口，避免以后任何新调用点复制
``ensure_schema()`` 的独立连接写法而重新踩坑。
"""
from __future__ import annotations

import sqlite3

from app import db, monitor_audit_buffer

_CREATE_STATEMENTS: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS user_import_batches (
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
    )""",
    "CREATE INDEX IF NOT EXISTS idx_user_import_batches_org ON user_import_batches(org_id)",
)

_ensured_paths: set[str] = set()


def ensure_tables_on_connection(conn: sqlite3.Connection) -> None:
    """轻量、同连接、无副作用的建表兜底——不开新连接、不申请新锁，安全用于
    调用方已持有事务的场景。见模块文档"两个入口"一段。

    逐条 ``execute``，绝不能用 ``executescript``：后者在执行前会对当前连接
    做一次隐式 COMMIT（CPython sqlite3 文档行为）。这个函数唯一的存在理由
    就是跑在调用方传入的、可能正处于 ``BEGIN IMMEDIATE`` 里的连接上——一旦
    内部用 executescript，会把调用方尚未提交的事务连同它持有的写锁一起偷
    偷放掉，是 CLAUDE.md「不得在调用方的连接上隐式提交」记录的那三次真实
    事故同一类地雷（``tests/test_schema_guard.py`` 有 AST 守卫钉死这一点）。
    """
    for statement in _CREATE_STATEMENTS:
        conn.execute(statement)
    _add_column_if_missing(conn, "users", "email", "TEXT")
    _add_column_if_missing(conn, "users", "employee_no", "TEXT")


def ensure_schema() -> None:
    """幂等建表 + ``users`` 补列；按当前 ``db.DB_PATH`` 记忆已建。独立连接
    （``db._run_write_transaction_once``），只供不在调用方事务里嵌套的入口
    使用——``ensure_tables_on_connection`` 见该函数文档。

    这条独立连接跑在自己的事务里，没有调用方的事务可毁，技术上即使用
    ``executescript`` 也不会重演 ``ensure_tables_on_connection`` 那种隐式
    COMMIT 事故；但这里仍然复用 ``ensure_tables_on_connection(conn)`` 而不是
    另起一份 ``executescript`` DDL——两处各维护一份建表语句，日后改表结构
    只改了一边，就会制造"独立连接建的表"与"调用方连接建的表"逐渐不一致的
    新隐患，安全性不需要靠两份代码互相印证。"""
    key = str(db.DB_PATH)
    if key in _ensured_paths:
        return

    def operation(conn: sqlite3.Connection) -> None:
        ensure_tables_on_connection(conn)

    try:
        db._run_write_transaction_once(operation)
    except Exception as exc:  # noqa: BLE001 建表失败留到下一次调用重试，不阻塞
        # 调用方；不能悄悄吞掉——落一条可观测记录，见 app/orgs/schema.py 同名
        # except 分支的注释，理由完全一致。
        monitor_audit_buffer.note_schema_ensure_failure(__name__, exc)
        return
    _ensured_paths.add(key)


def _add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, coltype: str) -> None:
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
    except sqlite3.OperationalError as exc:
        # 与 app/db.py::init_db() 现有做法一致：只吞"列已存在"，别的一律抛出。
        if "duplicate column name" not in str(exc).lower():
            raise
