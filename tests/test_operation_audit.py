"""账号管理「最近活跃」+「操作审计」回归：登录/命令总线/直接 REST 三条来源
都要落 operation_audit 行且不重不漏，GET 请求只打最近活跃不落审计行，脱敏/
写锁争用缓冲/保留期/嵌套调用不重复记录也各自要有独立观察点。

全程走 HTTP（照 ``test_rbac_admin_api.py`` 的 fixture 风格），只有测试
``queries``/``store``/``redact`` 这几个纯 DB 层能力时才直接调用模块函数——
那些本来就不是 HTTP 契约本身，见各测试函数内的说明。
"""
from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import config as app_config
from app.audit import queries, retention, store
from app.audit.recorder import record_command, source_context
from app.audit.redact import redact_and_truncate
from app.auth.passwords import hash_password
from app.auth.sessions import create_session
from app.capabilities.bus import get_command_bus
from app.capabilities.direct import enter_handler
from app.db import get_conn, new_id, now
from app.main import app
from app.monitor_audit_buffer import flush as flush_audit_buffer

_HEADERS = {"Host": "43.153.78.247", "Origin": "http://43.153.78.247"}


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture()
def admin_headers() -> dict[str, str]:
    """唯一一处裸 SQL：引导首个系统管理员，与 test_rbac_admin_api.py 同一手法。"""
    conn = get_conn()
    user_id = new_id("usr")
    conn.execute(
        "INSERT INTO users(id, username, password_hash, status, is_system_admin, "
        "must_change_password, created_at) VALUES(?,?,?,'active',1,0,?)",
        (user_id, "root", hash_password("pw-root"), now()),
    )
    conn.commit()
    return {**_HEADERS, "X-Manju-Session": create_session(user_id)}


