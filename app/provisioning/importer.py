"""EP-03 §4 CSV 批量导入的业务动作（L2，见 app/LAYERS.toml::app.provisioning）。

三段式 preview -> apply -> report，与 ``app.orgs.service`` 同一惯例：每个
"动作"函数自己用 ``get_conn()`` 取本线程/任务局部连接并显式 ``commit()``，
调用方不传入、也不持有连接。

**一次性口令**：随机生成的初始口令只在 ``apply`` 的响应与 ``report`` 的
CSV 里出现一次，不落明文库（EP-03 §4/§9 已知陷阱 2）。做法：``apply`` 时把
``{username: 明文}`` 写进本模块的进程内存缓存 ``_PENDING_PASSWORDS[batch_id]``，
持久化到 ``user_import_batches.report_json`` 的那一份只写占位符 ``***``；
``get_report_csv`` 第一次读到某个 batch 的缓存时用明文覆盖对应行，然后立即
从缓存里弹出该 batch——第二次下载同一份报告只能看到占位符。缓存不跨进程
重启存活（本仓库是单进程 SQLite 部署，与 ``app/capabilities/attachments.py``
的一次性 attachment_token 同一类设计），并设 TTL 兜底防止无人下载时占内存。

**分批提交**：``apply`` 按 ``_CHUNK_SIZE``（50）行一批 ``commit()``（EP-03 §9
已知陷阱 5：SQLite 长写事务会阻塞整站）；单行处理异常只把该行记成 error 继续，
不中止整批（EP-03 §8 验收项"单行错误不影响其他行落地"）。**不支持跨 HTTP
请求的断点续传**——若这次 ``apply`` 调用中途进程崩溃，已提交的分批不会回滚，
但客户端拿不到那次调用本该返回的响应（含那批新建账号的初始口令）；这是
"明文口令不落库"与"允许续跑"两个约束在极端场景下的真实张力，留在交付报告
"已知限制"，不是遗漏。
"""
from __future__ import annotations

import csv
import io
import json
import secrets
import time
from dataclasses import asdict, dataclass

from fastapi import HTTPException

from app.auth.passwords import hash_password
from app.db import get_conn, new_id, now
from app.orgs import schema as orgs_schema
from app.orgs import store as orgs_store
from app.provisioning import csv_parse, schema
from app.quota_tiers import VALID_TIERS

_CHUNK_SIZE = 50
_PASSWORD_PLACEHOLDER = "***"
_PENDING_TTL_S = 24 * 3600.0
_REPORT_COLUMNS = (
    "line_no", "username", "display_name", "email", "employee_no", "team_name", "role_name",
    "tier", "action", "initial_password", "reason",
)

# 一次性明文口令：batch_id -> {username: 明文}，见模块文档"一次性口令"一节。
_PENDING_PASSWORDS: dict[str, dict[str, str]] = {}
_PENDING_PASSWORDS_AT: dict[str, float] = {}


@dataclass
class RowPlan:
    line_no: int
    username: str
    action: str  # "create" | "update" | "skip" | "error"
    reason: str | None = None
    column: str | None = None
    display_name: str = ""
    email: str = ""
    employee_no: str = ""
    team_id: str | None = None
    team_name: str = ""
    role_id: str | None = None
    role_name: str = ""
    tier: str | None = None


def _ensure_schemas() -> None:
    schema.ensure_schema()
    orgs_schema.ensure_schema()  # users.org_id 由 orgs 包补列，两者都要确保建好


