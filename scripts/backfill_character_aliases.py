#!/usr/bin/env python3
"""人物别名回填部署脚本（层一，一次性历史人物谱回填）。

背景：`app/stages.py:backfill_character_aliases()` 是纯函数——接受已完成的
`Bible` 实例 + chapters 列表，就地核验并追加 `Character.aliases`，不做任何
DB 读写（见该函数 docstring）。本脚本是它的唯一调用方：负责从 DB 读出项目
的 bible_json 与全部 chapters、调用回填、把结果写回 DB。

用法：
    .venv/bin/python scripts/backfill_character_aliases.py --dry-run
    .venv/bin/python scripts/backfill_character_aliases.py
    .venv/bin/python scripts/backfill_character_aliases.py --project proj_xxx --dry-run

--dry-run 只调用模型 + 核验 + 打印将要登记的别名（含证据），不写库；
不带 --dry-run 时才会把核验通过的别名落库（bible_version 乐观并发 CAS，
写入前检查该项目没有正在跑的 jobs/workflow_runs，避免覆盖后端并发写入）。

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

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import db  # noqa: E402
from app.schemas import Bible  # noqa: E402
from app.stages import backfill_character_aliases  # noqa: E402

DEFAULT_PROJECT_ID = "proj_3ac0b627fa46"
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

    logger.info("\n===== 回填结果（模型申报 + 代码三闸核验后通过的别名）=====")
    _print_result(added, bible, logger)

    if dry_run:
        logger.info("\n[dry-run] 未写库，未持久化任何改动。")
        return 0

    if not added:
        logger.info("\n没有新增别名，无需写库。")
        return 0

    reasons = _active_work_reasons(project_id)
    if reasons:
        logger.error(
            "检测到该项目当前有并发活动，为避免覆盖后端正在写入的数据，本次拒绝落库："
            + "; ".join(reasons)
        )
        return 1

    expected_version = int(proj["bible_version"] or 0)
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
            "本次回填未落库，未发生任何数据覆盖；请重新运行脚本。"
        )
        return 1

    logger.info(f"已写库：bible_version {expected_version} -> {expected_version + 1}")

    proj2, _ = _load_project_and_chapters(project_id)
    bible2 = Bible.model_validate(json.loads(proj2["bible_json"]))
    after_count = sum(len(c.aliases) for c in bible2.characters)
    logger.info(f"\n===== 落库核验（重新从 DB 读取）=====")
    logger.info(f"别名总数: {before_count} -> {after_count}")
    for c in bible2.characters:
        if c.aliases:
            logger.info(f"  {c.name}: " + "、".join(f"「{a.text}」" for a in c.aliases))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="人物别名回填（层一，一次性历史人物谱回填）")
    parser.add_argument(
        "--project", default=DEFAULT_PROJECT_ID,
        help=f"project_id（默认当前回归项目 {DEFAULT_PROJECT_ID}）",
    )
    parser.add_argument("--dry-run", action="store_true", help="只报告将登记的别名，不写库")
    args = parser.parse_args()

    logger = _setup_logging()
    logger.info(
        f"\n===== backfill_character_aliases 开始 project={args.project} "
        f"dry_run={args.dry_run} ====="
    )
    try:
        return asyncio.run(_run(args.project, args.dry_run, logger))
    except SystemExit as exc:
        logger.error(f"终止: {exc}")
        return 1
    except Exception:
        logger.exception("回填脚本异常退出")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
