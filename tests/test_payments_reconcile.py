"""对账：主动查单收敛 pending 订单 + 重跑 paid-未发货订单。渠道 HTTP 调用全部
打桩（不打真实网络），只验证收到查单结果之后的状态收敛逻辑本身。
"""
from __future__ import annotations

import pytest

from app.auth.passwords import hash_password
from app.db import get_conn, new_id, now
from app.payments import orders, reconcile
from app.payments.config import PaymentConfigError
from app.payments.models import CHANNEL_WECHAT, PRODUCT_VIDEO_ADDON, STATUS_FULFILLED, STATUS_PAID


def _make_user() -> str:
    conn = get_conn()
    user_id = new_id("user")
    conn.execute(
        """INSERT INTO users(
               id, username, display_name, password_hash, auth_provider, status,
               is_system_admin, must_change_password, created_at, tier
           ) VALUES(?,?,?,?,'local','active',0,0,?,'free')""",
        (user_id, f"buyer-{user_id}", "测试买家", hash_password("pw-test-000000"), now()),
    )
    conn.commit()
    return user_id


def _make_stale_pending_order(conn, user_id: str, *, amount_fen: int = 19900) -> str:
    order_id = new_id("pay")
    orders.create_order(
        conn, order_id=order_id, user_id=user_id, channel=CHANNEL_WECHAT, product=PRODUCT_VIDEO_ADDON,
        product_detail={"packages": 1}, amount_fen=amount_fen, created_at=now() - 3600,
    )
    conn.commit()
    return order_id


@pytest.mark.asyncio
async def test_sync_order_with_channel_marks_paid_when_query_confirms_success(monkeypatch):
    conn = get_conn()
    uid = _make_user()
    order_id = _make_stale_pending_order(conn, uid)

    async def _fake_query_order(cfg, *, order_id):
        return {"trade_state": "SUCCESS", "amount": {"total": 19900}, "transaction_id": "wx-query-1"}

    monkeypatch.setattr(reconcile.wechat, "query_order", _fake_query_order)
    monkeypatch.setattr(reconcile, "wechat_merchant_config", lambda: object())

    order = orders.get_order(conn, order_id)
    outcome = await reconcile.sync_order_with_channel(conn, order)
    assert outcome == "paid_by_query"
    assert orders.get_order(conn, order_id)["status"] == STATUS_FULFILLED


@pytest.mark.asyncio
async def test_sync_order_with_channel_leaves_unpaid_orders_alone(monkeypatch):
    conn = get_conn()
    uid = _make_user()
    order_id = _make_stale_pending_order(conn, uid)

    async def _fake_query_order(cfg, *, order_id):
        return {"trade_state": "NOTPAY"}

    monkeypatch.setattr(reconcile.wechat, "query_order", _fake_query_order)
    monkeypatch.setattr(reconcile, "wechat_merchant_config", lambda: object())

    order = orders.get_order(conn, order_id)
    outcome = await reconcile.sync_order_with_channel(conn, order)
    assert outcome == "still_unpaid"
    assert orders.get_order(conn, order_id)["status"] == "pending"


@pytest.mark.asyncio
async def test_sync_order_with_channel_skips_when_unconfigured(monkeypatch):
    conn = get_conn()
    uid = _make_user()
    order_id = _make_stale_pending_order(conn, uid)

    def _raise_unconfigured():
        raise PaymentConfigError("缺少 .env 变量: WECHAT_PAY_MCH_ID")

    monkeypatch.setattr(reconcile, "wechat_merchant_config", _raise_unconfigured)

    order = orders.get_order(conn, order_id)
    outcome = await reconcile.sync_order_with_channel(conn, order)
    assert outcome == "skipped_unconfigured"
    assert orders.get_order(conn, order_id)["status"] == "pending"


@pytest.mark.asyncio
async def test_reconcile_due_orders_retries_paid_but_unfulfilled_order(monkeypatch):
    """模拟"上一次发货和标记之间崩溃过"：订单已经是 paid 但从未 fulfilled，
    对账循环应该重跑发货（幂等）而不是当成待查单的 pending 订单处理。"""
    conn = get_conn()
    uid = _make_user()
    order_id = _make_stale_pending_order(conn, uid)
    orders.mark_paid(conn, order_id, channel_txn_id="wx-crash-recovery", paid_at=now())
    conn.commit()
    assert orders.get_order(conn, order_id)["status"] == STATUS_PAID

    counts = await reconcile.reconcile_due_orders(stale_after_s=0)
    assert counts.get("fulfilled_retry") == 1
    assert orders.get_order(conn, order_id)["status"] == STATUS_FULFILLED
