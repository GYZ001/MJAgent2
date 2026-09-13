"""EP-04 §6 / EP-05 §10.5 第二条：把公平调度排序键接进真实调度器（L2，
见 app/LAYERS.toml::app.quota_policy）。

**为什么现在能接、上一轮不能接**：``app/media_exec/dispatch.py::
_dispatch_due_jobs_stage_aware`` 每秒轮询一次，是全库唯一真正跨项目/跨团队
排队竞争的热路径，且有「事件循环被 sqlite 忙等冻结」的事故记录（主线程同步
写心跳等 30 秒写锁，整站无响应）。本模块因此照抄 ``app.models_registry.
health`` 的既有"拉"式范式：排序依据在进程内缓存、定期刷新（``_REFRESH_MIN_
INTERVAL_S``，默认 10s，≥5s 下限）、刷新用独立连接（``db._open_connection``，
不复用调用方正在使用的连接/事务）；轮询本身只读内存字典，不新增每秒一次的
库查询。本模块全程零写入（纯读 + 进程内缓存），比 health.py 更简单——不需要
它那条"落盘走独立连接"的分支。

**fail-safe 方向**：``reorder_lanes`` 是唯一对外入口，内部任何异常都被吞掉，
缓存缺失（从未刷新成功过）或过期（超过 ``_STALE_AFTER_S``，刷新持续失败）都
会退化为调用方原有的纯 ``-score`` 排序（``_score_only_sort``，与本模块接入
前 ``app/media_exec/dispatch.py`` 的 ``video_ready.sort(key=lambda item:
-item[0])`` 等四行逐字等价）——不阻塞、不抛出、不让队列停摆。每次退化都调
``app.observability.metrics_registry.record_fair_ordering_degraded`` 记一次
（区分 cache_stale_or_missing / exception 两种原因），复用既有监控通道，不
新建。

**排序键为什么是加权和，不是字面的字典序元组**：EP-04 §6 写的是"排序键：
(团队剩余配额比例 降序, 已等待时长 降序)"。若照字面实现成严格字典序元组
``(-ratio, -wait_seconds, -score)``，同一团队内部（现实中大多数场景——一条
车道里的候选大多来自同一批正在生成的项目/团队）任意两个候选的 ``ratio``
必然相等，字典序会退化成几乎纯按 ``-wait_seconds`` 排序（真实的创建时间戳
几乎不会相等），把 ``job_scheduler_score`` 里精心调过的硬信号（连续镜剩余
链长、首轮覆盖、已完成参考图槽位）在同团队内部整体压制成摆设——这不是"排序
优化"，是"重新发明一遍调度器"，且与 ``job_scheduler_score`` 自己的文档约定
"不取代硬优先级"直接冲突。本模块改用与 ``job_scheduler_score`` 同一惯例的
**加权和**：``score + RATIO_WEIGHT*ratio + AGE_WEIGHT*min(wait_minutes,
AGE_CAP)``。``RATIO_WEIGHT=500`` 与 score 里 ``first_pass`` 那一项
（1000）同数量级——跨团队比拼时能起决定作用，但不到"一票通杀"：一条正在
连续镜链路上的高分任务（``100*continuity_remaining``）仍能局部反超一个刚好
配额充裕但本身不重要的普通任务。``AGE_WEIGHT_PER_MIN=2.5`` 且封顶
``AGE_CAP=240`` 分钟——``job_scheduler_score`` 自己的等待分封顶 30 分钟是为
了不让"老但不重要"的镜头压过高优先级新镜头；这里专门另开一个更高的封顶，
用来兑现"等久的优先"防饿死承诺：一个团队即使 ``ratio`` 长期是 0、对手团队
持续用全新（等待时长≈0）任务竞争，只要它自己最老的任务等够 240 分钟
（``AGE_WEIGHT_PER_MIN*240=600`` 分），依然会严格反超对手满额的
``RATIO_WEIGHT``（500 分）——**封顶年龄加成必须大于满额比例加成**，否则一个
比例长期为 0 的团队会被"对手永远新鲜"这一最坏情形永久压制，防饿死承诺就是
一句空话（本模块第一版曾用 2.0/240=480<500 组合，压测"团队 B 持续提交全新
任务"场景时证伪了这条承诺，见 tests/test_dispatch_fair_ordering.py 对应
用例；现改为 2.5，留 100 分安全边际）。这依然不是"一票通杀"：满 score 差距
仍能反超（见上一段）。这是本模块评估过的三个方案里选定
的一个：方案一（字面字典序元组）如上所述会压制硬优先级，否决；方案二（把
ratio/wait 各自分桶再退回字典序）引入两个新的分桶宽度常量，边界处仍会整体
压过 score，且没有比加权和更强的依据，否决；方案三（加权和）复用本文件所在
调度器已经验证过的惯例，被选中并直接实施。

**"团队"取哪一级、用哪个资源**：只看 ``video_seconds``（本模块只服务视频
job 派发；token/image 不在这条队列的排序范围内）。项目的团队归属走
``projects.owner_user_id → team_members``——一个账号理论上可属于多个团队时，
取**最紧**（``min``）的那个团队比例，与"防止某个团队把额度耗尽后还能占用
调度优先级"的直觉一致（取最松会让人通过挂靠一个宽松团队绕过紧张团队的排队
劣势）。团队自身没有显式 ``quota_allocations`` 覆盖时，退一级看所属组织；组
织也没配置则视为不限（``ratio=1.0``）——与 ``app.quota_policy.allocation.
resolve_effective_limits`` 的"继承上级"语义一致，但本模块只做两级（team→
org），不下探到 user 级：个人配额是"对某个成员的加严/放宽"，不适合代表整个
团队的排队优先级。

**用量统计用滚动窗口，不用全量历史**：``app.quota_policy.usage_query`` 的既
有用量看板对"团队用了多少"这个问题选择不设时间窗口（一次性查看没问题），但
用在持续运行的排序信号上会让任何存活够久的团队的比例单调走向 0、永不恢复
（``quota_ledger`` 的历史 delta 不会随周期滚动清零）。本模块也不做逐用户
``period_index`` 对齐（每个用户的周期锚点 ``quota_period_started_at`` 可能
互不相同，团队级聚合没有单一"当前周期"概念可用），改用一个与
``quota_plans.period_days`` 默认值一致的粗粒度滚动窗口（近 30 天），牺牲对账
精确性换取排序信号长期有效——这只是一个排序提示，不是配额判定，不需要
``app.quota`` 那种逐笔幂等精确性。

架构红线（与 app/quota_policy/__init__.py 同一条）：本模块不得 import
app.quota——app.quota 反过来 import app.quota_policy.allocation，方向必须
单向。
"""
from __future__ import annotations

