"""会员到期闸门：账号当前持有的付费档位是否已经过期，过期则拦截新任务发起。

与 ``app/quota.py`` 是两个独立维度，故意不合并：``app.quota`` 管「这一档位允许
多少」（项目数/并发/token/视频时长/图像五类上限，随 30 天用量周期滚动重置）；
本模块管「这个账号现在还配不配拥有它正持有的这一档」——一次性的、到期即翻转
成 free，不随用量重置。``users.tier_expires_at``（``app/db.py`` MIGRATIONS 新增
列，NULL＝不过期）是唯一判据；到期后的降级动作（清空这一列 + 裁剪超额项目）
是 ``app.domain.projects.downgrade`` 的职责，本模块只读、只判、只拦，不写。

产品规则（用户已拍板，本模块只负责第一条——"不能发起新的"）：
- 到期后不掐在途任务：本模块只在"是否开启新一轮生成任务"的入口被调用（与
  ``quota.assert_token_capacity``/``quota.check_module_concurrency`` 同一批
  接线点），从不读取 ``workflow_runs``/``jobs`` 之类的在途任务表，结构上就够
  不到任何一个已经在跑的任务，不存在"顺手拦一下在途任务"的风险。
- 降级后超额项目只保留最新的若干个：见 ``app.domain.projects.downgrade.
  trim_projects_to_tier_limit``。
- 加量包余额长期保留：本模块与加量包记账（``app/quota_addon.py``）毫无交集。

复用 ``app.quota.QuotaExceeded`` 而不是新造一个异常类型——同一批调用点（
``guarded.py``/``task_run.py``/``scene_bible_prep.py``/``refs_generation.py``/
``task_body.py``/``media_exec/enqueue.py``）已经在 ``except quota.QuotaExceeded``
或直接放行 HTTPException 子类给 FastAPI 转 429，多一个异常类型只会让调用方
多写一层 ``except``，且 ``QuotaExceeded`` 本身已经覆盖「拦住用户时必须给出路」
的四项要求（CLAUDE.md「错误要转成合适的状态码」/「拦住用户时必须给出路」）。
``app/quota.py`` 不反向 import 本模块——依赖方向单向，不构成环。
"""
from __future__ import annotations

import sqlite3
import time

from app.db import now
from app.quota import QuotaExceeded


def _user_row(conn: sqlite3.Connection, user_id: str) -> sqlite3.Row | None:
    """``None`` 既覆盖"找不到这个账号"，也覆盖"这张表/这几列压根不存在"——
    后者只发生在手写简化 schema 的既有测试双，与 ``app.quota._user_row`` 同一条
    既有约定（那边的注释解释了为什么这不是本模块新引入的口子）。"""
    try:
        return conn.execute(
            "SELECT tier, tier_expires_at FROM users WHERE id=?", (user_id,)
        ).fetchone()
    except sqlite3.OperationalError:
        return None


def assert_membership_active(conn: sqlite3.Connection, user_id: str) -> None:
    """新任务启动前的前置闸门：账号当前持有的付费档位是否已经过期。

    找不到账号行、``tier_expires_at`` 为 NULL（不过期：free 档、管理员手工授予
    的永久档位、历史账号、本功能尚未触达的任何账号）、或者还没到期，一律放行
    ——与 ``app.quota.effective_limits`` 对"账号不存在"的既有兼容处理是同一条
    既有约定，不是本模块新引入的口子。只有"确实过期"这一条路径会拦截。
    """
    row = _user_row(conn, user_id)
    if row is None:
        return
    expires_at = row["tier_expires_at"]
    if not expires_at:
        return
    if now() < float(expires_at):
        return
    tier = row["tier"] or "free"
    expired_str = time.strftime("%Y-%m-%d %H:%M", time.localtime(float(expires_at)))
    raise QuotaExceeded(
        gate="membership",
        tier=tier,
        limit=None,
        used=0,
        remaining=0,
        reset_at=float(expires_at),
        message=(
            f"{tier} 档会员已于 {expired_str} 到期，暂时不能发起新的生成任务；"
            "已在进行中的任务不受影响，会继续正常跑完、不会被中断。"
            "请前往账户中心续费。"
        ),
    )
