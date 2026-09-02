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


# ---------------------------------------------------------------------------
# apply（2026-09-02，用户拍板「按经验做，测试数据随时可删」）
# ---------------------------------------------------------------------------
import asyncio  # noqa: E402

from app.bible_store import mutate_bible_json  # noqa: E402
from app.schemas import Bible, CharacterAlias  # noqa: E402


def merge_card(conn: sqlite3.Connection, project_id: str, source: str, target: str) -> str:
    """把 ``source`` 卡并入 ``target``：source 登记为 target 的别名（非独占），删 source 卡及其定妆照记录。"""
    def mutate(data: dict) -> bool:
        chars = data.get("characters", [])
        src = next((c for c in chars if c.get("name") == source), None)
        tgt = next((c for c in chars if c.get("name") == target), None)
        if src is None or tgt is None:
            return False
        texts = {a.get("text") for a in tgt.get("aliases", [])}
        if source not in texts:
            tgt.setdefault("aliases", []).append(CharacterAlias(
                text=source, name_kind="referential", evidence_chapter_index=1,
                evidence_quote=f"人物卡迁移：「{source}」与「{target}」为同一人", is_exclusive=False,
            ).model_dump(mode="json"))
        for alias in src.get("aliases", []):
            if alias.get("text") not in texts and alias.get("text") != target:
                tgt["aliases"].append(alias)
        chars.remove(src)
        Bible.model_validate(data)
        return True

    if not mutate_bible_json(conn, project_id, mutate):
        return f"skip merge {source}->{target}（卡不存在）"
    ids = [r[0] for r in conn.execute("SELECT id FROM character_portraits WHERE project_id=? AND character_name=?", (project_id, source))]
    for pid in ids:
        conn.execute("DELETE FROM character_portrait_views WHERE portrait_id=?", (pid,))
    conn.execute("DELETE FROM character_portraits WHERE project_id=? AND character_name=?", (project_id, source))
    conn.commit()
    return f"merged {source} -> {target}（删除定妆照记录 {len(ids)} 条）"


def rename_card(conn: sqlite3.Connection, project_id: str, old: str, new: str) -> str:
    """把卡 ``old`` 改名为 ``new``，``old`` 登记为非独占别名；定妆照记录同步改名。"""
    def mutate(data: dict) -> bool:
        chars = data.get("characters", [])
        if any(c.get("name") == new for c in chars):
            raise ValueError(f"目标名「{new}」已存在，请改用 merge")
        card = next((c for c in chars if c.get("name") == old), None)
        if card is None:
            return False
        card["name"] = new
        if old not in {a.get("text") for a in card.get("aliases", [])}:
            card.setdefault("aliases", []).append(CharacterAlias(
                text=old, name_kind="referential", evidence_chapter_index=1,
                evidence_quote=f"人物卡迁移：原卡名「{old}」为通称，改为「{new}」", is_exclusive=False,
            ).model_dump(mode="json"))
        Bible.model_validate(data)
        return True

    if not mutate_bible_json(conn, project_id, mutate):
        return f"skip rename {old}（卡不存在）"
    conn.execute(
        "UPDATE character_portraits SET character_name=?, visual_entity_id=CASE WHEN visual_entity_id=? THEN ? ELSE visual_entity_id END "
        "WHERE project_id=? AND character_name=?",
        (new, f"bible:{old}", f"bible:{new}", project_id, old),
    )
    conn.commit()
    return f"renamed {old} -> {new}"


async def suggest_name(conn: sqlite3.Connection, project_id: str, label: str) -> str:
    """用新上线的建卡命名契约让模型给出有区分度的名字；只采信 accepted_card_name 认可的形态。"""
    from app.portraits.card_merge import accepted_card_name
    from app.portraits.cards import assess_new_character
    from app.portraits.discovery_fragments import _forward_fragments

    row = conn.execute("SELECT bible_json FROM projects WHERE id=?", (project_id,)).fetchone()
    bible = Bible.model_validate(json.loads(row["bible_json"]))
    fragments, ep_label, chapters_by_idx = _forward_fragments(conn, project_id, label, 1)
    if not fragments:
        return label
    verdict = await assess_new_character(
        label, fragments, style=bible.world.visual_style_canonical,
        known_names=[c.name for c in bible.characters if c.name != label], ep_label=ep_label,
        require_identity_card=True, chapters_by_idx=chapters_by_idx,
    )
    return accepted_card_name(label, verdict.get("canonical_name"), fragments)


def apply_plan(conn: sqlite3.Connection, plan: list[tuple[str, str, str, str]]) -> None:
    """plan 条目：(project_id, action, source/old, target/new)；target 为空的 rename 先向模型要名字。"""
    for project_id, action, source, target in plan:
        if action == "merge":
            print(merge_card(conn, project_id, source, target))
        elif action == "rename":
            new = target or asyncio.run(suggest_name(conn, project_id, source))
            if new == source:
                print(f"keep {source}（模型未给出可核验的新名，保留）")
            else:
                print(rename_card(conn, project_id, source, new))
