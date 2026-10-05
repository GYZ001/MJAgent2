#!/usr/bin/env python3
"""分镜道具/衣物状态续接复核 dry-run：对一集已落库的分镜跑复核判定 + 代码
核验 + （对局部可修的违规）最小替换提案核验与差异预览——不写
``shots``/``shot_versions``、不调用 ``identity_workspace.save_identity_
candidate``、不触发任何视频生成。

用途：下一阶段要在 B 沙箱对《顾念长安》第 1 集（``ep_a3c61162b4ce``）真实跑，
验证局部可修的违规（``prop_state_regression``/``skin_blush``/
``screen_side``/``prop_appearance``/``prop_duplication``/
``repeated_transition_action``/``negated_action``）能不能被最小替换提案稳定
核验通过，再决定要不要真的跑一遍写库版本
（``app.domain.storyboard_ops.prop_continuity_review.rewrite_flagged_segments``，
或分镜台页面对应按钮）。结构类违规（``action_density``/``unvoiced_speech``/
``time_jump``/``impossible_camera_move``）本脚本不发起替换提案调用，只在输出里
标成「需要人工修订本段」，与写库路径同一判据（``is_locally_patchable``）。

**真实会发起模型调用**：复核这一步、以及对局部可修违规的替换提案这一步都要
调用模型（分别是复核模型与最小修改提案模型），dry-run 只保证不写库、不触发
保存/视频生成——与 ``scripts/prop_card_audit_dry_run.py`` 的 dry_run 语义
一致，不是完全无副作用（网络调用本身会计入文本调用次数）。

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
from difflib import unified_diff
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.domain.storyboard_ops.prop_continuity_minimal_patch import (  # noqa: E402
    apply_replacements, is_locally_patchable, propose_minimal_patch, validate_replacements,
)
from app.domain.storyboard_ops.prop_continuity_review import (  # noqa: E402
    review_existing_episode_segments, stored_segment_prompt_text,
)
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


async def _patch_preview(conn, *, episode_id: str, segment_no: int, violations: list) -> dict:
    """只读试算：对局部可修的违规发一次最小替换提案、核验，但不应用到
    ``speech_template``/不保存——与写库路径共用同一份核验函数
    （``validate_replacements``），差异预览只用于人工判断，不代表写库路径
    一定会产出同样的候选（写库路径还要核验能否镜像套用到 speech_template）。"""
    patchable = [v for v in violations if is_locally_patchable(v.kind)]
    if not patchable:
        return {"attempted": False}
    prompt_text = stored_segment_prompt_text(conn, episode_id, segment_no)
    raw = await propose_minimal_patch(episode_id=episode_id, segment_no=segment_no, prompt_text=prompt_text, violations=patchable)
    accepted, rejected = validate_replacements(prompt_text, raw, violations=patchable)
    patched_text = apply_replacements(prompt_text, accepted) if accepted else prompt_text
    diff = list(unified_diff(prompt_text.splitlines(), patched_text.splitlines(), lineterm="", n=0)) if accepted else []
    return {
        "attempted": True,
        "proposed_replacements": [{"quote": r.quote, "replacement": r.replacement} for r in raw],
        "accepted": [{"quote": a.quote, "replacement": a.replacement} for a in accepted],
        "rejected": [r.to_dict() for r in rejected],
        "patched_prompt_text": patched_text,
        "diff": diff,
    }


async def _run(conn, episode: dict, bible) -> list[dict]:
    outcomes = await review_existing_episode_segments(conn, episode=episode, bible=bible)
    results = []
    for outcome in outcomes:
        result = outcome.to_dict()
        result["patch_preview"] = await _patch_preview(conn, episode_id=episode["id"], segment_no=outcome.segment_no, violations=outcome.violations)
        results.append(result)
    return results


def main() -> int:
    args = _parse_args()
    conn = _read_only_connection(args.db)
    try:
        episode, bible = _load_episode_and_bible(conn, args.episode)
        results = asyncio.run(_run(conn, episode, bible))
    except ValueError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    finally:
        conn.close()
    print(json.dumps({"episode_id": args.episode, "dry_run": True, "segments": results}, ensure_ascii=False, indent=2))
    flagged = sum(1 for r in results if r["violations"])
    patchable_ok = sum(1 for r in results if r["patch_preview"].get("accepted"))
    print(f"# 共 {len(results)} 段：{flagged} 段有已核验违规，{patchable_ok} 段有可应用的局部替换；其余段落需人工修订或没有违规", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
