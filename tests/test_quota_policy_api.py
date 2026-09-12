"""EP-04 第一阶段 REST 面：策略/分配管理 + 用量查询 + 预警 + 企业形态支付收口。

"绕过扫描"要求（EP-04 §10）：分配额度、追加额度、改策略全部有 HTTP 入口，
无一需要改库——本文件用真实 ``TestClient`` 请求逐条验证，不是直接调用
``app.quota_policy.*`` 的 Python 函数。
"""
from __future__ import annotations

import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient

from app import config
from app.auth.sessions import create_session
from app.db import get_conn, new_id
from app.main import app
from app.orgs import service as orgs_service
from app.orgs import store as orgs_store
from app.quota_policy import usage_query
from tests.rbac_isolation_helpers import _mk_user

_HEADERS = {"Host": "43.153.78.247", "Origin": "http://43.153.78.247"}


def _login(user_id: str) -> dict[str, str]:
    return {**_HEADERS, "X-Manju-Session": create_session(user_id)}


def _make_org_with_admin() -> tuple[str, str]:
    conn = get_conn()
    org_id = orgs_service.create_org(name=f"组织-{new_id('x')}", created_by="test")
    admin_id = _mk_user(conn, f"admin-{new_id('u')}")
    conn.execute("UPDATE users SET org_id=? WHERE id=?", (org_id, admin_id))
    org_admin_role = orgs_store.get_role_by_key(conn, None, "org_admin")
    team_id = orgs_service.create_team(org_id=org_id, name="管理组", description=None, created_by="test")
    orgs_service.add_team_members(team_id=team_id, members=[(admin_id, org_admin_role["id"])], created_by="test")
    conn.commit()
    return org_id, admin_id


def _make_plain_member(org_id: str, *, tier: str = "free") -> str:
    conn = get_conn()
    user_id = _mk_user(conn, f"member-{new_id('u')}")
    conn.execute("UPDATE users SET org_id=?, tier=? WHERE id=?", (org_id, tier, user_id))
    conn.commit()
    return user_id


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


# ---------------------------------------------------------------------------
# 策略管理
# ---------------------------------------------------------------------------


def test_create_plan_requires_org_admin(client: TestClient) -> None:
    org_id, _ = _make_org_with_admin()
    member = _make_plain_member(org_id)
    resp = client.post(
        "/api/system/quota/plans", headers=_login(member),
        json={"key": "vip", "name": "VIP", "limits": {"token": 100}},
    )
    assert resp.status_code == 403


def test_create_plan_then_list_includes_builtin_and_custom(client: TestClient) -> None:
    org_id, admin = _make_org_with_admin()
    resp = client.post(
        "/api/system/quota/plans", headers=_login(admin),
        json={"key": "vip", "name": "VIP 团队策略", "limits": {"token": 12345, "video_seconds": None}},
    )
    assert resp.status_code == 200, resp.text
    plan = resp.json()
    assert plan["limits"]["token"] == 12345
    assert plan["limits"]["video_seconds"] is None
    assert "projects" not in plan["limits"]  # 缺键=未设置，不是 0/不限

    listed = client.get("/api/system/quota/plans", headers=_login(admin)).json()["items"]
    keys = {item["key"] for item in listed}
    assert "vip" in keys
    assert {"free", "starter", "standard", "pro", "max"} <= keys
    assert any(item["builtin"] for item in listed if item["key"] == "free")


