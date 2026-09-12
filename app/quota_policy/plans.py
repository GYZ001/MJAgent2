"""quota_plans 读写（L2，见 app/LAYERS.toml::app.quota_policy）。

只做 SQL 读写 + 最小结构校验，零业务判定（三级取最紧判定在 allocation.py）。
每个函数的 ``conn: sqlite3.Connection`` 必填，事务边界由调用方决定（不在这里
``commit()``），与 ``app/orgs/store.py`` 同一惯例。``limits_json`` 只允许
``DIMENSIONS`` 里的键（多一个不认识的键、少一个都不报错——少的键语义是"继承
上级"，见 allocation.py 的合并算法），值只能是 ``None`` 或非负数。
"""
from __future__ import annotations

import json
import sqlite3

from fastapi import HTTPException

from app.db import new_id, now
from app.quota_policy import schema
from app.quota_policy.schema import DIMENSIONS as DIMENSIONS


def validate_limits_payload(payload: dict) -> dict[str, float | int | None]:
    """校验并规整一份 ``limits_json`` 候选值：只保留 ``DIMENSIONS`` 里出现的
    键（未出现的键＝调用方明确不设置这一维，交由 allocation.py 当"缺键继承
    上级"处理，与直接传 ``None``——"这一维显式不限"——是两码事，见该模块
    docstring）；非法值（非数字、非 None、负数）422。"""
    if not isinstance(payload, dict):
        raise HTTPException(422, "limits 必须是对象")
    result: dict[str, float | int | None] = {}
    for dim in DIMENSIONS:
        if dim not in payload:
            continue
        value = payload[dim]
        if value is None:
            result[dim] = None
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise HTTPException(422, f"limits.{dim} 必须是数字或 null（不限），收到 {value!r}")
        if value < 0:
            raise HTTPException(422, f"limits.{dim} 不能为负数，收到 {value!r}")
        result[dim] = value
    return result


def create_plan(
    conn: sqlite3.Connection, *, org_id: str, key: str, name: str,
    limits: dict[str, float | int | None], period_days: int, created_by: str,
) -> str:
    schema.ensure_schema()
    existing = conn.execute(
        "SELECT id FROM quota_plans WHERE org_id=? AND key=?", (org_id, key),
    ).fetchone()
    if existing is not None:
        raise HTTPException(409, f"本组织已存在同名策略 key={key!r}")
    plan_id = new_id("qp")
    ts = now()
    conn.execute(
        "INSERT INTO quota_plans(id, org_id, key, name, builtin, period_days, "
        "limits_json, created_at, created_by) VALUES(?,?,?,?,0,?,?,?,?)",
        (plan_id, org_id, key, name, period_days, json.dumps(limits, ensure_ascii=False), ts, created_by),
    )
    return plan_id


def get_plan(conn: sqlite3.Connection, plan_id: str) -> dict | None:
    schema.ensure_schema()
    row = conn.execute("SELECT * FROM quota_plans WHERE id=?", (plan_id,)).fetchone()
    if row is None:
        return None
    return _plan_view(row)


def list_plans(conn: sqlite3.Connection, org_id: str) -> list[dict]:
    """本组织自定义策略 + 全局内置五档模板（``org_id IS NULL``），与
    ``app.orgs.store.list_roles`` 的"自定义 + 全局内置"口径一致。"""
    schema.ensure_schema()
    rows = conn.execute(
        "SELECT * FROM quota_plans WHERE org_id=? OR org_id IS NULL "
        "ORDER BY builtin DESC, created_at", (org_id,),
    ).fetchall()
    return [_plan_view(r) for r in rows]


def _plan_view(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"], "org_id": row["org_id"], "key": row["key"], "name": row["name"],
        "builtin": bool(row["builtin"]), "period_days": row["period_days"],
        "limits": json.loads(row["limits_json"]),
        "created_at": row["created_at"], "updated_at": row["updated_at"],
    }
