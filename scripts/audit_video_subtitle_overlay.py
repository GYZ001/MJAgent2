"""对已采用的镜头视频跑字幕闸门判定，只读、不写库——用来标定判据与统计误报/漏报。

    py scripts/audit_video_subtitle_overlay.py --project 我欲封天 --from 1 --to 4

逐镜打印：集/镜、检查帧数、是否叠加字幕、命中帧（内容+位置），末尾给汇总。
在 B 上跑（视频与模型凭据都在 B）：ssh 起始目录是 /root，脚本自己切到仓库根。
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))


def _rows(project: str, ep_from: int, ep_to: int) -> list:
    from app.db import get_conn

    return get_conn().execute(
        """SELECT e.episode_no, s.shot_no, s.id AS shot_id, v.id AS version_id, v.video_path, p.id AS project_id
           FROM shots s JOIN shot_versions v ON v.id = s.adopted_version_id
           JOIN episodes e ON e.id = s.episode_id JOIN projects p ON p.id = e.project_id
           WHERE p.name LIKE ? AND e.episode_no BETWEEN ? AND ? AND v.video_path IS NOT NULL
           ORDER BY e.episode_no, s.shot_no""",
        (f"%{project}%", ep_from, ep_to),
    ).fetchall()


async def _audit(rows: list) -> tuple[int, int, int]:
    from app.media_exec import subtitle_gate

    hits = unchecked = 0
    for row in rows:
        try:
            verdict = await subtitle_gate.detect_subtitle_overlay(
                row["video_path"],
                call_meta={"purpose": "subtitle_gate_audit", "shot_id": row["shot_id"], "version_id": row["version_id"]},
            )
        except Exception as exc:  # noqa: BLE001 审计只记录，不中断
            unchecked += 1
            print(f"第{row['episode_no']}集 镜{row['shot_no']:>2}  未判定：{type(exc).__name__}: {exc}"[:200])
            continue
        flag = "叠加字幕" if verdict["subtitle_overlay"] else "无"
        hits += int(verdict["subtitle_overlay"])
        detail = "；".join(f"第{f['index']}帧『{f['text_seen']}』{f['where']}" for f in verdict["overlay_frames"])
        print(f"第{row['episode_no']}集 镜{row['shot_no']:>2}  {verdict['frames_checked']:>2} 帧  {flag}  {detail}")
    return len(rows), hits, unchecked


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--project", required=True)
    parser.add_argument("--from", dest="ep_from", type=int, default=1)
    parser.add_argument("--to", dest="ep_to", type=int, default=1)
    args = parser.parse_args()
    rows = _rows(args.project, args.ep_from, args.ep_to)
    if not rows:
        print("没有已采用的镜头视频")
        return 1
    total, hits, unchecked = asyncio.run(_audit(rows))
    print(f"\n汇总：{total} 镜，叠加字幕 {hits} 镜，未判定 {unchecked} 镜")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
