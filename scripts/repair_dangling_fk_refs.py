#!/usr/bin/env python3
"""把库里的悬挂外键引用按**建表语句自己声明的语义**修回去。

为什么会有悬挂引用：SQLite 的 ``PRAGMA foreign_keys`` 是**每连接**开关，默认关。
``app/db.py`` 的 ``get_conn()`` 开了它，所以走产品路径的删除会正常触发
``ON DELETE SET NULL`` / ``CASCADE``；但 ``scripts/`` 里直接删库的脚本
（``reset_project_episodes.py``、``reset_pipeline_data.py`` 等）走裸
``sqlite3.connect``，**没开这个 pragma，级联动作一次都没触发过**，父行删掉、子行
的引用原样留着。独立复现（/tmp 临时库，两次同样的删除）：

    pragma=True   删父行后 child.pid=None   foreign_key_check 违规=0
    pragma=False  删父行后 child.pid='p1'   foreign_key_check 违规=1

后果不是「查询偶尔查到空」，而是**每晚的数据库备份全部作废**：
``scripts/backup_manju_db.py`` 拿 ``PRAGMA foreign_key_check`` 当验证判据，一条不过
就把整份备份挪进隔离区。2026-09-10 在计算服务器 B 上实测 7332 条违规、最后一份
可用备份停在 2026-09-05——连续五晚没有备份，而没人看得见。

修复动作**从 schema 推导，不列表名白名单**，三条规则按顺序取第一条命中的：

1. ``on_delete=CASCADE``：声明本来就是「父行没了就删子行」，删。
2. 该列 ``NOT NULL``：父行已经不在、列又不可空，这一行在当前 schema 下**不可表示**，
   删。``gate_decisions.artifact_id`` 是这一类（``NOT NULL`` + 默认 ``NO ACTION``：
   带 pragma 时这次删除本该被拒绝，事实是它没被拒绝，已经发生了）。
3. 其余（含 ``SET NULL``）：把该列置 NULL，保留行本身。

判据只看 ``PRAGMA foreign_key_check`` 报出来的东西，因此**这个脚本对任何表都成立**，
将来加表不用改它。修完重跑一次 ``foreign_key_check`` 并断言归零——修完不复核，等于
把「可能没修干净」伪装成「修好了」。

默认 dry-run，只有显式 ``--apply`` 才写库。写库前请自己确认有可回滚的快照
（B 上是 ``/var/backups/mjagent2/db/quarantine/manju-<日期>.db.failed``，隔离的是
校验没过的副本，文件本身是完整的库）。

用法：
    py scripts/repair_dangling_fk_refs.py                    # 预览（本机 data/manju.db）
    py scripts/repair_dangling_fk_refs.py --db /path/x.db    # 预览指定库
    py scripts/repair_dangling_fk_refs.py --apply            # 真的修
"""
from __future__ import annotations

import argparse
import collections
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app import config  # noqa: E402

#: 一条修复计划：对某张表的某个外键，用哪个动作、影响哪些 rowid。
Plan = collections.namedtuple("Plan", "table column parent action rowids")


def _fk_meta(conn: sqlite3.Connection, table: str) -> dict[int, tuple[str, str, str]]:
    """``fk_id -> (子列名, 父表名, on_delete)``。"""
    return {
        row[0]: (row[3], row[2], (row[6] or "NO ACTION").upper())
        for row in conn.execute(f"PRAGMA foreign_key_list({table})")
    }


def _not_null_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})") if row[3]}


def _decide(on_delete: str, column: str, not_null: set[str]) -> str:
    """三条规则按顺序取第一条命中的（见模块 docstring）。"""
    if on_delete == "CASCADE":
        return "delete"
    if column in not_null:
        return "delete"
    return "set_null"