def _create_user(client: TestClient, admin_headers: dict[str, str], username: str) -> str:
    resp = client.post(
        "/api/system/users", headers=admin_headers,
        json={"username": username, "password": "initpass1"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


def _independent_conn() -> sqlite3.Connection:
    """独立观察点：不复用被测代码任何连接，直接读盘核对（CLAUDE.md 要求）。"""
    return sqlite3.connect(app_config.DB_PATH)


def _events(client: TestClient, admin_headers: dict[str, str], **params) -> dict:
    resp = client.get("/api/system/audit/events", headers=admin_headers, params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# 1. 登录成功/失败
# ---------------------------------------------------------------------------


def test_login_success_records_one_row_without_password(client: TestClient, admin_headers: dict[str, str]):
    _create_user(client, admin_headers, "zhangsan")
    resp = client.post(
        "/api/auth/login", headers=_HEADERS,
        json={"username": "zhangsan", "password": "initpass1"},
    )
    assert resp.status_code == 200, resp.text

    items = _events(client, admin_headers, event="POST /api/auth/login")["items"]
    assert len(items) == 1
    row = items[0]
    assert row["username"] == "zhangsan"
    assert row["outcome"] == "ok"
    assert row["http_status"] == 200

    detail = client.get(f"/api/system/audit/events/{row['id']}", headers=admin_headers).json()
    assert detail["args"] is None  # HTTP 级行不读 body，密码结构上不可能进 args_json


def test_login_failure_records_username_without_user_id(client: TestClient, admin_headers: dict[str, str]):
    _create_user(client, admin_headers, "lisi")
    resp = client.post(
        "/api/auth/login", headers=_HEADERS,
        json={"username": "lisi", "password": "wrong-password"},
    )
    assert resp.status_code == 401

    items = _events(client, admin_headers, event="POST /api/auth/login", outcome="rejected")["items"]
    matching = [r for r in items if r["username"] == "lisi"]
    assert len(matching) == 1
    assert matching[0]["user_id"] is None


# ---------------------------------------------------------------------------
# 2. 经总线的 REST 写操作：恰好一行，没有多出的 HTTP 行
# ---------------------------------------------------------------------------


def test_bus_routed_rest_write_records_exactly_one_row(client: TestClient, admin_headers: dict[str, str]):
    target_user = _create_user(client, admin_headers, "addon-target")
    before = _events(client, admin_headers, event="quota.grant_video_addon")["items"]

    resp = client.post(
        f"/api/system/users/{target_user}/video-addons", headers=admin_headers,
        json={"packages": 1},
    )
    assert resp.status_code == 200, resp.text

    after = _events(client, admin_headers, event="quota.grant_video_addon")["items"]
    assert len(after) == len(before) + 1
    row = after[0]
    assert row["source"] == "ui"
    assert row["event_label"] == "发放视频加量包"
    assert row["http_status"] == 200
    # 没有多出的 HTTP 级行：同一次请求不会既记总线行又记
    # "POST /api/system/users/{user_id}/video-addons"（路由模板）这条 HTTP 级行。
    extra = _events(client, admin_headers, event="POST /api/system/users/{user_id}/video-addons")["items"]
    assert extra == []


# ---------------------------------------------------------------------------
# 3. 直接 REST 写（不经总线）：一行，target 含 user_id
# ---------------------------------------------------------------------------


def test_direct_rest_write_records_http_level_row(client: TestClient, admin_headers: dict[str, str]):
    target_user = _create_user(client, admin_headers, "rename-target")
    resp = client.put(
        f"/api/system/users/{target_user}", headers=admin_headers,
        json={"display_name": "改名了"},
    )
    assert resp.status_code == 200, resp.text

    items = _events(client, admin_headers, event="PUT /api/system/users/{user_id}")["items"]
    matching = [r for r in items if r["target"] == f"user_id={target_user}"]
    assert len(matching) == 1
    row = matching[0]
    assert row["method"] == "PUT"
    assert row["http_status"] == 200


# ---------------------------------------------------------------------------
# 4. GET 不产生 operation_audit 行，但会写 user_activity；last_active_at/last_action 正确
# ---------------------------------------------------------------------------


def test_get_request_only_touches_activity_not_audit_log(client: TestClient, admin_headers: dict[str, str]):
    user_id = _create_user(client, admin_headers, "active-user")
    login = client.post(
        "/api/auth/login", headers=_HEADERS, json={"username": "active-user", "password": "initpass1"},
    ).json()
    user_headers = {**_HEADERS, "X-Manju-Session": login["session_token"]}

    resp = client.get("/api/auth/me", headers=user_headers)
    assert resp.status_code == 200

    # GET 不落 operation_audit 行。
    assert _events(client, admin_headers, event="GET /api/auth/me")["items"] == []

    listing = client.get("/api/system/users", headers=admin_headers).json()
    row = next(u for u in listing["items"] if u["id"] == user_id)
    assert row["last_active_at"] is not None
    assert row["last_action"] is not None  # 登录本身也是一次 operation_audit 行
    assert row["last_action"]["event"] == "POST /api/auth/login"


# ---------------------------------------------------------------------------
# 5. 列表 API：非管理员 403；过滤；cursor 翻页不重不漏；详情含 args；facets 计数
# ---------------------------------------------------------------------------


def test_list_api_requires_system_admin(client: TestClient, admin_headers: dict[str, str]):
    user_id = _create_user(client, admin_headers, "plain-viewer")
    login = client.post(
        "/api/auth/login", headers=_HEADERS, json={"username": "plain-viewer", "password": "initpass1"},
    ).json()
    plain_headers = {**_HEADERS, "X-Manju-Session": login["session_token"]}
    resp = client.get("/api/system/audit/events", headers=plain_headers)
    assert resp.status_code == 403
    del user_id


def test_list_api_filters_by_outcome(client: TestClient, admin_headers: dict[str, str]):
    _create_user(client, admin_headers, "filter-user")
    client.post("/api/auth/login", headers=_HEADERS, json={"username": "filter-user", "password": "initpass1"})
    client.post("/api/auth/login", headers=_HEADERS, json={"username": "filter-user", "password": "bad"})

    rejected = _events(client, admin_headers, event="POST /api/auth/login", outcome="rejected")["items"]
    assert all(r["outcome"] == "rejected" for r in rejected)
    assert any(r["username"] == "filter-user" for r in rejected)


def test_cursor_pagination_covers_all_rows_without_duplicates(client: TestClient, admin_headers: dict[str, str]):
    for i in range(5):
        client.post(
            "/api/auth/login", headers=_HEADERS,
            json={"username": f"page-test-{i}", "password": "whatever"},
        )

    seen: list[str] = []
    cursor = None
    for _ in range(10):
        page = _events(client, admin_headers, q="page-test", limit=2, cursor=cursor)
        seen.extend(item["id"] for item in page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert len(seen) == len(set(seen)) == 5


def test_event_detail_includes_args_and_facets_counts(client: TestClient, admin_headers: dict[str, str]):
    target_user = _create_user(client, admin_headers, "detail-target")
    client.post(
        f"/api/system/users/{target_user}/video-addons", headers=admin_headers, json={"packages": 2},
    )
    items = _events(client, admin_headers, event="quota.grant_video_addon")["items"]
    detail = client.get(f"/api/system/audit/events/{items[0]['id']}", headers=admin_headers).json()
    assert detail["args"]["packages"] == 2
    assert "user_agent" in detail

    facets = client.get("/api/system/audit/facets", headers=admin_headers).json()
    outcomes = {row["outcome"]: row["count"] for row in facets["outcomes"]}
    assert outcomes.get("ok", 0) >= 1
    sources = {row["source"]: row["count"] for row in facets["sources"]}
    assert sources.get("ui", 0) >= 1


def test_event_detail_404_for_unknown_id(client: TestClient, admin_headers: dict[str, str]):
    resp = client.get("/api/system/audit/events/no-such-id", headers=admin_headers)
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 6. 脱敏：直接验证 redact_and_truncate 与落库结果都不含明文密钥
# ---------------------------------------------------------------------------


def test_redaction_masks_sensitive_keys_in_persisted_row():
    text = redact_and_truncate({"password": "hunter2", "token": "abc", "packages": 1})
    assert "hunter2" not in text
    assert "abc" not in text
    assert '"password": "***"' in text or '"password":"***"' in text

    record_command(
        "test.sensitive_command", "测试命令", "system", "ok", None, "ok",
        None, None, {"password": "hunter2", "token": "abc"}, None,
    )
    conn = _independent_conn()
    try:
        row = conn.execute(
            "SELECT args_json FROM operation_audit WHERE event=? ORDER BY ts DESC LIMIT 1",
            ("test.sensitive_command",),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert "hunter2" not in row[0]
    assert "abc" not in row[0]


# ---------------------------------------------------------------------------
# 7. 写锁争用：落缓冲，flush 后入库
# ---------------------------------------------------------------------------


def test_write_lock_contention_falls_back_to_buffer_then_flushes():
    store.ensure_schema()
    row = {
        "id": new_id("opaudit"), "ts": now(), "user_id": None, "username": "lock-test",
        "is_system_admin": None, "source": "system", "event": "test.lock_contention",
        "event_label": None, "method": None, "path": None, "project_id": None,
        "episode_id": None, "target": None, "outcome": "ok", "http_status": None,
        "error_id": None, "error_code": None, "summary": None, "duration_ms": None,
        "ip": None, "user_agent": None, "args_json": "{}",
    }
    holder = sqlite3.connect(app_config.DB_PATH, timeout=0)
    holder.execute("BEGIN IMMEDIATE")
    try:
        store.insert_operation_audit_row(row)
    finally:
        holder.rollback()
        holder.close()

    conn = _independent_conn()
    try:
        before = conn.execute(
            "SELECT COUNT(*) FROM operation_audit WHERE id=?", (row["id"],)
        ).fetchone()[0]
    finally:
        conn.close()
    assert before == 0  # 直接写失败，这一刻库里还没有这行

    flushed = flush_audit_buffer()
    assert flushed >= 1

    conn = _independent_conn()
    try:
        after = conn.execute(
            "SELECT COUNT(*) FROM operation_audit WHERE id=?", (row["id"],)
        ).fetchone()[0]
    finally:
        conn.close()
    assert after == 1


def test_write_lock_contention_never_raises_even_when_buffer_write_also_fails(monkeypatch):
    """照抄 test_monitor_audit_reliable_delivery.py 的手法：连缓冲自身也失败时，
    调用方（store.insert_operation_audit_row）依然不能抛。"""
    from app import monitor_audit_buffer as buf

    store.ensure_schema()
    monkeypatch.setattr(
        buf, "_operation_audit_buffer_path",
        lambda: (_ for _ in ()).throw(OSError("simulated unwritable buffer")),
    )
    holder = sqlite3.connect(app_config.DB_PATH, timeout=0)
    holder.execute("BEGIN IMMEDIATE")
    try:
        store.insert_operation_audit_row({
            "id": new_id("opaudit"), "ts": now(), "user_id": None, "username": None,
            "is_system_admin": None, "source": "system", "event": "test.total_failure",
            "event_label": None, "method": None, "path": None, "project_id": None,
            "episode_id": None, "target": None, "outcome": "ok", "http_status": None,
            "error_id": None, "error_code": None, "summary": None, "duration_ms": None,
            "ip": None, "user_agent": None, "args_json": "{}",
        })
    finally:
        holder.rollback()
        holder.close()


# ---------------------------------------------------------------------------
# 8. 保留策略：366 天前的行被清理，新行保留
# ---------------------------------------------------------------------------


def test_retention_sweep_deletes_only_expired_rows():
    store.ensure_schema()
    old_ts = now() - retention.OPERATION_AUDIT_RETENTION_S - 86400
    fresh_id = new_id("opaudit")
    old_id = new_id("opaudit")
    for row_id, ts in ((old_id, old_ts), (fresh_id, now())):
        store.insert_operation_audit_row({
            "id": row_id, "ts": ts, "user_id": None, "username": None,
            "is_system_admin": None, "source": "system", "event": "test.retention",
            "event_label": None, "method": None, "path": None, "project_id": None,
            "episode_id": None, "target": None, "outcome": "ok", "http_status": None,
            "error_id": None, "error_code": None, "summary": None, "duration_ms": None,
            "ip": None, "user_agent": None, "args_json": "{}",
        })

    deleted = retention.sweep_expired()
    assert deleted >= 1

    conn = _independent_conn()
    try:
        remaining_ids = {
            r[0] for r in conn.execute(
                "SELECT id FROM operation_audit WHERE event=?", ("test.retention",)
            ).fetchall()
        }
    finally:
        conn.close()
    assert old_id not in remaining_ids
    assert fresh_id in remaining_ids


# ---------------------------------------------------------------------------
# 9. source_context("agent") 下执行总线命令；in_handler() 嵌套不记
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_source_context_agent_tags_bus_row(client: TestClient, admin_headers: dict[str, str]):
    target_user = _create_user(client, admin_headers, "agent-target")
    bus = get_command_bus()
    with source_context("agent"):
        result = await bus.execute_async(
            "quota.grant_video_addon", {"user_id": target_user, "packages": 1},
        )
    assert result.status.value in {"succeeded", "accepted", "failed"}

    conn = _independent_conn()
    try:
        row = conn.execute(
            "SELECT source FROM operation_audit WHERE event=? AND target LIKE ? ORDER BY ts DESC LIMIT 1",
            ("quota.grant_video_addon", f"%{target_user}%"),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row[0] == "agent"


@pytest.mark.asyncio
async def test_nested_bus_call_inside_handler_is_not_recorded(client: TestClient, admin_headers: dict[str, str]):
    target_user = _create_user(client, admin_headers, "nested-target")
    bus = get_command_bus()
    conn = _independent_conn()
    try:
        before = conn.execute("SELECT COUNT(*) FROM operation_audit").fetchone()[0]
    finally:
        conn.close()

    with enter_handler():
        await bus.execute_async("quota.grant_video_addon", {"user_id": target_user, "packages": 1})

    conn = _independent_conn()
    try:
        after = conn.execute("SELECT COUNT(*) FROM operation_audit").fetchone()[0]
    finally:
        conn.close()
    assert after == before  # 嵌套调用（in_handler() 为真）不产生新行


def test_queries_module_used_directly_stays_consistent_with_store():
    """补一条纯 DB 层烟雾测试：确认 queries 与 store 共用同一张表、同一个 DB_PATH。"""
    store.ensure_schema()
    result = queries.list_events(limit=1)
    assert "items" in result and "server_time" in result


# ---------------------------------------------------------------------------
# 10. 异常路径：bus_audit.run_audited/run_audited_sync 也要落一行 outcome=error，
#     error_code 是异常类名，且异常必须照常冒泡（不能被审计钩子吞掉）。
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_command_reraises_keyerror_and_records_error_row():
    bus = get_command_bus()
    with pytest.raises(KeyError):
        await bus.execute_async("no.such.command.xyz", {})

    conn = _independent_conn()
    try:
        row = conn.execute(
            "SELECT outcome, error_code, event FROM operation_audit WHERE event=? ORDER BY ts DESC LIMIT 1",
            ("no.such.command.xyz",),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row[0] == "error"
    assert row[1] == "KeyError"
    assert row[2] == "no.such.command.xyz"


@pytest.mark.asyncio
async def test_invalid_input_reraises_valueerror_and_records_error_row():
    bus = get_command_bus()
    with pytest.raises(ValueError):
        # packages 缺失，_parse_input 的 pydantic 校验必然拒绝——不是业务失败，
        # 是入参形状不对，走 ValueError 而不是正常 CommandResult(FAILED)。
        await bus.execute_async("quota.grant_video_addon", {"user_id": "no-such-user"})

    conn = _independent_conn()
    try:
        row = conn.execute(
            "SELECT outcome, error_code, event FROM operation_audit WHERE event=? ORDER BY ts DESC LIMIT 1",
            ("quota.grant_video_addon",),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row[0] == "error"
    assert row[1] == "ValueError"
