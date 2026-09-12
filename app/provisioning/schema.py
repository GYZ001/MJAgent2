"""EP-03：``user_import_batches``/``user_invitations``/``password_history`` 三
表 + ``users``/``user_sessions`` 补列，lazy 建表（L2）。

``app/db.py`` 的 line_count 基线已零余量，新表不走它——照抄
``app/orgs/schema.py``/``app/audit/store.py`` 的手法：``ensure_schema()``
按当前 ``db.DB_PATH`` 幂等记忆；``app.provisioning.importer``/
``app.provisioning.handover``/``app.provisioning.invitations``/
``app.auth.password_policy``/``app.auth.session_policy`` 的每个读写函数各自
兜底调用一次（多数测试不经 ``app.main`` lifespan，必须靠这条兜底，与
``app/orgs/store.py`` 同一惯例）。

``password_history``（EP-03 第二阶段）DDL 也放在本文件——虽然它服务的是
``app.auth.password_policy``，但按派单要求不再新起第三个包的 schema 模块，
复用本文件已有的 lazy 建表基础设施（``ensure_tables_on_connection``/
``ensure_schema`` 两个入口、``_add_column_if_missing`` helper）；
``app.auth.password_policy``（L2）import ``app.provisioning.schema``（L2）
是同层依赖，不构成上行边。``user_invitations``（EP-03 §6 邀请链接）同理，
虽然领域逻辑在 ``app.provisioning.invitations``，但表结构与本包已有的
``user_import_batches`` 同源（都是账号生命周期台账），不必拆表结构文件。

``user_sessions`` 补列 ``revoked_reason``：会话策略（``app.auth.
session_policy``）踢人时（空闲超时/超最长时长/并发超限）落一个人类可读原因，
供下一次请求携带同一枚 token 时能看到"为什么被登出"而不是笼统的"会话失效"
（CLAUDE.md「拦住用户时必须给出路」——被动下线也要让用户看得懂）。管理员/
用户主动登出、改密强制下线等既有路径不写这一列（保持 NULL），沿用现有的
笼统提示——那些场景操作者本来就知道原因，不需要额外解释。

``user_sessions`` 再补列 ``kind``（EP-03 第二阶段第二轮，协调方裁决"交互式
会话与服务账号共用同一种凭证"是真缺口）：``'interactive'``（默认）|
``'service'``。``ALTER TABLE ... ADD COLUMN kind TEXT NOT NULL DEFAULT
'interactive'`` 由 SQLite 对既有行做常量回填，不需要额外一条 ``UPDATE``——
回填后所有既有会话行为与上一轮交付逐字一致（``app.auth.session_policy`` 的
判据全部挂在 ``kind='service'`` 这一具体条件上，不触发就是原样的
interactive 路径）。``kind`` 是否豁免空闲超时/并发上限、服务会话的显式有效
期上下限，都是 ``app.auth.session_policy`` 的判定逻辑，本文件只管建列。

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

from app import db, db_schema, monitor_audit_buffer

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
    """CREATE TABLE IF NOT EXISTS user_invitations (
        id TEXT PRIMARY KEY,
        org_id TEXT,
        username TEXT NOT NULL,
        display_name TEXT,
        email TEXT,
        team_id TEXT,
        role_id TEXT,
        token_hash TEXT NOT NULL UNIQUE,
        expires_at REAL NOT NULL,
        created_by TEXT,
        created_at REAL NOT NULL,
        accepted_at REAL,
        accepted_user_id TEXT,
        revoked_at REAL,
        revoked_by TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_user_invitations_org ON user_invitations(org_id)",
    """CREATE TABLE IF NOT EXISTS password_history (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        password_hash TEXT NOT NULL,
        changed_at REAL NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_password_history_user ON password_history(user_id, changed_at)",
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
    _add_column_if_missing(conn, "user_sessions", "revoked_reason", "TEXT")
    _add_column_if_missing(conn, "user_sessions", "kind", "TEXT NOT NULL DEFAULT 'interactive'")


def ensure_schema() -> None:
    """幂等建表 + ``users`` 补列；按当前 ``db.DB_PATH`` 记忆已建。

    调用方选错入口不再有后果（2026-09-12 原语层修复，见
    ``app.db_schema.ensure_schema_respecting_caller_transaction`` 文档）：
    ``app.db.get_conn()`` 这条线程/任务局部连接若已经处在调用方开的事务里，
    直接改走同连接的 ``ensure_tables_on_connection(那个 conn)``，不开独立
    连接、不抢锁；否则保持原有独立连接行为（``db._run_write_transaction_
    once``），供不在调用方事务里嵌套的入口使用。

    独立连接这条分支跑在自己的事务里，没有调用方的事务可毁，技术上即使用
    ``executescript`` 也不会重演 ``ensure_tables_on_connection`` 那种隐式
    COMMIT 事故；但这里仍然复用 ``ensure_tables_on_connection(conn)`` 而不是
    另起一份 ``executescript`` DDL——两处各维护一份建表语句，日后改表结构
    只改了一边，就会制造"独立连接建的表"与"调用方连接建的表"逐渐不一致的
    新隐患，安全性不需要靠两份代码互相印证。"""
    key = str(db.DB_PATH)
    if key in _ensured_paths:
        return

    def _run_independent() -> None:
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

    db_schema.ensure_schema_respecting_caller_transaction(
        db.get_conn(),
        on_caller_connection=ensure_tables_on_connection,
        run_independent=_run_independent,
    )


def _add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, coltype: str) -> None:
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
    except sqlite3.OperationalError as exc:
        # 与 app/db.py::init_db() 现有做法一致：只吞"列已存在"，别的一律抛出。
        if "duplicate column name" not in str(exc).lower():
            raise
