"""核对每个项目的人物谱与定妆：卡片本身立不立得住、定妆是不是真落了盘。

判据全部从这次库里实际存在的数据推导，不维护任何角色名白名单——名单会随语料变，
而这个脚本要能对着任意新项目直接跑。

用法：py scripts/audit_bible_portraits.py [--project proj_xxx]
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

    portraits = {
        row["character_name"]: row
        for row in conn.execute(
            "SELECT * FROM character_portraits WHERE project_id=?", (pid,),
        )
    }

    bible_names = {c.get("name") for c in characters if c.get("name")}
    for orphan in sorted(set(portraits) - bible_names):
        issues.append(f"[{name}] 定妆 {orphan} 在人物谱里查不到，是孤儿记录")

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

    return issues


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", default=None)
    args = parser.parse_args()

    conn = sqlite3.connect(DB)
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
