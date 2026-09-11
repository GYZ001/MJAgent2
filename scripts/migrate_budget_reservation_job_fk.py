#!/usr/bin/env python3
"""把审计台账指向流水线产物的外键改成可空 + ``ON DELETE SET NULL``。

涉及两张表，同一个毛病、同一种修法：

* ``budget_reservations.job_id``   —— 建表写的是 ``NOT NULL ... ON DELETE CASCADE``
* ``gate_decisions.artifact_id``   —— 建表写的是 ``NOT NULL`` + 默认 ``NO ACTION``

两张都是台账：``app/media_exec/enqueue.py`` 明写「budget_reservations 审计台账完整，
不参与任何放行判断」，``scripts/reset_project_episodes.py`` 也刻意把同族的付款责任与
闸门决议排除在清除清单之外。但建表语句说的是另一回事（「预留跟着 job 一起死」／
「产物删不掉」），两条规矩直接打架。因为删库脚本的裸连接默认关着
``PRAGMA foreign_keys``，级联一次都没触发过，脚本那侧的意图悄悄赢了——代价是 7332
条悬挂引用把每晚的数据库备份全部打进隔离区（见
``scripts/repair_dangling_fk_refs.py`` 模块 docstring）。

用户 2026-09-10 拍板走「台账独立于产物存在」这一侧：**一行审计都不删**，把 schema
改成能表达这件事的形状。改完之后父行被删时 SET NULL 触发，台账行留下、只是不再指向
一个已经不存在的对象；``foreign_key_check`` 随之归零，备份恢复。

置空不会削弱 ``idx_gate_decisions_storyboard_pack_release``（「一个产物只能有一条
发布决议」）：那条唯一索引建在 ``artifact_id`` 上，SQLite 里 NULL 不与任何值相等，
被置空的孤儿行不可能和任何**存活**产物的行冲突。

``app/db.py`` 的 ``SCHEMA`` 已同步改成新形状，所以**新建的库天生正确**；本脚本只负责
把既有库迁过去。分成两件事而不是塞进 ``init_db``：SQLite 改不了列的可空性，必须整表
重建，而重建要求在事务外关掉 ``foreign_keys``，与 ``init_db`` 里既有的事务/pragma
上下文冲突；一次性历史迁移做成显式脚本，比让它每次启动都试一遍更诚实。

幂等：已经是新形状的表直接跳过。默认 dry-run，``--apply`` 才动手。重建后逐项复核
（行数、形状），有一项不符就回滚整个事务。

用法：
    py scripts/migrate_budget_reservation_job_fk.py                 # 预览
    py scripts/migrate_budget_reservation_job_fk.py --apply         # 执行
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app import config  # noqa: E402


@dataclass(frozen=True)
class LedgerTable:
    """一张待迁移的台账表。``new_body`` 与 app/db.py 的 SCHEMA 必须逐字一致——
    两份声明分叉时，新建库与迁移过的库会长出两种表，而分叉的那一种迟早成为
    「只在某些环境复现」的故障。tests/test_budget_reservation_job_fk.py 逐字比对。"""

    name: str
    column: str
    columns: str
    new_body: str


TABLES: tuple[LedgerTable, ...] = (
    LedgerTable(
        name="budget_reservations",
        column="job_id",
        columns="id, job_id, scope_type, scope_id, amount_cny, status, created_at, settled_at, actual_cost_cny",
        new_body="""
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
""",
    ),
    LedgerTable(
        name="gate_decisions",
        column="artifact_id",
        columns="id, artifact_id, run_id, gate_key, decision, decided_by, reason, accepted_risk, created_at",
        new_body="""
    id TEXT PRIMARY KEY,
    artifact_id TEXT,
    run_id TEXT,
    gate_key TEXT NOT NULL,
    decision TEXT NOT NULL,
    decided_by TEXT NOT NULL,
    reason TEXT NOT NULL,
    accepted_risk TEXT,
    created_at REAL NOT NULL,
    FOREIGN KEY(artifact_id) REFERENCES artifacts(id) ON DELETE SET NULL,
    FOREIGN KEY(run_id) REFERENCES workflow_runs(id)
