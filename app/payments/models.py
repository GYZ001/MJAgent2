"""订单常量、状态机、定价解析、请求/响应 schema。

不重新实现配额记账（CLAUDE.md 明确要求）：本模块只算"这笔订单该收多少钱"和
"状态能不能这样跳转"，真正发货调用 ``app.quota_addon``/直接写 ``users.tier``
的逻辑在 ``fulfillment.py``。
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from app.quota_addon import ADDON_PACKAGE_PRICE_CNY
from app.quota_tiers import VALID_TIERS

CHANNEL_WECHAT = "wechat"
CHANNEL_ALIPAY = "alipay"
VALID_CHANNELS = frozenset({CHANNEL_WECHAT, CHANNEL_ALIPAY})

PRODUCT_VIDEO_ADDON = "video_addon"
PRODUCT_TIER_UPGRADE = "tier_upgrade"
VALID_PRODUCTS = frozenset({PRODUCT_VIDEO_ADDON, PRODUCT_TIER_UPGRADE})

#: free 档不是可购买的升级目标（升级只能往上走，降级不通过支付发生）。
PURCHASABLE_TIERS = frozenset(VALID_TIERS - {"free"})

STATUS_PENDING = "pending"
STATUS_PAID = "paid"
STATUS_FULFILLED = "fulfilled"
STATUS_CLOSED = "closed"
VALID_STATUSES = frozenset({STATUS_PENDING, STATUS_PAID, STATUS_FULFILLED, STATUS_CLOSED})

#: 状态机只能向前走，不可逆向跳转；pending 可以走向 paid 或 closed（用户取消/
#: 超时未支付），paid 只能走向 fulfilled（发货），fulfilled/closed 是终态。
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    STATUS_PENDING: frozenset({STATUS_PAID, STATUS_CLOSED}),
    STATUS_PAID: frozenset({STATUS_FULFILLED}),
    STATUS_FULFILLED: frozenset(),
    STATUS_CLOSED: frozenset(),
}

# ---------------------------------------------------------------------------
# 档位订阅价（用户 2026-08-30 拍板，与 30 天周期一一对应）：
#   starter ¥99/5 分钟   standard ¥249/15 分钟
#   pro     ¥449/30 分钟 max      ¥699/50 分钟
# 分钟单价 19.8 → 16.6 → 15.0 → 14.0 递减，全部低于加量包的 19.9 元/分钟
# （``app.quota_addon.ADDON_PACKAGE_PRICE_CNY``）——买包永远不如升档，这是刻意
# 的产品引导，两处数字必须一起看，改一处不改另一处会让引导反向。
# ---------------------------------------------------------------------------
TIER_SUBSCRIPTION_PRICE_CNY: dict[str, float] = {
    "starter": 99.0,
    "standard": 249.0,
    "pro": 449.0,
    "max": 699.0,
}


class PricingError(ValueError):
    """商品参数不合法或无法定价——路由层应转成 422。"""


def resolve_amount_fen(product: str, detail: dict) -> int:
    """把商品参数换算成金额（分，整数，不用浮点存储）。中间用浮点算元后
    ``round()`` 成分，只在这一步短暂过渡；``round()`` 而不是截断，避免
    0.1 元这类十进制在二进制浮点下的表示误差把 199.00 元算成 19899 分。
    """
    if product == PRODUCT_VIDEO_ADDON:
        packages = detail.get("packages")
        if not isinstance(packages, int) or isinstance(packages, bool) or packages < 1:
            raise PricingError("packages 必须是正整数")
        return round(packages * ADDON_PACKAGE_PRICE_CNY * 100)
    if product == PRODUCT_TIER_UPGRADE:
        target_tier = detail.get("target_tier")
        if target_tier not in PURCHASABLE_TIERS:
            raise PricingError(f"target_tier 必须是 {'/'.join(sorted(PURCHASABLE_TIERS))} 之一")
        price = TIER_SUBSCRIPTION_PRICE_CNY[target_tier]
        return round(price * 100)
    raise PricingError(f"未知商品类型: {product}")


class CreateOrderRequest(BaseModel):
    channel: str
    product: str
    packages: int | None = Field(default=None, ge=1)
    target_tier: str | None = None

    def product_detail(self) -> dict:
        if self.product == PRODUCT_VIDEO_ADDON:
            return {"packages": self.packages}
        if self.product == PRODUCT_TIER_UPGRADE:
            return {"target_tier": self.target_tier}
        return {}
