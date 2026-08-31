"""会员到期后的降级机制：裁剪超额项目到新档位上限、周期性到期扫描。

与 ``app.quota_expiry``（只读闸门，拦截过期账号发起新任务）是两个独立职责：
本模块负责"降级这件事本身怎么做"——用户已拍板的规则是"超出上限的项目删除
最老的几个，只保留最新的上限数量"，加量包余额（``quota_ledger`` 的
``resource='video_addon_seconds'``）长期保留，不受影响（``TIER_TABLE`` 的项目
数上限逻辑与加量包记账毫无交集，本模块也不写任何加量包相关的行）。

``trim_projects_to_tier_limit`` 是可独立调用的原语（未来任意"改变某账号档位"
的路径都可以复用，不止到期降级）；``sweep_expired_memberships`` 是它的一个
调用方——周期性系统任务，与 ``lifecycle.sweep_expired_deleted_projects`` 同一
种调度形态，直接放在同一个文件里（同包、同关注点：项目回收站到期清理与会员
到期降级都是"账号到期后要不要动它名下项目"这条线的两端）。
"""
from __future__ import annotations

import sqlite3

from app.completion_grant import assert_provider_tasks_clearable
from app.db import get_conn, now
from app.domain.projects.lifecycle import _delete_project_core
from app.quota_tiers import TIER_TABLE


async def trim_projects_to_tier_limit(conn: sqlite3.Connection, user_id: str, tier: str) -> dict:
    """把某账号名下的活跃项目数裁剪到 ``tier`` 档的上限：删最老的，保留最新的。

    "最新" 显式按 ``created_at`` 排序，不用 ``id``/rowid——两者在这个仓库里没有
    绑定关系（``new_id()`` 不是自增/时间序），用 rowid 会在项目创建顺序与主键
    生成顺序不一致时删错项目。

    原子性（用户显式要的保证）：**先对全部超额项目做一遍只读的可清理性预检
    （``assert_provider_tasks_clearable``），全部通过后才真正开始删**。
    ``_delete_project_core`` 自己内部提交事务，多次调用没法包进同一个外层
    SQL 事务里做真正的原子性；这个"预检完再统一动手"的两阶段写法是这里唯一
    能落实"部分失败时一个项目都不动"的手段——只要预检阶段任何一个项目未到
    终态就整体 raise、且预检本身不产生任何写入，调用方看到的要么是"全部裁
    剪成功"要么是"一个都没动"，不会有"删了一半"的半途态。真正开始删除之后
    （第二个循环）单个项目失败是预检之后的真实竞态（例如供应商任务在预检
    通过后、真正删除前才变成非终态），不吞掉、直接向上抛——不能悄悄跳过一
    个本该删除的项目又假装裁剪已经完成。

    ``tier`` 未知时按 ``quota_tiers.TIER_TABLE.get`` 的既有兼容处理（外部调用
    方永远传合法档位字符串，这里不重复校验/不静默兜底成 free——降级调用方
    ``sweep_expired_memberships`` 固定传字面量 ``'free'``，未来任何新增调用方
    传错档位属于调用方缺陷，应该在这里直接 KeyError 而不是被吞掉）。
    """
    limits = TIER_TABLE[tier]
    limit = limits.projects
    rows = conn.execute(
        "SELECT id FROM projects WHERE owner_user_id=? AND deleted_at IS NULL "
        "ORDER BY created_at ASC, id ASC",
        (user_id,),
    ).fetchall()
    project_ids = [row["id"] for row in rows]
    if limit is None or len(project_ids) <= limit:
        return {"tier": tier, "kept_limit": limit, "deleted_project_ids": [], "deleted_count": 0}
    excess = project_ids[: len(project_ids) - limit]
    for project_id in excess:
        assert_provider_tasks_clearable(project_id=project_id, conn=conn)
    for project_id in excess:
        await _delete_project_core(project_id)
    return {
        "tier": tier, "kept_limit": limit,
        "deleted_project_ids": excess, "deleted_count": len(excess),
    }


async def sweep_expired_memberships() -> dict:
    """周期性系统任务：找出已过期的付费档位账号，降级回 free 并裁剪超额项目。

    与 ``lifecycle.sweep_expired_deleted_projects`` 同一种隔离粒度，但作用域
    不同——那个按"单个项目"隔离失败，这个按"单个账号"隔离失败：一个账号的
    裁剪失败（例如它名下某个超额项目的供应商任务未到终态）不得阻塞同一轮里
    其余到期账号的降级，见下方 per-user try/except + ``log_error`` + continue。

    档位翻转（``tier='free', tier_expires_at=NULL``）与项目裁剪是两步：先提交
    翻转（小且廉价，几乎不可能失败），再裁剪（可能因供应商任务未到终态而失
    败）。翻转一旦提交就不回滚——即使随后裁剪失败，"这个账号不再享有已过期的
    付费档位权益"本身没有错，只是"超额项目还没来得及删"需要下一轮重试；这与
    ``sweep_expired_deleted_projects``"单个项目失败保留、下一轮重试"是同一条
    自愈判据（挂产物信号，不挂状态字段：下一轮扫描不会重复处理已经翻成 free
    的账号，只有仍然超额的项目会在下一次 ``trim_projects_to_tier_limit`` 调用
    里被重新发现——这不是本函数负责的，是它下一轮再跑一次的自然结果）。
    """
    conn = get_conn()
    stamp = now()
    rows = conn.execute(
        "SELECT id, tier FROM users -- ALL_OWNERS: periodic background sweep "
        "loop (app.recovery.expired_membership_sweep_loop), no request "
        "context; expiry is enforced globally by tier_expires_at, not per "
        "caller\n"
        "WHERE tier_expires_at IS NOT NULL AND tier_expires_at < ? AND tier != 'free'",
        (stamp,),
    ).fetchall()
    downgraded: list[dict] = []
    failed: list[dict] = []
    for row in rows:
        user_id = row["id"]
        from_tier = row["tier"]
        try:
            conn.execute(
                "UPDATE users SET tier='free', tier_expires_at=NULL WHERE id=?",
                (user_id,),
            )
            conn.commit()
            trim_result = await trim_projects_to_tier_limit(conn, user_id, "free")
            downgraded.append({
                "user_id": user_id, "from_tier": from_tier,
                "deleted_project_ids": trim_result["deleted_project_ids"],
            })
        except Exception as exc:  # noqa: BLE001 — 单个账号失败不得阻塞其余到期账号
            if conn.in_transaction:
                conn.rollback()
            from app.errors import log_error
            rec = log_error(
                exc,
                action="expired_membership_sweep",
                context={"user_id": user_id},
                meta={"stage": "membership_downgrade"},
            )
            failed.append({"user_id": user_id, "error_id": rec.error_id, "error": str(exc)})
    return {"downgraded": downgraded, "downgraded_count": len(downgraded), "failed": failed}
