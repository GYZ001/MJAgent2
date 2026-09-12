"""``models`` / ``model_credentials`` 的表结构与 lazy 建表。L2（依赖 app.db）。

``app/db.py`` 的 line_count 基线已被 EP-01 的六张组织/角色表用满，两张新表不再
走「DDL 追加进 SCHEMA 串、随 ``init_db()`` 一次性执行」的路径，改由本包自己
lazy 建表——``ensure_schema()`` 按当前 ``db.DB_PATH`` 幂等记忆，``app/main.py``
的 lifespan 在 ``init_db()`` 之后显式调一次，``app/models_registry/store.py``
的每个读写函数也各自兜底调用一次（多数测试不经 lifespan，必须靠这条兜底）。
与 ``app/audit/store.py`` 同一手法，见该文件模块文档，此处不重复。

建表复用 ``app.db._run_write_transaction_once``（独立连接 + ``BEGIN IMMEDIATE``），
不在调用方持有的 ``get_conn()`` 连接上 commit——CLAUDE.md「不得在调用方的连接上
隐式提交」。
"""
from __future__ import annotations

from app import db
from app.models_registry.migration import migrate_model_credentials

_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS models (
    id TEXT PRIMARY KEY,
    org_id TEXT,
    name TEXT NOT NULL DEFAULT '',
    protocol TEXT NOT NULL DEFAULT '',
    provider_label TEXT,
    model_ref TEXT NOT NULL DEFAULT '',
    kinds_json TEXT NOT NULL DEFAULT '[]',
    base_url TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    capabilities_json TEXT NOT NULL DEFAULT '{}',
    rate_limit_json TEXT,
    notes TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    created_by TEXT
);
CREATE INDEX IF NOT EXISTS idx_models_org ON models(org_id);

-- base_url 与密文同行存（比 PRD EP-05 §3 原 schema 多一列），理由见 store.py：不能分开传。
CREATE TABLE IF NOT EXISTS model_credentials (
    model_id TEXT PRIMARY KEY,
    base_url TEXT NOT NULL DEFAULT '',
    key_ciphertext BLOB NOT NULL,
    key_nonce BLOB NOT NULL,
    key_fingerprint TEXT NOT NULL,
    rotated_at REAL NOT NULL,
    rotated_by TEXT,
    created_at REAL NOT NULL
);

-- EP-05 第二阶段。org_id 用空串 '' 表示"全局默认"而不是 SQL NULL——SQLite 的
-- UNIQUE 约束把每个 NULL 都当成互不相同的值，全部行都是 NULL 时约束形同虚设；
-- 按组织覆盖绑定是 PRD 明确写的 P1，本阶段不做，空串占位换来约束现在就生效。
CREATE TABLE IF NOT EXISTS model_bindings (
    id TEXT PRIMARY KEY,
    org_id TEXT NOT NULL DEFAULT '',
    purpose TEXT NOT NULL,
    model_id TEXT NOT NULL,
    priority INTEGER NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    params_json TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    created_by TEXT,
    UNIQUE(org_id, purpose, priority)
);
CREATE INDEX IF NOT EXISTS idx_model_bindings_purpose ON model_bindings(purpose, priority);

CREATE TABLE IF NOT EXISTS model_health (
    model_id TEXT PRIMARY KEY,
    state TEXT NOT NULL DEFAULT 'healthy',
    window_calls INTEGER NOT NULL DEFAULT 0,
    window_failures INTEGER NOT NULL DEFAULT 0,
    window_timeouts INTEGER NOT NULL DEFAULT 0,
    window_rate_limited INTEGER NOT NULL DEFAULT 0,
    p50_latency_ms INTEGER,
    p95_latency_ms INTEGER,
    opened_at REAL,
    half_open_at REAL,
    last_error_code TEXT,
    last_error_at REAL,
    updated_at REAL NOT NULL
);
"""

_ensured_paths: set[str] = set()


def ensure_schema() -> None:
    """幂等建表；按当前 ``db.DB_PATH`` 记忆已建，避免每次调用都重跑 DDL。

    ``db.DB_PATH`` 而不是 ``app.config.DB_PATH``：测试隔离下前者才是各模块
    实际用来开连接的、被 conftest 逐测试覆盖的那个绑定（见 ``app/audit/store.py``
    同名函数的说明）。
    """
    key = str(db.DB_PATH)
    if key in _ensured_paths:
        return

    def operation(conn):
        conn.executescript(_SCHEMA_DDL)

    try:
        db._run_write_transaction_once(operation)
    except Exception:  # noqa: BLE001 建表失败留到下一次调用重试，不阻塞调用方
        return
    _ensured_paths.add(key)
    # 两张表刚建好，把散落在 settings/.env 里的模型与密钥迁进来；迁移自身按
    # MIGRATION_FLAG 幂等，这里的 _ensured_paths 只保证「每个 DB_PATH 每进程
    # 最多触发一次」，不是迁移正确性的唯一保障。
    migrate_model_credentials()
    # 4 个旧 model_*_provider 设置迁成 priority=0 绑定（EP-05 第二阶段）不在这里
    # 触发——app.models_registry.binding_migration 真实依赖 app.model_registry
    # （L3，回落取第一条目要用它），本模块是 L2，模块级 import 会构成层级上行边；
    # 改由 app.models_registry.routing.resolve()（L3，同层）与 app.main 的
    # lifespan 触发，见 binding_migration.py 模块文档。
