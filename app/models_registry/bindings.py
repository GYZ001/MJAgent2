"""``model_bindings`` 表读写层：purpose 的优先级链。L2（依赖 app.db，同层）。

``org_id`` 本阶段恒为空字符串 ``""``（“全局默认”哨兵值，不是 SQL NULL——SQLite
的 UNIQUE 约束把每个 NULL 都当成互不相同的值，``UNIQUE(org_id, purpose,
priority)`` 在全部行都是 NULL 时形同虚设；按组织覆盖绑定是 EP-05 PRD 明确写的
P1，本阶段不做，但用空串占位能让 UNIQUE 约束现在就生效，将来加真实 org_id 时
不必迁移这一列的语义）。

不做 kind 层面的合法性校验——那是 ``app.model_registry``（活的模型库）与
``app.models_registry.purposes``（目录/自检）的职责；这里只负责把
``(purpose, priority)`` 与 ``model_id`` 的映射原子地存下来、按优先级取出来。
"""
from __future__ import annotations

import json
from typing import Any

from app.db import get_conn, new_id, now

from app.models_registry import schema


def list_bindings(purpose: str, *, org_id: str = "", enabled_only: bool = True) -> list[dict[str, Any]]:
    """按 purpose 取绑定，按 priority 升序（0 = 主用）。"""
    schema.ensure_schema()
    sql = "SELECT * FROM model_bindings WHERE purpose=? AND org_id=?"
    if enabled_only:
        sql += " AND enabled=1"
    sql += " ORDER BY priority ASC"
    rows = get_conn().execute(sql, (str(purpose or "").strip(), org_id)).fetchall()
    return [_row_to_dict(row) for row in rows]


def get_priority_zero(purpose: str, *, org_id: str = "") -> dict[str, Any] | None:
    schema.ensure_schema()
    row = get_conn().execute(
        "SELECT * FROM model_bindings WHERE purpose=? AND org_id=? AND priority=0",
        (str(purpose or "").strip(), org_id),
    ).fetchone()
    return _row_to_dict(row) if row is not None else None


def distinct_purposes(*, org_id: str = "") -> set[str]:
    """当前已经有至少一条绑定的 purpose 集合——供目录函数并入"已知 purpose"，
    不做任何过滤，纯粹反映表里已有什么。"""
    schema.ensure_schema()
    rows = get_conn().execute(
        "SELECT DISTINCT purpose FROM model_bindings WHERE org_id=?", (org_id,)
    ).fetchall()
    return {str(row["purpose"]) for row in rows}


def upsert_binding(
    *, purpose: str, model_id: str, priority: int,
    org_id: str = "", enabled: bool = True,
    params: dict[str, Any] | None = None, created_by: str | None = None,
) -> str:
    """按 ``(org_id, purpose, priority)`` 原子 UPSERT，返回行 id——命中已有行时
    返回**那一行原有的 id**，不是这次调用生成的新 id（``INSERT ... ON CONFLICT
    DO UPDATE`` 不会改写主键列，生成的 id 在冲突路径上从未落库，直接回它会把
    调用方引向一个不存在的行）。
    """
    purpose = str(purpose or "").strip()
    model_id = str(model_id or "").strip()
    if not purpose:
        raise ValueError("purpose 不能为空")
    if not model_id:
        raise ValueError("model_id 不能为空")
    if priority < 0:
        raise ValueError("priority 不能为负")
    schema.ensure_schema()
    ts = now()
    conn = get_conn()
    row_id = new_id("mbind")
    conn.execute(
        """INSERT INTO model_bindings
               (id, org_id, purpose, model_id, priority, enabled, params_json,
                created_at, updated_at, created_by)
           VALUES(?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(org_id, purpose, priority) DO UPDATE SET
               model_id=excluded.model_id, enabled=excluded.enabled,
               params_json=excluded.params_json, updated_at=excluded.updated_at""",
        (
            row_id, org_id, purpose, model_id, int(priority), 1 if enabled else 0,
            json.dumps(params or {}, ensure_ascii=False), ts, ts, created_by,
        ),
    )
    conn.commit()
    actual = conn.execute(
        "SELECT id FROM model_bindings WHERE org_id=? AND purpose=? AND priority=?",
        (org_id, purpose, int(priority)),
    ).fetchone()
    return str(actual["id"]) if actual is not None else row_id


def delete_binding(binding_id: str) -> None:
    schema.ensure_schema()
    conn = get_conn()
    conn.execute("DELETE FROM model_bindings WHERE id=?", (str(binding_id or "").strip(),))
    conn.commit()


def _row_to_dict(row: Any) -> dict[str, Any]:
    data = dict(row)
    try:
        data["params"] = json.loads(data.pop("params_json") or "{}")
    except (TypeError, ValueError):
        data["params"] = {}
    return data