""",
    ),
)


def fk_shape(conn: sqlite3.Connection, table: LedgerTable) -> tuple[bool, str]:
    """返回 ``(该列是否 NOT NULL, 该列外键的 on_delete)``。"""
    not_null = any(
        row[1] == table.column and row[3] for row in conn.execute(f"PRAGMA table_info({table.name})")
    )
    on_delete = next(
        ((row[6] or "NO ACTION").upper() for row in conn.execute(f"PRAGMA foreign_key_list({table.name})")
         if row[3] == table.column),
        "",
    )
    return not_null, on_delete


def already_migrated(conn: sqlite3.Connection, table: LedgerTable) -> bool:
    not_null, on_delete = fk_shape(conn, table)
    return (not not_null) and on_delete == "SET NULL"


def _indexes_of(conn: sqlite3.Connection, table: LedgerTable) -> list[str]:
    """表自带的索引 DDL。重建会把它们一起删掉，必须原样重建回去。"""
    return [
        row[0] for row in conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL",
            (table.name,),
        )
    ]


def migrate(conn: sqlite3.Connection, table: LedgerTable) -> int:
    """整表重建，返回搬过去的行数。调用方负责在事务外关好 ``foreign_keys``。"""
    before = conn.execute(f"SELECT COUNT(*) FROM {table.name}").fetchone()[0]
    indexes = _indexes_of(conn, table)
    tmp = f"{table.name}_migrating"
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(f"CREATE TABLE {tmp} ({table.new_body})")
        conn.execute(f"INSERT INTO {tmp} ({table.columns}) SELECT {table.columns} FROM {table.name}")
        conn.execute(f"DROP TABLE {table.name}")
        conn.execute(f"ALTER TABLE {tmp} RENAME TO {table.name}")
        for ddl in indexes:
            conn.execute(ddl)
        after = conn.execute(f"SELECT COUNT(*) FROM {table.name}").fetchone()[0]
        if after != before:
            raise RuntimeError(f"{table.name} 重建前后行数不一致：{before} -> {after}")
        if not already_migrated(conn, table):
            raise RuntimeError(f"{table.name} 重建后的形状仍不是「可空 + SET NULL」")
        if len(_indexes_of(conn, table)) != len(indexes):
            raise RuntimeError(f"{table.name} 重建后索引数量对不上：{len(indexes)}")
        conn.commit()
    except Exception:
        conn.rollback()  # 回滚是异常处理器的第一条语句，排在任何日志之前
        raise
    return before


def _report(conn: sqlite3.Connection, table: LedgerTable) -> bool:
    """打印一张表的现状，返回是否还需要迁移。"""
    not_null, on_delete = fk_shape(conn, table)
    print(f"== {table.name}.{table.column}：NOT NULL={not_null}，on_delete={on_delete or '(无外键)'}")
    if already_migrated(conn, table):
        print("   已经是「可空 + SET NULL」，跳过。")
        return False
    rows = conn.execute(f"SELECT COUNT(*) FROM {table.name}").fetchone()[0]
    dangling = conn.execute(
        f"SELECT COUNT(*) FROM {table.name} t WHERE t.{table.column} IS NOT NULL AND NOT EXISTS "
        f"(SELECT 1 FROM {_parent_of(conn, table)} p WHERE p.id=t.{table.column})"
    ).fetchone()[0]
    print(f"   待迁移 {rows} 行（其中 {dangling} 行的父行已不存在）")
    return True


def _parent_of(conn: sqlite3.Connection, table: LedgerTable) -> str:
    return next(
        row[2] for row in conn.execute(f"PRAGMA foreign_key_list({table.name})") if row[3] == table.column
    )


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
        pending = [t for t in TABLES if _report(conn, t)]
        if not pending:
            print("\n两张台账表都已是新形状，无需迁移。")
            return 0
        print("\n迁移只改表结构、不动任何一行数据；悬挂引用改完后用 "
              "scripts/repair_dangling_fk_refs.py 置空（那时规则会落到 set_null，不再删行）。")
        if not args.apply:
            print("--dry-run（默认）：没有执行。确认无误后加 --apply。")
            return 0
        for table in pending:
            moved = migrate(conn, table)
            print(f"   {table.name}：{moved} 行原样搬入新表，形状已是「可空 + SET NULL」。")
        print("\n迁移完成。")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
