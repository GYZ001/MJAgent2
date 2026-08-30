"""五档会员配额引擎：项目数 / 每模块并发 / 30 天 token 额度 / 30 天视频时长额度
/ 30 天定妆照与场景图（图像）额度，外加不随周期重置的「视频加量包」。

五类订阅配额语义互不混同，各自独立判据（CLAUDE.md「Gates and Criteria」）：
- ``projects``：账号名下未软删除项目数上限，判据挂在 ``projects`` 表的实时计数
  （回收站已软删除的不计入）。
- ``concurrency``：同一账号同一模块同时在跑的任务数上限，判据挂在
  ``workflow_runs``/``jobs`` 里实际处于活跃状态的行数——不挂内存计数器，重启
  不丢、语义与既有状态机一致。
- ``token``：文本/结构化调用的 ``usage.total_tokens``，按 30 天滚动周期累计。
- ``video_seconds``：按镜固定 15 秒计（不是实际渲染时长），按 30 天滚动周期
  累计。
- ``image``：定妆照/场景图生成成本，按各档位独立上限、与 token/video_seconds
  共用同一个 30 天滚动周期（同一账号同一时刻只有一个周期锚点，随周期重置，
  不再是终身一次性池）。额度用尽后续图像成本改记到 ``token`` 资源里
  （``charge_image_cost`` 一次决定两笔分账，同一调用方事务提交或回滚，不会
  出现"扣了图像额度没扣 token"的半途态）。

五档图像额度数值来自实测：建一个项目的定妆照/场景图成本 2.22M–3.97M（三个只
做筹备的真实项目），且与各档项目数上限对齐（image ≈ projects × 300 万）。
free 档是有意的不对称设计：只砍视频（5 分钟→1 分钟），不砍图像——300 万图像
仍够完整建 1 个项目，免费用户能看到世界书/定妆照/场景图和前几个镜头的成片，
只是视频产量被卡住，体验上"该看的都看到、就是不够产"。

视频加量包（``video_addon_seconds`` 资源）是订阅之外的第六类配额，与前五类
的关键差异：**不随 30 天周期重置**——买了就是买了，直到被消耗完。``TIER_TABLE``
只承载会随周期重置的订阅上限，加量包故意不进这张表，而是单独用 ``quota_ledger``
的 ``reason='grant'``（入账）/``reason='charge'``（消耗）/``reason='refund'``
（退还）三态记账，``addon_video_seconds_balance()`` 用 grant-charge+refund 现
算余额，不缓存总数——每一笔购买都是独立可溯源的 ledger 行（谁、何时、多少），
不是"加一个总数字"。``reserve_video_seconds`` 消耗顺序固定为先订阅、后加量包：
订阅额度本来就会随周期作废，先花掉它才不浪费；加量包不会过期，晚花不吃亏。
定价 ``ADDON_PACKAGE_PRICE_CNY``（¥199/10 分钟，19.9 元/分钟）故意高于所有订阅
档的分钟单价（19.8→14.0 元/分钟递减），让"买包"永远不如"升档"划算——这是产品
决策，不是本模块要校验的东西，本模块只管额度记账与消耗顺序。

幂等：``quota_ledger`` 对 ``(resource, attempt_key, reason)`` 加 UNIQUE 约束——
同一次尝试（attempt）的同一个动作（charge/refund/grant）只落一行；重放交给
SQLite 的 UNIQUE 冲突短路（``_record_ledger`` 捕获 ``IntegrityError``），不依赖
调用方自己去重。加量包入账复用同一条机制：``attempt_key`` 是这笔购买的稳定标
识（未来接真实支付时应传订单号），重复入账只生效一次。

所有函数都要求调用方显式传入 ``conn``（同一事务）与 ``user_id``——没有默认值、
没有隐式从 ContextVar 读取身份（CLAUDE.md「Ownership Must Be Explicit」）。
"""
from __future__ import annotations

import sqlite3

from fastapi import HTTPException

from app.db import now
from app.quota_addon import (
    ADDON_RESOURCE,
    addon_video_seconds_balance,
)
from app.quota_scope import (
    ACTIVE_JOB_STATUSES as ACTIVE_JOB_STATUSES,
    count_active_video_jobs as count_active_video_jobs,
    count_active_workflow_runs as count_active_workflow_runs,
    owner_of_episode as owner_of_episode,
    owner_of_project as owner_of_project,
)
from app.quota_tiers import (
    TIER_TABLE as TIER_TABLE,
    TierLimits as TierLimits,
    VALID_TIERS as VALID_TIERS,
    _UNLIMITED as _UNLIMITED,
    _UPGRADE_PATH as _UPGRADE_PATH,
)

