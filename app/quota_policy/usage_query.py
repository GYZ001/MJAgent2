"""用量聚合（EP-04 §7）与 80%/95% 预警（L2，见 app/LAYERS.toml::app.quota_policy）。

数据源固定为已有的 ``quota_ledger``（org/team/user 三级，token/video_seconds/
image 三个资源——``projects``/``concurrency`` 是实时计数不进 ledger，不在用量
查询范围内）+ ``provider_calls``（project/model 两个维度，只覆盖 token/image
两个资源；``video_seconds`` 的 project 维度改从 ``jobs``/``shot_versions`` 按
既有的"成功版本每镜 15 秒"口径统计）——**不新建统计表**（PRD 明确要求，慢了
先加索引，物化表需要单独实测证据）。

``storage_bytes``（EP-04 第二阶段新增）是例外：它不是事件流水账，是目录实测
占用的定时快照（``app.quota_policy.storage``，独立的 ``project_storage_samples``
表——这不是"新建统计表"违反上面那条红线，是判据本身就是快照而不是可累加的
事件，见该模块文档）。``usage_summary``/``usage_top`` 接入 ``storage_bytes``
（取各账号名下项目最新一次采样求和/排名）；``usage_timeseries`` 仍拒绝它——
按天分桶的存储趋势需要更细的采样保留与聚合策略，本阶段未实现，见该函数校验
分支，与 Phase 1 遗留的 ``test_usage_timeseries_rejects_unimplemented_storage_
resource`` 保持一致（不是这次改动放松了什么，是维持既有边界）。

token/image 的 ``response_json`` token 提取逻辑与 ``app.quota._extract_total_
tokens``/``_billing_category`` 刻意重复了一份小实现，而不是 import 它们——
本包架构红线是"任何模块都不得反向依赖 app.quota"（见 __init__.py 文档），这
两个函数又是 app.quota 的模块私有名字（下划线开头），跨包引用私有实现本就
不是好选择，一份 <15 行的小重复换来零耦合更划算。
"""
from __future__ import annotations

import json
import sqlite3

from app import db
from app.db import new_id, now
from app.quota_policy import schema

RESOURCES: tuple[str, ...] = ("token", "video_seconds", "image")
#: 存储是快照式资源（见模块文档），不进 quota_ledger 的通用聚合循环——单独
#: 一个常量，usage_summary/usage_top 各自按需分支处理，不并进 RESOURCES（并
#: 进去会让 _user_ids_for_scope 之后的 ledger SQL 循环误把它当 ledger 资源查）。
STORAGE_RESOURCE = "storage_bytes"
ALERT_THRESHOLDS: tuple[float, ...] = (0.8, 0.95)
_DAY_SECONDS = 86400.0
_SECONDS_PER_SHOT = 15.0  # 与 app.quota.SECONDS_PER_SHOT 同一产品口径，见模块文档


class UsageQueryError(ValueError):
    """校验失败（非法 scope/resource/dimension）；由 quota_policy.api 转 422。"""


def _user_ids_for_scope(conn: sqlite3.Connection, scope_type: str, scope_id: str) -> list[str]:
    if scope_type == "user":
        return [scope_id]
    if scope_type == "team":
        rows = conn.execute("SELECT user_id FROM team_members WHERE team_id=?", (scope_id,)).fetchall()
        return [r["user_id"] for r in rows]
    if scope_type == "org":
        rows = conn.execute("SELECT id FROM users WHERE org_id=?", (scope_id,)).fetchall()
        return [r["id"] for r in rows]
    raise UsageQueryError(f"scope 必须是 org/team/user 之一，收到 {scope_type!r}")


def usage_summary(
    conn: sqlite3.Connection, *, scope_type: str, scope_id: str,
    start: float | None = None, end: float | None = None,
) -> dict:
    if scope_type == "project":
        return _usage_summary_project(conn, scope_id, start, end)
    user_ids = _user_ids_for_scope(conn, scope_type, scope_id)
    usage = dict.fromkeys(RESOURCES, 0.0)
    if user_ids:
        uid_ph = ",".join("?" for _ in user_ids)
        res_ph = ",".join("?" for _ in RESOURCES)
        sql = (
            f"SELECT resource, COALESCE(SUM(delta),0) AS total FROM quota_ledger "
            f"WHERE user_id IN ({uid_ph}) AND resource IN ({res_ph})"
        )
        params: list = [*user_ids, *RESOURCES]
        sql, params = _append_time_range(sql, params, start, end)
        sql += " GROUP BY resource"
        for row in conn.execute(sql, params).fetchall():
            usage[row["resource"]] = float(row["total"])
    storage_bytes, sampled_at = _storage_usage_for_user_ids(conn, user_ids)
    usage[STORAGE_RESOURCE] = storage_bytes
    return {
        "scope_type": scope_type, "scope_id": scope_id, "usage": usage,
        "storage_sampled_at": sampled_at,
    }


