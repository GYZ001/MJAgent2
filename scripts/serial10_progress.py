#!/usr/bin/env python3
"""EP1-EP10 串行回归的进度与合理性检查。

`yyft_serial10.py` 只在**失败**时报警，一集跑得异常顺利它不会吭声。第 9 轮
就栽在这里：EP1-EP3 各自只发起 5-6 次模型调用、3-4 分钟就 "ready"，实际是
复用了旧代码产出的 Artifact，整轮回归对这三集完全无效，而日志一片绿。

所以这里除了进度，还检查**工作量是否合理**：真实全量生成一集剧本需要几十次
不同 operation_id 的模型调用（实测 EP4 为 83 次 / 51 分钟）。调用数远低于这个
量级却宣称成功，几乎一定是复用而不是生成。

用法：
    py scripts/serial10_progress.py            # 一次性快照
    py scripts/serial10_progress.py --watch 300  # 每 300 秒打印一次
"""
from __future__ import annotations

import argparse
import sqlite3
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROJECT_ID = "proj_3ac0b627fa46"

# 低于这个不同 operation_id 数就判为"疑似复用而非真实生成"。取值依据：EP4 的
# 一次真实全量生成是 83 个不同 op；第 9 轮那三集复用时只有 5-6 个。中间留足
# 缓冲，避免章节偏短的集数被误报。
SUSPICIOUS_DISTINCT_OPS = 25


def conn() -> sqlite3.Connection:
    c = sqlite3.connect(f"file:{ROOT / 'data' / 'manju.db'}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def snapshot() -> int:
    """打印每集状态与真实工作量；返回可疑集数。"""
    db = conn()
    try:
        rows = db.execute(
            "SELECT id, episode_no, screenplay_status, hook, cliffhanger, "
            "active_screenplay_run_id FROM episodes "
            "WHERE project_id=? AND episode_no<=10 ORDER BY episode_no",
            (PROJECT_ID,),
        ).fetchall()
        print(f"[{time.strftime('%m-%d %H:%M:%S')}] EP1-EP10 剧本进度")
        suspicious = 0
        for row in rows:
            # 取该集最近一次 run，统计它真正发出的不同 operation_id 数。
            run = db.execute(
                "SELECT id, status, started_at, finished_at FROM workflow_runs "
                "WHERE scope_id=? AND workflow_type='screenplay' "
                "ORDER BY started_at DESC LIMIT 1",
                (row["id"],),
            ).fetchone()
            ops = calls = 0
            elapsed = ""
            if run is not None:
                stat = db.execute(
                    "SELECT COUNT(*) n, COUNT(DISTINCT operation_id) d "
                    "FROM provider_calls WHERE run_id=? AND kind='chat'",
                    (run["id"],),
                ).fetchone()
                calls, ops = stat["n"], stat["d"]
                end = run["finished_at"] or time.time()
                if run["started_at"]:
                    elapsed = f"{(end - run['started_at']) / 60:.0f}m"
            state = str(row["screenplay_status"])
            # 只对"宣称成功"的集数做工作量合理性检查——失败/进行中的低调用数正常。
            flag = ""
            if state == "ready" and ops < SUSPICIOUS_DISTINCT_OPS:
                flag = f"  ⚠ 仅 {ops} 个不同 op，疑似复用旧 Artifact 而非真实生成"
                suspicious += 1
            hook = "有" if (row["hook"] or "").strip() else "-"
            cliff = "有" if (row["cliffhanger"] or "").strip() else "-"
            print(
                f"  EP{row['episode_no']:<2} {state:<9} calls={calls:<4} ops={ops:<4} "
                f"{elapsed:<5} hook={hook} cliff={cliff}{flag}"
            )
        return suspicious
    finally:
        db.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--watch", type=int, default=0, help="轮询间隔秒；0 表示只看一次")
    args = parser.parse_args()
    while True:
        suspicious = snapshot()
        if suspicious:
            print(f"  !! {suspicious} 集的工作量不足以支撑'真实生成'，这一轮对它们无效")
        if args.watch <= 0:
            return 1 if suspicious else 0
        time.sleep(args.watch)


if __name__ == "__main__":
    raise SystemExit(main())
