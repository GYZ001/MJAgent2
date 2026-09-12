"""EP-06 指标的 scrape 时态采集器：读现有表算 gauge，不新建统计表、不写库。

L2（见 app/LAYERS.toml）：只 import app.db（同层）与 app.quota_policy（同层），
不碰 app.models_registry.health/routing 那条"推/拉汇合点"路径——那条路径已经
在真实调用发生的那一刻把 model_health/model_failures_total/
provider_call_latency 写进了内存注册表（见 app/models_registry/health.py、
app/models_registry/routing.py 的改动），本文件不重复采集，避免同一数据源
被两条路径分别计一次。

本文件只负责两组"当下状态"型指标：
- ``manju_jobs_active`` / ``manju_jobs_queued``：workflow_runs 按 workflow_type
  分组（workflow_type 就是 PRD 里说的 "module" label）。
- ``manju_quota_usage_ratio``：quota_allocations 里显式配置过的 org/team/user
  配额，用量/上限。

两组都是"scrape 这一刻查一次库"，不是每次 HTTP 请求都查——只有 GET /metrics
被访问时才触发，且都是有索引支撑的小查询（workflow_runs 按 status 有索引，
quota_allocations 条数是"显式配置过配额的团队/用户数"量级，企业客户场景下不
会到需要分页的规模，先设一个保守上限 200 行，超出这个数量说明该上物化视图，
是个可观测的信号而不是静默截断——见 _MAX_ALLOCATION_ROWS 的用法）。
"""
from __future__ import annotations

import json
import sqlite3
import time

from app.db import get_conn
from app.observability import metrics_registry
from app.quota_policy import plans, schema
from app.quota_policy.schema import DIMENSIONS
from app.quota_policy.usage_query import RESOURCES, usage_summary

_MAX_ALLOCATION_ROWS = 200

# workflow_runs.status -> jobs_active / jobs_queued 的映射；其余状态（成功/
# 失败/取消/已归档）既不占运行资源也不在排队，不计入任何一个 gauge。
_ACTIVE_STATUSES = ("RUNNING", "WAITING_RETRY", "WAITING_HUMAN", "PAUSED_EXTERNAL")
_QUEUED_STATUSES = ("CREATED",)


def collect_job_gauges(conn: sqlite3.Connection | None = None) -> None:
    conn = conn or get_conn()
    active_rows = _status_group_counts(conn, _ACTIVE_STATUSES)
    queued_rows = _status_group_counts(conn, _QUEUED_STATUSES)
    metrics_registry.replace_gauge_family(
        metrics_registry.JOBS_ACTIVE,
        [({"module": module}, count) for module, count in active_rows.items()],
        help_text="正在运行的工作流数（按 workflow_type 分组）",
    )
    metrics_registry.replace_gauge_family(
        metrics_registry.JOBS_QUEUED,
        [({"module": module}, count) for module, count in queued_rows.items()],
        help_text="排队等待执行的工作流数（按 workflow_type 分组）",
    )


def _status_group_counts(conn: sqlite3.Connection, statuses: tuple[str, ...]) -> dict[str, int]:
    placeholders = ",".join("?" for _ in statuses)
    rows = conn.execute(
        f"SELECT workflow_type, COUNT(*) AS n FROM workflow_runs "
        f"WHERE status IN ({placeholders}) GROUP BY workflow_type",
        statuses,
    ).fetchall()
    return {str(row["workflow_type"]): int(row["n"]) for row in rows}


def collect_quota_usage_gauges(conn: sqlite3.Connection | None = None) -> None:
    conn = conn or get_conn()
    schema.ensure_tables_on_connection(conn)
    rows = conn.execute(
        "SELECT scope_type, scope_id, plan_id, overrides_json, period_started_at, expires_at "
        "FROM quota_allocations WHERE expires_at IS NULL OR expires_at > ? "
        "ORDER BY created_at DESC LIMIT ?",
        (time.time(), _MAX_ALLOCATION_ROWS),
    ).fetchall()
    gauge_rows: list[tuple[dict[str, str], float]] = []
    for row in rows:
        gauge_rows.extend(_ratios_for_allocation(conn, row))
    metrics_registry.replace_gauge_family(
        metrics_registry.QUOTA_USAGE_RATIO, gauge_rows,
        help_text="配额用量占上限的比例（0-1，>1 表示已超限）",
    )


def _ratios_for_allocation(conn: sqlite3.Connection, row: sqlite3.Row) -> list[tuple[dict[str, str], float]]:
    plan = plans.get_plan(conn, row["plan_id"])
    if plan is None:
        return []
    limits = _effective_limits(plan["limits"], row["overrides_json"])
    usage = usage_summary(
        conn, scope_type=row["scope_type"], scope_id=row["scope_id"],
        start=row["period_started_at"],
    )["usage"]
    out: list[tuple[dict[str, str], float]] = []
    for resource in RESOURCES:
        limit = limits.get(resource)
        # None = 这一维不限；0 会导致除零，两种情况都如实跳过而不是编一个 0/无穷
        # 出来（CLAUDE.md「不得兜底填充」）。
        if limit is None or limit <= 0:
            continue
        ratio = float(usage.get(resource, 0.0)) / float(limit)
        out.append(({
            "scope_type": row["scope_type"], "scope_id": row["scope_id"], "resource": resource,
        }, ratio))
    return out


def _effective_limits(plan_limits: dict, overrides_json: str | None) -> dict:
    limits = dict(plan_limits)
    if overrides_json:
        try:
            overrides = json.loads(overrides_json)
        except json.JSONDecodeError:
            overrides = {}
        for dim in DIMENSIONS:
            if dim in overrides:
                limits[dim] = overrides[dim]
    return limits


def collect_all(conn: sqlite3.Connection | None = None) -> None:
    """``/metrics`` 请求处理器的唯一入口：两组 scrape 时态 gauge 一起刷新。"""
    conn = conn or get_conn()
    collect_job_gauges(conn)
    collect_quota_usage_gauges(conn)
