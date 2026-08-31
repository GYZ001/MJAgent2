"""支付 REST 入口：下单（登录用户自助）+ 回调（公开，验签是唯一防线）+ 查单。

``router`` 挂会话鉴权（普通登录用户即可，不要求系统管理员——买加量包/升级
档位是账号自己的消费行为）；``public_router`` 不挂任何鉴权，回调请求量级
上等价于 ``app.system_api.public_router``（健康检查那一挂），见
``app/main.py`` 里怎么两条分别 ``include_router``。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from app.auth.principal import get_current_principal
from app.db import get_conn, new_id, now
from app.local_session import require_local_session
from app.payments import alipay, orders, wechat
from app.payments.config import (
    PaymentConfigError,
    alipay_merchant_config,
    public_base_url,
    wechat_merchant_config,
)
from app.payments.fulfillment import FulfillmentError, PaymentMismatchError, apply_confirmed_payment
from app.payments.models import (
    CHANNEL_ALIPAY,
    CHANNEL_WECHAT,
    VALID_CHANNELS,
    CreateOrderRequest,
    PricingError,
    resolve_amount_fen,
)
from app.payments.reconcile import sync_order_with_channel

router = APIRouter(prefix="/api/payments", dependencies=[Depends(require_local_session)])
public_router = APIRouter(prefix="/api/payments")


def _order_view(order) -> dict:
    return {
        "order_id": order["id"], "channel": order["channel"], "product": order["product"],
        "amount_fen": order["amount_fen"], "status": order["status"],
        "channel_txn_id": order["channel_txn_id"], "created_at": order["created_at"],
        "paid_at": order["paid_at"], "fulfilled_at": order["fulfilled_at"],
    }


@router.post("/orders")
async def create_order(body: CreateOrderRequest):
    """自助下单：登录用户购买自己账号的加量包/档位升级。返回渠道要求的支付
    参数（微信 code_url 前端渲成二维码；支付宝 redirect_url 前端跳转）。
    """
    principal = get_current_principal()
    if principal is None:
        raise HTTPException(401, "缺少或无效的本机会话凭证")
    if body.channel not in VALID_CHANNELS:
        raise HTTPException(422, f"channel 必须是 {'/'.join(sorted(VALID_CHANNELS))} 之一")
    try:
        amount_fen = resolve_amount_fen(body.product, body.product_detail())
    except PricingError as exc:
        raise HTTPException(422, str(exc)) from exc

    conn = get_conn()
    order_id = new_id("pay")
    orders.create_order(
        conn, order_id=order_id, user_id=principal.user_id, channel=body.channel,
        product=body.product, product_detail=body.product_detail(),
        amount_fen=amount_fen, created_at=now(),
    )
    conn.commit()

    try:
        pay_params = await _build_pay_params(body.channel, order_id=order_id, amount_fen=amount_fen)
    except PaymentConfigError as exc:
        raise _config_error_response(exc) from exc
    return {"order_id": order_id, "channel": body.channel, "amount_fen": amount_fen, "pay_params": pay_params}


def _config_error_response(exc: PaymentConfigError) -> HTTPException:
    """5xx 走 ``app.errors`` 的技术类分类会把原文脱敏成"系统内部错误"通用提示
    （见 ``app/errors.py::classify``——状态码落在 5xx 一律归 technical 类）。
    这里的消息恰恰是要给运维/调用方看的"去哪配"指引，不是要隐藏的内部细节，
    按本仓库既有惯例（``app/orchestration/api.py`` 等多处 503）用 dict detail
    绕开脱敏——``_error_json`` 只在 ``isinstance(detail, dict)`` 时才透传原样。
    """
    return HTTPException(503, {"code": "PAYMENT_CONFIG_MISSING", "message": str(exc)})


async def _build_pay_params(channel: str, *, order_id: str, amount_fen: int) -> dict:
    description = "漫剧 Agent 购买"
    if channel == CHANNEL_WECHAT:
        cfg = wechat_merchant_config()
        notify_url = f"{public_base_url()}/api/payments/notify/wechat"
        code_url = await wechat.create_native_order(
            cfg, order_id=order_id, amount_fen=amount_fen, description=description, notify_url=notify_url,
        )
        return {"code_url": code_url}
    cfg = alipay_merchant_config()
    notify_url = f"{public_base_url()}/api/payments/notify/alipay"
    redirect_url = alipay.build_page_pay_redirect_url(
        cfg, order_id=order_id, amount_fen=amount_fen, subject=description, notify_url=notify_url,
    )
    return {"redirect_url": redirect_url}


@router.get("/orders/{order_id}")
def get_order(order_id: str):
    principal = get_current_principal()
    if principal is None:
        raise HTTPException(401, "缺少或无效的本机会话凭证")
    order = orders.get_order_for_user(get_conn(), order_id, principal.user_id)
    if order is None:
        raise HTTPException(404, "订单不存在")
    return _order_view(order)


@router.post("/orders/{order_id}/sync")
async def sync_order(order_id: str):
    """主动查单：用户支付完成后返回本产品页面，不等被动回调，立即查一次渠道
    侧真实状态并按结果收敛（已支付则发货，幂等安全）。"""
    principal = get_current_principal()
    if principal is None:
        raise HTTPException(401, "缺少或无效的本机会话凭证")
    conn = get_conn()
    order = orders.get_order_for_user(conn, order_id, principal.user_id)
    if order is None:
        raise HTTPException(404, "订单不存在")
    try:
        outcome = await sync_order_with_channel(conn, order)
    except PaymentConfigError as exc:
        raise _config_error_response(exc) from exc
    except (PaymentMismatchError, FulfillmentError) as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"outcome": outcome, **_order_view(orders.get_order(conn, order_id))}


@public_router.post("/notify/wechat")
async def wechat_notify(request: Request):
    """微信支付异步通知：无会话鉴权，验签是唯一防线。成功要回 JSON
    ``{"code":"SUCCESS"}``；任何拒绝都返回非 2xx，微信会按其重试策略重试。"""
    raw_body = await request.body()
    headers = {k.lower(): v for k, v in request.headers.items()}
    try:
        cfg = wechat_merchant_config()
        payload = wechat.verify_and_decrypt_notify(cfg, headers=headers, raw_body=raw_body)
    except (PaymentConfigError, wechat.WechatNotifyError) as exc:
        _log_notify_rejected("wechat", str(exc))
        raise HTTPException(400, "FAIL") from exc
    if payload.get("trade_state") != "SUCCESS":
        return {"code": "SUCCESS", "message": "成功"}  # 非成功状态的通知，确认收到但不发货
    await _settle_notified_order(
        CHANNEL_WECHAT, out_trade_no=payload.get("out_trade_no", ""),
        channel_txn_id=str(payload.get("transaction_id", "")),
        confirmed_amount_fen=int(payload.get("amount", {}).get("total", -1)),
    )
    return {"code": "SUCCESS", "message": "成功"}


@public_router.post("/notify/alipay")
async def alipay_notify(request: Request):
    """支付宝异步通知：无会话鉴权，验签是唯一防线。成功必须回纯文本
    ``success``（不是 JSON）；任何拒绝返回非 ``success`` 文本，支付宝会重试。"""
    form = dict((await request.form()).items())
    try:
        cfg = alipay_merchant_config()
        verified = alipay.verify_and_parse_notify(cfg, form=form)
    except (PaymentConfigError, alipay.AlipayNotifyError) as exc:
        _log_notify_rejected("alipay", str(exc))
        raise HTTPException(400, "fail") from exc
    if not alipay.notify_is_paid(verified):
        return Response(content="success", media_type="text/plain")
    try:
        confirmed_amount_fen = alipay.notify_amount_fen(verified)
    except alipay.AlipayNotifyError as exc:
        _log_notify_rejected("alipay", str(exc))
        raise HTTPException(400, "fail") from exc
    await _settle_notified_order(
        CHANNEL_ALIPAY, out_trade_no=verified.get("out_trade_no", ""),
        channel_txn_id=str(verified.get("trade_no", "")), confirmed_amount_fen=confirmed_amount_fen,
    )
    return Response(content="success", media_type="text/plain")


async def _settle_notified_order(
    channel: str, *, out_trade_no: str, channel_txn_id: str, confirmed_amount_fen: int,
) -> None:
    conn = get_conn()
    order = orders.get_order(conn, out_trade_no)
    if order is None or order["channel"] != channel:
        _log_notify_rejected(channel, f"回调订单号未知或渠道不符: {out_trade_no}")
        raise HTTPException(400, "unknown order")
    try:
        apply_confirmed_payment(
            conn, order, channel_txn_id=channel_txn_id, confirmed_amount_fen=confirmed_amount_fen,
        )
    except PaymentMismatchError as exc:
        _log_notify_rejected(channel, str(exc))
        raise HTTPException(400, "amount mismatch") from exc


def _log_notify_rejected(channel: str, reason: str) -> None:
    from app.errors import log_error
    log_error(
        ValueError(reason), action="payments.notify_rejected",
        context={"channel": channel}, meta={"stage": "payment_notify"},
    )
