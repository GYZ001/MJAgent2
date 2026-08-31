"""支付宝：PC 网站支付（``alipay.trade.page.pay``）下单 + 回调验签 + 主动查单。

选 PC 网站支付而不是手机网站支付（``alipay.trade.wap.pay``）：两者接入形状
完全一致（都是"拼参数签名 -> 跳转网关 URL"，不像微信 Native/JSAPI 那样有
"要不要 openid" 的实质分支），选哪个只是最终跳转页面的样式差异；本产品当前
是桌面端为主的 Web 应用（监制房类工具），与微信侧选 Native（同样不需要移动端
专属的身份换取）保持同一种"最简单、不依赖额外身份绑定"的取舍口径。

签名用 RSA2（SHA256withRSA），公钥模式：应用私钥签我方请求，支付宝公钥验
回调——都用 ``app.payments.crypto_rsa``（纯标准库，见该模块风险披露）。
"""
from __future__ import annotations

import base64
import json
import time
from typing import Any
from urllib.parse import quote_plus

import httpx

from app.payments.config import AlipayMerchantConfig
from app.payments.crypto_asn1 import DerError, rsa_private_key_from_pem
from app.payments.crypto_rsa import load_public_key, sign_pkcs1v15_sha256, verify_pkcs1v15_sha256

_SUCCESS_TRADE_STATUSES = frozenset({"TRADE_SUCCESS", "TRADE_FINISHED"})


class AlipayNotifyError(ValueError):
    """回调验签/内容校验失败——路由层必须整体拒绝，不发货。"""


class AlipayApiError(RuntimeError):
    """调用支付宝网关本身失败。"""


def _fen_to_yuan_str(amount_fen: int) -> str:
    """分转元，纯整数运算拼字符串，不经过浮点除法。"""
    return f"{amount_fen // 100}.{amount_fen % 100:02d}"


def _sign_string(params: dict[str, str]) -> str:
    """签名基串：按 key 升序拼 ``k=v&k=v...``，排除空值与 ``sign``/``byte_sign``
    字段本身——公开签名算法，两端（我方签请求 / 支付宝签通知）共用。"""
    items = sorted((k, v) for k, v in params.items() if v and k not in ("sign", "sign_type"))
    return "&".join(f"{k}={v}" for k, v in items)


def _common_params(cfg: AlipayMerchantConfig, *, method: str, biz_content: str) -> dict[str, str]:
    return {
        "app_id": cfg.app_id, "method": method, "format": "JSON", "charset": "utf-8",
        "sign_type": "RSA2", "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "version": "1.0", "biz_content": biz_content,
    }


def _sign_request(cfg: AlipayMerchantConfig, params: dict[str, str]) -> str:
    n, d = rsa_private_key_from_pem(cfg.private_key_pem)
    signature = sign_pkcs1v15_sha256(_sign_string(params).encode("utf-8"), n, d)
    return base64.b64encode(signature).decode("ascii")


def build_page_pay_redirect_url(
    cfg: AlipayMerchantConfig, *, order_id: str, amount_fen: int, subject: str, notify_url: str,
) -> str:
    """构造电脑网站支付跳转 URL（前端把用户导向这个地址完成支付）。本环境没有
    真实商户号/私钥，这条链路结构完整但未做端到端验证，见验收报告。
    """
    biz_content = json.dumps(
        {
            "out_trade_no": order_id, "product_code": "FAST_INSTANT_TRADE_PAY",
            "total_amount": _fen_to_yuan_str(amount_fen), "subject": subject,
        },
        ensure_ascii=False, separators=(",", ":"),
    )
    params = _common_params(cfg, method="alipay.trade.page.pay", biz_content=biz_content)
    params["notify_url"] = notify_url
    params["sign"] = _sign_request(cfg, params)
    query = "&".join(f"{k}={quote_plus(v)}" for k, v in sorted(params.items()))
    return f"{cfg.gateway_url}?{query}"


async def query_order(cfg: AlipayMerchantConfig, *, order_id: str) -> dict[str, Any]:
    """主动查单（对账用，``alipay.trade.query``）。"""
    biz_content = json.dumps({"out_trade_no": order_id}, ensure_ascii=False, separators=(",", ":"))
    params = _common_params(cfg, method="alipay.trade.query", biz_content=biz_content)
    params["sign"] = _sign_request(cfg, params)
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(cfg.gateway_url, data=params)
    if resp.status_code >= 300:
        raise AlipayApiError(f"支付宝查单失败: HTTP {resp.status_code} {resp.text[:500]}")
    body = resp.json()
    return body.get("alipay_trade_query_response", body)


def verify_and_parse_notify(cfg: AlipayMerchantConfig, *, form: dict[str, str]) -> dict[str, str]:
    """验签支付宝异步通知（``application/x-www-form-urlencoded``）表单字段，
    验签通过后原样返回该表单（``out_trade_no``/``trade_no``/``trade_status``/
    ``total_amount`` 等业务字段都在里面）。签名字段本身缺失、格式非法、验签
    失败，一律抛 ``AlipayNotifyError``，不留旁路。
    """
    signature_b64 = form.get("sign", "")
    if not signature_b64:
        raise AlipayNotifyError("回调缺少 sign 字段")
    message = _sign_string(form).encode("utf-8")
    try:
        n, e = load_public_key(cfg.alipay_public_key_pem)
        signature = base64.b64decode(signature_b64)
    except DerError as exc:
        raise AlipayNotifyError(f"支付宝公钥解析失败: {exc}") from exc
    except Exception as exc:  # noqa: BLE001 — base64 解码失败等统一按验签失败处理
        raise AlipayNotifyError(f"签名字段解码失败: {exc}") from exc
    if not verify_pkcs1v15_sha256(message, signature, n, e):
        raise AlipayNotifyError("支付宝回调验签失败，拒绝这条通知")
    return form


def notify_is_paid(form: dict[str, str]) -> bool:
    return form.get("trade_status") in _SUCCESS_TRADE_STATUSES


def notify_amount_fen(form: dict[str, str]) -> int:
    """把回调里的 ``total_amount``（元，字符串）换算成分（整数），拒绝任何
    非法格式（不是"看着像数字就硬转"——非法格式本身就应该被当成篡改处理）。
    """
    raw = form.get("total_amount", "")
    if "." not in raw:
        raise AlipayNotifyError(f"total_amount 格式非法: {raw!r}")
    yuan_part, _, fen_part = raw.partition(".")
    fen_part = (fen_part + "00")[:2]
    if not (yuan_part.isdigit() and fen_part.isdigit()):
        raise AlipayNotifyError(f"total_amount 格式非法: {raw!r}")
    return int(yuan_part) * 100 + int(fen_part)
