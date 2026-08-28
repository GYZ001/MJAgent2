"""盯住指定的几个 workflow_run 直到它们各自落定，把结论写进 logs/。

用法：py scripts/watch_runs.py run_xxx=标签 run_yyy=标签 [--timeout-min 40]

判据挂在每个 run 自己的终态上，不挂在"库里还有没有别的任务在跑"——兄弟任务的
正常活动不该让这次观察提前收尾或永远收不了尾。
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "manju.db"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("runs", nargs="+", help="run_id=标签")
    parser.add_argument("--timeout-min", type=float, default=40.0)
    parser.add_argument("--interval-s", type=float, default=45.0)
    args = parser.parse_args()

    labels = {}
    for item in args.runs:
        run_id, _, label = item.partition("=")
        labels[run_id] = label or run_id

    deadline = time.time() + args.timeout_min * 60
    done: dict[str, tuple[str, str]] = {}
    while time.time() < deadline and len(done) < len(labels):
        conn = sqlite3.connect(DB)
        conn.row_factory = sqlite3.Row
        parts = []
        for run_id, label in labels.items():
            row = conn.execute(
                "SELECT status, failure_message FROM workflow_runs WHERE id=?", (run_id,),
            ).fetchone()
            status = row["status"] if row else "MISSING"
            parts.append(f"{label}={status}")
            if row and status not in ("RUNNING", "PENDING") and run_id not in done:
                done[run_id] = (status, row["failure_message"] or "")
        calls = conn.execute(
            "SELECT COUNT(*) c FROM provider_calls WHERE ts>?", (time.time() - 120,),
        ).fetchone()["c"]
        conn.close()
        print(
            f"{time.strftime('%H:%M:%S')} " + " | ".join(parts) + f" | 近2分模型调用={calls}",
            flush=True,
        )
        if len(done) == len(labels):
            break
        time.sleep(args.interval_s)

    print("\n=== 结果 ===", flush=True)
    for run_id, label in labels.items():
        if run_id not in done:
            print(f"{label}: 仍未落定（观察超时）", flush=True)
            continue
        status, message = done[run_id]
        print(f"{label}: {status}", flush=True)
        if message:
            print("   " + str(message)[:1200], flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
