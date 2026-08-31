"""微信支付 V3：Native（扫码支付）下单 + 回调验签解密 + 主动查单。

选 Native 而不是 JSAPI：JSAPI 需要先用 code 换用户 openid（要么走公众号网页
授权，要么走小程序登录），这条身份绑定链路本单没有——本产品目前是纯 Web 应用，
没有已接入的公众号/小程序登录态。Native 只需要商户号+APPID 就能生成 ``code_url``
（前端渲成二维码，用户扫码在微信内完成支付），服务端集成最简单，且没有
"必须先有 openid" 这个额外前置依赖，最贴近"先把骨架做扎实"这个目标。

签名用 ``app.payments.crypto_rsa``（纯标准库 RSA-SHA256/PKCS1v1.5，见该模块
文档的风险披露）。
"""
from __future__ import annotations

import base64
import json
import time
import uuid
from typing import Any

import httpx

from app.payments.config import WechatMerchantConfig
from app.payments.crypto_aesgcm import AeadError, aes256_gcm_decrypt
from app.payments.crypto_asn1 import DerError
from app.payments.crypto_rsa import load_public_key, sign_pkcs1v15_sha256, verify_pkcs1v15_sha256

_NATIVE_ORDER_PATH = "/v3/pay/transactions/native"
_QUERY_PATH_TEMPLATE = "/v3/pay/transactions/out-trade-no/{order_id}"
#: 回调时间戳与本地时钟的最大允许偏差（秒），超过按重放/伪造拒绝。
_NOTIFY_TIMESTAMP_TOLERANCE_S = 300


class WechatNotifyError(ValueError):
    """回调验签/解密/内容校验失败——路由层必须整体拒绝这条通知，不发货。"""


class WechatApiError(RuntimeError):
    """调用微信支付 API 本身失败（网络错误、微信返回非 2xx）。"""


def _signature_message(method: str, url_path: str, timestamp: str, nonce: str, body: str) -> bytes:
    return f"{method}\n{url_path}\n{timestamp}\n{nonce}\n{body}\n".encode("utf-8")


def _authorization_header(cfg: WechatMerchantConfig, *, method: str, url_path: str, body: str) -> str:
    """构造 ``WECHATPAY2-SHA256-RSA2048`` 签名串（微信支付 V3 API 认证规范）。

    ``serial_no`` 在这个头里指的是**商户**证书序列号（告诉微信用哪张商户公钥
    验我方签名），不是下面验回调要用的**平台**证书序列号——两者概念不同，
    容易搞混，命名上用 ``cfg.serial_no`` 精确对应"商户"这一份。
    """
    from app.payments.crypto_asn1 import rsa_private_key_from_pem

    timestamp = str(int(time.time()))
    nonce = uuid.uuid4().hex
    message = _signature_message(method, url_path, timestamp, nonce, body)
    n, d = rsa_private_key_from_pem(cfg.private_key_pem)
    signature = base64.b64encode(sign_pkcs1v15_sha256(message, n, d)).decode("ascii")
    return (
        f'WECHATPAY2-SHA256-RSA2048 mchid="{cfg.mch_id}",nonce_str="{nonce}",'
        f'timestamp="{timestamp}",serial_no="{cfg.serial_no}",signature="{signature}"'
    )


async def create_native_order(
    cfg: WechatMerchantConfig, *, order_id: str, amount_fen: int, description: str, notify_url: str,
) -> str:
    """向微信下单，返回 ``code_url``（前端渲成二维码）。本环境没有真实商户号，
    这个调用在这里必然失败——结构完整、字段正确，供接入真实商户号后直接可用；
    见验收报告"上线还差什么"。
    """
    body_obj = {
        "appid": cfg.app_id, "mchid": cfg.mch_id, "description": description,
        "out_trade_no": order_id, "notify_url": notify_url,
        "amount": {"total": amount_fen, "currency": "CNY"},
    }
    body = json.dumps(body_obj, ensure_ascii=False, separators=(",", ":"))
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": _authorization_header(cfg, method="POST", url_path=_NATIVE_ORDER_PATH, body=body),
    }
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(cfg.api_base + _NATIVE_ORDER_PATH, content=body, headers=headers)
    if resp.status_code >= 300:
        raise WechatApiError(f"微信支付下单失败: HTTP {resp.status_code} {resp.text[:500]}")
    code_url = resp.json().get("code_url")
    if not code_url:
        raise WechatApiError(f"微信支付下单响应缺少 code_url: {resp.text[:500]}")
    return code_url


