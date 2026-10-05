#!/usr/bin/env python3
"""分镜道具/衣物状态续接复核 dry-run：对一集已落库的分镜只跑复核判定 +
代码核验，输出拟重写的段与理由——不写 ``shots``/``shot_versions``、不调用
「修订本段」重写模型、不触发任何视频生成。

用途：下一阶段要在 B 沙箱对《顾念长安》第 1 集（``ep_a3c61162b4ce``）真实跑，
验证新增的 ``prop_state_regression`` 判据与起幅正面陈述规则上线后，复核模型
能不能稳定抓到真实的插座/插头一类状态回退，再决定要不要真的跑一遍写库版本
（``app.domain.storyboard_ops.prop_continuity_review.rewrite_flagged_segments``，
或分镜台页面对应按钮）。

**真实会发起模型调用**：复核这一步本身要调用分镜正文复核模型（与生成期
「边写边审」同一个模型），dry-run 只保证不写库、不触发重写/视频生成——与
``scripts/prop_card_audit_dry_run.py`` 的 dry_run 语义一致，不是完全无副作用
（网络调用本身会计入文本调用次数）。

**生产库只读**：默认用 ``sqlite3`` 的 ``mode=ro`` URI 打开，任何写操作在这条
连接上都会失败而不是静默成功——按 CLAUDE.md「生产库只读」的约定，不依赖
调用方记得不写。

用法：
    py scripts/storyboard_prop_continuity_dry_run.py --db data/manju.db --episode ep_a3c61162b4ce
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.domain.storyboard_ops.prop_continuity_review import review_existing_episode_segments  # noqa: E402
from app.visual_styles import _project_bible_or_placeholder  # noqa: E402


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="sqlite 数据库文件路径（只读打开，不接受写操作）")
    parser.add_argument("--episode", required=True, help="episode id（episodes.id）")
    return parser.parse_args()


def _read_only_connection(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _load_episode_and_bible(conn: sqlite3.Connection, episode_id: str) -> tuple[dict, object]:
    row = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    if row is None:
        raise ValueError(f"episode {episode_id} 不存在")
    episode = dict(row)
    project = conn.execute("SELECT * FROM projects WHERE id=?", (episode["project_id"],)).fetchone()
    return episode, _project_bible_or_placeholder(project)


def main() -> int:
    args = _parse_args()
    conn = _read_only_connection(args.db)
    try:
        episode, bible = _load_episode_and_bible(conn, args.episode)
        outcomes = asyncio.run(review_existing_episode_segments(conn, episode=episode, bible=bible))
    except ValueError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    finally:
        conn.close()
    results = [o.to_dict() for o in outcomes]
    print(json.dumps({"episode_id": args.episode, "dry_run": True, "segments": results}, ensure_ascii=False, indent=2))
    flagged = sum(1 for r in results if r["violations"])
    print(f"# 共 {len(results)} 段：{flagged} 段有已核验违规，拟重写；其余段落照常跳过", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
