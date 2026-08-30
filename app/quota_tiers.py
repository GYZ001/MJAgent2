"""五档会员配额的档位表：纯数据，不含判断/扣减/记账行为。

从 ``app/quota.py`` 拆出（``app/FILE_CONVENTIONS.toml`` 的行数棘轮逼的，不是过度
设计——``app/quota.py`` 在这次拆分前正好卡在 600/600 基线上，零余量）。本模块只
承载「档位 -> 上限数值」与「档位 -> 升级文案」两张静态表，不碰 ``sqlite3.Connection``、
不做任何判断：``TierLimits``/``TIER_TABLE``/``VALID_TIERS``/``_UNLIMITED``/
``_UPGRADE_PATH`` 都是加载期就能算完的常量。配额引擎（``effective_limits`` 判断
用哪张表、``QuotaExceeded`` 怎么报错、ledger 记账）留在 ``app/quota.py``，本模块
反过来不 import 任何 ``app.quota`` 的东西——依赖方向单向，``app.quota`` import
本模块（同层），不构成环。

档位数值原样照抄自 ``app/quota.py`` 拆分前的版本，一个都没改：用户逐条拍板过
（free 1 分钟 / starter 5 / standard 15 / pro 30 / max 50 视频分钟；项目数
1/2/3/6/10；token 30/60/90/180/300 万；图像 300/600/900/1800/3000 万）。五档图像
额度数值来自实测、与各档项目数上限对齐（image ≈ projects × 300 万）；free 档是
有意的不对称设计：只砍视频，不砍图像。完整业务语义见 ``app/quota.py`` 模块顶部
文档字符串，这里不重复。

⚠️ ``TIER_TABLE`` 是测试大量打桩的对象。``app/quota.py`` 用
``from app.quota_tiers import TIER_TABLE`` 把它导入自己的命名空间——那是一个独立
绑定，不是共享引用：``monkeypatch.setattr(quota, "TIER_TABLE", fake)`` 只改
``app.quota`` 自己那份绑定，不影响 ``app.quota_tiers.TIER_TABLE``；反过来
``monkeypatch.setattr(quota_tiers, "TIER_TABLE", fake)`` 也不影响
``app.quota.TIER_TABLE``（``effective_limits`` 定义在 ``app.quota`` 里，读的是
``app.quota`` 自己命名空间里的全局名，不会因为 ``quota_tiers`` 那边被重新绑定而
跟着变）。测试必须用 ``tests/conftest.py`` 的
``patch_quota_everywhere(monkeypatch, name, value)``，它按全限定名把两处绑定一起
打桩；裸 ``monkeypatch.setattr(quota, ...)`` / ``monkeypatch.setattr(quota_tiers,
...)`` 被 ``tests/test_quota_monkeypatch_guard.py`` 的 AST 守卫拦截。
"""
from __future__ import annotations

from dataclasses import dataclass

from app.quota_addon import ADDON_PACKAGE_PRICE_CNY, ADDON_PACKAGE_SECONDS


@dataclass(frozen=True)
class TierLimits:
    tier: str
    projects: int | None       # None = 无限（仅系统管理员）
    concurrency: int | None
    token: float | None
    video_seconds: float | None
    image: float | None        # 定妆照/场景图成本上限，与 token 同周期滚动重置


TIER_TABLE: dict[str, TierLimits] = {
    "free": TierLimits("free", 1, 1, 300_000.0, 1 * 60.0, 3_000_000.0),
    "starter": TierLimits("starter", 2, 2, 600_000.0, 5 * 60.0, 6_000_000.0),
    "standard": TierLimits("standard", 3, 3, 900_000.0, 15 * 60.0, 9_000_000.0),
    "pro": TierLimits("pro", 6, 6, 1_800_000.0, 30 * 60.0, 18_000_000.0),
    "max": TierLimits("max", 10, 10, 3_000_000.0, 50 * 60.0, 30_000_000.0),
}
VALID_TIERS = frozenset(TIER_TABLE)
_UNLIMITED = TierLimits("unlimited", None, None, None, None, None)

_UPGRADE_PATH = {
    "free": (
        "升级到入门档位（2 个项目 / 每模块 2 并发 / 60 万 token / 5 分钟视频 "
        "/ 600 万定妆照与场景图额度）"
    ),
    "starter": (
        "升级到标准档位（3 个项目 / 每模块 3 并发 / 90 万 token / 15 分钟视频 "
        "/ 900 万定妆照与场景图额度）"
    ),
    "standard": (
        "升级到专业档位（6 个项目 / 每模块 6 并发 / 180 万 token / 30 分钟视频 "
        "/ 1800 万定妆照与场景图额度）"
    ),
    "pro": (
        "升级到旗舰档位（10 个项目 / 每模块 10 并发 / 300 万 token / 50 分钟视频 "
        "/ 3000 万定妆照与场景图额度）"
    ),
    "max": (
        "已是最高付费档位；如需更多视频时长可购买加量包"
        f"（¥{ADDON_PACKAGE_PRICE_CNY:.0f}/{int(ADDON_PACKAGE_SECONDS // 60)} 分钟，"
        "不随 30 天周期重置），或联系管理员开通不限量账号"
    ),
}