import sqlite3
import time

from app import db
from app.observability import metrics_registry
from app.orgs import store as orgs_store
from app.quota_policy import allocation

_RATIO_RESOURCE = "video_seconds"
_ROLLING_WINDOW_S = 30 * 86400.0
_REFRESH_MIN_INTERVAL_S = 10.0
_STALE_AFTER_S = 60.0
_OPEN_TIMEOUT_S = 2.0
_FAIRNESS_RATIO_WEIGHT = 500.0
_FAIRNESS_AGE_WEIGHT_PER_MIN = 2.5  # 240*2.5=600 > RATIO_WEIGHT(500)，见模块文档
_FAIRNESS_AGE_CAP_MINUTES = 240.0

_ScoredRow = tuple[float, dict]

_PROJECT_TEAM_CACHE: dict[str, dict[str, tuple[str, ...]]] = {}
_TEAM_RATIO_CACHE: dict[str, dict[str, float]] = {}
_LAST_REFRESH_ATTEMPT_AT: dict[str, float] = {}
_LAST_REFRESH_OK_AT: dict[str, float] = {}


def _db_key() -> str:
    return str(db.DB_PATH)


def reset_for_tests(db_path: str | None = None) -> None:
    """测试专用：清空某个（默认当前）db_path 下的全部内存缓存，与
    ``app.models_registry.health.reset_for_tests`` 同一手法——否则上一个测试
    留下的缓存会泄漏进下一个测试。"""
    key = db_path if db_path is not None else _db_key()
    _PROJECT_TEAM_CACHE.pop(key, None)
    _TEAM_RATIO_CACHE.pop(key, None)
    _LAST_REFRESH_ATTEMPT_AT.pop(key, None)
    _LAST_REFRESH_OK_AT.pop(key, None)


