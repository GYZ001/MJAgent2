"""订单状态机 + 发货幂等的硬证明。

覆盖收尾自检要求：
1. 状态机只能前进（pending -> paid -> fulfilled，或 pending -> closed），
   非法跳转是结构上的 no-op（CAS 的 WHERE 卡住），不是靠调用方守规矩。
2. 金额不符（``PaymentMismatchError``）绝不发货——用第二条独立连接验证订单
   仍是 pending、没有任何 quota_ledger/tier 变化。
3. 重复确认同一笔支付（回调重放/对账循环重跑）只发一次货——同样用第二条
   独立连接读盘验证，不是同一连接读自己刚写的东西。
4. 两种商品类型（加量包/档位升级）各自的发货路径都接上了真实的
   ``app.quota_addon``/``users.tier``，不是只有订单状态机本身正确。
"""
from __future__ import annotations

import sqlite3

import pytest

from app import db, quota_addon
from app.auth.passwords import hash_password
from app.db import get_conn, new_id, now
from app.payments import orders
from app.payments.fulfillment import FulfillmentError, PaymentMismatchError, apply_confirmed_payment, fulfill_order
from app.payments.models import CHANNEL_WECHAT, PRODUCT_TIER_UPGRADE, PRODUCT_VIDEO_ADDON, STATUS_FULFILLED, STATUS_PAID


def _make_user(tier: str = "free") -> str:
    conn = get_conn()
    user_id = new_id("user")
    conn.execute(
        """INSERT INTO users(
               id, username, display_name, password_hash, auth_provider, status,
               is_system_admin, must_change_password, created_at, tier
           ) VALUES(?,?,?,?,'local','active',0,0,?,?)""",
        (user_id, f"buyer-{user_id}", "测试买家", hash_password("pw-test-000000"), now(), tier),
    )
    conn.commit()
    return user_id


def _make_addon_order(conn, user_id: str, *, packages: int = 1, amount_fen: int = 19900) -> str:
    order_id = new_id("pay")
    orders.create_order(
        conn, order_id=order_id, user_id=user_id, channel=CHANNEL_WECHAT, product=PRODUCT_VIDEO_ADDON,
        product_detail={"packages": packages}, amount_fen=amount_fen, created_at=now(),
    )
    conn.commit()
    return order_id


def _make_tier_order(conn, user_id: str, *, target_tier: str = "pro", amount_fen: int = 9900) -> str:
    order_id = new_id("pay")
    orders.create_order(
        conn, order_id=order_id, user_id=user_id, channel=CHANNEL_WECHAT, product=PRODUCT_TIER_UPGRADE,
        product_detail={"target_tier": target_tier}, amount_fen=amount_fen, created_at=now(),
    )
    conn.commit()
    return order_id