def test_create_plan_rejects_negative_limit(client: TestClient) -> None:
    _, admin = _make_org_with_admin()
    resp = client.post(
        "/api/system/quota/plans", headers=_login(admin),
        json={"key": "bad", "name": "坏策略", "limits": {"token": -1}},
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# 分配管理
# ---------------------------------------------------------------------------


def test_put_allocation_requires_org_admin(client: TestClient) -> None:
    org_id, admin = _make_org_with_admin()
    member = _make_plain_member(org_id)
    plan_id = client.get("/api/system/quota/plans", headers=_login(admin)).json()["items"][0]["id"]
    resp = client.put(
        f"/api/system/quota/allocations/user/{member}", headers=_login(member),
        json={"plan_id": plan_id},
    )
    assert resp.status_code == 403


def test_put_allocation_rejects_cross_org_scope(client: TestClient) -> None:
    org_a, admin_a = _make_org_with_admin()
    org_b, admin_b = _make_org_with_admin()
    member_b = _make_plain_member(org_b)
    plan_id = client.get("/api/system/quota/plans", headers=_login(admin_a)).json()["items"][0]["id"]
    resp = client.put(
        f"/api/system/quota/allocations/user/{member_b}", headers=_login(admin_a),
        json={"plan_id": plan_id},
    )
    assert resp.status_code == 404


def test_put_allocation_then_get_allocations_reports_usage(client: TestClient) -> None:
    org_id, admin = _make_org_with_admin()
    member = _make_plain_member(org_id, tier="max")  # max 档 concurrency=10，策略的 1 才真的更紧
    create_resp = client.post(
        "/api/system/quota/plans", headers=_login(admin),
        json={"key": "tight", "name": "紧策略", "limits": {"concurrency": 1}},
    )
    plan_id = create_resp.json()["id"]
    put_resp = client.put(
        f"/api/system/quota/allocations/user/{member}", headers=_login(admin),
        json={"plan_id": plan_id},
    )
    assert put_resp.status_code == 200, put_resp.text

    from app import quota
    result = quota.effective_limits(get_conn(), member)
    assert result.concurrency == 1
    assert result.bound_by["concurrency"].startswith("user=")

    listed = client.get(f"/api/system/quota/allocations?org_id={org_id}", headers=_login(admin)).json()
    user_entries = [item for item in listed["items"] if item["scope_type"] == "user"]
    assert any(item["scope_id"] == member for item in user_entries)
    for item in user_entries:
        assert "usage" in item and "token" in item["usage"]


# ---------------------------------------------------------------------------
# 用量查询：与独立连接手工聚合对账
# ---------------------------------------------------------------------------


def test_usage_summary_matches_manual_ledger_aggregation(client: TestClient) -> None:
    org_id, admin = _make_org_with_admin()
    member = _make_plain_member(org_id)
    conn = get_conn()
    from app import quota
    quota.charge_tokens(conn, member, 4200.0, attempt_key="usage-check-1")
    conn.commit()

    resp = client.get(
        f"/api/system/usage/summary?scope=user&id={member}", headers=_login(admin),
    )
    assert resp.status_code == 200, resp.text
    reported = resp.json()["usage"]["token"]

    independent = sqlite3.connect(config.DB_PATH)
    independent.row_factory = sqlite3.Row
    try:
        row = independent.execute(
            "SELECT COALESCE(SUM(delta),0) AS total FROM quota_ledger WHERE user_id=? AND resource='token'",
            (member,),
        ).fetchone()
    finally:
        independent.close()
    assert reported == row["total"]


def test_usage_top_by_user_ranks_higher_usage_first(client: TestClient) -> None:
    org_id, admin = _make_org_with_admin()
    low = _make_plain_member(org_id)
    high = _make_plain_member(org_id)
    conn = get_conn()
    from app import quota
    quota.charge_tokens(conn, low, 100.0, attempt_key="top-low")
    quota.charge_tokens(conn, high, 900.0, attempt_key="top-high")
    conn.commit()

    resp = client.get("/api/system/usage/top?dimension=user&resource=token&limit=5", headers=_login(admin))
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    ranked_ids = [item["user_id"] for item in items]
    assert ranked_ids.index(high) < ranked_ids.index(low)


def test_usage_summary_view_lazily_records_alert_once_crossed(client: TestClient) -> None:
    """查看用量看板顺带核对该 scope 自己配置的分配是否跨过 80%——重复查看
    同一周期不重复提醒（UNIQUE 兜底），见 usage_query.record_alerts_from_own_
    allocation 文档（本阶段的预警触发点是"惰性查看"，不是实时记账钩子）。"""
    org_id, admin = _make_org_with_admin()
    member = _make_plain_member(org_id)
    plan_resp = client.post(
        "/api/system/quota/plans", headers=_login(admin),
        json={"key": "watched", "name": "被监控策略", "limits": {"token": 1000}},
    )
    plan_id = plan_resp.json()["id"]
    put_resp = client.put(
        f"/api/system/quota/allocations/user/{member}", headers=_login(admin),
        json={"plan_id": plan_id},
    )
    assert put_resp.status_code == 200, put_resp.text

    from app import quota
    conn = get_conn()
    quota.charge_tokens(conn, member, 850.0, attempt_key="alert-view-1")  # 85% of 1000
    conn.commit()

    for _ in range(2):  # 重复查看不重复提醒
        resp = client.get(f"/api/system/usage/summary?scope=user&id={member}", headers=_login(admin))
        assert resp.status_code == 200, resp.text

    independent = sqlite3.connect(config.DB_PATH)
    try:
        rows = independent.execute(
            "SELECT threshold FROM quota_alerts WHERE scope_type='user' AND scope_id=?", (member,),
        ).fetchall()
    finally:
        independent.close()
    assert [r[0] for r in rows] == [0.8]


def test_usage_timeseries_rejects_unimplemented_storage_resource(client: TestClient) -> None:
    _, admin = _make_org_with_admin()
    resp = client.get(
        f"/api/system/usage/timeseries?scope=user&id={admin}&resource=storage_bytes", headers=_login(admin),
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# 预警：跨 80%/95% 各只提醒一次
# ---------------------------------------------------------------------------


def test_alerts_trigger_once_per_threshold_per_period() -> None:
    scope_id = new_id("team")
    first = usage_query.check_and_record_alerts(
        scope_type="team", scope_id=scope_id, resource="video_seconds",
        used=850.0, limit=1000.0, period_index=0,
    )
    assert {t["threshold"] for t in first} == {0.8}
    repeat = usage_query.check_and_record_alerts(
        scope_type="team", scope_id=scope_id, resource="video_seconds",
        used=860.0, limit=1000.0, period_index=0,
    )
    assert repeat == []  # 同周期同阈值不重复提醒
    crossed_95 = usage_query.check_and_record_alerts(
        scope_type="team", scope_id=scope_id, resource="video_seconds",
        used=960.0, limit=1000.0, period_index=0,
    )
    assert {t["threshold"] for t in crossed_95} == {0.95}


def test_alerts_never_trigger_for_unlimited_resource() -> None:
    result = usage_query.check_and_record_alerts(
        scope_type="user", scope_id=new_id("user"), resource="token",
        used=1_000_000.0, limit=None, period_index=0,
    )
    assert result == []


def test_alerts_endpoint_lists_recent_triggers_for_org_scope_family(client: TestClient) -> None:
    org_id, admin = _make_org_with_admin()
    member = _make_plain_member(org_id)
    plan_resp = client.post(
        "/api/system/quota/plans", headers=_login(admin),
        json={"key": "watched2", "name": "被监控策略2", "limits": {"token": 1000}},
    )
    plan_id = plan_resp.json()["id"]
    client.put(
        f"/api/system/quota/allocations/user/{member}", headers=_login(admin),
        json={"plan_id": plan_id},
    )
    from app import quota
    conn = get_conn()
    quota.charge_tokens(conn, member, 850.0, attempt_key="alert-endpoint-1")
    conn.commit()
    client.get(f"/api/system/usage/summary?scope=user&id={member}", headers=_login(admin))  # 惰性触发写入

    resp = client.get("/api/system/quota/alerts", headers=_login(admin))
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert any(i["scope_id"] == member and i["threshold"] == 0.8 for i in items)


def test_alerts_endpoint_requires_login(client: TestClient) -> None:
    resp = client.get("/api/system/quota/alerts?org_id=whatever")
    assert resp.status_code == 401


def _alert_race_once() -> None:
    scope_id = "race-scope-fixed"
    results: list[list[dict]] = []

    def _worker() -> None:
        results.append(usage_query.check_and_record_alerts(
            scope_type="team", scope_id=scope_id, resource="token",
            used=85.0, limit=100.0, period_index=0,
        ))

    threads = [threading.Thread(target=_worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    total_triggered = sum(len(r) for r in results)
    assert total_triggered == 1, f"UNIQUE 应该只放行一条，实际 {total_triggered} 条"

    independent = sqlite3.connect(config.DB_PATH)
    try:
        count = independent.execute(
            "SELECT COUNT(*) FROM quota_alerts WHERE scope_id=? AND threshold=0.8", (scope_id,),
        ).fetchone()[0]
    finally:
        independent.close()
    assert count == 1


@pytest.mark.parametrize("run", range(10))
def test_alerts_concurrent_dedupe_is_stable_across_ten_runs(run: int) -> None:
    """并发相关的测试单次绿等于没验（CLAUDE.md）：同一断言连跑 10 次。"""
    _alert_race_once()


# ---------------------------------------------------------------------------
# 企业形态：支付路由 403
# ---------------------------------------------------------------------------


def test_payments_orders_403_in_enterprise_profile(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(config, "DEPLOYMENT_PROFILE", "enterprise")
    conn = get_conn()
    uid = _mk_user(conn, f"buyer-{new_id('u')}")
    conn.commit()
    resp = client.post(
        "/api/payments/orders", headers=_login(uid),
        json={"channel": "wechat", "product": "video_addon", "packages": 1},
    )
    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "PAYMENTS_DISABLED_ENTERPRISE"


def test_payments_orders_not_blocked_in_saas_profile(client: TestClient) -> None:
    assert not config.is_enterprise_profile()
    conn = get_conn()
    uid = _mk_user(conn, f"buyer-{new_id('u')}")
    conn.commit()
    resp = client.post(
        "/api/payments/orders", headers=_login(uid),
        json={"channel": "wechat", "product": "video_addon", "packages": 1},
    )
    assert resp.status_code != 403
