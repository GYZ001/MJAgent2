#!/usr/bin/env python3
"""把 ``budget_reservations.job_id`` 改成可空 + ``ON DELETE SET NULL``。

为什么要改：这张表是**审计台账**——``app/media_exec/enqueue.py`` 明写「budget_
reservations 审计台账完整，不参与任何放行判断」，``scripts/reset_project_episodes.py``
也刻意把同族的付款责任排除在清除清单之外（「认领是 settled 的那笔钱真的花掉了，
删掉等于篡改账」）。但建表语句写的是 ``job_id TEXT NOT NULL ... ON DELETE
CASCADE``——「预留跟着 job 一起死」。两条规矩直接打架，而因为 ``PRAGMA
foreign_keys`` 在删库脚本的裸连接上默认是关的，级联从没触发过，于是脚本那条
「台账要留着」的意图悄悄赢了，代价是 2118 条悬挂引用把每晚的数据库备份
全部打进隔离区（见 scripts/repair_dangling_fk_refs.py 模块 docstring）。

用户 2026-09-10 拍板走「台账独立于 job 存在」这一侧：**一行审计都不删**，把 schema
改成能表达这件事的形状。改完之后 job 被删时 SET NULL 触发，台账行留下、只是不再
指向一个已经不存在的 job；``foreign_key_check`` 随之归零，备份恢复。

``app/db.py`` 的 ``SCHEMA`` 已同步改成新形状，所以**新建的库天生正确**；本脚本只
负责把既有库迁过去。分成两件事而不是塞进 ``init_db``：SQLite 改不了列的可空性，
必须整表重建，而重建要求在事务外关掉 ``foreign_keys``，与 ``init_db`` 里既有的
事务/pragma 上下文冲突；一次性历史迁移做成显式脚本，比让它每次启动都试一遍更诚实。

幂等：已经是新形状就直接返回。默认 dry-run，``--apply`` 才动手。重建后逐项复核
（行数、``foreign_key_check``、新 schema 形状），有一项不符就回滚整个事务。

用法：
    py scripts/migrate_budget_reservation_job_fk.py                 # 预览
    py scripts/migrate_budget_reservation_job_fk.py --apply         # 执行
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app import config  # noqa: E402

TABLE = "budget_reservations"

#: 目标形状。与 app/db.py 的 SCHEMA 必须逐字一致——两份声明分叉时，新建库与迁移
#: 过的库会长出两种表，而分叉的那一种迟早成为「只在某些环境复现」的故障。
#: tests/test_budget_reservation_job_fk.py 逐字比对这两处。
NEW_TABLE_SQL = """CREATE TABLE budget_reservations_new (
    id TEXT PRIMARY KEY,
    job_id TEXT UNIQUE,
    scope_type TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    amount_cny REAL NOT NULL,
    status TEXT NOT NULL,
    created_at REAL NOT NULL,
    settled_at REAL,
    actual_cost_cny REAL,
    FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE SET NULL
)"""

_COLUMNS = "id, job_id, scope_type, scope_id, amount_cny, status, created_at, settled_at, actual_cost_cny"


def job_fk_shape(conn: sqlite3.Connection) -> tuple[bool, str]:
    """返回 ``(job_id 是否 NOT NULL, job_id 外键的 on_delete)``。"""
    not_null = any(
        row[1] == "job_id" and row[3] for row in conn.execute(f"PRAGMA table_info({TABLE})")
    )
    on_delete = next(
        ((row[6] or "NO ACTION").upper() for row in conn.execute(f"PRAGMA foreign_key_list({TABLE})")
         if row[3] == "job_id"),
        "",
    )
    return not_null, on_delete


def already_migrated(conn: sqlite3.Connection) -> bool:
    not_null, on_delete = job_fk_shape(conn)
    return (not not_null) and on_delete == "SET NULL"


def migrate(conn: sqlite3.Connection) -> int:
    """整表重建，返回搬过去的行数。调用方负责在事务外关好 ``foreign_keys``。"""
    before = conn.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0]
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(NEW_TABLE_SQL)
        conn.execute(f"INSERT INTO budget_reservations_new ({_COLUMNS}) SELECT {_COLUMNS} FROM {TABLE}")
        conn.execute(f"DROP TABLE {TABLE}")
        conn.execute(f"ALTER TABLE budget_reservations_new RENAME TO {TABLE}")
        after = conn.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0]
        if after != before:
            raise RuntimeError(f"重建前后行数不一致：{before} -> {after}")
        if not already_migrated(conn):
            raise RuntimeError("重建后的表形状仍不是「可空 + SET NULL」")
        conn.commit()
    except Exception:
        conn.rollback()  # 回滚是异常处理器的第一条语句，排在任何日志之前
        raise
    return before


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=str(config.DB_PATH))
    parser.add_argument("--apply", action="store_true", help="真的写库；不给就是只预览")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db, timeout=60)
    # 整表重建必须在 foreign_keys 关闭且事务之外设置——开着的话 DROP TABLE 会让
    # 引用方的外键跟着改名走，SQLite 官方 12 步 ALTER 流程就是这么要求的。
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        not_null, on_delete = job_fk_shape(conn)
        print(f"当前形状：job_id NOT NULL={not_null}，on_delete={on_delete or '(无外键)'}")
        if already_migrated(conn):
            print("已经是「可空 + SET NULL」，无需迁移。")
            return 0
        rows = conn.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0]
        dangling = conn.execute(
            f"SELECT COUNT(*) FROM {TABLE} b WHERE b.job_id IS NOT NULL "
            "AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.id=b.job_id)"
        ).fetchone()[0]
        print(f"待迁移：{rows} 行（其中 {dangling} 行的 job 已不存在）")
        print("迁移只改表结构、不动任何一行数据；悬挂引用改完后用 "
              "scripts/repair_dangling_fk_refs.py 置空（那时规则会落到 set_null，不再删行）。")
        if not args.apply:
            print("\n--dry-run（默认）：没有执行。确认无误后加 --apply。")
            return 0
        moved = migrate(conn)
        print(f"\n迁移完成：{moved} 行原样搬入新表，形状已是「可空 + SET NULL」。")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
