"""EP-04 第一阶段：quota_plans / quota_allocations / quota_alerts 三表，lazy
建表（L2，见 app/LAYERS.toml::app.quota_policy）。照抄 app/orgs/schema.py 的
手法——``app/db.py`` 的 line_count 基线已被 orgs 那六张表用满过一次（已撤
回），本包不再碰 app/db.py。

``ensure_schema()`` 按当前 ``db.DB_PATH`` 幂等记忆（与 app/orgs/schema.py 同
一惯例）；``app.main`` 的 lifespan 显式调一次，``app/quota_policy/*.py`` 每个
读写函数也各自兜底调用一次（多数测试不经 lifespan）。

五档 ``TIER_TABLE``（app/quota_tiers.py）种子成 ``builtin=1`` 的 quota_plans
行——这是 PRD EP-04 §3 "TIER_TABLE 变成种子数据"的字面落地，但**只是种子/
展示数据**，不是 ``effective_limits()`` 默认路径的判定依据：账号没有任何
组织/团队/用户级 quota_allocations 时，``app.quota.effective_limits()`` 仍然
直接读活的 ``TIER_TABLE.get(tier, ...)``（见该函数与
``tests/test_quota.py::test_patch_quota_everywhere_makes_fake_tier_table_reach_real_call_path``
——那条测试 monkeypatch ``TIER_TABLE`` 后立刻断言 ``effective_limits`` 看到假
表，如果默认路径改成读这里种下的 DB 快照就会看不到运行时打的桩，是"迁移零
变化"与"TIER_TABLE 打桩不失效"两条红线的交叉点）。种子行的用途是：企业管理
员在 quota_policy/api.py 创建组织/团队级分配时，可以直接引用这 5 个内置
plan_id，而不必先手抄一遍五档数值建自定义 plan。

种子用确定性 id（``qp_builtin_<tier>``）+ ``INSERT OR IGNORE``，不依赖
``_ensured_paths`` 进程内缓存也能安全重复调用（进程重启后缓存清零、表已存在
时同样不会重复插入或报错）——``UNIQUE(org_id, key)`` 对 ``org_id IS NULL`` 的
多行不生效（SQLite 里 NULL 两两不相等），不能拿它当去重键。

``ensure_tables_on_connection()`` 是 ``ensure_schema()`` 之外单独留的第二个
入口，专供 ``app.quota_policy.allocation`` 的读路径（``resolve_effective_
limits``/``scope_limits_dict``）调用——那条路径被 ``app.quota`` 一批配额闸门
函数（``check_module_concurrency``/``reserve_video_seconds`` 等）在调用方
**已经持有的** ``BEGIN IMMEDIATE`` 事务里直接调用（这些函数的既有契约就是
如此，见各自 docstring），如果沿用 ``ensure_schema()`` 那样再开一条独立连接
去抢 ``BEGIN IMMEDIATE``，会跟调用方自己持有的写锁抢锁，2 秒
``WRITE_TXN_BUSY_TIMEOUT_S`` 超时后必然失败——实测正是这样炸的
（``test_reserve_video_seconds_rolls_back_with_caller_transaction`` 等一批
用例）。``ensure_tables_on_connection()`` 复用调用方传入的同一个 ``conn``
执行纯 ``CREATE TABLE/INDEX IF NOT EXISTS``（不做种子写入、不开新连接、不
申请新锁），参与调用方现有事务或没有事务时的默认自动提交，不会替调用方提
交任何尚未提交的写入。
"""
from __future__ import annotations

import json
import sqlite3

from app import db
from app import db_schema
from app import monitor_audit_buffer
from app import quota_tiers