def preview_batch(*, raw: bytes, filename: str, org_id: str, created_by: str) -> dict:
    """``POST .../import/preview`` 的领域实现：不写 users/teams，只写批次台账。"""
    _ensure_schemas()
    try:
        parsed = csv_parse.parse_csv(raw)
    except csv_parse.CsvParseError as exc:
        raise HTTPException(422, str(exc)) from exc

    conn = get_conn()
    plan = [_classify_row(conn, row, org_id=org_id) for row in parsed.rows]
    counts = _tally(plan)
    batch_id = new_id("imp")
    conn.execute(
        "INSERT INTO user_import_batches(id, org_id, created_by, created_at, filename, encoding,"
        " total_rows, create_rows, update_rows, skip_rows, error_rows, status, plan_json)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            batch_id, org_id, created_by, now(), filename, parsed.encoding, len(plan),
            counts["create"], counts["update"], counts["skip"], counts["error"],
            "previewing", json.dumps([asdict(p) for p in plan], ensure_ascii=False),
        ),
    )
    conn.commit()
    return {
        "batch_id": batch_id, "encoding": parsed.encoding, "counts": counts,
        "rows": [asdict(p) for p in plan],
    }


def _classify_row(conn, row: csv_parse.ParsedRow, *, org_id: str) -> RowPlan:
    if row.error:
        return RowPlan(
            line_no=row.line_no, username=row.username, action="error",
            reason=row.error, column=row.error_column,
        )

    team_id, role_id, team_role_error = _resolve_team_and_role(conn, org_id, row.team, row.role)
    if team_role_error is not None:
        reason, column = team_role_error
        return RowPlan(
            line_no=row.line_no, username=row.username, action="error", reason=reason,
            column=column, team_name=row.team, role_name=row.role,
        )

    tier = row.tier_or_quota_plan or None
    if tier is not None and tier not in VALID_TIERS:
        return RowPlan(
            line_no=row.line_no, username=row.username, action="error",
            reason=f"tier_or_quota_plan 不合法，必须是 {'/'.join(sorted(VALID_TIERS))} 之一或留空",
            column="tier_or_quota_plan",
        )

    base = RowPlan(
        line_no=row.line_no, username=row.username, action="create",
        display_name=row.display_name, email=row.email, employee_no=row.employee_no,
        team_id=team_id, team_name=row.team, role_id=role_id, role_name=row.role, tier=tier,
    )
    existing = conn.execute("SELECT * FROM users WHERE username=?", (row.username,)).fetchone()
    if existing is None:
        return base
    if existing["deleted_at"] is not None:
        return RowPlan(
            line_no=row.line_no, username=row.username, action="error",
            reason="用户名已被回收站中的账号占用，请先在回收站处理该账号", column="username",
        )
    return _plan_for_existing_user(conn, base, existing)


def _resolve_team_and_role(
    conn, org_id: str, team_name: str, role_name: str,
) -> tuple[str | None, str | None, tuple[str, str] | None]:
    if bool(team_name) != bool(role_name):
        missing = "role" if team_name else "team"
        return None, None, (f"team 与 role 必须同时提供或同时留空，缺少 {missing}", missing)
    if not team_name:
        return None, None, None
    team_id = _find_team_id(conn, org_id, team_name)
    if team_id is None:
        return None, None, (f"团队不存在：{team_name}", "team")
    role = orgs_store.get_role_by_key(conn, org_id, role_name) or orgs_store.get_role_by_key(conn, None, role_name)
    if role is None:
        return None, None, (f"角色不存在：{role_name}", "role")
    return team_id, role["id"], None


def _find_team_id(conn, org_id: str, name: str) -> str | None:
    row = conn.execute("SELECT id FROM teams WHERE org_id=? AND name=?", (org_id, name)).fetchone()
    return row["id"] if row else None


def _plan_for_existing_user(conn, base: RowPlan, existing) -> RowPlan:
    changed = (
        (base.display_name and base.display_name != (existing["display_name"] or ""))
        or (base.email and base.email != (existing["email"] or ""))
        or (base.employee_no and base.employee_no != (existing["employee_no"] or ""))
        or (base.tier and base.tier != existing["tier"])
        or (base.team_id and _team_role_differs(conn, base.team_id, base.role_id, existing["id"]))
    )
    base.action = "update" if changed else "skip"
    return base


def _team_role_differs(conn, team_id: str, role_id: str | None, user_id: str) -> bool:
    row = conn.execute(
        "SELECT role_id FROM team_members WHERE team_id=? AND user_id=?", (team_id, user_id)
    ).fetchone()
    return row is None or row["role_id"] != role_id


