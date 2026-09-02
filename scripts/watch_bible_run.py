"""盯人物谱生成：按阶段汇总模型调用与失败原因，供生成过程中查看。

用法：py scripts/watch_bible_run.py <project_id> [since_seconds]
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 直接 `python scripts/x.py` 运行时 sys.path[0] 是 scripts/，不是仓库根。
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from app.config import DB_PATH  # noqa: E402


def _meta_stage(meta_raw: str | None) -> str:
    if not meta_raw:
        return ""
    try:
        meta = json.loads(meta_raw)
    except (TypeError, ValueError):
        return ""
    return str(meta.get("stage") or meta.get("stage_key") or "")


def main() -> int:
    project_id = sys.argv[1] if len(sys.argv) > 1 else ""
    since = int(sys.argv[2]) if len(sys.argv) > 2 else 3600
    if not project_id:
        print("usage: py scripts/watch_bible_run.py <project_id> [since_seconds]")
        return 2

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT bible_status, bible_error, bible_version, refs_status, portraits_status "
        "FROM projects WHERE id=?", (project_id,),
    ).fetchone()
    if row is None:
        print(f"project not found: {project_id}")
        return 1
    print("project:", dict(row))

    stages: dict[tuple[str, str], int] = {}
    failures: list[tuple[str, str]] = []
    for call in conn.execute(
        "SELECT kind, status, error, meta FROM provider_calls "
        "WHERE project_id=? AND ts > strftime('%s','now') - ? ORDER BY ts",
        (project_id, since),
    ):
        stage = _meta_stage(call["meta"]) or (call["kind"] or "")
        key = (stage, call["status"] or "")
        stages[key] = stages.get(key, 0) + 1
        if call["status"] not in {"OK", "ok"} and call["error"]:
            failures.append((stage, str(call["error"])[:160]))

    print("\n--- 调用汇总（阶段 / 状态 / 次数）---")
    for (stage, status), count in sorted(stages.items(), key=lambda item: -item[1]):
        print(f"  {count:5d}  {status:10s} {stage}")

    if failures:
        print(f"\n--- 失败样本（共 {len(failures)} 条，显示前 10）---")
        for stage, error in failures[:10]:
            print(f"  [{stage}] {error}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
