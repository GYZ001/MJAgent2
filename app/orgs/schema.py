"""EP-01 组织/团队/角色/项目授权：表结构 + 种子/回填，lazy 建表（L2）。

``app/db.py`` 的 line_count 基线已被这六张表用满过一次（已撤回），改走
``app/audit/store.py`` / ``app/models_registry/schema.py`` 同一手法——
``ensure_schema()`` 按当前 ``db.DB_PATH`` 幂等记忆；``app/main.py`` 的
lifespan 在 ``init_db()`` 之后显式调一次；``app/orgs/store.py`` 的每个
读写函数也各自兜底调用一次（多数测试不经 lifespan，必须靠这条兜底）。

建表 + 种子（org_default + 5 个内置角色元数据行）+ 回填
（``users``/``projects.org_id``）放在同一次 ``db._run_write_transaction_once``
里完成，用独立连接，不在调用方持有的 ``get_conn()`` 连接上 commit
（CLAUDE.md「不得在调用方的连接上隐式提交」）。角色→权限点的具体映射
（``role_permissions`` 的内容）依赖运行时 Command Registry，不在这里算，由
``app.orgs.bootstrap.sync_builtin_role_permissions()`` 在
``ensure_catalog_loaded()`` 之后单独同步——见该模块文档。

单租户下默认一个 org_default，回填零风险；``roles.org_id=NULL`` 表示全局
内置模板（``builtin=1``，不可删、不可改权限点）。

``ensure_tables_on_connection()`` 是 ``ensure_schema()`` 之外单独留的第二个
入口，专供 ``app.orgs.store`` 里每个"接受调用方 conn"的函数调用——这些函数
被 ``app.quota_policy.allocation.resolve_effective_limits()`` 在调用方（
``app.quota`` 的并发闸门）已经持有的 ``BEGIN IMMEDIATE`` 事务里直接调用（见
``app/quota_policy/allocation.py``），如果沿用 ``ensure_schema()`` 那样再开
一条独立连接去抢 ``BEGIN IMMEDIATE``，会跟调用方自己持有的写锁抢锁，2 秒
``WRITE_TXN_BUSY_TIMEOUT_S`` 超时后必然失败——与 ``app/quota_policy/schema.py``
同一手法、同一故障模式。``ensure_tables_on_connection()`` 复用调用方传入的
同一个 ``conn`` 执行纯建表/加列（不做 org_default/角色种子写入、不开新连接、
不申请新锁）；种子仍然只在 ``ensure_schema()``（独立连接，供 ``app.main`` 的
lifespan 与测试模板初始化调用）里做一次——见 ``tests/conftest.py::
_initialize_database_template`` 现在显式调用 ``ensure_schema()``，不再依赖
``app.orgs.store`` 的防御性调用当种子的隐式触发点。
"""
from __future__ import annotations

import sqlite3

from app import db, db_schema, monitor_audit_buffer
from app.authz import policy

ORG_DEFAULT_ID = "org_default"

