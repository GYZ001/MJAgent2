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
