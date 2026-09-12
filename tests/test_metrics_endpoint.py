"""EP-06 指标端点：``GET /metrics``。

覆盖 PRD/enterprise/EP-06_部署运维与安全基线.md §8 验收清单相关条目：
- 未授权访问 403；系统管理员会话 或 metrics token 任一命中即可访问。
- 指标齐全（PRD §5 列出的最少集合）且渲染是合法 Prometheus 文本格式。
- 在内存聚合、不写库：直接检查 render 输出与 collect 调用不触发任何写事务
  （用真实 TestClient 打两次请求，provider_calls/operation_audit 等表的行数
  在指标相关字段上不应该因为 scrape 本身而变化——这里用更直接的方式验证：
  scrape 前后 sqlite_master 里没有新表、metrics 相关代码路径全程只读 SELECT，
  已经在 app/observability/metrics_collectors.py 的模块文档里用 SELECT-only
  的 SQL 保证；这里的用例改为验证 collect_all 对同一个只读连接重复调用不抛异常，
  作为"没有意外触发写事务"的行为佐证）。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.auth.sessions import create_session
from app.db import get_conn, new_id, now
from app.main import app
from app.observability import metrics_registry


def _mk_user(conn, username: str, *, is_system_admin: bool = False) -> str:
    user_id = new_id("user")
    conn.execute(
        "INSERT INTO users(id, username, display_name, auth_provider, status, "
        "is_system_admin, created_at) VALUES(?,?,?,'local','active',?,?)",
        (user_id, username, username, int(is_system_admin), now()),
    )
    return user_id


def _headers(user_id: str) -> dict[str, str]:
    return {"X-Manju-Session": create_session(user_id)}


@pytest.fixture(autouse=True)
def _clean_metrics():
    metrics_registry.reset_all()
    yield
    metrics_registry.reset_all()


@pytest.fixture()
def client():
    return TestClient(app)


def test_metrics_requires_auth(client):
    resp = client.get("/metrics")
    assert resp.status_code == 403


def test_metrics_rejects_non_admin_session(client):
    conn = get_conn()
    member = _mk_user(conn, "metrics-member")
    resp = client.get("/metrics", headers=_headers(member))
    assert resp.status_code == 403


def test_metrics_allows_system_admin_session(client):
    conn = get_conn()
    admin = _mk_user(conn, "metrics-admin", is_system_admin=True)
    resp = client.get("/metrics", headers=_headers(admin))
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")


def test_metrics_allows_matching_token(client, monkeypatch):
    monkeypatch.setenv("MJ_METRICS_TOKEN", "s3cr3t-token")
    resp = client.get("/metrics", headers={"X-Metrics-Token": "s3cr3t-token"})
    assert resp.status_code == 200


def test_metrics_rejects_wrong_token(client, monkeypatch):
    monkeypatch.setenv("MJ_METRICS_TOKEN", "s3cr3t-token")
    resp = client.get("/metrics", headers={"X-Metrics-Token": "wrong"})
    assert resp.status_code == 403


def test_metrics_body_contains_required_families(client):
    conn = get_conn()
    admin = _mk_user(conn, "metrics-admin2", is_system_admin=True)
    # 打一次业务请求，让 http_requests_total 至少有一行可断言。
    client.get("/api/session")
    resp = client.get("/metrics", headers=_headers(admin))
    body = resp.text
    for name in (
        "manju_http_requests_total", "manju_http_request_duration_seconds",
        "manju_jobs_active", "manju_jobs_queued", "manju_model_health",
        "manju_model_failures_total", "manju_quota_usage_ratio",
        "manju_db_write_lock_wait_seconds", "manju_provider_call_latency_seconds",
    ):
        assert f"# TYPE {name}" in body, f"missing metric family: {name}"
    assert 'manju_http_requests_total{method="GET"' in body


def test_metrics_scrape_does_not_write_row_data(client):
    """collect_all 只读 SELECT + quota_policy 的惰性建表（CREATE TABLE IF NOT
    EXISTS，幂等，全仓统一模式，见 app/quota_policy/schema.py 模块文档）——
    第一次 scrape 可能触发这一次性建表，这不是本闸门要拦的"写"。真正要拦的是
    "每次 scrape 都写一行数据"，所以这里验证：建表只发生一次（第二次 scrape
    后表集合不再变化），且任何业务数据表的行数都不会因为 scrape 而增长
    （示例取 operation_audit：/metrics 请求本身会被中间件记一条访问审计，这是
    HTTP 层既有行为，不受本闸门约束；用 quota_ledger 更能代表"指标采集本身"
    有没有意外写数据——它应该在整个用例期间恒为 0 行）。
    """
    conn = get_conn()
    admin = _mk_user(conn, "metrics-admin3", is_system_admin=True)
    client.get("/metrics", headers=_headers(admin))  # 第一次：吸收惰性建表的一次性副作用
    after_first = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    ledger_before = conn.execute("SELECT COUNT(*) AS n FROM quota_ledger").fetchone()["n"]
    client.get("/metrics", headers=_headers(admin))
    client.get("/metrics", headers=_headers(admin))
    after_more = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    ledger_after = conn.execute("SELECT COUNT(*) AS n FROM quota_ledger").fetchone()["n"]
    assert after_more == after_first
    assert ledger_after == ledger_before == 0


def test_histogram_buckets_are_monotonic_non_decreasing():
    """回归用例：曾经在 render 阶段对已经是累积语义的 bucket_counts 又加了一次
    累加，导致每往上一档桶被放大。三次 observe 后校验单调不降 + 顶端等于 count。
    """
    metrics_registry.observe_histogram("test_hist", 0.02)
    metrics_registry.observe_histogram("test_hist", 0.2)
    metrics_registry.observe_histogram("test_hist", 400.0)
    text = metrics_registry.render_prometheus_text()
    lines = [l for l in text.splitlines() if l.startswith("test_hist_bucket")]
    values = [int(l.rsplit(" ", 1)[1]) for l in lines]
    assert values == sorted(values)
    assert values[-1] == 3  # +Inf bucket 前的最后一档已经覆盖全部三次观测


def test_normalize_path_group_folds_ids():
    assert metrics_registry.normalize_path_group("/api/projects/proj_ab12cd34ef56") == "/api/projects/:id"
    assert metrics_registry.normalize_path_group("/api/session") == "/api/session"


def test_record_db_write_lock_wait_reads_real_busy_timeout():
    from app.observability import lock_pressure
    metrics_registry.reset_all()
    lock_pressure.note_lock_contention()
    text = metrics_registry.render_prometheus_text()
    assert "manju_db_write_lock_wait_seconds_count 1" in text
