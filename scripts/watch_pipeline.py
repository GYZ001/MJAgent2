#!/usr/bin/env python3
"""盯本次跑的全链路进度：项目状态、各环节调用汇总、失败明细。

和 ``watch_bible_run.py`` 的区别是这份只看**本次**运行：起点取当前
``workflow_runs`` 里最早的一条 RUNNING，没有就取最近一条。历史 provider_calls
是模型画像的依据、不该清，但汇总时混进来会让人把上一轮的战绩当成这一轮的。

用法：
    py scripts/watch_pipeline.py <project_id> [--since <unix_ts>]
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app import config  # noqa: E402

import sqlite3  # noqa: E402


def _fmt_age(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("project_id")
    parser.add_argument("--since", type=float, default=None)
    parser.add_argument("--failures", type=int, default=5, help="展开几条失败明细")
    args = parser.parse_args()

    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row

    since = args.since
    if since is None:
        row = conn.execute(
            "SELECT MIN(started_at) FROM workflow_runs WHERE status='RUNNING'"
        ).fetchone()
        since = row[0] if row and row[0] else None
    if since is None:
        row = conn.execute(
            "SELECT started_at FROM workflow_runs ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        since = row[0] if row else time.time()

    now = time.time()
    print(f"起点 since={since:.0f}（{_fmt_age(now - since)} 前）")

    project = conn.execute(
        "SELECT * FROM projects WHERE id=?", (args.project_id,)
    ).fetchone()
    if project is None:
        print(f"project not found: {args.project_id}")
        return 1
    fields = (
        "bible_status",
        "bible_error",
        "bible_version",
        "refs_status",
        "refs_error",
        "portraits_status",
        "scene_refs_status",
        "scene_refs_error",
    )
    print("项目:", {k: project[k] for k in fields})

    counts = conn.execute(
        """SELECT COUNT(*) total,
                  SUM(CASE WHEN status='SUCCEEDED' THEN 1 ELSE 0 END) ok,
                  SUM(CASE WHEN status='FAILED' THEN 1 ELSE 0 END) failed,
                  SUM(CASE WHEN status='RUNNING' THEN 1 ELSE 0 END) running
           FROM workflow_runs WHERE started_at >= ?""",
        (since,),
    ).fetchone()
    print(
        f"工作流: 共{counts['total']} 成功{counts['ok'] or 0} "
        f"失败{counts['failed'] or 0} 运行中{counts['running'] or 0}"
    )

    print()
    print("--- 本次调用汇总（阶段 / 状态 / 次数）---")
    # 阶段名落在 meta 这个 JSON 列里，没有独立字段；stage 与 stage_key 两种键
    # 都在用，取不到就退回 kind。
    phase_expr = (
        "COALESCE("
        "NULLIF(json_extract(meta,'$.stage'),''),"
        "NULLIF(json_extract(meta,'$.stage_key'),''),"
        "kind)"
    )
    rows = conn.execute(
        f"""SELECT {phase_expr} AS phase, status, COUNT(*) c
            FROM provider_calls WHERE ts >= ?
            GROUP BY phase, status ORDER BY c DESC""",
        (since,),
    ).fetchall()
    if not rows:
        print("  （本次尚无调用）")
    for r in rows:
        print(f"  {r['c']:5d}  {r['status'] or '':10s} {r['phase'] or ''}")

    fails = conn.execute(
        f"""SELECT {phase_expr} AS phase, model, error, ts
            FROM provider_calls
            WHERE ts >= ? AND status='FAILED'
            ORDER BY ts DESC LIMIT ?""",
        (since, args.failures),
    ).fetchall()
    if fails:
        print()
        print("--- 最近失败 ---")
        for r in fails:
            err = (r["error"] or "")[:200].replace("\n", " ")
            print(f"  [{_fmt_age(now - r['ts'])}前] {r['phase']} / {r['model']}")
            print(f"      {err}")

    run_fail = conn.execute(
        """SELECT id, workflow_type, status, failure_code, failure_message
           FROM workflow_runs
           WHERE started_at >= ? AND status NOT IN ('SUCCEEDED','RUNNING')
           ORDER BY started_at DESC LIMIT ?""",
        (since, args.failures),
    ).fetchall()
    if run_fail:
        print()
        print("--- 未成功的工作流 ---")
        for r in run_fail:
            msg = (r["failure_message"] or "")[:200].replace("\n", " ")
            print(
                f"  {r['id']} {r['workflow_type']} {r['status']} "
                f"{r['failure_code'] or ''}: {msg}"
            )

    print()
    print("--- 产出 ---")
    for table, label in (
        ("character_portraits", "定妆照"),
        ("scene_references", "场景图"),
        ("artifacts", "制品"),
    ):
        n = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        print(f"  {label}: {n}")
    eps = conn.execute(
        """SELECT screenplay_status, COUNT(*) c FROM episodes
           WHERE screenplay_status IS NOT NULL AND screenplay_status!='pending'
           GROUP BY screenplay_status"""
    ).fetchall()
    if eps:
        print("  映射包:", {r["screenplay_status"]: r["c"] for r in eps})
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
