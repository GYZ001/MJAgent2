"""按订单发货：调用**已有的**记账/账号写入逻辑，不重新实现。

加量包走 ``app.quota_addon.grant_video_addon_seconds``（``quota_ledger``
``UNIQUE(resource, attempt_key, reason)`` 保证幂等，``attempt_key`` 就是订单
号）；档位升级直接 ``UPDATE users SET tier=?``——和
``app.auth.admin_api.update_user`` 现有的管理员改档位路径同一种写法，档位
本来就没有 ledger，不是本模块发明了新记账方式。

幂等靠两层：订单自身的状态 CAS（``paid -> fulfilled`` 只能成功一次，见
``orders.mark_fulfilled``）是第一层；加量包额外有 ``quota_ledger`` 的
``attempt_key`` 唯一约束做第二层防线。档位升级只有第一层——直接写字段没有
"记一笔" 的概念，重复执行本身就是幂等的（写同一个值两次结果一样）。
"""
from __future__ import annotations

import json
import sqlite3

from app.db import now
from app.payments import orders
from app.payments.models import PRODUCT_TIER_UPGRADE, PRODUCT_VIDEO_ADDON, STATUS_FULFILLED, STATUS_PAID
from app.quota_addon import grant_video_addon_seconds


class FulfillmentError(RuntimeError):
    """订单状态不允许发货（不是 paid），或商品类型未知——调用方应视为 409。"""


class PaymentMismatchError(RuntimeError):
    """渠道确认的金额与订单记录不符——绝不发货，路由层应视为 400 并整体拒绝
    这条回调/查单结果（不是警告，是拒绝）。"""


def _deliver(conn: sqlite3.Connection, order: sqlite3.Row) -> dict:
    detail = json.loads(order["product_detail_json"])
    if order["product"] == PRODUCT_VIDEO_ADDON:
        return grant_video_addon_seconds(
            conn, order["user_id"], packages=int(detail["packages"]), attempt_key=order["id"],
        )
    if order["product"] == PRODUCT_TIER_UPGRADE:
        conn.execute("UPDATE users SET tier=? WHERE id=?", (detail["target_tier"], order["user_id"]))
        return {"tier": detail["target_tier"]}
    raise FulfillmentError(f"未知商品类型: {order['product']}")


def fulfill_order(conn: sqlite3.Connection, order_id: str) -> dict:
    """把一笔 ``paid`` 订单发货并标记 ``fulfilled``；对已 ``fulfilled`` 的订单
    重复调用是安全的只读 no-op（回调重放/对账循环重跑都会走到这里）。

    发货写入与订单状态 CAS 在同一次事务里提交：任一步失败整体回滚，订单退回
    ``paid``，下一次重跑（重复回调或对账循环）会再试一次——不会出现"发了货但
    订单没标记"或"标记了但没发货"两边不一致的情况。
    """
    order = orders.get_order(conn, order_id)
    if order is None:
        raise FulfillmentError(f"订单不存在: {order_id}")
    if order["status"] == STATUS_FULFILLED:
        return {"already_fulfilled": True, "order_id": order_id}
    if order["status"] != STATUS_PAID:
        raise FulfillmentError(f"订单状态是 {order['status']!r}，还不能发货")
    try:
        delivery_result = _deliver(conn, order)
        transitioned = orders.mark_fulfilled(conn, order_id, fulfilled_at=now())
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"already_fulfilled": not transitioned, "order_id": order_id, "delivery": delivery_result}


def apply_confirmed_payment(
    conn: sqlite3.Connection, order: sqlite3.Row, *, channel_txn_id: str, confirmed_amount_fen: int,
) -> dict:
    """渠道（回调或主动查单）确认"这笔订单收到钱了"之后的唯一入口：先核金额，
    金额不符直接拒绝——不能只信渠道说"成功"就发货；核对通过才推进
    ``pending -> paid``，再发货。通知处理器（``routes.py``）与对账循环
    （``reconcile.py``）共用这一个函数，不各自实现一遍"验完金额该干什么"。
    """
    if confirmed_amount_fen != order["amount_fen"]:
        raise PaymentMismatchError(
            f"订单 {order['id']} 金额不符：渠道确认 {confirmed_amount_fen} 分，"
            f"订单记录 {order['amount_fen']} 分"
        )
    orders.mark_paid(conn, order["id"], channel_txn_id=channel_txn_id, paid_at=now())
    conn.commit()
    return fulfill_order(conn, order["id"])