def build_plans(conn: sqlite3.Connection) -> tuple[list[Plan], list[str]]:
    """把 ``foreign_key_check`` 的逐行报告聚成按 (表, 外键) 分组的修复计划。

    返回 ``(计划, 拒绝理由)``。``rowid`` 为空（WITHOUT ROWID 表）时不猜行标识，
    直接记一条拒绝理由——猜错就是删错行。
    """
    violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    grouped: dict[tuple[str, int], list[int]] = collections.defaultdict(list)
    refusals: list[str] = []
    for table, rowid, _parent, fk_id in violations:
        if rowid is None:
            refusals.append(f"{table}: foreign_key_check 没给 rowid（WITHOUT ROWID 表），不处理")
            continue
        grouped[(table, fk_id)].append(int(rowid))

    plans: list[Plan] = []
    for (table, fk_id), rowids in sorted(grouped.items()):
        meta = _fk_meta(conn, table).get(fk_id)
        if meta is None:
            refusals.append(f"{table}: 外键 #{fk_id} 在 foreign_key_list 里找不到，不处理")
            continue
        column, parent, on_delete = meta
        action = _decide(on_delete, column, _not_null_columns(conn, table))
        plans.append(Plan(table, column, parent, action, sorted(set(rowids))))
    return plans, sorted(set(refusals))


def describe(plans: list[Plan], refusals: list[str]) -> None:
    total = sum(len(p.rowids) for p in plans)
    print(f"悬挂引用共 {total} 条，分 {len(plans)} 组：")
    for p in plans:
        verb = "删除整行" if p.action == "delete" else f"把 {p.column} 置 NULL"
        print(f"  {len(p.rowids):6d}  {p.table}.{p.column} -> {p.parent}   {verb}")
    for reason in refusals:
        print(f"  [拒绝] {reason}")


def apply_plans(conn: sqlite3.Connection, plans: list[Plan]) -> None:
    """在一个事务里执行全部计划。删除优先于置空——同一行可能同时踩两个外键。"""
    doomed: dict[str, set[int]] = collections.defaultdict(set)
    for p in plans:
        if p.action == "delete":
            doomed[p.table].update(p.rowids)
    conn.execute("BEGIN IMMEDIATE")
    for table, rowids in sorted(doomed.items()):
        _execute_in_chunks(conn, f"DELETE FROM {table} WHERE rowid IN ", sorted(rowids))
    for p in plans:
        if p.action != "set_null":
            continue
        rowids = [r for r in p.rowids if r not in doomed.get(p.table, ())]
        _execute_in_chunks(conn, f"UPDATE {p.table} SET {p.column}=NULL WHERE rowid IN ", rowids)
    conn.commit()


def _execute_in_chunks(conn: sqlite3.Connection, prefix: str, rowids: list[int], size: int = 500) -> None:
    """SQLITE_MAX_VARIABLE_NUMBER 默认 999，分批发，不拼字面量。"""
    for start in range(0, len(rowids), size):
        chunk = rowids[start:start + size]
        conn.execute(prefix + "(" + ",".join("?" * len(chunk)) + ")", chunk)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=str(config.DB_PATH), help="数据库路径，缺省取 app.config.DB_PATH")
    parser.add_argument("--apply", action="store_true", help="真的写库；不给就是只预览")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db, timeout=60)
    # 修复期间必须关着：开着的话下面的 DELETE 会再触发一轮级联，把「修哪些行」
    # 从计划里悄悄变成计划外的行。修完用 foreign_key_check 复核，不靠 pragma。
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        plans, refusals = build_plans(conn)
        describe(plans, refusals)
        if not plans:
            print("没有可修的悬挂引用。")
            return 1 if refusals else 0
        if not args.apply:
            print("\n--dry-run（默认）：以上都没有执行。确认无误后加 --apply。")
            return 0
        apply_plans(conn, plans)
        remaining = conn.execute("PRAGMA foreign_key_check").fetchall()
        print(f"\n修复完成，复核 foreign_key_check：剩余 {len(remaining)} 条违规。")
        if remaining or refusals:
            print("仍有违规或被拒绝项，备份校验还会失败——请逐条排查后再跑一次。")
            return 1
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