def cache_snapshot() -> dict[str, object]:
    """只读快照：当前是健康态还是退化态、缓存了多少个团队。供测试与未来的
    观测面板判断用，本身零副作用。"""
    key = _db_key()
    return {
        "healthy": not _is_degraded(),
        "last_refresh_ok_at": _LAST_REFRESH_OK_AT.get(key),
        "teams_cached": len(_TEAM_RATIO_CACHE.get(key, {})),
    }


def _is_degraded() -> bool:
    last_ok = _LAST_REFRESH_OK_AT.get(_db_key())
    if last_ok is None:
        return True  # 缓存缺失：从未成功刷新过
    return (time.time() - last_ok) > _STALE_AFTER_S  # 缓存过期：刷新持续失败


def _refresh_if_due(*, force: bool = False) -> None:
    """节流刷新；任何异常（含建表/连接失败）原样吞掉，保留上一份缓存不动，
    交给 ``_is_degraded`` 按陈旧程度判断是否已经该退化。"""
    key = _db_key()
    now_ts = time.time()
    if not force and now_ts - _LAST_REFRESH_ATTEMPT_AT.get(key, 0.0) < _REFRESH_MIN_INTERVAL_S:
        return
    _LAST_REFRESH_ATTEMPT_AT[key] = now_ts
    conn: sqlite3.Connection | None = None
    try:
        conn = db._open_connection(timeout=_OPEN_TIMEOUT_S)
        project_team = _load_project_team_map(conn)
        team_ratio = _load_team_ratios(conn)
    except Exception:
        return
    finally:
        if conn is not None:
            conn.close()
    _PROJECT_TEAM_CACHE[key] = project_team
    _TEAM_RATIO_CACHE[key] = team_ratio
    _LAST_REFRESH_OK_AT[key] = now_ts


def _load_project_team_map(conn: sqlite3.Connection) -> dict[str, tuple[str, ...]]:
    rows = conn.execute(
        "SELECT p.id AS project_id, tm.team_id AS team_id FROM projects p "
        "JOIN team_members tm ON tm.user_id = p.owner_user_id "
        "WHERE p.deleted_at IS NULL"
    ).fetchall()
    mapping: dict[str, list[str]] = {}
    for row in rows:
        mapping.setdefault(row["project_id"], []).append(row["team_id"])
    return {project_id: tuple(team_ids) for project_id, team_ids in mapping.items()}


def _load_team_ratios(conn: sqlite3.Connection) -> dict[str, float]:
    team_ids = {r["team_id"] for r in conn.execute("SELECT DISTINCT team_id FROM team_members").fetchall()}
    if not team_ids:
        return {}
    cutoff = time.time() - _ROLLING_WINDOW_S
    used_rows = conn.execute(
        "SELECT tm.team_id AS team_id, COALESCE(SUM(l.delta),0) AS total "
        "FROM quota_ledger l JOIN team_members tm ON tm.user_id = l.user_id "
        "WHERE l.resource=? AND l.created_at >= ? GROUP BY tm.team_id",
        (_RATIO_RESOURCE, cutoff),
    ).fetchall()
    used = {r["team_id"]: max(0.0, float(r["total"])) for r in used_rows}
    return {team_id: _ratio(conn, team_id, used.get(team_id, 0.0)) for team_id in team_ids}


def _ratio(conn: sqlite3.Connection, team_id: str, used: float) -> float:
    limit = _team_effective_video_limit(conn, team_id)
    if limit is None or limit <= 0:
        return 1.0  # 不限/非法上限：不惩罚，与 health.py「无证据说它坏，不拦」同一方向
    return max(0.0, min(1.0, 1.0 - used / limit))


