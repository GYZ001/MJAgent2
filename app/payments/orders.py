"""``payment_orders`` 表的读写原语：建单、状态迁移、查询。

所有函数都接一个调用方持有的 ``conn``，都不自行 ``commit()``——所有权在调用方
（路由层/对账循环），见模块 ``fulfillment.py`` 和 ``routes.py`` 里具体的事务
边界划法。状态迁移一律用 ``UPDATE ... WHERE status=期望的当前状态`` 做原子
compare-and-swap，SQLite 单写者串行化保证并发下不会出现"两次都判断成功"。
"""
from __future__ import annotations

import json
import sqlite3

from app.payments.models import STATUS_FULFILLED, STATUS_PAID, STATUS_PENDING


def create_order(
    conn: sqlite3.Connection,
    *,
    order_id: str,
    user_id: str,
    channel: str,
    product: str,
    product_detail: dict,
    amount_fen: int,
    created_at: float,
) -> None:
    """插入一条 ``pending`` 订单。金额/商品合法性由调用方在算 ``amount_fen``
    时已经校验过（``models.resolve_amount_fen``），这里不重复校验，只负责落库。
    """
    conn.execute(
        "INSERT INTO payment_orders("
        "  id, user_id, channel, product, product_detail_json, amount_fen,"
        "  status, created_at"
        ") VALUES(?,?,?,?,?,?,?,?)",
        (
            order_id, user_id, channel, product,
            json.dumps(product_detail, ensure_ascii=False), amount_fen,
            STATUS_PENDING, created_at,
        ),
    )


def get_order(conn: sqlite3.Connection, order_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM payment_orders WHERE id=?", (order_id,)).fetchone()


def get_order_for_user(conn: sqlite3.Connection, order_id: str, user_id: str) -> sqlite3.Row | None:
    """按用户过滤的查询——普通用户查订单状态只能看自己的，不能靠猜订单号看别人的。"""
    return conn.execute(
        "SELECT * FROM payment_orders WHERE id=? AND user_id=?", (order_id, user_id)
    ).fetchone()


def mark_paid(
    conn: sqlite3.Connection, order_id: str, *, channel_txn_id: str, paid_at: float,
) -> bool:
    """``pending -> paid`` 原子迁移。返回是否真的发生了迁移；``False`` 表示
    订单已经是 ``paid``/``fulfilled``（重复回调的正常重放），调用方应继续走
    发货步骤（发货本身幂等），不当错误处理。金额校验必须在调用这个函数之前
    完成——这里只管状态迁移，不重复验一遍金额。
    """
    cur = conn.execute(
        "UPDATE payment_orders SET status=?, channel_txn_id=?, paid_at=? "
        "WHERE id=? AND status=?",
        (STATUS_PAID, channel_txn_id, paid_at, order_id, STATUS_PENDING),
    )
    return cur.rowcount == 1


def mark_fulfilled(conn: sqlite3.Connection, order_id: str, *, fulfilled_at: float) -> bool:
    """``paid -> fulfilled`` 原子迁移，调用方（``fulfillment.fulfill_order``）
    负责把这条 UPDATE 和真正发货的写入放进同一次 commit。"""
    cur = conn.execute(
        "UPDATE payment_orders SET status=?, fulfilled_at=? WHERE id=? AND status=?",
        (STATUS_FULFILLED, fulfilled_at, order_id, STATUS_PAID),
    )
    return cur.rowcount == 1


def list_stale_pending_orders(
    conn: sqlite3.Connection, *, older_than_s: float, now: float, limit: int = 200,
) -> list[sqlite3.Row]:
    """超过 ``older_than_s`` 还没等到回调的 ``pending`` 订单——对账循环用来做
    主动查单（可能是回调丢了，也可能用户压根没付）。"""
    cutoff = now - older_than_s
    return conn.execute(
        "SELECT * FROM payment_orders WHERE status=? AND created_at<=? "
        "ORDER BY created_at ASC LIMIT ?",
        (STATUS_PENDING, cutoff, limit),
    ).fetchall()


def list_unfulfilled_paid_orders(conn: sqlite3.Connection, *, limit: int = 200) -> list[sqlite3.Row]:
    """已支付但还没发货成功的订单——正常情况下发货紧跟支付发生，这里非空
    通常意味着上一次 ``fulfill_order`` 在发货和标记之间崩溃过，需要对账循环
    补跑（``fulfill_order`` 本身幂等，重跑安全）。"""
    return conn.execute(
        "SELECT * FROM payment_orders WHERE status=? ORDER BY paid_at ASC LIMIT ?",
        (STATUS_PAID, limit),
    ).fetchall()