def _storage_usage_for_user_ids(conn: sqlite3.Connection, user_ids: list[str]) -> tuple[float, float | None]:
    """延迟 import（不在模块顶层碰 app.quota_policy.storage）：storage.py 与本
    模块同层同包，模块级互相 import 不会成环，延迟只是避免给一个纯查询模块
    增加它用不到的启动期依赖面，与本文件其它函数的既有风格一致。"""
    from app.quota_policy import storage as quota_storage

    return quota_storage.bytes_used_for_user_ids(conn, user_ids)


def _append_time_range(
    sql: str, params: list, start: float | None, end: float | None, *, column: str = "created_at",
) -> tuple[str, list]:
    """``column`` 按调用方所查的表指定——``quota_ledger``/``jobs`` 都叫
    ``created_at``，``provider_calls`` 叫 ``ts``，不能共用一个硬编码列名。"""
    if start is not None:
        sql += f" AND {column} >= ?"
        params.append(start)
    if end is not None:
        sql += f" AND {column} < ?"
        params.append(end)
    return sql, params


def usage_timeseries(
    conn: sqlite3.Connection, *, scope_type: str, scope_id: str, resource: str,
    start: float | None = None, end: float | None = None,
) -> list[dict]:
    if resource not in RESOURCES:
        raise UsageQueryError(
            f"resource 必须是 {'/'.join(RESOURCES)} 之一（storage_bytes 本阶段未实现），收到 {resource!r}"
        )
    if scope_type == "project":
        raise UsageQueryError("project 维度的时间序列本阶段未实现，请用 scope=org/team/user")
    user_ids = _user_ids_for_scope(conn, scope_type, scope_id)
    if not user_ids:
        return []
    uid_ph = ",".join("?" for _ in user_ids)
    sql = (
        "SELECT CAST(created_at / ? AS INTEGER) AS bucket, COALESCE(SUM(delta),0) AS total "
        f"FROM quota_ledger WHERE user_id IN ({uid_ph}) AND resource=?"
    )
    params: list = [_DAY_SECONDS, *user_ids, resource]
    sql, params = _append_time_range(sql, params, start, end)
    sql += " GROUP BY bucket ORDER BY bucket"
    rows = conn.execute(sql, params).fetchall()
    return [{"day_started_at": int(r["bucket"]) * int(_DAY_SECONDS), "value": float(r["total"])} for r in rows]


def usage_top(conn: sqlite3.Connection, *, dimension: str, resource: str, limit: int = 20) -> list[dict]:
    if resource == STORAGE_RESOURCE:
        if dimension != "project":
            raise UsageQueryError(
                f"resource=storage_bytes 目前只支持 dimension=project（按最新采样排名），收到 {dimension!r}"
            )
        from app.quota_policy import storage as quota_storage

        return quota_storage.top_projects_by_storage(conn, limit)
    if resource not in RESOURCES:
        raise UsageQueryError(
            f"resource 必须是 {'/'.join((*RESOURCES, STORAGE_RESOURCE))} 之一，收到 {resource!r}"
        )
    limit = max(1, min(int(limit), 200))
    if dimension == "user":
        return _usage_top_by_ledger_group(conn, "user_id", "user_id", resource, limit)
    if dimension == "team":
        return _usage_top_team(conn, resource, limit)
    if dimension == "project":
        return _usage_top_project(conn, resource, limit)
    if dimension == "model":
        return _usage_top_model(conn, resource, limit)
    raise UsageQueryError(f"dimension 必须是 team/user/project/model 之一，收到 {dimension!r}")


def _usage_top_by_ledger_group(conn, group_col: str, key_name: str, resource: str, limit: int) -> list[dict]:
    rows = conn.execute(
        f"SELECT {group_col} AS key, COALESCE(SUM(delta),0) AS total FROM quota_ledger "
        f"WHERE resource=? GROUP BY {group_col} HAVING total > 0 ORDER BY total DESC LIMIT ?",
        (resource, limit),
    ).fetchall()
    return [{key_name: r["key"], "total": float(r["total"])} for r in rows]


def _usage_top_team(conn: sqlite3.Connection, resource: str, limit: int) -> list[dict]:
    rows = conn.execute(
        "SELECT tm.team_id AS key, COALESCE(SUM(l.delta),0) AS total FROM quota_ledger l "
        "JOIN team_members tm ON tm.user_id = l.user_id WHERE l.resource=? "
        "GROUP BY tm.team_id HAVING total > 0 ORDER BY total DESC LIMIT ?",
        (resource, limit),
    ).fetchall()
    return [{"team_id": r["key"], "total": float(r["total"])} for r in rows]