PERIOD_SECONDS = 30 * 86400.0
SECONDS_PER_SHOT = 15.0

MODULE_SCREENPLAY = "screenplay"
MODULE_STORYBOARD = "storyboard"
MODULE_VIDEO = "video"


class QuotaExceeded(HTTPException):
    """某一类配额已耗尽。直接是 HTTPException 子类，任何入口 raise 都会被
    FastAPI 转成 429，不依赖某个特定路由层做二次翻译（CLAUDE.md「错误要转成
    合适的状态码」）。``detail`` 覆盖「拦住用户时必须给出路」的四项要求：还
    剩多少（remaining）、下次重置时间（reset_at，并发/项目数两类没有固定重置
    时间，为 None）、当前档位（tier）、怎么升级（upgrade_path）；``gate`` 字段
    区分四类配额里具体是哪一类卡住了，不糊成一个笼统的"配额不足"。
    """

    def __init__(
        self, *, gate: str, tier: str, limit: float | int | None,
        used: float | int, remaining: float | int, message: str,
        reset_at: float | None = None,
    ) -> None:
        detail = {
            "code": f"QUOTA_EXCEEDED_{gate.upper()}",
            "gate": gate, "message": message, "tier": tier, "limit": limit,
            "used": used, "remaining": remaining, "reset_at": reset_at,
            "upgrade_path": _UPGRADE_PATH.get(tier, _UPGRADE_PATH["free"]),
        }
        super().__init__(status_code=429, detail=detail)


# ---------------------------------------------------------------------------
# 周期与档位解析
# ---------------------------------------------------------------------------

