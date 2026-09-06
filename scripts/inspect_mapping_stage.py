"""映射台耗时画像（每轮映射台结束后的检查项，2026-09-06 用户要求）。

对最近成功的映射台运行逐集打印：总时长、供应商忙时（调用时间并集）、等待时长（排队/退避）、
调用次数；再按阶段汇总 p50/p90 延迟与思考 token 中位数。判据：等待占比 > 50% 说明在排队
（槽位/退避），单阶段 p50 > 20s 说明思考档位过高或提示词过长。

用法：python scripts/inspect_mapping_stage.py [--db data/manju.db] [--hours 3] [--limit 8]
在 B 上：ssh mjb "/root/MJAgent2/.venv/bin/python /root/MJAgent2/scripts/inspect_mapping_stage.py --db /root/MJAgent2/data/manju.db"
"""
from __future__ import annotations

import argparse
import collections
import json
import sqlite3
import time


def _profile_runs(conn: sqlite3.Connection, hours: float, limit: int) -> None:
    t0 = time.time() - hours * 3600
    runs = conn.execute(
        "SELECT r.id, r.started_at, r.finished_at, e.episode_no FROM workflow_runs r "
        "JOIN episodes e ON e.id = r.scope_id WHERE r.workflow_type='screenplay' AND r.status='SUCCEEDED' "
        "AND r.started_at > ? ORDER BY r.finished_at DESC LIMIT ?", (t0, limit),
    ).fetchall()
    print(f"== 映射台运行（最近 {hours:g} 小时，{len(runs)} 集）==")
    print(f"{'集':>4} {'总分钟':>7} {'忙分钟':>7} {'等待分钟':>8} {'等待占比':>8} {'调用':>4} {'最大空档s':>9}")
    for run in runs:
        calls = conn.execute(
            "SELECT ts, latency_ms FROM provider_calls WHERE run_id=? ORDER BY ts", (run["id"],)
        ).fetchall()
        total = float(run["finished_at"] - run["started_at"])
        busy, last_end, max_gap = 0.0, float(run["started_at"]), 0.0
        for call in calls:
            start = float(call["ts"]); end = start + float(call["latency_ms"] or 0) / 1000
            if start > last_end:
                max_gap = max(max_gap, start - last_end)
            busy += max(0.0, min(end, float(run["finished_at"])) - max(start, last_end))
            last_end = max(last_end, end)
        idle = total - busy
        flag = "  <-- 排队/退避为主" if total > 0 and idle / total > 0.5 else ""
        print(f"{run['episode_no']:>4} {total/60:7.1f} {busy/60:7.1f} {idle/60:8.1f} {idle/max(total,1):8.0%} {len(calls):4d} {max_gap:9.0f}{flag}")


def _profile_stages(conn: sqlite3.Connection, hours: float) -> None:
    t0 = time.time() - hours * 3600
    latency: dict[str, list[float]] = collections.defaultdict(list)
    reasoning: dict[str, list[int]] = collections.defaultdict(list)
    for row in conn.execute(
        "SELECT latency_ms, meta, response_json FROM provider_calls WHERE ts>? AND status='OK' "
        "AND kind IN ('chat','chat_tools')", (t0,),
    ):
        try:
            meta = json.loads(row["meta"] or "{}")
        except (TypeError, ValueError):
            meta = {}
        stage = str(meta.get("stage") or meta.get("purpose") or meta.get("substage") or meta.get("stage_key") or "?")[:30]
        latency[stage].append(float(row["latency_ms"] or 0) / 1000)
        try:
            usage = (json.loads(row["response_json"] or "{}") or {}).get("usage") or {}
            tokens = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens") or usage.get("reasoning_tokens")
            if tokens is not None:
                reasoning[stage].append(int(tokens))
        except (TypeError, ValueError):
            pass
    print(f"\n== 按阶段（p50 > 20s 标 <--）==\n{'阶段':32} {'次数':>4} {'p50s':>6} {'p90s':>6} {'合计分钟':>8} {'思考token中位':>12}")
    for stage, values in sorted(latency.items(), key=lambda kv: -sum(kv[1]))[:16]:
        ordered = sorted(values)
        p50 = ordered[len(ordered) // 2]; p90 = ordered[max(0, int(len(ordered) * 0.9) - 1)]
        tokens = sorted(reasoning.get(stage) or [])
        median_tokens = tokens[len(tokens) // 2] if tokens else "-"
        flag = "  <--" if p50 > 20 else ""
        print(f"{stage:32} {len(values):4d} {p50:6.1f} {p90:6.1f} {sum(values)/60:8.1f} {str(median_tokens):>12}{flag}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--db", default="data/manju.db")
    parser.add_argument("--hours", type=float, default=3.0)
    parser.add_argument("--limit", type=int, default=8)
    args = parser.parse_args()
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    _profile_runs(conn, args.hours, args.limit)
    _profile_stages(conn, args.hours)


if __name__ == "__main__":
    main()
