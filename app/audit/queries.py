"""操作审计与最近活跃的只读查询——供 app/audit/api.py 与
app/auth/admin_api.py::list_users 复用。查询走 ``app.db.get_conn()``（读，
线程/任务局部连接，天然反映当前测试的 DB_PATH），不单独开只读连接。

ALL_OWNERS 依据：app/audit/api.py 的全部路由都挂 Depends(require_system_admin)（同 app/observability/api.py::system_overview 的先例——见 tests/test_project_ownership_query_guard.py 模块 docstring 里 "a system-admin-only dashboard" 这一档），本模块任何函数都不会被非系统管理员触达；SQL 文本必须直接内联在 .execute(...) 调用处（不能赋给变量），见 test_no_opaque_sql_variables_hide_projects_queries。
"""
from __future__ import annotations

import json
from typing import Any

from app import db
from app.audit import store

_DEFAULT_LIMIT = 50
_MAX_LIMIT = 200
_FACET_LIMIT = 50

_LIST_COLUMNS = (
    "a.id", "a.ts", "a.user_id", "a.username", "a.is_system_admin", "a.source",
    "a.event", "a.event_label", "a.method", "a.path", "a.project_id",
    "p.name AS project_name", "a.episode_id", "a.target", "a.outcome",
    "a.http_status", "a.error_id", "a.error_code", "a.summary",
    "a.duration_ms", "a.ip",
)
# 注意：projects JOIN 片段不得再抽成模块级常量——test_no_opaque_sql_variables_
# hide_projects_queries 会把任何提到 "FROM projects"/"JOIN projects" 的赋值都
# 判违规，不管有没有被内联使用；必须逐处直接写进 .execute(...) 调用。


def list_events(
    *, since: float | None = None, until: float | None = None, user_id: str | None = None,
    event: str | None = None, outcome: str | None = None, source: str | None = None,
    project_id: str | None = None, q: str | None = None, limit: int = _DEFAULT_LIMIT,
    cursor: str | None = None,
) -> dict[str, Any]:
    store.ensure_schema()
    limit = max(1, min(int(limit or _DEFAULT_LIMIT), _MAX_LIMIT))
    clauses: list[str] = []
    params: list[Any] = []
    _append_filters(
        clauses, params, since=since, until=until, user_id=user_id, event=event,
        outcome=outcome, source=source, project_id=project_id, q=q,
    )
    _append_cursor(clauses, params, cursor)
    where_sql = " WHERE " + " AND ".join(clauses) if clauses else ""
    params.append(limit + 1)
    rows = db.get_conn().execute(
        "SELECT " + ",".join(_LIST_COLUMNS) + " FROM operation_audit a "
        "LEFT JOIN projects p ON p.id = a.project_id "
        "-- ALL_OWNERS: app.audit.api 全部路由都挂 require_system_admin\n"
        + where_sql + " ORDER BY a.ts DESC, a.id DESC LIMIT ?",
        params,
    ).fetchall()
    items = [_row_to_item(r) for r in rows[:limit]]
    next_cursor = f"{items[-1]['ts']}:{items[-1]['id']}" if len(rows) > limit else None
    return {"items": items, "next_cursor": next_cursor, "server_time": db.now()}


def get_event(event_id: str) -> dict[str, Any] | None:
    store.ensure_schema()
    row = db.get_conn().execute(
        "SELECT " + ",".join(_LIST_COLUMNS) + ",a.user_agent,a.args_json FROM operation_audit a "
        "LEFT JOIN projects p ON p.id = a.project_id "
        "-- ALL_OWNERS: app.audit.api 全部路由都挂 require_system_admin\n"
        "WHERE a.id=?",
        (event_id,),
    ).fetchone()
    if row is None:
        return None
    item = _row_to_item(row)
    item["args"] = _parse_args_json(item.pop("args_json", None))
    return item


def facets(*, since: float | None = None, until: float | None = None) -> dict[str, Any]:
    store.ensure_schema()
    conn = db.get_conn()
    where, params = _time_range_clause(since, until)
    return {
        "events": _facet_group(conn, "a.event, a.event_label", "a.event, a.event_label", where, params),
        "users": _facet_group(
            conn, "a.user_id, a.username", "a.user_id, a.username",
            _require_not_null(where, "a.user_id"), params,
        ),
        "outcomes": _facet_group(conn, "a.outcome", "a.outcome", where, params),
        "sources": _facet_group(conn, "a.source", "a.source", where, params),
        "projects": _facet_group(
            conn, "a.project_id, p.name AS project_name", "a.project_id, p.name",
            _require_not_null(where, "a.project_id"), params,
        ),
    }


