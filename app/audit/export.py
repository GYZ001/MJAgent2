"""操作审计导出：CSV/JSON。复用 ``app.audit.queries.list_events`` 做分页拉取，
不新造查询层（PRD/enterprise/EP-06 §2 明确要求）——过滤条件与页面筛选完全是
同一个函数、同一套 SQL，导出行数天然与页面筛选一致，不需要另外证明两者对齐。

L2（前缀 "app.audit" = 2 覆盖）：只 import app.audit.queries（同层）+ stdlib。
"""
from __future__ import annotations

import csv
import io
from typing import Any

_PAGE_SIZE = 200
# 单次导出的硬上限：企业客户一次性导出几万行审计已经是很夸张的筛选范围，
# 超过这个数就要求收窄时间/用户范围——用可见的截断信号（返回值里的
# ``truncated``）而不是默默吐一半数据充当"导出完成"，见 CLAUDE.md
# 「空集合不等于无需检查」同一条精神：达到上限这件事本身要能被看见。
MAX_EXPORT_ROWS = 20_000

CSV_COLUMNS: tuple[str, ...] = (
    "id", "ts", "user_id", "username", "is_system_admin", "source", "event",
    "event_label", "method", "path", "project_id", "project_name", "episode_id",
    "target", "outcome", "http_status", "error_id", "error_code", "summary",
    "duration_ms", "ip",
)


def collect_events(
    *, since: float | None, until: float | None, user_id: str | None, event: str | None,
    outcome: str | None, source: str | None, project_id: str | None, q: str | None,
) -> tuple[list[dict[str, Any]], bool]:
    """翻页拉满全部匹配行（上限 ``MAX_EXPORT_ROWS``）。返回 (items, truncated)。"""
    from app.audit import queries

    items: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        page = queries.list_events(
            since=since, until=until, user_id=user_id, event=event, outcome=outcome,
            source=source, project_id=project_id, q=q, limit=_PAGE_SIZE, cursor=cursor,
        )
        page_items = page["items"]
        remaining = MAX_EXPORT_ROWS - len(items)
        if len(page_items) > remaining:
            # 这一页本身就超过剩余额度：取够额度立即截断返回，不再继续翻页。
            items.extend(page_items[:remaining])
            return items, True
        items.extend(page_items)
        cursor = page["next_cursor"]
        if cursor is None:
            return items, False
        if len(items) >= MAX_EXPORT_ROWS:
            # 恰好用完额度这一页：cursor 非 None 说明还有更多行没取，仍是截断。
            return items, True


def to_csv(items: list[dict[str, Any]]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=CSV_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for item in items:
        writer.writerow({col: _csv_cell(item.get(col)) for col in CSV_COLUMNS})
    return buf.getvalue()


def _csv_cell(value: Any) -> Any:
    if isinstance(value, bool):
        return int(value)
    return value if value is not None else ""