def _extract_total_tokens_local(response_json_text: str | None) -> float:
    """从 ``provider_calls.response_json``（TEXT 列，JSON 字符串）里取
    ``usage.total_tokens``（缺失时退化为 prompt+completion 之和）；与
    ``app.quota._extract_total_tokens`` 同一口径的独立小实现，见模块文档。"""
    if not response_json_text:
        return 0.0
    try:
        payload = json.loads(response_json_text)
    except (TypeError, ValueError):
        return 0.0
    usage = payload.get("usage") if isinstance(payload, dict) else None
    if not isinstance(usage, dict):
        return 0.0
    total = usage.get("total_tokens")
    if isinstance(total, (int, float)) and not isinstance(total, bool):
        return float(total)
    prompt = usage.get("prompt_tokens") or 0
    completion = usage.get("completion_tokens") or 0
    return float(prompt) + float(completion)


def _provider_calls_kind_filter(resource: str) -> str:
    return "kind LIKE 'image%'" if resource == "image" else "(kind='chat' OR kind LIKE 'chat_%')"


def _usage_top_project(conn: sqlite3.Connection, resource: str, limit: int) -> list[dict]:
    if resource == "video_seconds":
        rows = conn.execute(
            "SELECT j.project_id AS key, COUNT(*) * ? AS total FROM jobs j "
            "JOIN shot_versions v ON v.id = j.version_id "
            "WHERE j.kind='video' AND v.status='succeeded' AND j.project_id IS NOT NULL "
            "GROUP BY j.project_id HAVING total > 0 ORDER BY total DESC LIMIT ?",
            (_SECONDS_PER_SHOT, limit),
        ).fetchall()
        return [{"project_id": r["key"], "total": float(r["total"])} for r in rows]
    kind_filter = _provider_calls_kind_filter(resource)
    rows = conn.execute(
        f"SELECT project_id, response_json FROM provider_calls "
        f"WHERE {kind_filter} AND status='OK' AND project_id IS NOT NULL",
    ).fetchall()
    return _top_from_extracted_tokens(rows, "project_id", limit)


def _usage_top_model(conn: sqlite3.Connection, resource: str, limit: int) -> list[dict]:
    if resource == "video_seconds":
        raise UsageQueryError(
            "video_seconds 按 model 聚合本阶段未实现：jobs 表未记录所用视频模型，仅支持 team/user/project"
        )
    kind_filter = _provider_calls_kind_filter(resource)
    rows = conn.execute(
        f"SELECT model, response_json FROM provider_calls WHERE {kind_filter} AND status='OK' AND model IS NOT NULL",
    ).fetchall()
    return _top_from_extracted_tokens(rows, "model", limit)


def _top_from_extracted_tokens(rows: list[sqlite3.Row], key_col: str, limit: int) -> list[dict]:
    totals: dict[str, float] = {}
    for row in rows:
        tokens = _extract_total_tokens_local(row["response_json"])
        if tokens <= 0:
            continue
        key = row[key_col]
        totals[key] = totals.get(key, 0.0) + tokens
    ranked = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)[:limit]
    return [{key_col: key, "total": total} for key, total in ranked]


def _usage_summary_project(conn: sqlite3.Connection, project_id: str, start, end) -> dict:
    usage = dict.fromkeys(RESOURCES, 0.0)
    for resource in ("token", "image"):
        kind_filter = _provider_calls_kind_filter(resource)
        sql = f"SELECT response_json FROM provider_calls WHERE {kind_filter} AND status='OK' AND project_id=?"
        params: list = [project_id]
        sql, params = _append_time_range(sql, params, start, end, column="ts")
        usage[resource] = sum(_extract_total_tokens_local(r["response_json"]) for r in conn.execute(sql, params))
    video_sql = (
        "SELECT COUNT(*) AS c FROM jobs j JOIN shot_versions v ON v.id = j.version_id "
        "WHERE j.kind='video' AND v.status='succeeded' AND j.project_id=?"
    )
    video_params: list = [project_id]
    video_sql, video_params = _append_time_range(video_sql, video_params, start, end, column="j.created_at")
    row = conn.execute(video_sql, video_params).fetchone()
    usage["video_seconds"] = float((row["c"] or 0) * _SECONDS_PER_SHOT)
    from app.quota_policy import storage as quota_storage

    storage_bytes, sampled_at = quota_storage.bytes_used_for_project(conn, project_id)
    usage[STORAGE_RESOURCE] = storage_bytes
    return {
        "scope_type": "project", "scope_id": project_id, "usage": usage,
        "storage_sampled_at": sampled_at,
    }


