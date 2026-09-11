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
"""
from __future__ import annotations

import sqlite3

from app import db
from app.authz import policy

ORG_DEFAULT_ID = "org_default"

_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS orgs (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL DEFAULT 'default',
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created_at REAL NOT NULL,
    created_by TEXT
);

CREATE TABLE IF NOT EXISTS teams (
    id TEXT PRIMARY KEY,
    org_id TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    created_at REAL NOT NULL,
    created_by TEXT,
    UNIQUE(org_id, name),
    FOREIGN KEY(org_id) REFERENCES orgs(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_teams_org ON teams(org_id);

CREATE TABLE IF NOT EXISTS team_members (
    team_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    role_id TEXT NOT NULL,
    created_at REAL NOT NULL,
    created_by TEXT,
    PRIMARY KEY(team_id, user_id),
    FOREIGN KEY(team_id) REFERENCES teams(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_team_members_user ON team_members(user_id);

CREATE TABLE IF NOT EXISTS roles (
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
);
CREATE INDEX IF NOT EXISTS idx_roles_org ON roles(org_id);

CREATE TABLE IF NOT EXISTS role_permissions (
    role_id TEXT NOT NULL,
    permission_key TEXT NOT NULL,
    PRIMARY KEY(role_id, permission_key),
    FOREIGN KEY(role_id) REFERENCES roles(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS project_grants (
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
);
CREATE INDEX IF NOT EXISTS idx_project_grants_subject ON project_grants(subject_type, subject_id);
"""

_ensured_paths: set[str] = set()


def ensure_schema() -> None:
    """幂等建表 + 种子 + 回填；按当前 ``db.DB_PATH`` 记忆已建。"""
    key = str(db.DB_PATH)
    if key in _ensured_paths:
        return

    def operation(conn: sqlite3.Connection) -> None:
        conn.executescript(_SCHEMA_DDL)
        _add_column_if_missing(conn, "users", "org_id", "TEXT")
        _add_column_if_missing(conn, "projects", "org_id", "TEXT")
        _seed_org_default_and_roles(conn)

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
