#!/usr/bin/env python3
"""修复 ``shots.scene_time`` 被角色描述污染的存量数据。

背景：2026-09-15 修掉的「年龄漏抄 / 时段错位」缺陷会把角色年龄写进镜头的时段
字段。2026-09-16 实测有 3 集残留（龙猫出爪 ep1 整集 ``五十岁上下``、跑不快的孩子
ep1 ``八岁``、我欲封天 ep8 ``约莫二十四五岁``）。这些集在修复前生成，数据留着坏值。

为什么必须连 ``scene_setting`` 一起改：``app.scene_contract.scene_time_of`` 在
``scene_time`` 为空时会回落到 ``split_legacy_scene_setting(scene_setting)``——只清
``scene_time`` 而把 ``五十岁上下，宠物医院前台`` 留在 ``scene_setting`` 里，污染值
会原样被取回来，等于没修。

**时段只从原文取，取不到就清空，绝不猜**。剧本体原文带 ``【段 NN｜地点｜时段】``
标注，逐字取用；小说体原文没有时段标注，那就写空——空是诚实的「未知」，而
``八岁`` 是个会流进色温判定的假答案。清空不会让换场判定变差：整集原本都是同一个
污染值，判定本来就只由 scene_name 区分，清空后仍然如此。

**fail closed，判据是棘轮而不是绝对值**：``app.continuity`` 用
``(scene_name, scene_time)`` 判换场，与镜头已固化的 ``continuity_mode`` 必须自洽
（mode=scene_change 要求与上一镜不同，其余要求相同）。脚本在内存里按**修复前**和
**修复后**两套值各重算一遍整集判定，只有「修复后新增了修复前没有的冲突镜」才整集
拒绝写入。

不能用「修复后必须零冲突」做判据：受污染的集往往**修复前就已经是冲突态**——整集
时段同一个假值、而 scene_name 在变，``mode=空`` 要求同场却不同场，本来就红。实测
跑不快的孩子 ep1 与我欲封天 ep8 都是这样，用零冲突判据会把它们一并拒掉，而它们
正是最该清掉假时段的集。修复的责任是不让情况变坏，不是替分镜承担叙事对齐。
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.scene_contract import compose_scene_setting  # noqa: E402

# 剧本体场次标注：【段 01｜人间·晚安宠物医院·门口｜傍晚】
_HEADING_RE = re.compile(r"【段\s*\d+｜([^｜】]*)｜([^｜】]*)】")


def _normalize(value: str) -> str:
    """与 app.continuity._scene_time_context 同一口径（去空白 + casefold）。"""
    return re.sub(r"\s+", "", value or "").casefold()


def _time_from_excerpt(excerpt: str) -> str:
    match = _HEADING_RE.search(excerpt or "")
    return match.group(2).strip() if match else ""


def _episode_shots(conn: sqlite3.Connection, project_id: str, episode_no: int) -> list[sqlite3.Row]:
    row = conn.execute(
        "SELECT id FROM episodes WHERE project_id=? AND episode_no=?", (project_id, episode_no),
    ).fetchone()
    if not row:
        raise SystemExit(f"找不到 {project_id} 第 {episode_no} 集")
    return conn.execute(
        "SELECT id,shot_no,scene_time,scene_name,scene_setting,continuity_mode,source_excerpt "
        "FROM shots WHERE episode_id=? ORDER BY shot_no", (row["id"],),
    ).fetchall()


def _continuity_conflicts(plan: list[tuple[int, str, str, str]]) -> dict[int, str]:
    """plan: (shot_no, scene_name, scene_time, continuity_mode) -> {镜号: 冲突描述}。

    按镜号返回而不是返回一个列表：调用方要做「修复后新增的冲突」这个集合差，
    只比条数会把「修好一镜、同时弄坏另一镜」判成无变化。
    """
    conflicts: dict[int, str] = {}
    prev: tuple[str, str] | None = None
    for shot_no, scene_name, scene_time, mode in plan:
        current = (_normalize(scene_name), _normalize(scene_time))
        if prev is not None:
            same = current == prev
            if mode == "scene_change" and same:
                conflicts[shot_no] = f"镜{shot_no}: continuity_mode=scene_change 但与上一镜同场同时段"
            elif mode != "scene_change" and not same:
                conflicts[shot_no] = f"镜{shot_no}: continuity_mode={mode or '空'} 但与上一镜场景/时段已变"
        prev = current
    return conflicts


def _scan(conn: sqlite3.Connection) -> int:
    """列出整集 scene_time 全部相同的集——正常剧集会跨多个时段，全同是结构性可疑信号。

    故意不用「年龄词表」判定：词表是封闭集合，穷举不完，而「整集同一个时段」这个
    信号从数据本身推导，不依赖任何词汇假设。是不是真污染由人看着值来判断。
    """
    rows = conn.execute(
        "SELECT e.project_id,e.episode_no,s.scene_time FROM shots s JOIN episodes e ON s.episode_id=e.id",
    ).fetchall()
    grouped: dict[tuple[str, int], set[str]] = {}
    for row in rows:
        grouped.setdefault((row["project_id"], row["episode_no"]), set()).add(str(row["scene_time"] or "").strip())
    suspicious = {k: v.pop() for k, v in grouped.items() if len(v) == 1 and next(iter(v))}
    if not suspicious:
        print("没有发现整集时段全同的剧集")
        return 0
    print(f"整集 scene_time 全同的剧集（{len(suspicious)} 集），请人工判断哪些是污染：")
    for (project_id, episode_no), value in sorted(suspicious.items()):
        print(f"  {project_id} 第 {episode_no} 集: {value!r}")
    return 0


def _repair(conn: sqlite3.Connection, project_id: str, episode_no: int, *, apply: bool) -> int:
    shots = _episode_shots(conn, project_id, episode_no)
    before_plan, after_plan, updates = [], [], []
    for shot in shots:
        new_time = _time_from_excerpt(shot["source_excerpt"] or "")
        scene_name = str(shot["scene_name"] or "")
        mode = str(shot["continuity_mode"] or "")
        shot_no = int(shot["shot_no"])
        before_plan.append((shot_no, scene_name, str(shot["scene_time"] or ""), mode))
        after_plan.append((shot_no, scene_name, new_time, mode))
        new_setting = compose_scene_setting(new_time, scene_name, fallback=scene_name)
        if new_time != str(shot["scene_time"] or "") or new_setting != str(shot["scene_setting"] or ""):
            updates.append((new_time, new_setting, shot["id"], shot_no, shot["scene_time"]))

    before = _continuity_conflicts(before_plan)
    after = _continuity_conflicts(after_plan)
    introduced = sorted(set(after) - set(before))
    if introduced:
        print(f"拒绝修改 {project_id} 第 {episode_no} 集：修复会新增 {len(introduced)} 处 continuity 冲突", file=sys.stderr)
        for shot_no in introduced:
            print(f"  {after[shot_no]}", file=sys.stderr)
        return 1

    filled = sum(1 for u in updates if u[0])
    healed = len(set(before) - set(after))
    print(f"{project_id} 第 {episode_no} 集：{len(shots)} 镜，待改 {len(updates)} 镜"
          f"（原文取到时段 {filled} 镜，取不到清空 {len(updates) - filled} 镜）；"
          f"continuity 冲突 {len(before)} -> {len(after)}（顺带消掉 {healed} 处，未新增）")
    for new_time, new_setting, _id, shot_no, old_time in updates[:40]:
        print(f"  镜{shot_no:2}: {str(old_time)!r} -> {new_time!r}  setting={new_setting!r}")
    if not apply:
        print("（--dry-run，未写库）")
        return 0
    with conn:
        for new_time, new_setting, shot_id, _shot_no, _old in updates:
            conn.execute(
                "UPDATE shots SET scene_time=?,scene_setting=? WHERE id=?", (new_time, new_setting, shot_id),
            )
    print(f"已写入 {len(updates)} 镜")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="data/manju.db")
    parser.add_argument("--scan", action="store_true", help="列出整集时段全同的可疑剧集，不修改")
    parser.add_argument("--project", help="项目 id")
    parser.add_argument("--episode", type=int, help="集号")
    parser.add_argument("--apply", action="store_true", help="真正写库；不给就是预演")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    try:
        if args.scan:
            return _scan(conn)
        if not args.project or args.episode is None:
            parser.error("需要 --scan，或同时给 --project 与 --episode")
        return _repair(conn, args.project, args.episode, apply=args.apply)
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
