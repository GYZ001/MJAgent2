#!/usr/bin/env python3
"""统计「本轮整改是否只动了测试项目 EP1-EP10」的越界快照。

用法:
    py scripts/scope_snapshot.py                 # 打印快照
    py scripts/scope_snapshot.py --save logs/x.json
    py scripts/scope_snapshot.py --compare logs/scope_snapshot_before.json

只读打开数据库，不做任何写入。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from scripts.yyft_serial10 import EPISODES  # noqa: E402

SCOPE = {ep_id for _, ep_id in EPISODES}


def snapshot() -> dict[str, int]:
    conn = sqlite3.connect(f"file:{ROOT / 'data' / 'manju.db'}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        marks = ",".join("?" * len(SCOPE))
        scope = sorted(SCOPE)
        one = lambda sql, args=(): conn.execute(sql, args).fetchone()[0]  # noqa: E731
        return {
            "projects": one("SELECT COUNT(*) FROM projects"),
            "episodes_total": one("SELECT COUNT(*) FROM episodes"),
            "episodes_with_screenplay_outside_scope": one(
                f"SELECT COUNT(*) FROM episodes WHERE screenplay_json IS NOT NULL"
                f" AND TRIM(COALESCE(screenplay_json,''))<>'' AND id NOT IN ({marks})",
                scope,
            ),
            "ready_outside_scope": one(
                f"SELECT COUNT(*) FROM episodes WHERE screenplay_status='ready'"
                f" AND id NOT IN ({marks})",
                scope,
            ),
            "shots_outside_scope": one(
                f"SELECT COUNT(*) FROM shots WHERE episode_id NOT IN ({marks})", scope
            ),
            "artifacts_total": one("SELECT COUNT(*) FROM artifacts"),
            "chapters": one("SELECT COUNT(*) FROM chapters"),
        }
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--save")
    ap.add_argument("--compare")
    args = ap.parse_args()
    snap = snapshot()
    print(json.dumps(snap, indent=2, ensure_ascii=False))
    if args.save:
        Path(args.save).write_text(
            json.dumps(snap, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        print(f"saved -> {args.save}")
    if args.compare:
        before = json.loads(Path(args.compare).read_text(encoding="utf-8"))
        # artifacts_total 会随本项目 EP1-EP10 的生成增长，不是越界指标。
        watched = [
            "projects",
            "episodes_total",
            "episodes_with_screenplay_outside_scope",
            "ready_outside_scope",
            "shots_outside_scope",
            "chapters",
        ]
        drift = {k: (before.get(k), snap[k]) for k in watched if before.get(k) != snap[k]}
        if drift:
            print(f"outside-scope changed: {json.dumps(drift, ensure_ascii=False)}")
            return 1
        print("outside-scope changed: NONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