def _tally(plan: list[RowPlan]) -> dict[str, int]:
    counts = {"create": 0, "update": 0, "skip": 0, "error": 0}
    for item in plan:
        counts[item.action] += 1
    return counts


def apply_batch(*, batch_id: str, created_by: str) -> dict:
    """``POST .../import/{batch_id}/apply``：只应用预检通过的行，分批提交。

    对已经 ``applied`` 的批次幂等短路（原样返回持久化的报告，不重复写库、
    不重新暴露明文口令）——这是"同一批次重复提交"的安全出口，不是本次
    "同一份 CSV 连导两次"验收项（那对应两次**各自独立**的 preview+apply）。
    """
    _ensure_schemas()
    _purge_expired_pending()
    conn = get_conn()
    row = conn.execute("SELECT * FROM user_import_batches WHERE id=?", (batch_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "导入批次不存在")
    if row["status"] == "applied":
        return _applied_response(batch_id, row)
    if row["status"] != "previewing":
        raise HTTPException(409, f"批次状态为 {row['status']}，无法提交")

    plan = [RowPlan(**entry) for entry in json.loads(row["plan_json"] or "[]")]
    org_id = row["org_id"]
    pending_passwords: dict[str, str] = {}
    report: list[dict] = []
    for start in range(0, len(plan), _CHUNK_SIZE):
        for item in plan[start:start + _CHUNK_SIZE]:
            report.append(_apply_row(conn, item, org_id=org_id, created_by=created_by,
                                      pending_passwords=pending_passwords))
        conn.commit()

    counts = _tally_reports(report)
    stamp = now()
    conn.execute(
        "UPDATE user_import_batches SET status='applied', applied_at=?, report_json=?,"
        " create_rows=?, update_rows=?, skip_rows=?, error_rows=? WHERE id=?",
        (stamp, json.dumps(_redact_report(report), ensure_ascii=False),
         counts["create"], counts["update"], counts["skip"], counts["error"], batch_id),
    )
    conn.commit()
    if pending_passwords:
        _PENDING_PASSWORDS[batch_id] = pending_passwords
        _PENDING_PASSWORDS_AT[batch_id] = time.time()
    _record_apply_audit(batch_id, row["filename"], counts)
    return {"batch_id": batch_id, "counts": counts, "rows": report, "applied_at": stamp}


def _record_apply_audit(batch_id: str, filename: str | None, counts: dict[str, int]) -> None:
    from app.audit import recorder

    recorder.record_command(
        "provisioning.user_import_apply", "CSV 批量导入确认提交", recorder.current_source(),
        "ok", None, f"批次 {batch_id}：{counts}", None, None,
        {"batch_id": batch_id, "filename": filename, "counts": counts}, None,
    )


def _applied_response(batch_id: str, row) -> dict:
    report = json.loads(row["report_json"] or "[]")
    counts = {
        "create": row["create_rows"], "update": row["update_rows"],
        "skip": row["skip_rows"], "error": row["error_rows"],
    }
    return {"batch_id": batch_id, "counts": counts, "rows": report, "applied_at": row["applied_at"]}


def _apply_row(conn, item: RowPlan, *, org_id: str, created_by: str, pending_passwords: dict) -> dict:
    try:
        if item.action == "create":
            password = _create_user(conn, item, org_id=org_id, created_by=created_by)
            pending_passwords[item.username] = password
        elif item.action == "update":
            _update_user(conn, item, created_by=created_by)
        # skip/error：不碰库，原样透传
    except Exception as exc:  # noqa: BLE001 单行失败不阻塞其余行（EP-03 §8）
        return _row_report(item, action="error", reason=f"写入失败：{exc}", initial_password=None)
    return _row_report(item, action=item.action, reason=item.reason,
                        initial_password=pending_passwords.get(item.username))


def _row_report(item: RowPlan, *, action: str, reason: str | None, initial_password: str | None) -> dict:
    payload = asdict(item)
    payload["action"] = action
    payload["reason"] = reason
    payload["initial_password"] = initial_password
    return payload


def _create_user(conn, item: RowPlan, *, org_id: str, created_by: str) -> str:
    password = secrets.token_urlsafe(9)
    user_id = new_id("user")
    stamp = now()
    display_name = item.display_name or item.username
    conn.execute(
        "INSERT INTO users(id, username, display_name, password_hash, status, is_system_admin,"
        " must_change_password, created_at, tier, quota_period_started_at, org_id, email,"
        " employee_no, created_by) VALUES(?,?,?,?,'active',0,1,?,?,?,?,?,?,?)",
        (user_id, item.username, display_name, hash_password(password), stamp,
         item.tier or "free", stamp, org_id, item.email or None, item.employee_no or None, created_by),
    )
    if item.team_id and item.role_id:
        orgs_store.add_team_member(
            conn, team_id=item.team_id, user_id=user_id, role_id=item.role_id, created_by=created_by,
        )
    return password


def _update_user(conn, item: RowPlan, *, created_by: str) -> None:
    existing = conn.execute("SELECT id FROM users WHERE username=?", (item.username,)).fetchone()
    if existing is None:
        raise RuntimeError("预检后账号被并发删除，无法更新")
    user_id = existing["id"]
    fields: list[str] = []
    values: list[object] = []
    for column, value in (
        ("display_name", item.display_name), ("email", item.email),
        ("employee_no", item.employee_no), ("tier", item.tier),
    ):
        if value:
            fields.append(f"{column}=?")
            values.append(value)
    if fields:
        values.append(user_id)
        conn.execute(f"UPDATE users SET {', '.join(fields)} WHERE id=?", values)
    if item.team_id and item.role_id:
        orgs_store.add_team_member(
            conn, team_id=item.team_id, user_id=user_id, role_id=item.role_id, created_by=created_by,
        )


def _tally_reports(report: list[dict]) -> dict[str, int]:
    counts = {"create": 0, "update": 0, "skip": 0, "error": 0}
    for row in report:
        counts[row["action"]] += 1
    return counts


def _redact_report(report: list[dict]) -> list[dict]:
    redacted = []
    for row in report:
        row = dict(row)
        if row.get("initial_password"):
            row["initial_password"] = _PASSWORD_PLACEHOLDER
        redacted.append(row)
    return redacted


def get_report_csv(batch_id: str) -> tuple[str, str]:
    """返回 ``(filename, csv_text)``；明文口令只在本函数**第一次**为某个
    ``batch_id`` 调用时补回（弹出内存缓存），之后同一批次再下载只剩占位符。
    """
    _ensure_schemas()
    _purge_expired_pending()
    conn = get_conn()
    row = conn.execute(
        "SELECT filename, report_json, status FROM user_import_batches WHERE id=?", (batch_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(404, "导入批次不存在")
    if row["status"] != "applied":
        raise HTTPException(409, "批次尚未提交，没有可下载的报告")

    report = json.loads(row["report_json"] or "[]")
    plaintext = _PENDING_PASSWORDS.pop(batch_id, {})
    _PENDING_PASSWORDS_AT.pop(batch_id, None)
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=_REPORT_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for entry in report:
        entry = dict(entry)
        if entry.get("username") in plaintext:
            entry["initial_password"] = plaintext[entry["username"]]
        writer.writerow(entry)
    base_name = (row["filename"] or "import").rsplit(".", 1)[0]
    return f"{base_name}-report-{batch_id}.csv", buf.getvalue()


def _purge_expired_pending() -> None:
    cutoff = time.time() - _PENDING_TTL_S
    expired = [bid for bid, ts in _PENDING_PASSWORDS_AT.items() if ts < cutoff]
    for bid in expired:
        _PENDING_PASSWORDS.pop(bid, None)
        _PENDING_PASSWORDS_AT.pop(bid, None)