async def query_order(cfg: WechatMerchantConfig, *, order_id: str) -> dict:
    """主动查单（对账用）：按商户订单号查渠道侧真实状态。"""
    url_path = _QUERY_PATH_TEMPLATE.format(order_id=order_id) + f"?mchid={cfg.mch_id}"
    headers = {
        "Accept": "application/json",
        "Authorization": _authorization_header(cfg, method="GET", url_path=url_path, body=""),
    }
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.get(cfg.api_base + url_path, headers=headers)
    if resp.status_code == 404:
        return {"trade_state": "NOTFOUND"}
    if resp.status_code >= 300:
        raise WechatApiError(f"微信支付查单失败: HTTP {resp.status_code} {resp.text[:500]}")
    return resp.json()


def _verify_signature(cfg: WechatMerchantConfig, headers: dict[str, str], raw_body: bytes) -> None:
    timestamp = headers.get("wechatpay-timestamp", "")
    nonce = headers.get("wechatpay-nonce", "")
    signature_b64 = headers.get("wechatpay-signature", "")
    if not (timestamp and nonce and signature_b64):
        raise WechatNotifyError("回调缺少 Wechatpay-Timestamp/Nonce/Signature 请求头")
    try:
        if abs(int(timestamp) - int(time.time())) > _NOTIFY_TIMESTAMP_TOLERANCE_S:
            raise WechatNotifyError("回调时间戳与本地时钟偏差过大，按重放/伪造拒绝")
    except ValueError as exc:
        raise WechatNotifyError("回调时间戳格式非法") from exc
    message = f"{timestamp}\n{nonce}\n{raw_body.decode('utf-8')}\n".encode("utf-8")
    try:
        n, e = load_public_key(cfg.platform_cert_pem)
        signature = base64.b64decode(signature_b64)
    except DerError as exc:
        raise WechatNotifyError(f"平台证书解析失败: {exc}") from exc
    except Exception as exc:  # noqa: BLE001 — base64 解码失败等统一按验签失败处理
        raise WechatNotifyError(f"签名头解码失败: {exc}") from exc
    if not verify_pkcs1v15_sha256(message, signature, n, e):
        raise WechatNotifyError("Wechatpay-Signature 验签失败，拒绝这条回调")


def verify_and_decrypt_notify(cfg: WechatMerchantConfig, *, headers: dict[str, str], raw_body: bytes) -> dict[str, Any]:
    """验签 + 解密回调，返回明文通知内容（含 ``out_trade_no``/``transaction_id``/
    ``trade_state``/``amount``）。任何一步失败都抛 ``WechatNotifyError``，
    调用方必须整体拒绝，不能"验签失败但内容看着对就发货"。
    """
    _verify_signature(cfg, headers, raw_body)
    try:
        envelope = json.loads(raw_body)
        resource = envelope["resource"]
        plaintext = aes256_gcm_decrypt(
            key=cfg.api_v3_key.encode("utf-8"),
            nonce=resource["nonce"].encode("utf-8"),
            ciphertext_and_tag=base64.b64decode(resource["ciphertext"]),
            aad=resource["associated_data"].encode("utf-8"),
        )
        return json.loads(plaintext)
    except AeadError as exc:
        raise WechatNotifyError(f"回调 resource 解密失败: {exc}") from exc
    except (KeyError, ValueError, TypeError) as exc:
        raise WechatNotifyError(f"回调结构不符合预期: {exc}") from exc
