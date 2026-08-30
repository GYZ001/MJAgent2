"""视频加量包：订阅之外的第六类配额，不随 30 天周期重置。

从 ``app/quota.py`` 拆出（文件行数棘轮 ``scripts/check_file_conventions.py``
逼的，不是过度设计）。设计要点见 ``app/quota.py`` 模块顶部对加量包的完整说
明；本模块只负责两件事：算余额、入账，都不依赖 ``app.quota`` 的任何符号（依赖
方向单向——``app.quota.reserve_video_seconds`` 会 import 本模块的
``addon_video_seconds_balance``，本模块反过来不 import ``app.quota``，不构成
环）。消耗（charge）与退还（refund）两个动作因为要跟订阅额度的扣费在同一次
调用里原子完成，逻辑仍留在 ``app.quota.reserve_video_seconds`` /
``refund_video_seconds`` 里，不搬过来。

``quota_ledger`` 是唯一事实来源，本模块与 ``app.quota`` 共用同一张表、同一套
``UNIQUE(resource, attempt_key, reason)`` 幂等机制，只是各自操作不同的
``resource`` 值。
"""
from __future__ import annotations

import sqlite3

from fastapi import HTTPException

from app.db import now

#: 不进 TIER_TABLE（不随周期重置），单独用 quota_ledger 的
#: resource=ADDON_RESOURCE 记账。
ADDON_RESOURCE = "video_addon_seconds"
ADDON_PACKAGE_SECONDS = 10 * 60.0  # 每包 10 分钟
ADDON_PACKAGE_PRICE_CNY = 199.0  # 19.9 元/分钟，故意高于所有订阅档（用户拍板）


def addon_video_seconds_balance(conn: sqlite3.Connection, user_id: str) -> float:
    """加量包视频秒数余额 = 全生命周期已入账（grant）- 净消耗（charge 与 refund
    两个 reason 的代数和；``refund_video_seconds`` 写 refund 行时 delta 已经是
    负数，即"抵消掉之前记的那笔 charge"，因此这里直接相加，不能再减一次——
    减一次等于把同一笔退款扣了两遍）。不按 period_index 过滤——这类资源压根不
    随 30 天周期重置，``period_index`` 列在这类行上只是审计信息（记录"哪个订
    阅周期里发生的"），不是重置边界。"""
    rows = conn.execute(
        "SELECT reason, COALESCE(SUM(delta),0) AS s FROM quota_ledger "
        "WHERE user_id=? AND resource=? GROUP BY reason",
        (user_id, ADDON_RESOURCE),
    ).fetchall()
    totals = {r["reason"]: float(r["s"]) for r in rows}
    granted = totals.get("grant", 0.0)
    net_consumed = totals.get("charge", 0.0) + totals.get("refund", 0.0)
    return max(0.0, granted - net_consumed)


def grant_video_addon_seconds(
    conn: sqlite3.Connection, user_id: str, *, packages: int, attempt_key: str
) -> dict:
    """管理员手工发放加量包（本次不接真实支付，见 ``app.quota`` 模块文档）。
    ``packages`` 是购买的包数（每包 ``ADDON_PACKAGE_SECONDS``），``attempt_key``
    是这笔购买的稳定标识——同一笔购买（未来接支付后是订单号，现在是调用方显式
    提供或生成的一次性 key）重复入账只生效一次，复用 ``quota_ledger`` 的
    ``UNIQUE(resource, attempt_key, reason)``（``period_index`` 恒填 0——加量包
    不随周期重置，这一列在此纯粹是 NOT NULL 占位，不参与任何判据）。
    """
    if packages < 1:
        raise HTTPException(422, "加量包数量必须是正整数")
    existing = conn.execute(
        "SELECT delta FROM quota_ledger WHERE resource=? AND attempt_key=? AND reason='grant'",
        (ADDON_RESOURCE, attempt_key),
    ).fetchone()
    if existing is not None:
        return {"granted_s": 0.0, "idempotent_replay": True, "seconds": float(existing["delta"])}
    seconds = float(packages) * ADDON_PACKAGE_SECONDS
    try:
        conn.execute(
            "INSERT INTO quota_ledger(user_id,resource,period_index,attempt_key,"
            "reason,delta,created_at) VALUES(?,?,0,?,?,?,?)",
            (user_id, ADDON_RESOURCE, attempt_key, "grant", seconds, now()),
        )
        applied = True
    except sqlite3.IntegrityError:
        applied = False
    return {
        "granted_s": seconds if applied else 0.0, "idempotent_replay": not applied,
        "seconds": seconds,
    }
