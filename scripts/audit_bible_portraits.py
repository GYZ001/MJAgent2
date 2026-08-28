"""核对每个项目的人物谱与定妆：卡片本身立不立得住、定妆是不是真落了盘。

判据全部从这次库里实际存在的数据推导，不维护任何角色名白名单——名单会随语料变，
而这个脚本要能对着任意新项目直接跑。

用法：py scripts/audit_bible_portraits.py [--project proj_xxx] [--db 路径]

``--db`` 指向库的副本，用来在不碰生产数据的前提下验证判据真的会红。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "manju.db"

# 外观提示词会被逐字送进画图模型，出现在里面的元话语就是画图指令的一部分。
# 这些词条挑的是"对模型说话"而不是"描述长相"的固定搭配，不是穷举敏感词。
META_MARKERS = (
    "原文未",
    "原文没有",
    "未点明",
    "未提及",
    "未描写",
    "无法确定",
    "不详",
    "根据上下文",
    "推测",
    "此处为",
    "注：",
)

# 语气助词/结构助词开头的称呼是被截断的短语碎片，不是独立称谓。
FRAGMENT_HEADS = ("的", "了", "着", "过", "地", "得", "和", "与", "跟", "及")


def _load_bible(row: sqlite3.Row) -> dict:
    raw = row["bible_json"]
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def _alias_texts(character: dict) -> list[str]:
    out: list[str] = []
    for item in character.get("aliases") or []:
        if isinstance(item, str):
            text = item
        elif isinstance(item, dict):
            text = item.get("text") or ""
        else:
            continue
        text = text.strip()
        if text:
            out.append(text)
    return out


def audit_project(conn: sqlite3.Connection, project: sqlite3.Row) -> list[str]:
    issues: list[str] = []
    bible = _load_bible(project)
    characters = bible.get("characters") or []
    pid = project["id"]
    name = project["name"]

    if not characters:
        issues.append(f"[{name}] 人物谱为空")
        return issues

    # 定妆按 ep_start/ep_end 区间做版本化，同一个角色会同时存在历史段（ep_end 已
    # 关闭或 ep_start 为负）。取生效段用的是产品自己那条区间判据，不是「按名字取
    # 最后一行」——后者能不能取对全看行序，同一份数据换个顺序结论就变。
    portraits = {
        row["character_name"]: row
        for row in conn.execute(
            "SELECT * FROM character_portraits WHERE project_id=? "
            "AND ep_start<=1 AND (ep_end IS NULL OR ep_end>=1) "
            "ORDER BY ep_start DESC, created_at DESC",
            (pid,),
        )
    }
    retired = {
        row["character_name"]
        for row in conn.execute(
            "SELECT DISTINCT character_name FROM character_portraits WHERE project_id=? "
            "AND NOT (ep_start<=1 AND (ep_end IS NULL OR ep_end>=1))",
            (pid,),
        )
    }

    bible_names = {c.get("name") for c in characters if c.get("name")}
    for orphan in sorted(set(portraits) - bible_names):
        issues.append(f"[{name}] 定妆 {orphan} 在人物谱里查不到，是孤儿记录")
    # 历史段留着是版本化的一部分；只有「人物谱里还有这个人、生效段却没了」才是缺口。
    for stranded in sorted((retired & bible_names) - set(portraits)):
        issues.append(f"[{name}] {stranded} 只剩历史定妆段，本集没有生效的定妆")

    for character in characters:
        cname = character.get("name") or "<无名>"
        tag = f"[{name}/{cname}]"

        appearance = (character.get("appearance_canonical") or "").strip()
        if not appearance:
            issues.append(f"{tag} appearance_canonical 为空，画图没有长相来源")
        else:
            for marker in META_MARKERS:
                if marker in appearance:
                    issues.append(
                        f"{tag} appearance_canonical 含元话语「{marker}」，"
                        f"会被逐字画进图里：{appearance[:60]}"
                    )
                    break

        for alias in _alias_texts(character):
            if len(alias) <= 1:
                issues.append(f"{tag} 别名「{alias}」是单字，子串检索必然误命中")
            if alias and alias[0] in FRAGMENT_HEADS:
                issues.append(f"{tag} 别名「{alias}」以助词开头，是短语碎片不是称谓")
            if alias == cname:
                issues.append(f"{tag} 别名「{alias}」与本名重复")

        portrait = portraits.get(cname)
        if portrait is None:
            issues.append(f"{tag} 没有定妆记录")
            continue
        if portrait["pack_status"] not in ("ready", "approved", "selected"):
            issues.append(f"{tag} 定妆状态为 {portrait['pack_status']}，未就绪")

        # 「人物谱重生 → 定妆没跟着重画」是这条工作流本身会造出来的漂移：定妆
        # 留在盘上、状态照旧 ready，一切看起来都对，画的却是上一版的长相。挂的
        # 是两段外观文本本身，不是 bible_version 号——版本号会被改名、加场景这
        # 类与长相无关的操作推高，拿它比对只会一片假红。
        drawn_from = (portrait["appearance"] or "").strip()
        if appearance and drawn_from != appearance:
            issues.append(
                f"{tag} 定妆画的是旧长相，与人物谱当前 appearance_canonical 不符"
                f"\n      人物谱：{appearance[:70]}"
                f"\n      定妆依据：{drawn_from[:70] or '（空）'}"
            )

        views = list(
            conn.execute(
                "SELECT * FROM character_portrait_views WHERE portrait_id=?",
                (portrait["id"],),
            )
        )
        if not views:
            issues.append(f"{tag} 定妆没有任何视图")
            continue
        selected = [v for v in views if v["selected"]]
        if not selected:
            issues.append(f"{tag} 定妆 {len(views)} 张视图无一被选中")
        pack_prompt = portrait["prompt"] or ""
        for view in selected or views:
            path = view["image_path"]
            if not path:
                issues.append(f"{tag} 视图 {view['view_role']} 没有图片路径")
                continue
            full = Path(path)
            if not full.is_absolute():
                full = ROOT / path
            if not full.exists():
                issues.append(f"{tag} 视图 {view['view_role']} 的图片不在盘上：{path}")
            # 上面那条比的是定妆记录自己抄的一份外观，它和真正送进画图模型的
            # 提示词是两回事——抄对了不等于送对了。独立观察点：当前外观必须逐字
            # 出现在这张图实际用的提示词里。
            if appearance and appearance not in (view["prompt"] or "") \
                    and appearance not in pack_prompt:
                issues.append(
                    f"{tag} 视图 {view['view_role']} 的画图提示词里没有当前外观描述，"
                    f"这张图不是照人物谱画的"
                )

    return issues


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", default=None)
    parser.add_argument("--db", default=None)
    args = parser.parse_args()

    conn = sqlite3.connect(args.db or DB)
    conn.row_factory = sqlite3.Row
    sql = "SELECT * FROM projects"
    params: tuple = ()
    if args.project:
        sql += " WHERE id=?"
        params = (args.project,)

    total_chars = 0
    all_issues: list[str] = []
    for project in conn.execute(sql, params):
        bible = _load_bible(project)
        chars = bible.get("characters") or []
        total_chars += len(chars)
        issues = audit_project(conn, project)
        all_issues.extend(issues)
        status = "OK" if not issues else f"{len(issues)} 条问题"
        print(f"{project['name']}（{len(chars)} 角色）：{status}")
        for line in issues:
            print("  - " + line)

    print(f"\n合计 {total_chars} 角色，{len(all_issues)} 条问题")
    return 1 if all_issues else 0


if __name__ == "__main__":
    sys.exit(main())