def _team_effective_video_limit(conn: sqlite3.Connection, team_id: str) -> float | None:
    """团队自身显式配置优先；未配置（键不存在，区别于"键存在但值是 null"）
    则看所属组织；组织也未配置则视为不限。只做 team→org 两级，不下探 user
    级——见模块文档。"""
    team_limits = allocation.scope_limits_dict(conn, "team", team_id)
    if _RATIO_RESOURCE in team_limits:
        return team_limits[_RATIO_RESOURCE]
    team_row = orgs_store.get_team(conn, team_id)
    org_id = team_row["org_id"] if team_row else None
    if not org_id:
        return None
    return allocation.scope_limits_dict(conn, "org", org_id).get(_RATIO_RESOURCE)


def _team_ratio_for_project(project_id: str | None) -> float:
    if not project_id:
        return 1.0
    key = _db_key()
    team_ids = _PROJECT_TEAM_CACHE.get(key, {}).get(project_id, ())
    if not team_ids:
        return 1.0
    ratios = _TEAM_RATIO_CACHE.get(key, {})
    return min((ratios.get(team_id, 1.0) for team_id in team_ids), default=1.0)


def _fairness_bonus(project_id: str | None, wait_minutes: float) -> float:
    ratio = _team_ratio_for_project(project_id)
    capped_wait = min(max(0.0, wait_minutes), _FAIRNESS_AGE_CAP_MINUTES)
    return _FAIRNESS_RATIO_WEIGHT * ratio + _FAIRNESS_AGE_WEIGHT_PER_MIN * capped_wait


def _score_only_sort(lane: list[_ScoredRow]) -> list[_ScoredRow]:
    """与本模块接入前 dispatch.py 的 ``lane.sort(key=lambda item: -item[0])``
    逐字等价——退化路径必须原样复刻旧行为，不能是"未排序"。"""
    return sorted(lane, key=lambda item: -item[0])


def _fair_sort(lane: list[_ScoredRow], stamp: float) -> list[_ScoredRow]:
    def key(item: _ScoredRow) -> float:
        score, row = item
        wait_minutes = max(0.0, (stamp - float(row.get("created_at") or stamp)) / 60.0)
        return -(score + _fairness_bonus(row.get("project_id"), wait_minutes))

    return sorted(lane, key=key)


def reorder_lanes(
    video_ready: list[_ScoredRow],
    reference_critical: list[_ScoredRow],
    reference_normal: list[_ScoredRow],
    retake_jobs: list[_ScoredRow],
    *,
    stamp: float,
) -> tuple[list[_ScoredRow], list[_ScoredRow], list[_ScoredRow], list[_ScoredRow]]:
    """``app.media_exec.dispatch._dispatch_due_jobs_stage_aware`` 的唯一外部
    调用点：对四条 QPSP 车道分别重排。车道分类（硬优先级：finalize >
    video_ready > reference(cohort) > retake）不变，只改同车道内候选的先后
    顺序；不过滤、不新增、不丢弃任何候选——返回值是输入的一个纯排列，调度
    集合本身由既有闸门（项目级/账号级并发、配额）决定，本函数不参与准入。

    健康态：按 (score, 团队剩余配额比例, 已等待时长) 的加权和排序（见模块
    文档"排序键为什么是加权和"一节）。退化态（缓存缺失/过期/刷新异常）：
    原样退回调用方原有的纯 ``-score`` 排序，并记一次可观测的退化信号。
    """
    lanes = (video_ready, reference_critical, reference_normal, retake_jobs)
    try:
        _refresh_if_due()
        if _is_degraded():
            metrics_registry.record_fair_ordering_degraded("cache_stale_or_missing")
            return tuple(_score_only_sort(lane) for lane in lanes)
        return tuple(_fair_sort(lane, stamp) for lane in lanes)
    except Exception:
        metrics_registry.record_fair_ordering_degraded("exception")
        return tuple(_score_only_sort(lane) for lane in lanes)


__all__ = ["reorder_lanes", "reset_for_tests", "cache_snapshot"]