_CREATE_STATEMENTS: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS quota_plans (
        id TEXT PRIMARY KEY,
        org_id TEXT,
        key TEXT NOT NULL,
        name TEXT NOT NULL,
        builtin INTEGER NOT NULL DEFAULT 0,
        period_days INTEGER NOT NULL DEFAULT 30,
        limits_json TEXT NOT NULL,
        created_at REAL NOT NULL,
        updated_at REAL,
        created_by TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_quota_plans_org ON quota_plans(org_id)",
    """CREATE TABLE IF NOT EXISTS quota_allocations (
        id TEXT PRIMARY KEY,
        scope_type TEXT NOT NULL,
        scope_id TEXT NOT NULL,
        plan_id TEXT NOT NULL,
        overrides_json TEXT,
        period_started_at REAL,
        expires_at REAL,
        created_at REAL NOT NULL,
        created_by TEXT,
        UNIQUE(scope_type, scope_id),
        FOREIGN KEY(plan_id) REFERENCES quota_plans(id)
    )""",
    """CREATE TABLE IF NOT EXISTS quota_alerts (
        id TEXT PRIMARY KEY,
        scope_type TEXT NOT NULL,
        scope_id TEXT NOT NULL,
        resource TEXT NOT NULL,
        threshold REAL NOT NULL,
        triggered_at REAL NOT NULL,
        period_index INTEGER NOT NULL,
        notified INTEGER NOT NULL DEFAULT 0,
        UNIQUE(scope_type, scope_id, resource, period_index, threshold)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_quota_alerts_scope ON quota_alerts(scope_type, scope_id, period_index)",
    # EP-04 第二阶段：项目目录实测占用的采样结果，append-only（每次巡检插入
    # 新行，不 UPDATE 覆盖旧行）——保留历史是 usage_query 支持 storage_bytes
    # 时间趋势的前提；「最新占用」永远是按 project_id 取 sampled_at 最大的一
    # 行，见 app/quota_policy/storage.py::latest_sample。独立连接写入（诊断类
    # 采样不占用调用方事务/写锁，同 quota_alerts 的既有惯例），见该模块
    # sample_project_storage 的文档。
    """CREATE TABLE IF NOT EXISTS project_storage_samples (
        id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL,
        owner_user_id TEXT,
        bytes_total INTEGER NOT NULL,
        duration_s REAL,
        status TEXT NOT NULL DEFAULT 'ok',
        error TEXT,
        sampled_at REAL NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_project_storage_samples_project "
    "ON project_storage_samples(project_id, sampled_at)",
    "CREATE INDEX IF NOT EXISTS idx_project_storage_samples_owner "
    "ON project_storage_samples(owner_user_id, sampled_at)",
)

#: 每个维度对应 TierLimits 的同名字段；顺序即 limits_json 的字段写入顺序。
#: EP-04 第二阶段追加 storage_bytes/project_concurrency 两维——DIMENSIONS 是
#: plans.py::validate_limits_payload / allocation.py::resolve_effective_limits
#: 唯一驱动源，两处都按这个元组通用遍历（getattr(TierLimits 实例, dim)），加进
#: 这里即自动纳入三级取最紧合并与种子写入，不需要在 allocation.py 里另写分支。
DIMENSIONS: tuple[str, ...] = (
    "projects", "concurrency", "token", "video_seconds", "image",
    "storage_bytes", "project_concurrency",
)

_ensured_paths: set[str] = set()


def ensure_tables_on_connection(conn: sqlite3.Connection) -> None:
    """轻量、同连接、无副作用的建表兜底——不开新连接、不申请新锁、不做种子
    写入，安全用于调用方已持有事务的场景。见模块文档"两个入口"一段。"""
    for statement in _CREATE_STATEMENTS:
        conn.execute(statement)


def ensure_schema() -> None:
    """幂等建表 + 五档内置 plan 种子；按当前 ``db.DB_PATH`` 记忆已建。

    调用方选错入口不再有后果（2026-09-12 原语层修复，见
    ``app.db_schema.ensure_schema_respecting_caller_transaction`` 文档）：
    ``app.db.get_conn()`` 这条线程/任务局部连接若已经处在调用方开的事务里，
    直接改走同连接的 ``ensure_tables_on_connection(那个 conn)``，不开独立
    连接、不抢锁、不做种子写入；否则保持原有独立连接行为
    （``db._run_write_transaction_once``），供 ``app.main`` 启动时、
    ``app.quota_policy.api``/``plans``/``allocation`` 的写操作使用——读路径
    仍然直接调 ``ensure_tables_on_connection``，本函数的分派对它没有影响。"""
    key = str(db.DB_PATH)
    if key in _ensured_paths:
        return

    def _run_independent() -> None:
        def operation(conn: sqlite3.Connection) -> None:
            ensure_tables_on_connection(conn)
            _seed_builtin_plans(conn)

        try:
            db._run_write_transaction_once(operation)
        except Exception as exc:  # noqa: BLE001 建表失败留到下一次调用重试，不阻塞
            # 调用方；不能悄悄吞掉——落一条可观测记录，见 app/orgs/schema.py 同名
            # except 分支的注释，理由完全一致。
            monitor_audit_buffer.note_schema_ensure_failure(__name__, exc)
            return
        _ensured_paths.add(key)

    db_schema.ensure_schema_respecting_caller_transaction(
        db.get_conn(),
        on_caller_connection=ensure_tables_on_connection,
        run_independent=_run_independent,
    )


def _seed_builtin_plans(conn: sqlite3.Connection) -> None:
    ts = db.now()
    # 属性访问 quota_tiers.TIER_TABLE（不是 `from app.quota_tiers import
    # TIER_TABLE`）：种子只在建表这一刻读一次真实数值落库，不是长期持有的
    # 绑定，用哪种 import 写法本无所谓；但全仓统一"读常量一律走属性访问"能让
    # 未来有人复制这段代码去做别的事时，不会不小心引入 app/quota.py 文档里
    # 警告过的"两个独立绑定"陷阱。
    for tier_key, limits in quota_tiers.TIER_TABLE.items():
        plan_id = f"qp_builtin_{tier_key}"
        limits_json = json.dumps(
            {dim: getattr(limits, dim) for dim in DIMENSIONS}, ensure_ascii=False,
        )
        conn.execute(
            "INSERT OR IGNORE INTO quota_plans("
            "id, org_id, key, name, builtin, period_days, limits_json, created_at, created_by) "
            "VALUES(?,NULL,?,?,1,30,?,?,?)",
            (plan_id, tier_key, f"{tier_key} 档（内置）", limits_json, ts, "system"),
        )
