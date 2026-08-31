"""微信支付 V3 / 支付宝商户配置：读 ``.env``，未配置时报清楚缺什么。

复用 ``app.config`` 现有的 ``.env`` 加载机制（导入触发其模块级
``_load_env()``），但**不**把这些 key 塞进 ``app.config.MANAGED_KEYS``——那张表
是"前端监制房页面可编辑"的 API Key 登记表，加进去意味着要同时改
``app/config.py`` 的公开契约；而 ``app/config.py`` 当前行数（613）已经卡在
``FILE_CONVENTIONS.toml`` 的棘轮基线上、零余量，任何净增行数都会撞违规，
CLAUDE.md 明确禁止为了自己的改动过关上调基线数字。商户号/密钥/证书路径本来
就该走运维配置（不是产品内可自助改的 API Key），因此改成本模块直接读
``os.environ``——同一条 ``.env`` 加载管线，只是不经 ``MANAGED_KEYS`` 这张 UI
登记表，语义上更准确，也不需要碰 ``app/config.py``。

证书/私钥文件本身不落库、不出现在日志——只存**文件路径**在 ``.env``，内容在
调用时现读现用。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import app.config  # noqa: F401 — 触发其模块级 _load_env()，保证 os.environ 已从 .env 合并

#: 生产网关；沙箱/联调可用 ``ALIPAY_GATEWAY_URL`` 覆盖，不是密钥，不走
#: 文件路径读取那一套。
_ALIPAY_DEFAULT_GATEWAY = "https://openapi.alipay.com/gateway.do"
_WECHAT_API_BASE = "https://api.mch.weixin.qq.com"


class PaymentConfigError(RuntimeError):
    """商户配置缺失/不完整或证书文件读取失败。路由层必须转成明确的 4xx/5xx
    错误告诉调用方去配哪个 ``.env`` 变量，不能吞掉或静默走某个默认值。"""


@dataclass(frozen=True, slots=True)
class WechatMerchantConfig:
    app_id: str
    mch_id: str
    api_v3_key: str
    serial_no: str
    private_key_pem: str
    platform_cert_pem: str
    api_base: str


@dataclass(frozen=True, slots=True)
class AlipayMerchantConfig:
    app_id: str
    private_key_pem: str
    alipay_public_key_pem: str
    gateway_url: str


def _read_cert_file(env_name: str, path_value: str) -> str:
    path = Path(path_value).expanduser()
    if not path.is_file():
        raise PaymentConfigError(f"{env_name} 指向的文件不存在: {path}")
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PaymentConfigError(f"{env_name} 指向的文件读取失败: {path} ({exc})") from exc


def _require_env(names: tuple[str, ...]) -> dict[str, str]:
    values = {name: os.environ.get(name, "").strip() for name in names}
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise PaymentConfigError(
            "支付商户配置未完成，缺少 .env 变量: " + "、".join(missing)
            + "（在服务器 .env 文件里补上后重启后端；本次不接受任何跳过配置的开关）"
        )
    return values


def public_base_url() -> str:
    """本服务对外可访问的 HTTPS 根地址（用于拼渠道回调 ``notify_url``）。

    不给默认值——不同环境（开发/生产）域名不同，猜一个默认值只会在真正上线
    时悄悄用错地址，回调永远收不到还不容易定位。必须在 ``.env`` 显式配置
    ``PAYMENTS_PUBLIC_BASE_URL``（见 nginx 反代那两个域名/IP 证书的既有约定）。
    """
    value = os.environ.get("PAYMENTS_PUBLIC_BASE_URL", "").strip()
    if not value:
        raise PaymentConfigError(
            "支付商户配置未完成，缺少 .env 变量: PAYMENTS_PUBLIC_BASE_URL"
            "（渠道回调需要一个外部可达的 HTTPS 根地址，例如 https://automanju.com）"
        )
    return value.rstrip("/")


def wechat_merchant_config() -> WechatMerchantConfig:
    """读取微信支付 V3 商户配置；任一必填项缺失或证书文件不可读都抛
    ``PaymentConfigError``，不返回半成品配置。"""
    values = _require_env((
        "WECHAT_PAY_APP_ID", "WECHAT_PAY_MCH_ID", "WECHAT_PAY_API_V3_KEY",
        "WECHAT_PAY_SERIAL_NO", "WECHAT_PAY_PRIVATE_KEY_PATH", "WECHAT_PAY_PLATFORM_CERT_PATH",
    ))
    private_key_pem = _read_cert_file("WECHAT_PAY_PRIVATE_KEY_PATH", values["WECHAT_PAY_PRIVATE_KEY_PATH"])
    platform_cert_pem = _read_cert_file("WECHAT_PAY_PLATFORM_CERT_PATH", values["WECHAT_PAY_PLATFORM_CERT_PATH"])
    return WechatMerchantConfig(
        app_id=values["WECHAT_PAY_APP_ID"],
        mch_id=values["WECHAT_PAY_MCH_ID"],
        api_v3_key=values["WECHAT_PAY_API_V3_KEY"],
        serial_no=values["WECHAT_PAY_SERIAL_NO"],
        private_key_pem=private_key_pem,
        platform_cert_pem=platform_cert_pem,
        api_base=os.environ.get("WECHAT_PAY_API_BASE", _WECHAT_API_BASE).rstrip("/"),
    )


def alipay_merchant_config() -> AlipayMerchantConfig:
    """读取支付宝商户配置（公钥模式：应用私钥签名请求，支付宝公钥验回调签名）。"""
    values = _require_env(("ALIPAY_APP_ID", "ALIPAY_PRIVATE_KEY_PATH", "ALIPAY_PUBLIC_KEY_PATH"))
    private_key_pem = _read_cert_file("ALIPAY_PRIVATE_KEY_PATH", values["ALIPAY_PRIVATE_KEY_PATH"])
    public_key_pem = _read_cert_file("ALIPAY_PUBLIC_KEY_PATH", values["ALIPAY_PUBLIC_KEY_PATH"])
    return AlipayMerchantConfig(
        app_id=values["ALIPAY_APP_ID"],
        private_key_pem=private_key_pem,
        alipay_public_key_pem=public_key_pem,
        gateway_url=os.environ.get("ALIPAY_GATEWAY_URL", _ALIPAY_DEFAULT_GATEWAY).rstrip("/"),
    )
