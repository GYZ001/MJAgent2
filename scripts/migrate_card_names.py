"""人物卡命名迁移清单（2A）：找出「字/改名各成一卡」「通称成卡」「一个卡名是另一个的组成部分」。

用法：
    py scripts/migrate_card_names.py --dry-run [--project <id> ...]
只读、只输出建议，不改任何数据。判据全部取自数据：原文里的显式介绍句（app.portraits.name_intro）、
人物谱里现有卡名之间的包含关系、以及各集身份决议里把该卡名记为功能身份（通称）的历史。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import DB_PATH  # noqa: E402
from app.portraits.name_intro import find_name_introductions, intro_owner_of  # noqa: E402


def _proposals_for_project(conn: sqlite3.Connection, project_id: str) -> list[dict]:
    row = conn.execute("SELECT name, bible_json FROM projects WHERE id=?", (project_id,)).fetchone()
    bible = json.loads(row["bible_json"] or "{}")
    names = [str(c.get("name") or "") for c in bible.get("characters", []) if c.get("name")]
    text = "\n".join(
        str(r["content"] or "") for r in conn.execute(
            "SELECT content FROM chapters WHERE project_id=? ORDER BY idx", (project_id,),
        )
    )
    intros = find_name_introductions(text)
    generic: set[str] = set()
    for r in conn.execute(
        "SELECT screenplay_character_resolutions FROM episodes WHERE project_id=? AND screenplay_character_resolutions IS NOT NULL",
        (project_id,),
    ):
        for item in json.loads(r[0] or "[]"):
            if item.get("resolution") == "functional_identity" and item.get("canonical_name") in names:
                generic.add(str(item["canonical_name"]))
    # 身份判别模型自己对该称谓的形态标注（N 分支 name_kind）：非人名（referential/title）却建了卡 → 通称成卡。
    non_personal: set[str] = set()
    for r in conn.execute(
        "SELECT response_json FROM provider_calls WHERE project_id=? AND operation_id LIKE 'screenplay.identity.current%' AND status='OK'",
        (project_id,),
    ):
        try:
            payload = json.loads(json.loads(r[0])["choices"][0]["message"]["content"])
        except (TypeError, ValueError, KeyError, IndexError):
            continue
        for item in payload.get("n") or []:
            label = str(item.get("identity_label") or "").strip()
            if label in names and str(item.get("name_kind") or "") not in ("", "personal_name"):
                non_personal.add(label)
    generic |= non_personal
    proposals: list[dict] = []
    for name in names:
        owner = intro_owner_of(name, intros)
        if owner is not None:
            action = "merge_into" if owner.full_name in names else "rename_to"
            proposals.append({
                "project": row["name"], "card": name, "action": action, "target": owner.full_name,
                "reason": f"原文介绍句「{owner.quote}」表明它是「{owner.full_name}」的字/改名",
            })
            continue
        containers = [other for other in names if other != name and name in other]
        if containers:
            proposals.append({
                "project": row["name"], "card": name, "action": "review_merge", "target": "/".join(containers),
                "reason": f"卡名是「{'/'.join(containers)}」的组成部分，多半是同一人的简称",
            })
        if name in generic:
            proposals.append({
                "project": row["name"], "card": name, "action": "rename_distinctive", "target": "",
                "reason": "身份判别曾把它记为功能身份（通称）；应改成有区分度的固定称谓并把通称登记为别名",
            })
    return proposals


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", default=True)
    parser.add_argument("--project", action="append", default=None)
    args = parser.parse_args()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    if args.project:
        project_ids = args.project
    else:
        project_ids = [r["id"] for r in conn.execute("SELECT id FROM projects WHERE deleted_at IS NULL ORDER BY created_at")]
    total = 0
    for pid in project_ids:
        for item in _proposals_for_project(conn, pid):
            total += 1
            print(f"[{item['project']}] {item['card']} -> {item['action']} {item['target']} | {item['reason']}")
    print(f"共 {total} 条建议（只读，未改动任何数据）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
