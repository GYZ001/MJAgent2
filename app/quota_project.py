"""EP-04 第二阶段：项目级并发上限 + 存储维度闸门（L2，见 app/LAYERS.toml::
app.quota_project）。

从 ``app/quota.py`` 拆出——``app/quota.py`` 在这次改造前已经卡在 559/559 行
数基线零余量（``app/FILE_CONVENTIONS.toml`` 棘轮），装不下新增两个闸门函数
的空间，拆分而不是加基线（CLAUDE.md「装不下时先想怎么拆，不要先想加基线」）。

``app.quota`` 用 ``from app.quota_project import (...) as (...)`` 在模块顶部
做 re-export，外部调用方一律仍走 ``quota.check_project_concurrency`` /
``quota.assert_storage_capacity`` 既有路径，不用改调用点（与 ``app.quota_scope``
/``app.quota_tiers`` 的既有拆分同一惯例）。

**依赖方向必须单向、且不能在模块顶层成环**：本模块两个函数都需要
``app.quota.effective_limits``/``app.quota.QuotaExceeded``——但 ``app.quota``
反过来在模块顶部 import 本模块做 re-export，若本模块也在模块顶部
``from app.quota import ...``，Python 会在 ``app.quota`` 尚未执行完
（``effective_limits``/``QuotaExceeded`` 还未定义）时就试图导入本模块，本模块
再回头找一个还没准备好的名字，直接 ``ImportError``。解法：本模块两个函数体内
一律用函数级延迟 ``from app import quota``（调用发生时 ``app.quota`` 早已
初始化完毕），不在模块顶层碰 ``app.quota`` 一个字——与 ``app/LAYERS.toml``
里 ``app.db``(finish_provider_call) 反过来按需记账用延迟 import 避免与
``app.quota`` 真实循环导入是同一个手法。"""
from __future__ import annotations

import sqlite3
import time

from app.quota_policy import allocation as alloc


def check_project_concurrency(
    conn: sqlite3.Connection, user_id: str, project_id: str, module: str, *, active_count: int
) -> None:
    """``check_module_concurrency``（账号维度）之外再加一层项目级上限：防止一
    个项目吃满账号/团队全部并发槽位、饿死同账号下的其它项目。同一份契约（纯
    判断不取数不占位；``active_count`` 是调用方在同一 ``BEGIN IMMEDIATE`` 事
    务里用 ``app.quota_scope.count_*_for_project`` 查到的项目级活跃行数，判
    据挂实际在跑的行）。``limits.project_concurrency`` 为 ``None``（仅系统管
    理员）时不拦截。"""
    from app import quota

    limits = quota.effective_limits(conn, user_id)
    if limits.project_concurrency is None:
        return
    if active_count >= limits.project_concurrency:
        raise quota.QuotaExceeded(
            gate="project_concurrency", tier=limits.tier, limit=limits.project_concurrency,
            used=active_count, remaining=max(0, limits.project_concurrency - active_count),
            conn=conn, user_id=user_id,
            message=(
                f"项目 {project_id} 在 {module} 模块同时在跑的任务已达"
                f"{alloc.tier_or_scope_label(limits, 'project_concurrency')}的项目级上限"
                f"（{limits.project_concurrency} 个）——账号/团队还有其它并发余量，"
                "但已分配给了同账号下的其它项目；请等待本项目现有任务结束后再试"
            ),
        )


def assert_storage_capacity(conn: sqlite3.Connection, user_id: str, project_id: str) -> None:
    """新媒体生成任务发起前的前置闸门：账号（三级取最紧后）名下全部项目最新
    一次采样的存储占用总和是否已见顶。只读上一次采样结果（``app.quota_policy.
    storage.bytes_used_for_user_ids``），绝不在请求路径里触发目录遍历。超限
    不删数据，只拒绝这次请求，消息里指向清理候选清单入口（拦人给出路）。"""
    from app import quota

    limits = quota.effective_limits(conn, user_id)
    if limits.storage_bytes is None:
        return
    from app.quota_policy import storage as quota_storage

    used, sampled_at = quota_storage.bytes_used_for_user_ids(conn, [user_id])
    if used < limits.storage_bytes:
        return
    sampled_str = (
        time.strftime("%Y-%m-%d %H:%M", time.localtime(sampled_at)) if sampled_at else "尚未采样"
    )
    raise quota.QuotaExceeded(
        gate="storage", tier=limits.tier, limit=limits.storage_bytes,
        used=used, remaining=max(0.0, limits.storage_bytes - used), conn=conn, user_id=user_id,
        message=(
            f"存储占用已达{alloc.tier_or_scope_label(limits, 'storage_bytes')}上限"
            f"（{int(limits.storage_bytes)} 字节，截至 {sampled_str} 的采样结果），"
            f"暂时不能在项目 {project_id} 发起新的媒体生成；请前往项目页查看"
            "「清理建议清单」，确认后删除不再需要的历史视频版本以释放空间"
        ),
    )