def activity_summary_by_user() -> dict[str, dict[str, Any]]:
    """每个有活跃或操作记录的用户的 ``last_active_at`` 与最新一条 ``last_action``。"""
    store.ensure_schema()
    conn = db.get_conn()
    summary: dict[str, dict[str, Any]] = {}
    for row in conn.execute("SELECT user_id, last_active_at FROM user_activity").fetchall():
        summary[row["user_id"]] = {"last_active_at": row["last_active_at"], "last_action": None}
    for row in conn.execute(
        "SELECT a.user_id, a.id, a.ts, a.event, a.event_label, a.outcome, a.project_id, "
        "p.name AS project_name FROM operation_audit a "
        "LEFT JOIN projects p ON p.id = a.project_id "
        "-- ALL_OWNERS: app.audit.api 全部路由都挂 require_system_admin\n"
        "JOIN (SELECT user_id, MAX(ts) mts FROM operation_audit WHERE user_id IS NOT NULL "
        "GROUP BY user_id) latest ON latest.user_id = a.user_id AND latest.mts = a.ts"
    ).fetchall():
        entry = summary.setdefault(row["user_id"], {"last_active_at": None, "last_action": None})
        entry["last_action"] = {
            "id": row["id"], "ts": row["ts"], "event": row["event"],
            "event_label": row["event_label"], "outcome": row["outcome"],
            "project_id": row["project_id"], "project_name": row["project_name"],
        }
    return summary


def apply_activity_summary(user_payloads: list[dict[str, Any]]) -> None:
    """原地把 last_active_at/last_action 写进用户列表 payload（供 admin_api.list_users 用）。

    ``last_active_at`` 取 ``users.last_login_at`` 与 ``user_activity.last_active_at``
    的较大值，两者都空则 None——「有历史登录，直接打开页面/发请求也算活跃」。
    """
    summary = activity_summary_by_user()
    for payload in user_payloads:
        entry = summary.get(payload["id"])
        values = [v for v in (payload.get("last_login_at"), entry and entry["last_active_at"]) if v is not None]
        payload["last_active_at"] = max(values) if values else None
        payload["last_action"] = entry["last_action"] if entry else None


def _append_filters(clauses, params, *, since, until, user_id, event, outcome, source, project_id, q) -> None:
    if since is not None:
        clauses.append("a.ts >= ?"); params.append(since)
    if until is not None:
        clauses.append("a.ts <= ?"); params.append(until)
    if user_id:
        clauses.append("a.user_id = ?"); params.append(user_id)
    if event:
        clauses.append("a.event = ?"); params.append(event)
    if outcome:
        clauses.append("a.outcome = ?"); params.append(outcome)
    if source:
        clauses.append("a.source = ?"); params.append(source)
    if project_id:
        clauses.append("a.project_id = ?"); params.append(project_id)
    if q:
        like = f"%{q}%"
        clauses.append(
            "(a.event LIKE ? OR a.event_label LIKE ? OR a.path LIKE ? OR a.target LIKE ? "
            "OR a.summary LIKE ? OR a.username LIKE ?)"
        )
        params.extend([like, like, like, like, like, like])


def _append_cursor(clauses: list[str], params: list[Any], cursor: str | None) -> None:
    parsed = _parse_cursor(cursor)
    if parsed is None:
        return
    cur_ts, cur_id = parsed
    clauses.append("(a.ts < ? OR (a.ts = ? AND a.id < ?))")
    params.extend([cur_ts, cur_ts, cur_id])


def _parse_cursor(cursor: str | None) -> tuple[float, str] | None:
    if not cursor:
        return None
    ts_str, _, id_part = cursor.partition(":")
    if not id_part:
        return None
    try:
        return float(ts_str), id_part
    except ValueError:
        return None


def _time_range_clause(since: float | None, until: float | None) -> tuple[str, list[Any]]:
    clauses = []
    params: list[Any] = []
    if since is not None:
        clauses.append("a.ts >= ?"); params.append(since)
    if until is not None:
        clauses.append("a.ts <= ?"); params.append(until)
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


def _require_not_null(where: str, column: str) -> str:
    """匿名/无归属行（user_id 或 project_id 为空）不该在对应分桶里现身——桶名
    要给人看，NULL 渲染出来是一个空白选项，不是"匿名"那类有意义的合法值。"""
    return f"{where} AND {column} IS NOT NULL" if where else f" WHERE {column} IS NOT NULL"


def _facet_group(conn, select_cols: str, group_cols: str, where: str, params: list[Any]) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(
        f"SELECT {select_cols}, COUNT(*) AS count FROM operation_audit a "
        "LEFT JOIN projects p ON p.id = a.project_id "
        "-- ALL_OWNERS: app.audit.api 全部路由都挂 require_system_admin\n"
        f"{where} GROUP BY {group_cols} ORDER BY count DESC LIMIT {_FACET_LIMIT}",
        params,
    ).fetchall()]


def _row_to_item(row: Any) -> dict[str, Any]:
    item = dict(row)
    if item.get("is_system_admin") is not None:
        item["is_system_admin"] = bool(item["is_system_admin"])
    return item


def _parse_args_json(raw: str | None) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw
