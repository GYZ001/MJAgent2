#!/usr/bin/env python3
"""存量项目切章修正：已进入生产的集/章节一个字节不动，只对未产出尾部按修
好的切章器重切（见 ``app.domain.projects.rechapter`` 包的文档字符串）。

用法：
    py scripts/rechapter_project.py --project proj_xxx              # 默认 dry-run
    py scripts/rechapter_project.py --project proj_xxx --apply      # 真正写库
    py scripts/rechapter_project.py --all                           # 扫全部项目，只列出会变化的
    py scripts/rechapter_project.py --project proj_xxx --json

只通过 ``app.config``（经 ``app.db.get_conn()``）取数据库路径——沙箱演练设
``MANJU_TEST_PROFILE=isolated`` 与 ``MANJU_TEST_SANDBOX=<目录>`` 两个环境变量
即可让本脚本读写沙箱数据库，不需要改一行代码，也不会碰到生产库。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.db import get_conn  # noqa: E402
from app.domain.projects.rechapter.apply import apply_project  # noqa: E402
from app.domain.projects.rechapter.plan import plan_project  # noqa: E402
from app.domain.projects.rechapter.report import (  # noqa: E402
    affected_old_idx,
    format_report,
    plan_to_json,
    scan_bible_chapter_references,
    series_task_gaps,
)


def _enrich_plan(conn, plan) -> None:
    """补齐只读报告字段：世界书引用扫描 + 连播任务覆盖缺口。这两项都只读
    不写库，特意不放进 ``plan_project`` 本体，避免那个文件再背上报告职责。"""
    if not plan.ok:
        return
    affected = affected_old_idx(plan)
    if affected:
        row = conn.execute(
            "SELECT bible_json FROM projects WHERE id=?", (plan.project_id,)
        ).fetchone()
        plan.bible_reference_report = scan_bible_chapter_references(
            row["bible_json"] if row else None, affected,
        )
    new_episode_nos = [w.episode_no for w in plan.episode_writes if w.action == "insert"]
    plan.series_task_gap_report = series_task_gaps(conn, plan.project_id, new_episode_nos)


def _all_project_ids(conn) -> list[str]:
    return [
        row["id"] for row in
        conn.execute("SELECT id FROM projects WHERE deleted_at IS NULL ORDER BY id").fetchall()
    ]


def _run_one(conn, project_id: str, *, do_apply: bool) -> dict:
    plan = plan_project(conn, project_id)
    _enrich_plan(conn, plan)
    outcome = {"plan": plan_to_json(plan), "text": format_report(plan)}
    if do_apply and plan.ok and plan.changed:
        outcome["apply"] = apply_project(conn, project_id, plan)
    return outcome


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", action="append", default=[], help="项目 id，可重复传入")
    parser.add_argument("--all", action="store_true", help="扫描全部项目，只列出结果会变化的")
    parser.add_argument("--apply", action="store_true", help="真正写库；默认 dry-run 只打印报告")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON 而非中文文本")
    args = parser.parse_args()
    if not args.project and not args.all:
        parser.error("必须指定 --project 或 --all")
    return args


def main() -> int:
    args = _parse_args()
    conn = get_conn()
    project_ids = args.project or _all_project_ids(conn)
    scanning_all = bool(args.all and not args.project)

    results = []
    for project_id in project_ids:
        outcome = _run_one(conn, project_id, do_apply=args.apply)
        if scanning_all and not outcome["plan"]["changed"]:
            continue
        results.append(outcome)

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return 0
    for outcome in results:
        print(outcome["text"])
        if "apply" in outcome:
            print(f"  写入结果：{outcome['apply']}")
    if not results:
        print("没有需要处理的项目。" if scanning_all else "")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