def check_and_record_alerts(
    *, scope_type: str, scope_id: str, resource: str, used: float,
    limit: float | int | None, period_index: int,
) -> list[dict]:
    """跨 80%/95% 阈值时各写一行 ``quota_alerts``；同周期同阈值靠 UNIQUE 兜底
    只提醒一次（``INSERT OR IGNORE`` 命中冲突＝已经提醒过，不是错误）。
    ``limit`` 为 None（不限）或 <=0 时永不触发。写入走独立连接
    （``db._run_write_transaction_once``，与 ``app/quota_policy/schema.py``
    同一手法）——不复用调用方连接、不在调用方事务上隐式提交。
    """
    if not limit or limit <= 0:
        return []
    schema.ensure_schema()
    ratio = used / limit
    crossed = [t for t in ALERT_THRESHOLDS if ratio >= t]
    if not crossed:
        return []
    triggered: list[dict] = []

    def operation(conn: sqlite3.Connection) -> None:
        for threshold in crossed:
            cur = conn.execute(
                "INSERT OR IGNORE INTO quota_alerts("
                "id, scope_type, scope_id, resource, threshold, triggered_at, period_index, notified) "
                "VALUES(?,?,?,?,?,?,?,0)",
                (new_id("qal"), scope_type, scope_id, resource, threshold, now(), period_index),
            )
            if cur.rowcount > 0:
                triggered.append({
                    "scope_type": scope_type, "scope_id": scope_id,
                    "resource": resource, "threshold": threshold,
                })

    db._run_write_transaction_once(operation)
    return triggered


def record_alerts_from_own_allocation(
    conn: sqlite3.Connection, *, scope_type: str, scope_id: str, usage: dict[str, float],
) -> None:
    """管理员查看某个 org/team/user 用量看板时顺带核对预警——只对**这个 scope
    自己配置的** quota_allocations 上限比较（``allocation.scope_limits_dict``
    的原始返回，不做三级合并：一个团队/组织本身没有单一"生效上限"，那是
    per-user 概念），没配置分配的 scope 直接跳过（没有上限可比）。

    惰性触发：只在有人查看用量时才可能落一条预警行，不是常驻轮询/事件驱动。
    这是刻意的取舍——``charge_tokens``/``charge_image_cost``/
    ``reserve_video_seconds`` 都在调用方尚未提交的事务里运行，若在那里直接
    调用需要独立连接的 ``check_and_record_alerts``，会重新触发
    ``app/quota_policy/schema.py`` 文档记录的写锁竞争问题（``BEGIN
    IMMEDIATE`` 互斥，2 秒 busy_timeout）；真正的实时触发点需要一个不持有
    调用方事务的旁路（后台巡检 loop 或提交后钩子），本阶段未做，见交付报告
    "已知限制"。这里退而求其次：用量看板本来就是全新的 ``get_conn()``、不
    嵌套在任何配额闸门事务里，可以安全地捎带查一次。
    """
    from app.quota_policy import allocation as alloc

    schema.ensure_tables_on_connection(conn)
    row = conn.execute(
        "SELECT plan_id, period_started_at FROM quota_allocations WHERE scope_type=? AND scope_id=?",
        (scope_type, scope_id),
    ).fetchone()
    if row is None:
        return
    limits = alloc.scope_limits_dict(conn, scope_type, scope_id)
    plan = conn.execute("SELECT period_days FROM quota_plans WHERE id=?", (row["plan_id"],)).fetchone()
    period_days = (plan["period_days"] if plan else 30) or 30
    anchor = row["period_started_at"] or now()
    period_index = int(max(0.0, now() - anchor) // (period_days * _DAY_SECONDS))
    for resource, used in usage.items():
        limit = limits.get(resource)
        if limit is None:
            continue
        check_and_record_alerts(
            scope_type=scope_type, scope_id=scope_id, resource=resource,
            used=used, limit=limit, period_index=period_index,
        )


def list_recent_alerts(conn: sqlite3.Connection, scopes: list[tuple[str, str]], *, limit: int = 50) -> list[dict]:
    """给定 scope 集合（如某组织自身 + 下属团队 + 下属用户）里最近触发的预警，
    按 ``triggered_at`` 降序——供管理员首页横幅用（EP-04 §7）。``scopes`` 为空
    直接返回空列表，不构造恒真 SQL（CLAUDE.md「空集合不等于无需检查」的镜像
    情形：这里空集合就是真的没有 scope 可查，不是漏传）。"""
    if not scopes:
        return []
    schema.ensure_tables_on_connection(conn)
    conditions = " OR ".join("(scope_type=? AND scope_id=?)" for _ in scopes)
    params: list = [v for pair in scopes for v in pair]
    rows = conn.execute(
        f"SELECT * FROM quota_alerts WHERE {conditions} ORDER BY triggered_at DESC LIMIT ?",
        (*params, limit),
    ).fetchall()
    return [dict(r) for r in rows]
