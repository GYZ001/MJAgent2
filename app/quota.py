"""三档会员配额引擎：项目数 / 每模块并发 / 30 天 token 额度 / 30 天视频时长额度
+ 一次性图像成本上限池。

四类配额语义互不混同，各自独立判据（CLAUDE.md「Gates and Criteria」）：
- ``projects``：账号名下未软删除项目数上限，判据挂在 ``projects`` 表的实时计数
  （回收站已软删除的不计入）。
- ``concurrency``：同一账号同一模块同时在跑的任务数上限，判据挂在
  ``workflow_runs``/``jobs`` 里实际处于活跃状态的行数——不挂内存计数器，重启
  不丢、语义与既有状态机一致。
- ``token``：文本/结构化调用的 ``usage.total_tokens``，按 30 天滚动周期累计。
- ``video_seconds``：按镜固定 15 秒计（不是实际渲染时长），按 30 天滚动周期
  累计。

一次性图像成本池（``IMAGE_POOL_CAP``）不随 30 天周期滚动——用哨兵
``IMAGE_POOL_PERIOD_INDEX`` 存进同一张 ledger，代表"终身只有这一份"；用尽后续
图像成本改记到 ``token`` 资源里（``charge_image_cost`` 一次决定两笔分账，同一
调用方事务提交或回滚，不会出现"扣了池子没扣 token"的半途态）。

幂等：``quota_ledger`` 对 ``(resource, attempt_key, reason)`` 加 UNIQUE 约束——
同一次尝试（attempt）的同一个动作（charge/refund）只落一行；重放交给 SQLite
的 UNIQUE 冲突短路（``_record_ledger`` 捕获 ``IntegrityError``），不依赖调用方
自己去重。

所有函数都要求调用方显式传入 ``conn``（同一事务）与 ``user_id``——没有默认值、
没有隐式从 ContextVar 读取身份（CLAUDE.md「Ownership Must Be Explicit」）。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from fastapi import HTTPException

from app.db import now

PERIOD_SECONDS = 30 * 86400.0
SECONDS_PER_SHOT = 15.0
IMAGE_POOL_CAP = 3_000_000.0
IMAGE_POOL_PERIOD_INDEX = -1  # 终身池的哨兵周期号，永不随 30 天滚动前进

MODULE_SCREENPLAY = "screenplay"
MODULE_STORYBOARD = "storyboard"
MODULE_VIDEO = "video"


@dataclass(frozen=True)
class TierLimits:
    tier: str
    projects: int | None       # None = 无限（仅系统管理员）
    concurrency: int | None
    token: float | None
    video_seconds: float | None


TIER_TABLE: dict[str, TierLimits] = {
    "free": TierLimits("free", 1, 1, 300_000.0, 5 * 60.0),
    "pro": TierLimits("pro", 3, 3, 900_000.0, 15 * 60.0),
    "max": TierLimits("max", 10, 10, 3_000_000.0, 50 * 60.0),
}
VALID_TIERS = frozenset(TIER_TABLE)
_UNLIMITED = TierLimits("unlimited", None, None, None, None)

_UPGRADE_PATH = {
    "free": "升级到 Pro 档位（3 个项目 / 每模块 3 并发 / 90 万 token / 15 分钟视频）",
    "pro": "升级到 Max 档位（10 个项目 / 每模块 10 并发 / 300 万 token / 50 分钟视频）",
    "max": "已是最高付费档位；如需更高配额请联系管理员开通不限量账号",
}


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


# ---------------------------------------------------------------------------
# 归属解析（供并发/退还的调用点复用，避免各处重复写 JOIN）
# ---------------------------------------------------------------------------

def owner_of_project(conn: sqlite3.Connection, project_id: str | None) -> str | None:
    if not project_id:
        return None
    row = conn.execute(
        "SELECT owner_user_id FROM projects WHERE id=?", (project_id,)
    ).fetchone()
    if not row or not row["owner_user_id"]:
        return None
    return str(row["owner_user_id"])


def owner_of_episode(conn: sqlite3.Connection, episode_id: str | None) -> str | None:
    if not episode_id:
        return None
    row = conn.execute(
        "SELECT project_id FROM episodes WHERE id=?", (episode_id,)
    ).fetchone()
    return owner_of_project(conn, row["project_id"]) if row else None


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
    """``active_count`` 是调用方查到的、不含本次即将新建的这一个的当前活跃数。"""
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


def count_active_workflow_runs(
    conn: sqlite3.Connection, owner_user_id: str, workflow_type: str,
    *, exclude_run_id: str | None = None,
) -> int:
    """统计某账号名下、某 workflow_type 当前处于 CREATED/RUNNING 的 run 数。
    scope_type='episode' 是 screenplay/storyboard 两类 run 的既有约定，归属
    通过 episode -> project -> owner_user_id 解析。"""
    # SQL 整条内联在 execute 调用点上（可选条件用 ``? IS NULL OR`` 并进常量，
    # 不再拼变量）：tests/test_project_ownership_query_guard.py 的静态扫描只能
    # 分析调用点上的字面量，SQL 一旦先存进变量就成了它的盲区——而这条查询正是
    # 靠 ``p.owner_user_id=?`` 做账号隔离的，必须留在它看得见的地方。
    return int(
        conn.execute(
            "SELECT COUNT(*) AS c FROM workflow_runs wr "
            "JOIN episodes e ON e.id = wr.scope_id "
            "JOIN projects p ON p.id = e.project_id "
            "WHERE wr.workflow_type=? AND wr.scope_type='episode' "
            "AND p.owner_user_id=? AND wr.status IN ('CREATED','RUNNING') "
            "AND (? IS NULL OR wr.id != ?)",
            (workflow_type, owner_user_id, exclude_run_id, exclude_run_id),
        ).fetchone()["c"]
        or 0
    )


#: jobs 表里代表"这个视频任务还在推进、没有走到头"的状态集合，与既有
#: ``reconcile_episode_generation_status`` 用的口径完全一致（不新造一套判断）。
ACTIVE_JOB_STATUSES = ("queued", "running", "waiting_provider", "waiting_retry")


def count_active_video_jobs(
    conn: sqlite3.Connection, owner_user_id: str, *, exclude_job_id: str | None = None
) -> int:
    # 同上：整条内联在调用点，可选条件并进常量。f-string 里只插入由本模块常量
    # ACTIVE_JOB_STATUSES 派生的占位符，不插入任何外部输入。
    placeholders = ",".join("?" for _ in ACTIVE_JOB_STATUSES)
    return int(
        conn.execute(
            f"SELECT COUNT(*) AS c FROM jobs WHERE kind='video' AND project_id IN "
            f"(SELECT id FROM projects WHERE owner_user_id=?) "
            f"AND status IN ({placeholders}) AND (? IS NULL OR id != ?)",
            (owner_user_id, *ACTIVE_JOB_STATUSES, exclude_job_id, exclude_job_id),
        ).fetchone()["c"]
        or 0
    )


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
    """图像生成成本：先扣「定妆照/场景图」终身上限池，池子不够再扣 30 天 token
    额度；两段不足以覆盖时整体拒绝（两条 ledger 写入与调用方其它写入共享同一
    个未提交事务，拒绝时整体回滚，不留"扣一半"的半途态）。"""
    if cost <= 0:
        return {"pool_charged": 0.0, "token_charged": 0.0, "idempotent_replay": False}
    pool_existing = _charge_exists(conn, "image_pool", attempt_key)
    token_existing = _charge_exists(conn, "token", attempt_key)
    if pool_existing is not None or token_existing is not None:
        return {
            "pool_charged": float(pool_existing["delta"]) if pool_existing else 0.0,
            "token_charged": float(token_existing["delta"]) if token_existing else 0.0,
            "idempotent_replay": True,
        }
    limits = effective_limits(conn, user_id)
    if limits.token is None:
        # 无限量账号：全记进池子做审计留痕，不做上限判断。
        _record_ledger(
            conn, user_id=user_id, resource="image_pool", pidx=IMAGE_POOL_PERIOD_INDEX,
            attempt_key=attempt_key, reason="charge", delta=float(cost),
        )
        return {"pool_charged": float(cost), "token_charged": 0.0, "idempotent_replay": False}
    pool_used = usage_for(conn, user_id, "image_pool", IMAGE_POOL_PERIOD_INDEX)
    from_pool = min(cost, max(0.0, IMAGE_POOL_CAP - pool_used))
    remainder = cost - from_pool
    anchor = period_anchor(conn, user_id)
    pidx = period_index(anchor)
    token_used = usage_for(conn, user_id, "token", pidx)
    if remainder > 0 and token_used + remainder > limits.token:
        raise QuotaExceeded(
            gate="token", tier=limits.tier, limit=limits.token, used=token_used,
            remaining=max(0.0, limits.token - token_used), reset_at=period_reset_at(anchor),
            message=(
                "图像生成的一次性上限池已耗尽，30 天 token 额度也不足以覆盖"
                f"剩余成本（{limits.tier} 档上限 {int(limits.token)}）"
            ),
        )
    if from_pool > 0:
        _record_ledger(
            conn, user_id=user_id, resource="image_pool", pidx=IMAGE_POOL_PERIOD_INDEX,
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
# 视频时长额度（按镜固定 15 秒）
# ---------------------------------------------------------------------------

def reserve_video_seconds(
    conn: sqlite3.Connection, user_id: str, *, attempt_key: str, seconds: float = SECONDS_PER_SHOT
) -> dict:
    """在视频任务（这一镜的这一次尝试）创建的同一事务里调用；``attempt_key``
    用 jobs.id（一个 job_id 即一次完整的"占位 -> 提交 -> 轮询 -> 完成"生命周
    期，重试释放槽位并开新 job_id，天然对应"这一次尝试"）。"""
    if _charge_exists(conn, "video_seconds", attempt_key) is not None:
        return {"charged_s": 0.0, "idempotent_replay": True}
    limits = effective_limits(conn, user_id)
    anchor = period_anchor(conn, user_id)
    pidx = period_index(anchor)
    if limits.video_seconds is not None:
        used = usage_for(conn, user_id, "video_seconds", pidx)
        if used + seconds > limits.video_seconds:
            raise QuotaExceeded(
                gate="video_seconds", tier=limits.tier, limit=limits.video_seconds,
                used=used, remaining=max(0.0, limits.video_seconds - used),
                reset_at=period_reset_at(anchor),
                message=(
                    f"30 天视频时长额度已用尽（{limits.tier} 档上限 "
                    f"{int(limits.video_seconds)} 秒）"
                ),
            )
    applied = _record_ledger(
        conn, user_id=user_id, resource="video_seconds", pidx=pidx,
        attempt_key=attempt_key, reason="charge", delta=float(seconds),
    )
    return {"charged_s": float(seconds) if applied else 0.0, "idempotent_replay": not applied}


def refund_video_seconds(conn: sqlite3.Connection, user_id: str, *, attempt_key: str) -> dict:
    """按产物信号退还：调用方只需判断"这一次尝试有没有产出可用视频"，不问原
    因（超时/门禁拦截/畸形 JSON 等任何失败原因都走同一条路径）。幂等：没有对
    应 charge 记录、或已经退过款，都安全返回 no-op。"""
    charge = _charge_exists(conn, "video_seconds", attempt_key)
    if charge is None:
        return {"refunded_s": 0.0, "idempotent_replay": False, "no_charge_found": True}
    if _refund_exists(conn, "video_seconds", attempt_key):
        return {"refunded_s": 0.0, "idempotent_replay": True, "no_charge_found": False}
    amount = float(charge["delta"])
    applied = _record_ledger(
        conn, user_id=user_id, resource="video_seconds", pidx=int(charge["period_index"]),
        attempt_key=attempt_key, reason="refund", delta=-amount,
    )
    return {
        "refunded_s": amount if applied else 0.0, "idempotent_replay": not applied,
        "no_charge_found": False,
    }


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
