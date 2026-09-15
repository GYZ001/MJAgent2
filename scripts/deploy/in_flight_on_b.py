"""部署前的在途检查（在 B 上跑，只读）：B 有正在跑的运行或任务就拒绝重启。

2026-09-15 16:31 CST 事故：用户的映射包生成跑到一半，另一会话在 A 上手动跑部署脚本
重启了 B，运行被标成 SERVICE_RESTART、自动续跑从头再来；加上此前供应商 450 秒没返回
一个字节的停顿，用户空等了近二十分钟、看着链路页显示「0ms」。重启只有在 B 空闲时
才是无损的，所以这一步放在部署脚本推代码之前。

判据与配额模块同源（app.quota_scope）：workflow_runs 状态在 CREATED/RUNNING 算在途，
jobs 状态在 ACTIVE_JOB_STATUSES 算在途。

用法（部署脚本通过 stdin 把本文件送到 B 上执行，不依赖 B 的检出里有没有它）：
    ssh mjb '/root/MJAgent2/.venv/bin/python -' < scripts/deploy/in_flight_on_b.py
输出一行：``IDLE`` 或 ``IN_FLIGHT runs=<n> jobs=<m> concats=<k>``；退出码 0 空闲、1 有在途、2 检查本身失败。
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time

REPO = os.environ.get("MJAGENT2_REPO") or "/root/MJAgent2"
os.chdir(REPO)
sys.path.insert(0, REPO)


def main() -> int:
    from app.quota_scope import ACTIVE_JOB_STATUSES

    db_path = os.path.join(REPO, "data", "manju.db")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    try:
        runs = int(conn.execute(
            "SELECT COUNT(*) FROM workflow_runs WHERE status IN ('CREATED','RUNNING')"
        ).fetchone()[0] or 0)
        placeholders = ",".join("?" for _ in ACTIVE_JOB_STATUSES)
        jobs = int(conn.execute(
            f"SELECT COUNT(*) FROM jobs WHERE status IN ({placeholders})", ACTIVE_JOB_STATUSES,
        ).fetchone()[0] or 0)
        try:  # 整集合成（concat_operation_receipts 租约未过期）也是在途：2026-09-15 部署重启掐断过用户的合成
            concats = int(conn.execute(
                "SELECT COUNT(*) FROM concat_operation_receipts WHERE status='running' AND lease_expires_at > ?",
                (time.time(),),
            ).fetchone()[0] or 0)
        except sqlite3.OperationalError:
            concats = 0
    finally:
        conn.close()
    if runs or jobs or concats:
        print(f"IN_FLIGHT runs={runs} jobs={jobs} concats={concats}")
        return 1
    print("IDLE")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 —— 检查本身失败也要有独立退出码，调用方按「未知」处理
        print(f"CHECK_FAILED {type(exc).__name__}: {exc}")
        sys.exit(2)
