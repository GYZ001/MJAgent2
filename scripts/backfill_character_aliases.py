#!/usr/bin/env python3
"""人物别名回填部署脚本（层一，一次性历史人物谱回填 + 历史批次复核）。

背景：`app/stages.py:backfill_character_aliases()` / `reverify_character_aliases()`
都是纯函数——接受已完成的 `Bible` 实例 + chapters 列表，就地核验并改写
`Character.aliases`，不做任何 DB 读写（见各自 docstring）。本脚本是它们的唯一
调用方：负责从 DB 读出项目的 bible_json 与全部 chapters、调用回填/复核、把结果
写回 DB。

用法（--project 必填，无默认值——历史默认项目 proj_3ac0b627fa46 已随项目重建
失效，写死一次就要再踩一次）：
    .venv/bin/python scripts/backfill_character_aliases.py --project proj_xxx --dry-run
    .venv/bin/python scripts/backfill_character_aliases.py --project proj_xxx
    .venv/bin/python scripts/backfill_character_aliases.py --project proj_xxx --reverify --dry-run
    .venv/bin/python scripts/backfill_character_aliases.py --project proj_xxx --reverify

--dry-run 只调用模型 + 核验 + 打印将要登记/移除的别名（含证据/理由），不写库；
不带 --dry-run 时才会把核验结果落库（bible_version 乐观并发 CAS，写入前检查该
项目没有正在跑的 jobs/workflow_runs，避免覆盖后端并发写入）。

--reverify 切换到复核模式：不发起新的模型申报回填，而是对 bible 中**已经登记**
的全部别名重跑一遍当前完整核验闸门（含裁决闸——见 `app/stages.py` "A1b. 裁决闸"
一节）；过不了闸的条目不再从 bible 中移除，而是原样保留、把 `is_exclusive` 降级
为 False（见 `CharacterAlias.is_exclusive` 与 `reverify_character_aliases`
docstring）。用于清理裁决闸补上之前已经落库、但实际未必真的"指同一人"的历史
误登记对排他性的误判（真实事故：孟浩←虎爷爷）。不加 --reverify 时是原有的回填
模式，行为不变。

日志写 logs/backfill_character_aliases.log（同时打印到终端）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sqlite3
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import db  # noqa: E402
from app.schemas import Bible  # noqa: E402
from app.stages import backfill_character_aliases, reverify_character_aliases  # noqa: E402

LOG_PATH = ROOT / "logs" / "backfill_character_aliases.log"

# 判定"该项目当前有并发活动"的活跃状态集合——覆盖大小写是因为不同子系统的
# status 枚举风格不完全一致（jobs 用小写，workflow_runs 见过大写），宁可
# 多拦一种拼写也不要漏判。
_ACTIVE_JOB_STATUSES = ("queued", "running", "processing")
_ACTIVE_WORKFLOW_STATUSES = (
    "queued", "running", "processing", "pending",
    "QUEUED", "RUNNING", "PROCESSING", "PENDING",
)


def _setup_logging() -> logging.Logger:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("backfill_character_aliases")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(fh)
        sh = logging.StreamHandler()
        sh.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(sh)
    return logger


def _readonly_conn() -> sqlite3.Connection:
    """只读连接：既不与后端写事务竞争，也不占用 app.db 的任务级连接池。"""
    conn = sqlite3.connect(f"file:{db.DB_PATH.resolve()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _load_project_and_chapters(project_id: str) -> tuple[dict, list[dict]]:
    conn = _readonly_conn()
    try:
        proj = conn.execute(
            "SELECT id, name, bible_json, bible_version FROM projects WHERE id=?",
            (project_id,),
        ).fetchone()
        if not proj:
            raise SystemExit(f"项目不存在: {project_id}")
        if not proj["bible_json"]:
            raise SystemExit(f"项目 {project_id} 尚无人物谱（bible_json 为空），无法回填")
        chapters = conn.execute(
            "SELECT idx, title, content FROM chapters WHERE project_id=? ORDER BY idx",
            (project_id,),
        ).fetchall()
        return dict(proj), [dict(c) for c in chapters]
    finally:
        conn.close()


def _active_work_reasons(project_id: str) -> list[str]:
    """写库前的并发安全闸：该项目若有正在跑的任务/工作流，返回非空原因列表——
    调用方据此拒绝写入，避免覆盖后端正在写的 bible_json。这是只读探测，探测到
    写入之间仍有极短窗口，靠写入本身的 bible_version CAS 兜底（一旦版本号在
    此期间被后端改写，UPDATE 的 WHERE 条件不命中，rowcount=0，本脚本视为失败
    并拒绝覆盖，不会重试硬写）。"""
    conn = _readonly_conn()
    try:
        reasons: list[str] = []
        job_marks = ",".join("?" for _ in _ACTIVE_JOB_STATUSES)
        jobs_active = conn.execute(
            f"SELECT count(*) c FROM jobs WHERE project_id=? AND status IN ({job_marks})",
            (project_id, *_ACTIVE_JOB_STATUSES),
        ).fetchone()["c"]
        if jobs_active:
            reasons.append(f"{jobs_active} 个 jobs 处于活跃状态")

        wf_marks = ",".join("?" for _ in _ACTIVE_WORKFLOW_STATUSES)
        wf_active = conn.execute(
            f"SELECT count(*) c FROM workflow_runs WHERE scope_id=? AND status IN ({wf_marks})",
            (project_id, *_ACTIVE_WORKFLOW_STATUSES),
        ).fetchone()["c"]
        if wf_active:
            reasons.append(f"{wf_active} 个 workflow_runs 处于活跃状态")
        return reasons
    finally:
        conn.close()


def _print_result(added: dict[str, list[str]], bible: Bible, logger: logging.Logger) -> None:
    if not added:
        logger.info("本次回填没有新增任何可核验别名。")
        return
    by_name = {c.name: c for c in bible.characters}
    for name, texts in added.items():
        character = by_name[name]
        logger.info(f"\n■ {name} 新增 {len(texts)} 条别名：")
        by_text = {a.text: a for a in character.aliases}
        for text in texts:
            alias = by_text[text]
            logger.info(
                f"  - 「{alias.text}」[{alias.name_kind}] "
                f"证据：第 {alias.evidence_chapter_index} 章 "
                f"「{alias.evidence_quote}」"
            )


def _alias_exclusivity_snapshot(bible: Bible) -> dict[str, dict[str, bool]]:
    """按 (角色, 别名文本) 快照当前每条别名的 `is_exclusive`，供复核前后比较。
    `reverify_character_aliases` 判不过不再删除条目，只把 `is_exclusive` 降级为
    False（见该函数 docstring）——"这次复核有没有产生需要持久化的变化"必须从这份
    快照的前后差异推导（条目消失 或 is_exclusive 变化），不能再挂在别名总数上：
    新语义下总数恒定不变，挂总数的判据会让写库闸口永远打不开。"""
    return {c.name: {a.text: a.is_exclusive for a in c.aliases} for c in bible.characters}


def _print_reverify_result(
    report: dict[str, list[dict[str, Any]]],
    before: dict[str, dict[str, bool]],
    after: dict[str, dict[str, bool]],
    logger: logging.Logger,
) -> None:
    if not report:
        logger.info("没有带别名的角色，复核范围为空。")
        return
    for name, entries in report.items():
        before_map = before.get(name, {})
        after_map = after.get(name, {})
        removed = [e for e in entries if e["text"] not in after_map]
        downgraded = [
            e for e in entries
            if before_map.get(e["text"]) is True and after_map.get(e["text"]) is False
        ]
        upgraded = [
            e for e in entries
            if before_map.get(e["text"]) is False and after_map.get(e["text"]) is True
        ]
        logger.info(
            f"\n■ {name}：{len(entries)} 条别名，移除 {len(removed)} 条，"
            f"降级为非排他 {len(downgraded)} 条，升级为排他 {len(upgraded)} 条"
        )
        for entry in entries:
            text = entry["text"]
            reason = f"（拒绝原因：{entry['reason']}）" if entry["reason"] else ""
            if text not in after_map:
                mark = "移除"
            else:
                before_v, after_v = before_map.get(text), after_map[text]
                mark = (
                    f"排他性变化：{before_v} -> {after_v}"
                    if before_v != after_v
                    else f"保留（is_exclusive={after_v}）"
                )
            logger.info(f"  - 「{text}」{mark}{reason}")


async def _guarded_cas_write(
    project_id: str, bible: Bible, expected_version: int, logger: logging.Logger,
) -> int:
    """写库前的并发安全闸 + bible_version 乐观并发 CAS 写入，供回填与复核两条命令
    共用。返回 0=成功（已记录日志），1=失败（已记录日志，未发生任何数据覆盖）。"""
    reasons = _active_work_reasons(project_id)
    if reasons:
        logger.error(
            "检测到该项目当前有并发活动，为避免覆盖后端正在写入的数据，本次拒绝落库："
            + "; ".join(reasons)
        )
        return 1

    payload = json.dumps(bible.model_dump(mode="json"), ensure_ascii=False)

    def _write(conn: sqlite3.Connection) -> int:
        cursor = conn.execute(
            "UPDATE projects SET bible_json=?, bible_version=? "
            "WHERE id=? AND COALESCE(bible_version,0)=?",
            (payload, expected_version + 1, project_id, expected_version),
        )
        return cursor.rowcount

    rowcount = await db.run_write_transaction(_write)
    if rowcount != 1:
        logger.error(
            f"写入失败：bible_version 已不再是 {expected_version}（写入期间被后端并发改写），"
            "本次改动未落库，未发生任何数据覆盖；请重新运行脚本。"
        )
        return 1

    logger.info(f"已写库：bible_version {expected_version} -> {expected_version + 1}")
    return 0


def _log_db_alias_snapshot(project_id: str, before_count: int, logger: logging.Logger) -> None:
    """写库后重新从 DB 读取，打印落库核验（回填/复核共用）。"""
    proj2, _ = _load_project_and_chapters(project_id)
    bible2 = Bible.model_validate(json.loads(proj2["bible_json"]))
    after_count = sum(len(c.aliases) for c in bible2.characters)
    logger.info("\n===== 落库核验（重新从 DB 读取）=====")
    logger.info(f"别名总数: {before_count} -> {after_count}")
    for c in bible2.characters:
        if c.aliases:
            logger.info(f"  {c.name}: " + "、".join(f"「{a.text}」" for a in c.aliases))


async def _run(project_id: str, dry_run: bool, logger: logging.Logger) -> int:
    proj, chapters = _load_project_and_chapters(project_id)
    logger.info(
        f"项目: {proj['name']} ({project_id})，章节数: {len(chapters)}，"
        f"当前 bible_version: {proj['bible_version']}"
    )
    bible = Bible.model_validate(json.loads(proj["bible_json"]))
    logger.info(
        f"人物谱角色数: {len(bible.characters)} -> "
        + "、".join(c.name for c in bible.characters)
    )
    before_count = sum(len(c.aliases) for c in bible.characters)
    logger.info(f"回填前已登记别名总数: {before_count}")

    added = await backfill_character_aliases(bible, chapters, project_id=project_id)

    logger.info("\n===== 回填结果（模型申报 + 代码核验闸门核验后通过的别名）=====")
    _print_result(added, bible, logger)

    if dry_run:
        logger.info("\n[dry-run] 未写库，未持久化任何改动。")
        return 0

    if not added:
        logger.info("\n没有新增别名，无需写库。")
        return 0

    expected_version = int(proj["bible_version"] or 0)
    result = await _guarded_cas_write(project_id, bible, expected_version, logger)
    if result != 0:
        return result
    _log_db_alias_snapshot(project_id, before_count, logger)
    return 0


async def _run_reverify(project_id: str, dry_run: bool, logger: logging.Logger) -> int:
    """复核模式：对 bible 中已登记的全部别名重跑 `reverify_character_aliases`
    （核验入口与回填共用，见 `app/stages.py`）。判不过不再删除条目，只把该条目的
    `is_exclusive` 降级为 False（见该函数 docstring）；写库闸口挂在"别名集合或
    is_exclusive 是否有变化"上，不再是"是否有别名被移除"。用于清理裁决闸补上之前
    已经落库的历史误登记别名对排他性的误判（真实事故：孟浩←虎爷爷）。"""
    proj, chapters = _load_project_and_chapters(project_id)
    logger.info(
        f"项目: {proj['name']} ({project_id})，章节数: {len(chapters)}，"
        f"当前 bible_version: {proj['bible_version']}"
    )
    bible = Bible.model_validate(json.loads(proj["bible_json"]))
    logger.info(
        f"人物谱角色数: {len(bible.characters)} -> "
        + "、".join(c.name for c in bible.characters)
    )
    before_count = sum(len(c.aliases) for c in bible.characters)
    logger.info(f"复核前已登记别名总数: {before_count}")
    before_snapshot = _alias_exclusivity_snapshot(bible)

    report = await reverify_character_aliases(bible, chapters, project_id=project_id)

    after_snapshot = _alias_exclusivity_snapshot(bible)
    logger.info("\n===== 复核结果（对已登记别名重跑完整核验闸门，含裁决闸）=====")
    _print_reverify_result(report, before_snapshot, after_snapshot, logger)

    after_count = sum(len(c.aliases) for c in bible.characters)
    removed_count = before_count - after_count
    downgraded_count = sum(
        1
        for name, after_map in after_snapshot.items()
        for text, after_v in after_map.items()
        if before_snapshot.get(name, {}).get(text) is True and after_v is False
    )
    upgraded_count = sum(
        1
        for name, after_map in after_snapshot.items()
        for text, after_v in after_map.items()
        if before_snapshot.get(name, {}).get(text) is False and after_v is True
    )
    logger.info(
        f"\n别名总数变化（内存中，尚未写库）: {before_count} -> {after_count}"
        f"（移除 {removed_count} 条，降级为非排他 {downgraded_count} 条，"
        f"升级为排他 {upgraded_count} 条）"
    )

    # 写库闸口挂在"这次复核有没有产生需要持久化的变化"上——从数据推导：比较复核
    # 前后的别名集合与各自的 is_exclusive，而不是只看总数（新语义下判不过不再删除
    # 条目，总数恒定不变，挂总数会让这条写库路径永远打不开，见本脚本改动背景）。
    changed = before_snapshot != after_snapshot

    if dry_run:
        logger.info("\n[dry-run] 未写库，未持久化任何改动。")
        return 0

    if not changed:
        logger.info("\n复核结果与库中现状完全一致（无别名被移除，is_exclusive 均未变化），无需写库。")
        return 0

    expected_version = int(proj["bible_version"] or 0)
    result = await _guarded_cas_write(project_id, bible, expected_version, logger)
    if result != 0:
        return result
    _log_db_alias_snapshot(project_id, before_count, logger)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="人物别名回填 / 复核（层一，一次性历史人物谱回填与历史批次复核）"
    )
    parser.add_argument(
        "--project", default=None,
        help="project_id（必填，无默认值——历史默认项目已随重建失效）",
    )
    parser.add_argument("--dry-run", action="store_true", help="只报告将登记/移除的别名，不写库")
    parser.add_argument(
        "--reverify", action="store_true",
        help="复核模式：不回填新别名，对 bible 中已登记的全部别名重跑完整核验闸门"
             "（含裁决闸），不通过的移除并写库",
    )
    args = parser.parse_args()

    if not args.project:
        print(
            "用法：.venv/bin/python scripts/backfill_character_aliases.py "
            "--project <project_id> [--dry-run] [--reverify]\n"
            "缺少 --project：历史默认项目 id 已随项目重建失效，必须显式指定目标项目。",
            file=sys.stderr,
        )
        return 2

    logger = _setup_logging()
    mode = "reverify" if args.reverify else "backfill"
    logger.info(
        f"\n===== backfill_character_aliases 开始 project={args.project} "
        f"mode={mode} dry_run={args.dry_run} ====="
    )
    try:
        runner = _run_reverify if args.reverify else _run
        return asyncio.run(runner(args.project, args.dry_run, logger))
    except SystemExit as exc:
        logger.error(f"终止: {exc}")
        return 1
    except Exception:
        logger.exception("脚本异常退出")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
