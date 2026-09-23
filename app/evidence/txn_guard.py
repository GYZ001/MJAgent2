"""数据库写事务安全网：block 内抛异常时把未提交的写回滚掉，再原样重新抛出。

``app.multiview`` 的多视角包生成/重做函数（``ensure_character_multiview_pack``/
``ensure_scene_multiview_pack``/``regenerate_character_view``/
``regenerate_scene_view``）在多个 ``await``（出图、存图）之间穿插十余次
``conn.commit()``，此前全程零 try/except/rollback：某次 upsert 写入和紧随其后
的 ``conn.commit()`` 之间一旦抛异常，连接上就留下未提交事务——调用方（同一个
asyncio task）随后可能带着它去 await 下一次长等待（出图信号量，动辄几分钟），
SQLite 写锁在此期间不释放。2026-09-05 实测：场景库出图任务带着未提交的
``scene_reference_views`` 插入去等图片生成信号量 5 分钟，9 个并行映射台全部
``database is locked``。

本模块只做「block 内抛异常时回滚未提交的写、原样重新抛出」这一个动作：不
commit（调用方自己逐段 commit 的既有结构与语义不变）、不重试、不吞异常。放在
``app.evidence``（L2 持久化层，``app.multiview`` 是 L4，可以依赖）而不是
``app/`` 根目录或 ``app.multiview`` 自身，是因为 multiview.py 已经卡在
``FILE_CONVENTIONS.toml`` 的行数基线上限，装不下重复的 try/rollback/raise 样板
（见 ``app/LAYERS.toml`` 对 ``app.evidence`` 的分层声明）。
"""
from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

_LOGGER = logging.getLogger(__name__)


@contextmanager
def rollback_uncommitted_on_error(conn: sqlite3.Connection, *, where: str) -> Iterator[None]:
    """块内抛异常时回滚 ``conn`` 上未提交的写，再原样重新抛出；正常退出不做任何事。

    进入时若 ``conn`` 已经带着未提交事务——按约定这不应该发生（调用方理应总是
    传入一个干净连接，见 ``app.observability.write_lock_holders.
    rollback_before_long_wait`` 这一类长等待前的外部护栏），这里不代为回滚或
    提交，只记一条告警定位是谁破坏了约定：那是调用方自己的事务，回滚/提交的
    时机该由它决定，不该被这层安全网悄悄改写。

    回滚必须排在任何日志/recorder 调用之前（CLAUDE.md「Ownership Must Be
    Explicit」：日志本身也可能隐式 commit），因此异常分支里 ``conn.rollback()``
    永远是 except 块的第一条语句。

    这里接的是 ``BaseException`` 而不是 ``Exception``：``asyncio.CancelledError``
    从 Python 3.8 起改为 ``BaseException`` 的子类，``except Exception`` 接不住
    它。本模块保护的四个函数跨多个 await（出图信号量、存图），用户取消映射台/
    场景库任务时取消异常正是在某个 await 点抛出——如果只接 ``Exception``，恰好
    是 2026-09-05 那类"带着未提交写入挂起、别人全部 database is locked"故障
    的翻版。接 ``BaseException`` 同样不吞异常，``KeyboardInterrupt``/
    ``SystemExit`` 等一并回滚后原样重新抛出。
    """
    if conn.in_transaction:
        _LOGGER.warning(
            "[MULTIVIEW_TXN_ENTRY] %s：进入时连接已带着未提交事务（不应发生），"
            "不代为回滚或提交",
            where,
        )
    try:
        yield
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
            _LOGGER.warning(
                "[MULTIVIEW_TXN_ROLLBACK] %s：写入未提交前抛出异常，已回滚防止"
                "长等待期间继续持有写锁",
                where,
            )
        raise
