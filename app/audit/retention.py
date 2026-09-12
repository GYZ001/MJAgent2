"""operation_audit 保留策略：365 天后清理，调度形态照抄
``app.recovery.account_recycle_bin_sweep_loop``（挂时间戳判据、单批失败不
退出循环）。失败用 stdlib logging 记录，不借道 app.errors——本模块只 import
app.audit.store + stdlib，维持 app.audit 对高层模块零依赖的既定边界。
"""
from __future__ import annotations

import asyncio
import logging

from app.audit import store
from app.db import now as db_now

OPERATION_AUDIT_RETENTION_S = 365 * 24 * 60 * 60
_SWEEP_BATCH_SIZE = 500
_LOGGER = logging.getLogger(__name__)


def sweep_expired() -> int:
    """删除 ``ts < now - 365 天`` 的行，返回本次实际删除的行数。"""
    cutoff = db_now() - OPERATION_AUDIT_RETENTION_S
    return store.delete_expired_operation_audit(cutoff, _SWEEP_BATCH_SIZE)


def _sweep_expired_sso_auth_requests() -> None:
    """EP-02：``sso_auth_requests``（10 分钟一次性 state/nonce/PKCE 凭据）的
    过期清理挂在这条既有的 6 小时巡检循环上，不新开定时器（PRD EP-02 §10
    陷阱 2）。函数内延迟 import：``app.audit`` 是全仓共享的审计基础设施
    模块，本文件自己的模块文档写明"只 import app.audit.store + stdlib"，
    模块级 import 一个具体业务包（``app.sso``）会让这条边界失真；延迟到
    调用时才导入，与 ``app.recovery`` 里其它域专属 sweep 钩子
    （``account_recycle_bin_sweep_loop`` 等）同一手法——本模块只是把"挂钩"
    这件事从 ``app.recovery`` 挪到了同样已有巡检节奏的 ``app.audit``。
    """
    from app.sso.store import purge_expired_auth_requests

    purge_expired_auth_requests()


def _sweep_expired_sso_login_exchanges() -> None:
    """同上一条：会话交接一次性交换码（60 秒 TTL）的兜底清理，理论上多数
    行会先被 ``POST /api/auth/sso/exchange`` 兑换掉，这里只清理"发了从没
    兑换"的少数遗留行。同一条延迟 import 理由，见上方 ``_sweep_expired_
    sso_auth_requests`` 的说明。"""
    from app.sso.store import purge_expired_login_exchanges

    purge_expired_login_exchanges()


def _sweep_expired_user_invitations() -> None:
    """EP-03 第二阶段：邀请链接（``user_invitations``）过期/已用/已撤销行的
    物理清理挂在同一条 6 小时巡检上（PRD §6：不新开定时器）。同一条延迟
    import 理由，见上方 ``_sweep_expired_sso_auth_requests`` 的说明——
    ``app.provisioning`` 是具体业务包，本文件只 import ``app.audit.store``
    + stdlib 的边界不能被模块级 import 破坏。"""
    from app.provisioning.invitations import sweep_expired

    sweep_expired()


async def operation_audit_sweep_loop(interval_s: float = 6 * 60 * 60) -> None:
    """周期性清理过期审计行；单轮失败不影响下一轮，也不会让循环退出。"""
    while True:
        try:
            sweep_expired()
            _sweep_expired_sso_auth_requests()
            _sweep_expired_sso_login_exchanges()
            _sweep_expired_user_invitations()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 巡检循环自身不得因单批坏数据退出
            _LOGGER.warning("operation_audit_sweep_loop failed", exc_info=True)
        await asyncio.sleep(max(60.0, min(float(interval_s), 6 * 60 * 60)))