_CREATE_STATEMENTS: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS orgs (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL DEFAULT 'default',
        name TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'active',
        created_at REAL NOT NULL,
        created_by TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS teams (
        id TEXT PRIMARY KEY,
        org_id TEXT NOT NULL,
        name TEXT NOT NULL,
        description TEXT,
        status TEXT NOT NULL DEFAULT 'active',
        created_at REAL NOT NULL,
        created_by TEXT,
        UNIQUE(org_id, name),
        FOREIGN KEY(org_id) REFERENCES orgs(id) ON DELETE CASCADE
    )""",
    "CREATE INDEX IF NOT EXISTS idx_teams_org ON teams(org_id)",
    """CREATE TABLE IF NOT EXISTS team_members (
        team_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        role_id TEXT NOT NULL,
        created_at REAL NOT NULL,
        created_by TEXT,
        PRIMARY KEY(team_id, user_id),
        FOREIGN KEY(team_id) REFERENCES teams(id) ON DELETE CASCADE
    )""",
    "CREATE INDEX IF NOT EXISTS idx_team_members_user ON team_members(user_id)",
    """CREATE TABLE IF NOT EXISTS roles (
        id TEXT PRIMARY KEY,
        org_id TEXT,
        key TEXT NOT NULL,
        name TEXT NOT NULL,
        description TEXT,
        builtin INTEGER NOT NULL DEFAULT 0,
        created_at REAL NOT NULL,
        created_by TEXT,
        updated_at REAL,
        UNIQUE(org_id, key)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_roles_org ON roles(org_id)",
    """CREATE TABLE IF NOT EXISTS role_permissions (
        role_id TEXT NOT NULL,
        permission_key TEXT NOT NULL,
        PRIMARY KEY(role_id, permission_key),
        FOREIGN KEY(role_id) REFERENCES roles(id) ON DELETE CASCADE
    )""",
    """CREATE TABLE IF NOT EXISTS project_grants (
        project_id TEXT NOT NULL,
        subject_type TEXT NOT NULL,
        subject_id TEXT NOT NULL,
        role_id TEXT NOT NULL,
        created_at REAL NOT NULL,
        created_by TEXT,
        expires_at REAL,
        PRIMARY KEY(project_id, subject_type, subject_id),
        FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
        FOREIGN KEY(role_id) REFERENCES roles(id)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_project_grants_subject ON project_grants(subject_type, subject_id)",
)

_ensured_paths: set[str] = set()


def ensure_tables_on_connection(conn: sqlite3.Connection) -> None:
    """轻量、同连接、无副作用的建表兜底——不开新连接、不申请新锁、不做种子
    写入，安全用于调用方已持有事务的场景。见模块文档"两个入口"一段。

    逐条 ``execute``，绝不能用 ``executescript``：后者在执行前会对当前连接
    做一次隐式 COMMIT（CPython sqlite3 文档行为）。这个函数唯一的存在理由
    就是跑在调用方传入的、可能正处于 ``BEGIN IMMEDIATE`` 里的连接上——一旦
    内部用 executescript，会把调用方尚未提交的事务连同它持有的写锁一起偷
    偷放掉。这不是假设性风险：配额并发闸门在 ``BEGIN IMMEDIATE`` 占位后经
    ``app.quota_policy.allocation`` 调用链间接触达本函数，隐式 COMMIT 把刚
    拿到的独占写锁悄悄放掉，两个并发预约都被放行
    （``tests/test_quota_concurrency_atomicity.py`` 约 20% 间歇复现）——与
    CLAUDE.md「不得在调用方的连接上隐式提交」记录的那三次真实事故同一类
    地雷（``tests/test_schema_guard.py`` 有 AST 守卫钉死这一点）。
    """
    for statement in _CREATE_STATEMENTS:
        conn.execute(statement)
    _add_column_if_missing(conn, "users", "org_id", "TEXT")
    _add_column_if_missing(conn, "projects", "org_id", "TEXT")


def ensure_schema() -> None:
    """幂等建表 + 种子 + 回填；按当前 ``db.DB_PATH`` 记忆已建。

    调用方选错入口不再有后果（2026-09-12 原语层修复，见
    ``app.db_schema.ensure_schema_respecting_caller_transaction`` 文档）：
    ``app.db.get_conn()`` 这条线程/任务局部连接若已经处在调用方开的事务里，
    直接改走同连接的 ``ensure_tables_on_connection(那个 conn)``，不开独立
    连接、不抢锁（此时不做种子/回填——那次调用只保证表/列存在，等下一次不在
    事务里的 ``ensure_schema()`` 调用补种子）；否则保持原有独立连接行为
    （``db._run_write_transaction_once``），供 ``app.main`` 启动、测试模板
    初始化这类不嵌套在别的事务里的入口使用。

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
            _seed_org_default_and_roles(conn)

        try:
            db._run_write_transaction_once(operation)
        except Exception as exc:  # noqa: BLE001 建表失败留到下一次调用重试，不阻塞
            # 调用方（并发进程/请求同时触发建表是预期状况，抛出会打断当时其实不
            # 需要这张新表的正常请求）——但不能悄悄吞掉：落一条可观测记录（本地
            # 文件缓冲，不占 SQLite 写锁，由 monitor_audit_flush_loop 之后补进
            # monitor_audit 表），下次再发生同类锁争用时能在审计里看见，而不是
            # 只看到下游一句 no such table。
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


def _seed_org_default_and_roles(conn: sqlite3.Connection) -> None:
    ts = db.now()
    conn.execute(
        "INSERT OR IGNORE INTO orgs(id, tenant_id, name, status, created_at, created_by) "
        "VALUES(?,?,?,?,?,?)",
        (ORG_DEFAULT_ID, "default", "默认组织", "active", ts, "system"),
    )
    for key, name, description in policy.BUILTIN_ROLE_TEMPLATES:
        row = conn.execute(
            "SELECT id FROM roles WHERE org_id IS NULL AND key=?", (key,)
        ).fetchone()
        if row is not None:
            continue
        conn.execute(
            "INSERT INTO roles(id, org_id, key, name, description, builtin, created_at, created_by) "
            "VALUES(?,NULL,?,?,?,1,?,?)",
            (db.new_id("role"), key, name, description, ts, "system"),
        )
    conn.execute("UPDATE users SET org_id=? WHERE org_id IS NULL", (ORG_DEFAULT_ID,))
    conn.execute("UPDATE projects SET org_id=? WHERE org_id IS NULL", (ORG_DEFAULT_ID,))
