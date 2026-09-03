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


async def operation_audit_sweep_loop(interval_s: float = 6 * 60 * 60) -> None:
    """周期性清理过期审计行；单轮失败不影响下一轮，也不会让循环退出。"""
    while True:
        try:
            sweep_expired()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 巡检循环自身不得因单批坏数据退出
            _LOGGER.warning("operation_audit_sweep_loop failed", exc_info=True)
        await asyncio.sleep(max(60.0, min(float(interval_s), 6 * 60 * 60)))
