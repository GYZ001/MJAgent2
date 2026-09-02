"""``scripts/smoke_live_routes.py`` 的路径参数解析子模块。

按参数名匹配 ``data/manju.db`` 真实表/列，解析不到就 SKIP（不写死枚举外的
兜底值）。从主文件拆出，纯为压回 CLAUDE.md 的 Python 新文件 ≤500 行上限——
`scripts/check_file_conventions.py` 不扫描 `scripts/`，但规范本身不因此失效。
只用标准库，无独立入口，只被 ``smoke_live_routes.py`` import。
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from urllib.parse import quote, urlencode

_PARAM_RE = re.compile(r"\{(\w+)\}")

#: 单参数路由的通用表/列映射；键是路径参数名，值是 (表名, 列名)。
SIMPLE_LOOKUPS: dict[str, tuple[str, str]] = {
    "project_id": ("projects", "id"), "episode_id": ("episodes", "id"),
    "shot_id": ("shots", "id"), "job_id": ("jobs", "id"),
    "run_id": ("workflow_runs", "id"), "version_id": ("shot_versions", "id"),
    "artifact_id": ("artifacts", "id"), "call_id": ("provider_calls", "id"),
    "package_id": ("delivery_packages", "id"), "order_id": ("payment_orders", "id"),
    "conversation_id": ("agent_conversations", "id"), "turn_id": ("agent_turns", "id"),
    "error_id": ("error_logs", "id"), "user_id": ("users", "id"),
    "character_id": ("character_portraits", "id"), "scene_id": ("scene_references", "id"),
    "grant_id": ("production_grants", "id"), "idx": ("chapters", "idx"),
    "chapter_no": ("chapters", "idx"),
}

#: 多参数路由的联合表/列映射：键是参数名集合，值是一条 SQL（列别名须与参数名一致），
#: 保证同一行取出的多个参数彼此归属一致（project_id 与 artifact_id 真的是同一项目下
#: 的产物），不是各自独立瞎凑（CLAUDE.md「配套参数必须一起传递」）。
JOINT_SQL: dict[frozenset, str] = {
    frozenset({"project_id"}):
        "SELECT id AS project_id FROM projects WHERE deleted_at IS NULL ORDER BY rowid DESC LIMIT ?",
    frozenset({"project_id", "idx"}):
        "SELECT project_id, idx FROM chapters ORDER BY rowid DESC LIMIT ?",
    frozenset({"project_id", "character_name"}):
        "SELECT project_id, character_name FROM character_portraits ORDER BY rowid DESC LIMIT ?",
    frozenset({"project_id", "artifact_id"}):
        "SELECT scope_id AS project_id, id AS artifact_id FROM artifacts "
        "WHERE scope_type='project' ORDER BY rowid DESC LIMIT ?",
    # project_id 有 639 行是空字符串哨兵（不是 NULL，代表「未关联项目」），必须一起
    # 排除，否则会选出 project_id='' 的行拼出错误 URL（实测命中过一次）。
    frozenset({"project_id", "call_id"}):
        "SELECT project_id, id AS call_id FROM provider_calls "
        "WHERE project_id IS NOT NULL AND project_id!='' ORDER BY rowid DESC LIMIT ?",
    frozenset({"project_id", "job_id"}):
        "SELECT project_id, id AS job_id FROM jobs WHERE project_id IS NOT NULL ORDER BY rowid DESC LIMIT ?",
    frozenset({"project_id", "run_id"}):
        "SELECT project_id, run_id FROM jobs "
        "WHERE project_id IS NOT NULL AND run_id IS NOT NULL ORDER BY rowid DESC LIMIT ?",
    frozenset({"provider", "model"}):
        "SELECT provider, model FROM provider_video_capability_snapshots ORDER BY rowid DESC LIMIT ?",
}


def path_param_names(template: str) -> list[str]:
    return _PARAM_RE.findall(template)


def resolve_media_path(conn: sqlite3.Connection, limit: int, projects_dir: Path) -> list[dict]:
    rows = conn.execute(
        "SELECT file_path FROM artifacts WHERE file_path IS NOT NULL AND file_path!='' "
        "ORDER BY rowid DESC LIMIT ?", (limit * 5,),
    ).fetchall()
    out: list[dict] = []
    for (raw,) in rows:
        try:
            rel = Path(raw).resolve().relative_to(projects_dir)
        except (ValueError, OSError):
            continue
        out.append({"path": str(rel)})
        if len(out) >= limit:
            break
    return out


def resolve_route_params(
    conn: sqlite3.Connection, param_names: list[str], per_route: int, projects_dir: Path,
) -> tuple[list[dict] | None, str | None]:
    names = frozenset(param_names)
    if not names:
        return [{}], None
    sql = JOINT_SQL.get(names)
    if sql:
        rows = [dict(r) for r in conn.execute(sql, (per_route,)).fetchall()]
        return (rows, None) if rows else (None, f"表中没有满足参数 {sorted(names)} 的数据（表为空）")
    if len(names) == 1:
        name = next(iter(names))
        if name == "path":
            rows = resolve_media_path(conn, per_route, projects_dir)
            return (rows, None) if rows else (None, "artifacts 表中没有可用的落盘文件路径")
        mapping = SIMPLE_LOOKUPS.get(name)
        if mapping:
            table, col = mapping
            rows = [
                dict(r) for r in conn.execute(
                    f"SELECT {col} AS {name} FROM {table} WHERE {col} IS NOT NULL "
                    f"ORDER BY rowid DESC LIMIT ?", (per_route,),
                ).fetchall()
            ]
            return (rows, None) if rows else (None, f"{table}.{col} 没有可用数据（表为空）")
    return None, (
        f"参数组合 {sorted(names)} 超出通用表/列映射范围，需读路由源码才能确定合法取值，已跳过"
    )


def build_url(template: str, params: dict) -> str:
    url = template
    for name, value in params.items():
        safe = "/" if name == "path" else ""
        url = url.replace("{" + name + "}", quote(str(value), safe=safe))
    return url


def format_args(params: dict) -> str:
    return ", ".join(f"{k}={v}" for k, v in params.items()) or "-"


def resolve_query_params(
    conn: sqlite3.Connection, op: dict,
) -> tuple[dict[str, object], list[str]]:
    """query 参数与路径参数走同一张 ``SIMPLE_LOOKUPS`` 表按名解析，返回
    ``(要填进 query string 的字典, 无法满足的必填参数名列表)``——后者非空时
    整条路由应 SKIP，不得裸打（裸打必然 422，且那是脚本覆盖范围的问题，不是
    后端问题）。

    多个可解析参数只填**第一个真的查到值的**，不是全填：像
    ``/api/observability/resolve`` 的 ``run_id``/``job_id``/``call_id``
    互斥三选一，全填反而会撞上「只能提供一个」的业务校验。名字不在
    ``SIMPLE_LOOKUPS`` 里的参数：必填则记入缺失（SKIP），可选则原样不填、
    交给服务端默认值。
    """
    filled: dict[str, object] = {}
    missing: list[str] = []
    for p in op.get("parameters", []):
        if p.get("in") != "query":
            continue
        name = p["name"]
        mapping = SIMPLE_LOOKUPS.get(name)
        if mapping is None:
            if p.get("required"):
                missing.append(name)
            continue
        if filled:
            if p.get("required"):
                missing.append(name)
            continue
        table, col = mapping
        row = conn.execute(
            f"SELECT {col} FROM {table} WHERE {col} IS NOT NULL ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        if row is not None:
            filled[name] = row[0]
        elif p.get("required"):
            missing.append(name)
    return filled, missing


def append_query(url: str, query: dict) -> str:
    if not query:
        return url
    return f"{url}?{urlencode(query)}"
