"""人物卡不得用通称命名（2A）：「老人」→「守墓老人」，原称谓登记为别名。

真实事故（2026-09-02《神墓》）：映射台按称谓「老人」「孩子」「神秘人」各建了一张卡，后续每集里的同称谓
都与这些卡冲突（ERR-20260902-ba850c）。现在评估模型必须给出 canonical_name，代码只在两种可核验形态下
采用：在原称谓上加限定（包含原称谓且更长），或原文片段里逐字出现的真名；其它一律沿用称谓。
"""
from __future__ import annotations

import asyncio
import json
import sqlite3

from app import portraits
from app.portraits.card_merge import accepted_card_name, resolve_card_name
from app.schemas import Bible, Character, World
from tests.conftest import patch_portraits_everywhere

CHAPTER = "老人拄着一条拐杖颤颤巍巍向他走来。老人是这片神魔陵园的守墓人。老人叹了口气。" * 3


def test_accepted_card_name_rules() -> None:
    fragments = "老人拄着拐杖走来。众人都叫他守墓人。"
    assert accepted_card_name("老人", "守墓老人", fragments) == "守墓老人"  # 加限定：包含原称谓且更长
    assert accepted_card_name("老人", "张三", fragments) == "老人"  # 凭空起名：不采信
    assert accepted_card_name("老人", "守墓人", fragments) == "守墓人"  # 原文逐字出现的名字
    assert accepted_card_name("老人", "", fragments) == "老人"
    assert accepted_card_name("刘备", "刘备", fragments) == "刘备"
    assert accepted_card_name("老人", "老", fragments) == "老人"  # 缩短：不采信


def _conn(*characters: Character) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE projects(id TEXT PRIMARY KEY, bible_json TEXT, bible_version INTEGER DEFAULT 0)")
    conn.execute("CREATE TABLE chapters(project_id TEXT, idx INTEGER, content TEXT)")
    conn.execute("CREATE TABLE episodes(project_id TEXT, episode_no INTEGER, source_chapters TEXT)")
    conn.execute(
        "CREATE TABLE character_portraits(id TEXT, project_id TEXT, character_name TEXT, ep_start INTEGER, "
        "ep_end INTEGER, appearance TEXT, prompt TEXT, image_path TEXT, base_portrait_id TEXT, "
        "bible_version INTEGER, created_at REAL)"
    )
    bible = Bible(world=World(visual_style_canonical="国风"), characters=list(characters))
    conn.execute("INSERT INTO projects(id, bible_json, bible_version) VALUES('p1', ?, 1)", (json.dumps(bible.model_dump(), ensure_ascii=False),))
    conn.execute("INSERT INTO episodes(project_id, episode_no, source_chapters) VALUES('p1', 1, '[1]')")
    conn.execute("INSERT INTO chapters(project_id, idx, content) VALUES('p1', 1, ?)", (CHAPTER,))
    conn.commit()
    return conn


def _patch(monkeypatch, conn) -> None:
    settings: dict[str, str] = {}
    patch_portraits_everywhere(monkeypatch, "get_conn", lambda: conn)
    patch_portraits_everywhere(monkeypatch, "get_setting", lambda k: settings.get(k))
    patch_portraits_everywhere(monkeypatch, "set_setting", lambda k, v: settings.__setitem__(k, v))


def test_resolve_card_name_reuses_existing_card_and_registers_label_as_alias(monkeypatch) -> None:
    conn = _conn(Character(name="守墓老人", role="重要配角", appearance_canonical="白须老者，灰布粗衣，拄木杖"))
    _patch(monkeypatch, conn)
    result = asyncio.run(resolve_card_name(conn, "p1", "老人", {"canonical_name": "守墓老人"}, CHAPTER, {1: CHAPTER}, None))
    assert result == {"status": "exists", "name": "守墓老人"}
    bible = Bible.model_validate(json.loads(conn.execute("SELECT bible_json FROM projects").fetchone()[0]))
    alias = next(a for a in bible.characters[0].aliases if a.text == "老人")
    assert alias.is_exclusive is False and "老人" in alias.evidence_quote


def test_ensure_character_card_builds_under_distinctive_name_with_label_alias(monkeypatch) -> None:
    conn = _conn()
    _patch(monkeypatch, conn)

    async def fake_assess(name, fragments, *, style, known_names, ep_label, **_kwargs):
        assert name == "老人"
        return {"subject_kind": "person", "important": True, "canonical_name": "守墓老人", "reason": "反复出场",
                "role": "重要配角", "appearance_canonical": "白须老者，灰布粗衣，拄木杖，面容枯瘦皱纹深刻，身形瘦弱佝偻",
                "personality": "沉默", "speech_style": "缓慢", "relationships": []}

    patch_portraits_everywhere(monkeypatch, "assess_new_character", fake_assess)
    result = asyncio.run(portraits.ensure_character_card("p1", "老人", 1, generate_portrait=False))

    assert result["status"] == "added" and result["name"] == "守墓老人"
    bible = Bible.model_validate(json.loads(conn.execute("SELECT bible_json FROM projects").fetchone()[0]))
    names = [c.name for c in bible.characters]
    assert names == ["守墓老人"]
    assert [a.text for a in bible.characters[0].aliases] == ["老人"]
    assert bible.characters[0].aliases[0].is_exclusive is False
