"""操作审计导出（PRD/enterprise/EP-06 §2/§8）：按页面同款筛选导出 CSV/JSON，
行数必须与页面筛选（``/api/system/audit/events``）一致；复用
``app.audit.queries.list_events``，不新造查询层——本测试文件同时覆盖 HTTP 契约
与 ``app.audit.export.collect_events`` 的截断逻辑单元测试两档。
"""
from __future__ import annotations

import csv
import io

import pytest
from fastapi.testclient import TestClient

from app.audit import export as audit_export
from app.audit import store as audit_store
from app.auth.passwords import hash_password
from app.auth.sessions import create_session
from app.db import get_conn, new_id, now
from app.main import app


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture()
def admin_headers() -> dict[str, str]:
    conn = get_conn()
    user_id = new_id("usr")
    conn.execute(
        "INSERT INTO users(id, username, password_hash, status, is_system_admin, "
        "must_change_password, created_at) VALUES(?,?,?,'active',1,0,?)",
        (user_id, "export-admin", hash_password("pw-root"), now()),
    )
    conn.commit()
    return {"X-Manju-Session": create_session(user_id)}


def _touch_some_events(client: TestClient, admin_headers: dict[str, str], n: int) -> None:
    """打几次真实 REST 写请求，让 operation_audit 里有可导出的行——用真实登录
    失败最省事，不需要额外的业务资源。"""
    for i in range(n):
        client.post("/api/auth/login", json={"username": "nope", "password": f"x{i}"})


def test_export_csv_matches_events_count(client, admin_headers):
    _touch_some_events(client, admin_headers, 3)
    listed = client.get(
        "/api/system/audit/events", headers=admin_headers,
        params={"outcome": "rejected", "limit": 200},
    )
    assert listed.status_code == 200
    expected_count = len(listed.json()["items"])
    assert expected_count >= 3

    resp = client.get(
        "/api/system/audit/export", headers=admin_headers,
        params={"outcome": "rejected", "format": "csv"},
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert resp.headers["x-manju-audit-export-truncated"] == "false"
    rows = list(csv.DictReader(io.StringIO(resp.text)))
    assert len(rows) == expected_count
    assert set(audit_export.CSV_COLUMNS) <= set(rows[0].keys())


def test_export_json_matches_events_count(client, admin_headers):
    _touch_some_events(client, admin_headers, 2)
    listed = client.get(
        "/api/system/audit/events", headers=admin_headers,
        params={"outcome": "rejected", "limit": 200},
    )
    expected_count = len(listed.json()["items"])

    resp = client.get(
        "/api/system/audit/export", headers=admin_headers,
        params={"outcome": "rejected", "format": "json"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == expected_count
    assert resp.headers["x-manju-audit-export-count"] == str(expected_count)


def test_export_filters_by_user_id(client, admin_headers):
    """按用户筛选导出：只导出该用户的行，不是全表。"""
    conn = get_conn()
    other = new_id("usr")
    conn.execute(
        "INSERT INTO users(id, username, password_hash, status, is_system_admin, "
        "must_change_password, created_at) VALUES(?,?,?,'active',0,0,?)",
        (other, "export-other", hash_password("pw-other"), now()),
    )
    conn.commit()
    resp = client.get(
        "/api/system/audit/export", headers=admin_headers,
        params={"user_id": other, "format": "json"},
    )
    assert resp.status_code == 200
    assert resp.json() == []  # 该用户没有任何操作记录


def test_export_rejects_bad_format(client, admin_headers):
    resp = client.get(
        "/api/system/audit/export", headers=admin_headers, params={"format": "xml"},
    )
    assert resp.status_code == 422


def test_export_requires_system_admin(client):
    conn = get_conn()
    member = new_id("usr")
    conn.execute(
        "INSERT INTO users(id, username, password_hash, status, is_system_admin, "
        "must_change_password, created_at) VALUES(?,?,?,'active',0,0,?)",
        (member, "export-member", hash_password("pw-member"), now()),
    )
    conn.commit()
    resp = client.get(
        "/api/system/audit/export",
        headers={"X-Manju-Session": create_session(member)},
    )
    assert resp.status_code == 403


def test_collect_events_truncates_with_visible_signal(monkeypatch):
    """截断信号必须可见（CLAUDE.md「空集合不等于无需检查」同一条精神：达到
    上限这件事要能被看见，不能悄悄吐一半数据当作"导出完成"）。用真实几行数据
    + 调小 MAX_EXPORT_ROWS，验证 collect_events 精确截到上限且报告 truncated。
    """
    audit_store.ensure_schema()
    conn = get_conn()
    ts = now()
    for i in range(5):
        conn.execute(
            "INSERT INTO operation_audit(id, ts, source, event, outcome) "
            "VALUES(?,?,?,?,?)",
            (f"aud_trunc_{i}", ts + i, "test", "manual.probe", "ok"),
        )
    conn.commit()
    monkeypatch.setattr(audit_export, "MAX_EXPORT_ROWS", 3)
    items, truncated = audit_export.collect_events(
        since=None, until=None, user_id=None, event="manual.probe", outcome=None,
        source=None, project_id=None, q=None,
    )
    assert len(items) == 3
    assert truncated is True


def test_collect_events_not_truncated_when_under_cap():
    audit_store.ensure_schema()
    conn = get_conn()
    ts = now()
    conn.execute(
        "INSERT INTO operation_audit(id, ts, source, event, outcome) VALUES(?,?,?,?,?)",
        ("aud_small_1", ts, "test", "manual.probe.small", "ok"),
    )
    conn.commit()
    items, truncated = audit_export.collect_events(
        since=None, until=None, user_id=None, event="manual.probe.small", outcome=None,
        source=None, project_id=None, q=None,
    )
    assert len(items) == 1
    assert truncated is False