def period_index(started_at: float, at: float | None = None) -> int:
    """30 天为一个周期，从 ``started_at``（开户日）起算——不是自然月。"""
    at = now() if at is None else at
    if not started_at or started_at <= 0:
        started_at = at
    return int(max(0.0, at - started_at) // PERIOD_SECONDS)


def period_reset_at(started_at: float, at: float | None = None) -> float:
    at = now() if at is None else at
    idx = period_index(started_at, at)
    if not started_at or started_at <= 0:
        started_at = at
    return started_at + (idx + 1) * PERIOD_SECONDS


def _user_row(conn: sqlite3.Connection, user_id: str) -> sqlite3.Row | None:
    """``None`` 既覆盖"找不到这个账号"，也覆盖"这张表/这几列压根不存在"——
    后者只发生在手写简化 schema 的既有测试双（真实库永远由 ``init_db()`` 建全
    ``users`` 表 + 本模块的迁移列），与 ``effective_limits`` 对"账号不存在"的
    既有兼容处理是同一个既有约定，不是本模块新引入的口子。"""
    try:
        return conn.execute(
            "SELECT id,tier,quota_period_started_at,is_system_admin,created_at "
            "FROM users WHERE id=?", (user_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        return None


def effective_limits(conn: sqlite3.Connection, user_id: str) -> TierLimits:
    """account -> 生效配额上限。找不到 ``user_id`` 对应的账号行（兼容期共享
    会话占位账号 ``legacy-shared``、内部脚本、测试直接调用等，见
    ``app.domain.projects._creation_owner_user_id`` 的同一条注释）时按无限量
    放行——与 ``app.domain.common._assert_principal_owns`` 对
    ``principal is None`` 的处理是同一个既有约定，不是本模块新引入的口子。
    """
    row = _user_row(conn, user_id)
    if row is None:
        return _UNLIMITED
    if int(row["is_system_admin"] or 0):
        return _UNLIMITED
    tier = row["tier"] or "free"
    return TIER_TABLE.get(tier, TIER_TABLE["free"])


def period_anchor(conn: sqlite3.Connection, user_id: str) -> float:
    row = _user_row(conn, user_id)
    if row is None:
        return now()
    started = row["quota_period_started_at"]
    return float(started) if started else float(row["created_at"] or now())


# 归属解析（owner_of_project/owner_of_episode）实现见 app/quota_scope.py，已
# import 进本模块命名空间，外部调用方一律仍走 quota.owner_of_project 等既有
# 路径，不用改调用点。
# ---------------------------------------------------------------------------
# Ledger 原语
# ---------------------------------------------------------------------------

def _record_ledger(
    conn: sqlite3.Connection, *, user_id: str, resource: str, pidx: int,
    attempt_key: str, reason: str, delta: float,
) -> bool:
    """写一行 ledger；命中 UNIQUE 冲突（重放）时返回 False，不报错。"""
    try:
        conn.execute(
            "INSERT INTO quota_ledger(user_id,resource,period_index,attempt_key,"
            "reason,delta,created_at) VALUES(?,?,?,?,?,?,?)",
            (user_id, resource, pidx, attempt_key, reason, float(delta), now()),
        )
        return True
    except sqlite3.IntegrityError:
        return False


def usage_for(conn: sqlite3.Connection, user_id: str, resource: str, pidx: int) -> float:
    row = conn.execute(
        "SELECT COALESCE(SUM(delta),0) AS u FROM quota_ledger "
        "WHERE user_id=? AND resource=? AND period_index=?",
        (user_id, resource, pidx),
    ).fetchone()
    return float(row["u"] or 0.0)


def _charge_exists(conn: sqlite3.Connection, resource: str, attempt_key: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT delta,period_index FROM quota_ledger "
        "WHERE resource=? AND attempt_key=? AND reason='charge'",
        (resource, attempt_key),
    ).fetchone()


def _refund_exists(conn: sqlite3.Connection, resource: str, attempt_key: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM quota_ledger WHERE resource=? AND attempt_key=? AND reason='refund'",
        (resource, attempt_key),
    ).fetchone()
    return row is not None


# ---------------------------------------------------------------------------
# 项目数
# ---------------------------------------------------------------------------

def check_project_slot(conn: sqlite3.Connection, user_id: str, *, active_count: int) -> None:
    """创建新项目前调用；``active_count`` 是调用方在同一事务里查到的、当前未
    软删除的项目数（回收站不计入）。判据挂在这个实时计数，不挂状态字段。"""
    limits = effective_limits(conn, user_id)
    if limits.projects is None:
        return
    if active_count >= limits.projects:
        raise QuotaExceeded(
            gate="projects", tier=limits.tier, limit=limits.projects,
            used=active_count, remaining=max(0, limits.projects - active_count),
            message=(
                f"{limits.tier} 档最多同时拥有 {limits.projects} 个项目"
                f"（回收站中的不计入），当前已有 {active_count} 个"
            ),
        )


# ---------------------------------------------------------------------------
# 每模块并发
# ---------------------------------------------------------------------------

def check_module_concurrency(
    conn: sqlite3.Connection, user_id: str, module: str, *, active_count: int
) -> None:
    """判断该账号在此模块是否还有并发余量。纯判断：不取数、不占位。

    ⚠️ 契约：``active_count`` 的计数与占位（新建那行 ``workflow_runs``）必须在
    **同一个 ``BEGIN IMMEDIATE`` 事务**里——本函数拿不到调用方的事务边界。占位先于
    判断，故 ``active_count`` = 除刚插入那行外还在跑的数量。照抄 ``_reserve_*``。"""
    limits = effective_limits(conn, user_id)
    if limits.concurrency is None:
        return
    if active_count >= limits.concurrency:
        raise QuotaExceeded(
            gate="concurrency", tier=limits.tier, limit=limits.concurrency,
            used=active_count, remaining=max(0, limits.concurrency - active_count),
            message=(
                f"{module} 同时在跑的任务已达 {limits.tier} 档上限"
                f"（{limits.concurrency} 个），请等待现有任务结束后再试"
            ),
        )


# count_active_workflow_runs / count_active_video_jobs / ACTIVE_JOB_STATUSES
# 实现见 app/quota_scope.py，已 import 进本模块命名空间，见上。


# ---------------------------------------------------------------------------
# token 额度（含图像溢出到 token 的那一段）
# ---------------------------------------------------------------------------

def assert_token_capacity(conn: sqlite3.Connection, user_id: str) -> None:
    """新任务启动前的前置闸门：账号当前 30 天 token 用量是否已经见顶。token 的
    确切花费只有调用真正结束时才知道（``charge_tokens`` 在那一刻记账），不能
    像视频时长那样在任务创建的同一事务里精确预扣；这里在"是否开启新一轮生成
    任务"的入口挡住已经超额的账号——同一任务内部可能因这次调用把额度压过线，
    但下一次新任务会被这里拦下，不会无限透支。
    """
    limits = effective_limits(conn, user_id)
    if limits.token is None:
        return
    anchor = period_anchor(conn, user_id)
    pidx = period_index(anchor)
    used = usage_for(conn, user_id, "token", pidx)
    if used >= limits.token:
        raise QuotaExceeded(
            gate="token", tier=limits.tier, limit=limits.token, used=used,
            remaining=max(0.0, limits.token - used), reset_at=period_reset_at(anchor),
            message=f"30 天 token 额度已用尽（{limits.tier} 档上限 {int(limits.token)}）",
        )


def charge_tokens(
    conn: sqlite3.Connection, user_id: str, tokens: float, *, attempt_key: str
) -> dict:
    """按实际 usage.total_tokens 记账。``attempt_key`` 是这次 provider call 的
    稳定标识（如 ``provider_calls.id``），重放保证只记一次。"""
    if tokens <= 0:
        return {"charged": 0.0, "idempotent_replay": False}
    if _charge_exists(conn, "token", attempt_key) is not None:
        return {"charged": 0.0, "idempotent_replay": True}
    pidx = period_index(period_anchor(conn, user_id))
    applied = _record_ledger(
        conn, user_id=user_id, resource="token", pidx=pidx,
        attempt_key=attempt_key, reason="charge", delta=float(tokens),
    )
    return {"charged": float(tokens) if applied else 0.0, "idempotent_replay": not applied}


def charge_image_cost(
    conn: sqlite3.Connection, user_id: str, cost: float, *, attempt_key: str
) -> dict:
    """图像生成成本：先扣本账号档位当前 30 天周期的「定妆照/场景图」额度，额
    度不够再扣同一周期的 token 额度；两段不足以覆盖时整体拒绝（两条 ledger 写
    入与调用方其它写入共享同一个未提交事务，拒绝时整体回滚，不留"扣一半"的
    半途态）。image 与 token 用同一个 ``period_index``（同一账号同一周期锚
    点），随 30 天周期一起重置，不再是终身一次性池。"""
    if cost <= 0:
        return {"pool_charged": 0.0, "token_charged": 0.0, "idempotent_replay": False}
    pool_existing = _charge_exists(conn, "image", attempt_key)
    token_existing = _charge_exists(conn, "token", attempt_key)
    if pool_existing is not None or token_existing is not None:
        return {
            "pool_charged": float(pool_existing["delta"]) if pool_existing else 0.0,
            "token_charged": float(token_existing["delta"]) if token_existing else 0.0,
            "idempotent_replay": True,
        }
    limits = effective_limits(conn, user_id)
    anchor = period_anchor(conn, user_id)
    pidx = period_index(anchor)
    if limits.token is None:
        # 无限量账号：全记进图像资源做审计留痕，不做上限判断。
        _record_ledger(
            conn, user_id=user_id, resource="image", pidx=pidx,
            attempt_key=attempt_key, reason="charge", delta=float(cost),
        )
        return {"pool_charged": float(cost), "token_charged": 0.0, "idempotent_replay": False}
    image_cap = limits.image if limits.image is not None else 0.0
    pool_used = usage_for(conn, user_id, "image", pidx)
    from_pool = min(cost, max(0.0, image_cap - pool_used))
    remainder = cost - from_pool
    token_used = usage_for(conn, user_id, "token", pidx)
    if remainder > 0 and token_used + remainder > limits.token:
        raise QuotaExceeded(
            gate="token", tier=limits.tier, limit=limits.token, used=token_used,
            remaining=max(0.0, limits.token - token_used), reset_at=period_reset_at(anchor),
            message=(
                "30 天定妆照/场景图额度已耗尽，30 天 token 额度也不足以覆盖"
                f"剩余成本（{limits.tier} 档上限 {int(limits.token)}）"
            ),
        )
    if from_pool > 0:
        _record_ledger(
            conn, user_id=user_id, resource="image", pidx=pidx,
            attempt_key=attempt_key, reason="charge", delta=float(from_pool),
        )
    if remainder > 0:
        _record_ledger(
            conn, user_id=user_id, resource="token", pidx=pidx,
            attempt_key=attempt_key, reason="charge", delta=float(remainder),
        )
    return {
        "pool_charged": float(from_pool), "token_charged": float(remainder),
        "idempotent_replay": False,
    }


def _billing_category(kind: str) -> str | None:
    """从 ``provider_calls.kind`` 推断计费类别：``'text'``/``'image'``/``None``
    （不计费）。``kind`` 是我们自己在调用点赋的内部标签（闭合枚举，不是模型/
    用户可控的开放文本，与 CLAUDE.md「禁止黑白名单」针对的是两类不同问题），
    前缀匹配覆盖 chat/chat_tools 等文本变体与 image_generate/image_edit 等图
    像变体。video_* 及其它一律不计费——未识别的新 kind 保守跳过而不是乱扣。
    """
    if not kind:
        return None
    if kind.startswith("image"):
        return "image"
    if kind == "chat" or kind.startswith("chat_"):
        return "text"
    return None


def _extract_total_tokens(response_json: object) -> float:
    if not isinstance(response_json, dict):
        return 0.0
    usage = response_json.get("usage")
    if not isinstance(usage, dict):
        return 0.0
    total = usage.get("total_tokens")
    if isinstance(total, (int, float)) and not isinstance(total, bool):
        return float(total)
    prompt = usage.get("prompt_tokens")
    completion = usage.get("completion_tokens")
    if isinstance(prompt, (int, float)) or isinstance(completion, (int, float)):
        prompt_v = float(prompt) if isinstance(prompt, (int, float)) else 0.0
        completion_v = float(completion) if isinstance(completion, (int, float)) else 0.0
        return prompt_v + completion_v
    return 0.0


def charge_for_finished_provider_call(
    conn: sqlite3.Connection, *, call_id: int, kind: str,
    project_id: str | None, response_json: object,
) -> None:
    """``finish_provider_call`` 落库后的记账挂钩：只记账，不拦截。调用已经真
    实发生、成本已经产生——这里如实记录，准入判断在"是否开启新一轮任务"的
    入口（见 ``assert_token_capacity``）。主动吞掉 ``QuotaExceeded``（图像溢出
    到 token 且 token 也不足时会抛），不能让记账异常打断刚落库的调用状态更新。
    """
    category = _billing_category(kind)
    if category is None or not project_id:
        return
    owner_user_id = owner_of_project(conn, project_id)
    if owner_user_id is None:
        return
    tokens = _extract_total_tokens(response_json)
    if tokens <= 0:
        return
    attempt_key = f"call:{call_id}"
    try:
        if category == "text":
            charge_tokens(conn, owner_user_id, tokens, attempt_key=attempt_key)
        else:
            charge_image_cost(conn, owner_user_id, tokens, attempt_key=attempt_key)
    except QuotaExceeded:
        pass


# ---------------------------------------------------------------------------
# 视频时长额度（按镜固定 15 秒；订阅额度用尽后自动溢出到加量包）
# 加量包余额计算/入账实现见 app/quota_addon.py，已 import 进本模块命名空间。
# ---------------------------------------------------------------------------

def reserve_video_seconds(
    conn: sqlite3.Connection, user_id: str, *, attempt_key: str, seconds: float = SECONDS_PER_SHOT
) -> dict:
    """在视频任务（这一镜的这一次尝试）创建的同一事务里调用；``attempt_key``
    用 jobs.id（一个 job_id 即一次完整的"占位 -> 提交 -> 轮询 -> 完成"生命周
    期，重试释放槽位并开新 job_id，天然对应"这一次尝试"）。

    消耗顺序：先扣本周期订阅额度，订阅额度不够再扣加量包余额——订阅额度会随
    30 天周期作废，加量包不会过期，先花订阅额度不浪费。两笔（或一笔）charge
    在同一次调用里一起记账，对上层是原子的一次"扣费"。
    """
    if (
        _charge_exists(conn, "video_seconds", attempt_key) is not None
        or _charge_exists(conn, ADDON_RESOURCE, attempt_key) is not None
    ):
        return {"charged_s": 0.0, "idempotent_replay": True}
    limits = effective_limits(conn, user_id)
    anchor = period_anchor(conn, user_id)
    pidx = period_index(anchor)
    from_sub = float(seconds)
    from_addon = 0.0
    if limits.video_seconds is not None:
        used = usage_for(conn, user_id, "video_seconds", pidx)
        sub_remaining = max(0.0, limits.video_seconds - used)
        from_sub = min(seconds, sub_remaining)
        overflow = seconds - from_sub
        if overflow > 0:
            addon_balance = addon_video_seconds_balance(conn, user_id)
            if overflow > addon_balance:
                raise QuotaExceeded(
                    gate="video_seconds", tier=limits.tier, limit=limits.video_seconds,
                    used=used, remaining=max(0.0, sub_remaining + addon_balance),
                    reset_at=period_reset_at(anchor),
                    message=(
                        f"30 天视频时长额度已用尽（{limits.tier} 档上限 "
                        f"{int(limits.video_seconds)} 秒），加量包余额 "
                        f"{int(addon_balance)} 秒也不足以覆盖本次所需的 "
                        f"{int(seconds)} 秒"
                    ),
                )
            from_addon = overflow
    charged = 0.0
    if from_sub > 0:
        applied_sub = _record_ledger(
            conn, user_id=user_id, resource="video_seconds", pidx=pidx,
            attempt_key=attempt_key, reason="charge", delta=from_sub,
        )
        if applied_sub:
            charged += from_sub
    if from_addon > 0:
        applied_addon = _record_ledger(
            conn, user_id=user_id, resource=ADDON_RESOURCE, pidx=pidx,
            attempt_key=attempt_key, reason="charge", delta=from_addon,
        )
        if applied_addon:
            charged += from_addon
    return {"charged_s": charged, "idempotent_replay": False}


def refund_video_seconds(conn: sqlite3.Connection, user_id: str, *, attempt_key: str) -> dict:
    """按产物信号退还：调用方只需判断"这一次尝试有没有产出可用视频"，不问原
    因（超时/门禁拦截/畸形 JSON 等任何失败原因都走同一条路径）。幂等：没有对
    应 charge 记录、或已经退过款，都安全返回 no-op。

    一次 ``reserve_video_seconds`` 可能同时在订阅（``video_seconds``）和加量包
    （``ADDON_RESOURCE``）两个资源上各留一笔 charge（订阅额度不够、溢出到加量
    包的那一次尝试）；退还时两笔一起找、一起退，不会漏掉加量包那一半。
    """
    sub_charge = _charge_exists(conn, "video_seconds", attempt_key)
    addon_charge = _charge_exists(conn, ADDON_RESOURCE, attempt_key)
    if sub_charge is None and addon_charge is None:
        return {"refunded_s": 0.0, "idempotent_replay": False, "no_charge_found": True}
    if _refund_exists(conn, "video_seconds", attempt_key) or _refund_exists(
        conn, ADDON_RESOURCE, attempt_key
    ):
        return {"refunded_s": 0.0, "idempotent_replay": True, "no_charge_found": False}
    refunded = 0.0
    if sub_charge is not None:
        amount = float(sub_charge["delta"])
        if _record_ledger(
            conn, user_id=user_id, resource="video_seconds",
            pidx=int(sub_charge["period_index"]), attempt_key=attempt_key,
            reason="refund", delta=-amount,
        ):
            refunded += amount
    if addon_charge is not None:
        amount = float(addon_charge["delta"])
        if _record_ledger(
            conn, user_id=user_id, resource=ADDON_RESOURCE,
            pidx=int(addon_charge["period_index"]), attempt_key=attempt_key,
            reason="refund", delta=-amount,
        ):
            refunded += amount
    return {"refunded_s": refunded, "idempotent_replay": False, "no_charge_found": False}


def reconcile_video_seconds_refunds(conn: sqlite3.Connection, episode_id: str) -> int:
    """按产物信号回收本集里未产出可用视频的镜头尝试的 15 秒预扣。只看两件事：
    这一次尝试（jobs.id）的槽位是否已经释放（video_slot_active=0，代表这次
    尝试已经走到头），以及它挂的版本是否成功（shot_versions.status=
    'succeeded'）——不看 jobs.status/error 具体是什么（CLAUDE.md：退还判据挂
    产物信号，不挂错误码）。视频流水线里有 30+ 处会把 video_slot_active 置 0，
    本函数不追着每一处单独埋点，而是在既有的、几乎每次状态变化后都会被调用
    一次的 ``reconcile_episode_generation_status`` 里做一次收口检查——产物信
    号本身是唯一权威依据，调用时机不影响判断结果。幂等：``refund_video_
    seconds`` 保证同一个 job_id 只退一次，本函数可反复调用不会重复退款。
    """
    rows = conn.execute(
        """SELECT j.id AS job_id, j.project_id, v.status AS version_status
             FROM jobs j
             LEFT JOIN shot_versions v ON v.id = j.version_id
            WHERE j.episode_id=? AND j.kind='video' AND j.video_slot_active=0""",
        (episode_id,),
    ).fetchall()
    if not rows:
        return 0
    if conn.in_transaction:
        conn.commit()
    refunded = 0
    try:
        for row in rows:
            owner_user_id = owner_of_project(conn, row["project_id"])
            if owner_user_id is None or row["version_status"] == "succeeded":
                continue
            result = refund_video_seconds(conn, owner_user_id, attempt_key=row["job_id"])
            if result["refunded_s"] > 0:
                refunded += 1
        conn.commit()
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    return refunded
