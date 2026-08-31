"""对账：主动查渠道单 + 后台巡检循环，收敛"用户付了但回调没到"这类情况。

后台循环函数**只定义，不注册**——``app/recovery.py`` 目前被另一个 agent 占用，
按派单要求不去碰它；注册方式（``task_registry.spawn`` + 挂在
``app/main.py::lifespan`` 恢复协调者分支）写在验收报告里，由派单方接线。
本文件自身与 ``app/recovery.py`` 现有的巡检循环（``project_recycle_bin_sweep_loop``
等）用同一种调度形态：``while True`` + ``try/except`` 吞掉单轮异常 + 有界
``asyncio.sleep``，单笔订单处理失败不影响其余订单，也不让循环退出。
"""
from __future__ import annotations

import asyncio
import sqlite3

from app.db import get_conn, now
from app.payments import alipay, orders, wechat
from app.payments.config import PaymentConfigError, alipay_merchant_config, wechat_merchant_config
from app.payments.fulfillment import apply_confirmed_payment, fulfill_order
from app.payments.models import CHANNEL_ALIPAY, CHANNEL_WECHAT

#: 下单后多久还没等到回调才主动查一次渠道（秒）；太短会在用户还在扫码/输密码
#: 时就去查，意义不大且浪费一次 API 调用。
_DEFAULT_STALE_AFTER_S = 900.0


async def _query_channel_paid_amount(conn: sqlite3.Connection, order: sqlite3.Row) -> int | None:
    """查渠道侧这笔订单是否已支付成功；已支付返回渠道确认的金额（分），未支付/
    未找到返回 ``None``。渠道未配置（``PaymentConfigError``）视为"这一轮跳过"，
    不当错误处理——本环境本来就没有真实商户号。
    """
    if order["channel"] == CHANNEL_WECHAT:
        result = await wechat.query_order(wechat_merchant_config(), order_id=order["id"])
        if result.get("trade_state") == "SUCCESS":
            return int(result["amount"]["total"])
        return None
    if order["channel"] == CHANNEL_ALIPAY:
        result = await alipay.query_order(alipay_merchant_config(), order_id=order["id"])
        if str(result.get("trade_status")) in ("TRADE_SUCCESS", "TRADE_FINISHED"):
            return alipay.notify_amount_fen({"total_amount": str(result.get("total_amount", ""))})
        return None
    return None


async def sync_order_with_channel(conn: sqlite3.Connection, order: sqlite3.Row) -> str:
    """查一次渠道侧真实状态并按结果收敛。对账循环（批量、只处理 pending）与
    ``routes.sync_order``（单笔、用户主动触发，任意状态都能调）共用这一个
    函数——只要渠道确认已支付，``apply_confirmed_payment`` 内部的状态 CAS 与
    发货幂等本身就保证了对非 pending 订单调用也是安全的。
    """
    try:
        confirmed_amount_fen = await _query_channel_paid_amount(conn, order)
    except PaymentConfigError:
        return "skipped_unconfigured"
    if confirmed_amount_fen is None:
        return "still_unpaid"
    apply_confirmed_payment(
        conn, order, channel_txn_id=f"reconcile:{order['id']}", confirmed_amount_fen=confirmed_amount_fen,
    )
    return "paid_by_query"


async def reconcile_due_orders(*, stale_after_s: float = _DEFAULT_STALE_AFTER_S) -> dict[str, int]:
    """一轮对账。返回处理计数，供日志/测试断言。"""
    conn = get_conn()
    counts: dict[str, int] = {}

    def _bump(key: str) -> None:
        counts[key] = counts.get(key, 0) + 1

    for order in orders.list_stale_pending_orders(conn, older_than_s=stale_after_s, now=now()):
        try:
            _bump(await sync_order_with_channel(conn, order))
        except Exception as exc:  # noqa: BLE001 — 单笔失败不影响其余订单
            _bump("errors")
            from app.errors import log_error
            log_error(
                exc, action="payment_reconcile.pending",
                context={"order_id": order["id"], "channel": order["channel"]},
                meta={"stage": "payment_reconcile"},
            )
    for order in orders.list_unfulfilled_paid_orders(conn):
        try:
            fulfill_order(conn, order["id"])
            _bump("fulfilled_retry")
        except Exception as exc:  # noqa: BLE001 — 单笔失败不影响其余订单
            _bump("errors")
            from app.errors import log_error
            log_error(
                exc, action="payment_reconcile.paid_unfulfilled",
                context={"order_id": order["id"]}, meta={"stage": "payment_reconcile"},
            )
    return counts


async def payment_reconcile_loop(interval_s: float = 300.0) -> None:
    """后台巡检循环——**未注册**，见模块文档。派单方按
    ``app/recovery.py`` 里 ``project_recycle_bin_sweep_loop`` 同款
    ``task_registry.spawn("system", "payment_reconcile", payment_reconcile_loop())``
    的写法接线到 ``app/main.py::lifespan`` 的恢复协调者分支即可。
    """
    while True:
        try:
            await reconcile_due_orders()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — 巡检循环自身不得因单轮失败退出
            from app.errors import log_error
            log_error(
                exc, action="payment_reconcile_loop", context={"interval_s": interval_s},
                meta={"stage": "payment_reconcile", "isolation": "loop"},
            )
        await asyncio.sleep(max(60.0, min(float(interval_s), 900.0)))
