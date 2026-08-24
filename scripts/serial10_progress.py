#!/usr/bin/env python3
"""EP1-EP10 串行回归的进度与合理性检查。

`yyft_serial10.py` 只在**失败**时报警，一集跑得异常顺利它不会吭声。第 9 轮
就栽在这里：EP1-EP3 各自只发起 5-6 次模型调用、3-4 分钟就 "ready"，实际是
复用了旧代码产出的 Artifact，整轮回归对这三集完全无效，而日志一片绿。

所以这里除了进度，还检查**工作量是否合理**。

【2026-08-24 重标定】旧判据是"不同 operation_id 数低于 SUSPICIOUS_DISTINCT_OPS=25
就疑似复用"，取值依据是重型「蓝图→场次分片→编译→修复回路」管线的实测基线
（EP4 83 op / 51 分钟）。剧本台已改造为轻量 episode_prep_pack 流程（契约
6.0.0），EP1 实测单集 80-263s、仅 2 次 chat 调用 / 2 个 distinct operation_id
就是合法的完整生成；含新角色发现的集（身份判定 + 定妆照/场景参考图生成）op 数
会明显更高，且没有一个下限能同时圈住这两种合法形态——继续用 25 当门槛只会
全是误报。

真正的判据改回本文件已经实现过的"产物新建时间"逻辑，且不再受 op 数门槛管
控——只要状态是 ready 就检查：本轮 run 期间没有新建的已发布 artifact 却报
ready，几乎一定是端出旧货；op/calls 数继续展示，纯信息，不再驱动任何报警。

用法：
    py scripts/serial10_progress.py            # 一次性快照
    py scripts/serial10_progress.py --watch 300  # 每 300 秒打印一次
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROJECT_ID = "proj_3ac0b627fa46"


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
            "published_screenplay_artifact_id, "
            "active_screenplay_run_id FROM episodes "
            "WHERE project_id=? AND episode_no<=10 ORDER BY episode_no",
            (PROJECT_ID,),
        ).fetchall()
        print(f"[{time.strftime('%m-%d %H:%M:%S')}] EP1-EP10 剧本进度")
        suspicious = 0
        for row in rows:
            # 取该集最近一次 run，统计它真正发出的不同 operation_id 数（纯信息，
            # 不再驱动报警——见模块 docstring 的 2026-08-24 重标定说明）。
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
            # 主判据：产物新建时间。对每个"宣称成功"的集数都查——不再用 op 数
            # 网关它（含新角色发现的集 op 数可能超过旧的 25 阈值，用旧逻辑反而
            # 会被跳过检查）。找不到 run/artifact 时保守按"非新建"处理，
            # fail closed。
            flag = ""
            prep_info = ""
            if state == "ready":
                art = None
                if row["published_screenplay_artifact_id"]:
                    art = db.execute(
                        "SELECT created_at, content_json FROM artifacts WHERE id=?",
                        (row["published_screenplay_artifact_id"],),
                    ).fetchone()
                fresh = bool(
                    art is not None and run is not None and run["started_at"]
                    and art["created_at"] >= run["started_at"]
                )
                if art is not None:
                    try:
                        content = json.loads(art["content_json"] or "{}")
                    except json.JSONDecodeError:
                        content = {}
                    ledger = content.get("coverage_ledger") or {}
                    # 覆盖段显示的是硬门禁真正认定的覆盖范围——四账并集
                    # （delivered ∪ retained_as_context ∪ merged ∪
                    # proven_duplicates 去重计数），不是单看 delivered。只显示
                    # delivered 会让满覆盖（uncovered 为空）的集看起来像"没
                    # 覆盖完"，而 uncovered 才是门禁唯一的阻断条件（见
                    # app/validators.py:assert_prep_pack_coverage_complete）。
                    # merged/proven_duplicates 在当前记账设计下恒为空
                    # （app/validators.py:build_prep_pack_span_ledger 的
                    # docstring），但仍按通用并集公式计算，不硬编码假设。
                    delivered_list = ledger.get("delivered") or []
                    covered_union = set(delivered_list)
                    covered_union |= set(ledger.get("retained_as_context") or [])
                    covered_union |= set(ledger.get("merged") or [])
                    covered_union |= {
                        int(item["segment_index"])
                        for item in (ledger.get("proven_duplicates") or [])
                        if isinstance(item, dict) and item.get("segment_index") is not None
                    }
                    prep_info = (
                        f" pack={content.get('prep_pack_version', '?')} "
                        f"events={len(content.get('event_chain') or [])} "
                        f"覆盖={len(covered_union)}/{ledger.get('total_segments', '?')}"
                        f"(delivered={len(delivered_list)})"
                    )
                if fresh:
                    flag = "  ○ 产物为本轮新建"
                elif art is None:
                    flag = "  ⚠ ready 但找不到已发布 artifact"
                    suspicious += 1
                else:
                    flag = "  ⚠ 产物非本轮新建：整集端出旧货，本轮对它无效"
                    suspicious += 1
            hook = "有" if (row["hook"] or "").strip() else "-"
            cliff = "有" if (row["cliffhanger"] or "").strip() else "-"
            print(
                f"  EP{row['episode_no']:<2} {state:<9} calls={calls:<4} ops={ops:<4} "
                f"{elapsed:<5} hook={hook} cliff={cliff}{prep_info}{flag}"
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
            print(f"  !! {suspicious} 集疑似端出旧货，这一轮对它们无效")
        if args.watch <= 0:
            return 1 if suspicious else 0
        time.sleep(args.watch)


if __name__ == "__main__":
    raise SystemExit(main())