def _verify_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(db.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# 状态机
# ---------------------------------------------------------------------------

def test_order_starts_pending_and_mark_paid_transitions_once():
    conn = get_conn()
    uid = _make_user()
    order_id = _make_addon_order(conn, uid)
    assert orders.get_order(conn, order_id)["status"] == "pending"

    assert orders.mark_paid(conn, order_id, channel_txn_id="wx-txn-1", paid_at=now()) is True
    conn.commit()
    assert orders.get_order(conn, order_id)["status"] == STATUS_PAID

    # 重复迁移是 no-op，不会覆盖 channel_txn_id 或重复"支付一次"。
    assert orders.mark_paid(conn, order_id, channel_txn_id="wx-txn-DIFFERENT", paid_at=now()) is False
    assert orders.get_order(conn, order_id)["channel_txn_id"] == "wx-txn-1"


def test_close_order_only_affects_pending():
    conn = get_conn()
    uid = _make_user()
    order_id = _make_addon_order(conn, uid)
    orders.mark_paid(conn, order_id, channel_txn_id="wx-txn-2", paid_at=now())
    conn.commit()

    # 已经 paid 的订单不能被 close_order 影响——不是"退款"的替代品。
    assert orders.close_order(conn, order_id, reason="用户取消", closed_at=now()) is False
    assert orders.get_order(conn, order_id)["status"] == STATUS_PAID


def test_fulfill_order_rejects_pending_order():
    conn = get_conn()
    uid = _make_user()
    order_id = _make_addon_order(conn, uid)
    with pytest.raises(FulfillmentError):
        fulfill_order(conn, order_id)


# ---------------------------------------------------------------------------
# 金额校验：绝不能只信"成功"就发货
# ---------------------------------------------------------------------------

def test_amount_mismatch_rejected_and_verified_via_independent_connection():
    conn = get_conn()
    uid = _make_user()
    order_id = _make_addon_order(conn, uid, amount_fen=19900)

    order = orders.get_order(conn, order_id)
    with pytest.raises(PaymentMismatchError):
        apply_confirmed_payment(conn, order, channel_txn_id="wx-txn-bad", confirmed_amount_fen=100)

    verify = _verify_conn()
    row = verify.execute("SELECT status FROM payment_orders WHERE id=?", (order_id,)).fetchone()
    assert row["status"] == "pending"  # 金额不符：订单原地不动，不是"部分处理"
    ledger_rows = verify.execute(
        "SELECT COUNT(*) c FROM quota_ledger WHERE user_id=? AND resource='video_addon_seconds'", (uid,)
    ).fetchone()
    assert ledger_rows["c"] == 0  # 没有任何发货记账
    verify.close()


# ---------------------------------------------------------------------------
# 幂等：重复确认只发一次货（用第二条独立连接验证，不是同连接读自己写的）
# ---------------------------------------------------------------------------

def test_duplicate_confirmed_payment_only_delivers_once_video_addon():
    conn = get_conn()
    uid = _make_user()
    order_id = _make_addon_order(conn, uid, packages=2, amount_fen=39800)
    order = orders.get_order(conn, order_id)

    result1 = apply_confirmed_payment(conn, order, channel_txn_id="wx-txn-3", confirmed_amount_fen=39800)
    assert result1["already_fulfilled"] is False

    # 重放：同一笔支付的回调/查单结果再来一次（渠道重试的真实行为）。
    order_again = orders.get_order(conn, order_id)
    result2 = apply_confirmed_payment(conn, order_again, channel_txn_id="wx-txn-3", confirmed_amount_fen=39800)
    assert result2["already_fulfilled"] is True

    verify = _verify_conn()
    order_row = verify.execute("SELECT status FROM payment_orders WHERE id=?", (order_id,)).fetchone()
    assert order_row["status"] == STATUS_FULFILLED
    grant_rows = verify.execute(
        "SELECT COUNT(*) c FROM quota_ledger WHERE resource='video_addon_seconds' AND attempt_key=? AND reason='grant'",
        (order_id,),
    ).fetchone()
    assert grant_rows["c"] == 1  # 恰好一行，不是两行
    balance = verify.execute(
        "SELECT COALESCE(SUM(delta),0) s FROM quota_ledger WHERE user_id=? AND resource='video_addon_seconds'",
        (uid,),
    ).fetchone()
    assert balance["s"] == 2 * quota_addon.ADDON_PACKAGE_SECONDS  # 2 包，没有翻倍
    verify.close()


def test_duplicate_confirmed_payment_only_delivers_once_tier_upgrade():
    conn = get_conn()
    uid = _make_user(tier="free")
    order_id = _make_tier_order(conn, uid, target_tier="pro", amount_fen=9900)
    order = orders.get_order(conn, order_id)

    apply_confirmed_payment(conn, order, channel_txn_id="wx-txn-4", confirmed_amount_fen=9900)
    order_again = orders.get_order(conn, order_id)
    result2 = apply_confirmed_payment(conn, order_again, channel_txn_id="wx-txn-4", confirmed_amount_fen=9900)
    assert result2["already_fulfilled"] is True

    verify = _verify_conn()
    user_row = verify.execute("SELECT tier FROM users WHERE id=?", (uid,)).fetchone()
    assert user_row["tier"] == "pro"
    order_row = verify.execute("SELECT status FROM payment_orders WHERE id=?", (order_id,)).fetchone()
    assert order_row["status"] == STATUS_FULFILLED
    verify.close()


def test_fulfill_order_no_op_on_already_fulfilled_order():
    conn = get_conn()
    uid = _make_user()
    order_id = _make_addon_order(conn, uid)
    order = orders.get_order(conn, order_id)
    apply_confirmed_payment(conn, order, channel_txn_id="wx-txn-5", confirmed_amount_fen=19900)

    result = fulfill_order(conn, order_id)
    assert result == {"already_fulfilled": True, "order_id": order_id}
