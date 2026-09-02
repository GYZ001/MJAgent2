"""字/改名归位：「长生」不是新角色，是「关羽」的字（2026-09-02《三国演义》旧项目一人两卡）。

第一回里「长生」先于「关羽」出现：旧流程在候选集里没有关羽时建了「长生」卡，随后「关羽」出现，
合并判定选中「长生」却因钉证要求引句逐字含「关羽」（原文写「姓关，名羽」）被拒，又建了「关羽」卡。
现在：原文显式介绍句是最强的身份链接证据——建卡前先归位，钉证也接受介绍句形态。
"""
from __future__ import annotations

import asyncio
import json
import sqlite3

from app.portraits import card_merge
from app.schemas import Bible, Character, World
from tests.conftest import patch_portraits_everywhere

INTRO = "其人曰：“吾姓关，名羽，字长生，后改云长，河东解良人也。”"


def _conn(bible: Bible, chapter: str) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE projects(id TEXT PRIMARY KEY, bible_json TEXT, bible_version INTEGER DEFAULT 0)")
    conn.execute("CREATE TABLE chapters(project_id TEXT, idx INTEGER, content TEXT)")
    conn.execute("INSERT INTO projects(id, bible_json, bible_version) VALUES('p1', ?, 1)", (json.dumps(bible.model_dump(), ensure_ascii=False),))
    conn.execute("INSERT INTO chapters VALUES('p1', 1, ?)", (chapter,))
    conn.commit()
    return conn


def _bible(*names: str) -> Bible:
    return Bible(
        characters=[Character(name=n, role="重要配角", appearance_canonical="红脸长须，绿袍") for n in names],
        world=World(visual_style_canonical="国漫风格"),
    )


def test_courtesy_name_redirects_to_existing_card_and_registers_alias(monkeypatch) -> None:
    conn = _conn(_bible("关羽"), INTRO)
    patch_portraits_everywhere(monkeypatch, "get_conn", lambda: conn)

    result = asyncio.run(card_merge.courtesy_name_redirect(conn, "p1", "长生", {1: INTRO}, None))

    assert result == {"status": "exists", "name": "关羽"}
    bible = Bible.model_validate(json.loads(conn.execute("SELECT bible_json FROM projects").fetchone()[0]))
    alias = next(a for a in bible.characters[0].aliases if a.text == "长生")
    assert alias.name_kind == "courtesy_name" and alias.is_exclusive is True
    assert "姓关，名羽，字长生" in alias.evidence_quote


def test_courtesy_name_without_owner_card_returns_full_name_to_build_under() -> None:
    conn = _conn(_bible(), INTRO)
    assert asyncio.run(card_merge.courtesy_name_redirect(conn, "p1", "长生", {1: INTRO}, None)) == "关羽"
    assert asyncio.run(card_merge.courtesy_name_redirect(conn, "p1", "云长", {1: INTRO}, None)) == "关羽"


def test_plain_labels_are_not_redirected() -> None:
    conn = _conn(_bible("关羽"), INTRO)
    assert asyncio.run(card_merge.courtesy_name_redirect(conn, "p1", "关羽", {1: INTRO}, None)) is None
    assert asyncio.run(card_merge.courtesy_name_redirect(conn, "p1", "老人", {1: "老人拄着拐杖走来。"}, None)) is None


def test_merge_pin_accepts_introduction_sentence_as_link_evidence(monkeypatch) -> None:
    """合并判定选中「关羽」、钉在「姓关，名羽，字长生」那段：全名两字不相连，介绍句本身就是证据。"""
    chapter = INTRO + "\n后来长生与玄德结为兄弟。"
    conn = _conn(_bible("关羽"), chapter)
    patch_portraits_everywhere(monkeypatch, "get_conn", lambda: conn)

    async def fake_verdict(*, label, dossier, candidates):
        return card_merge._CardMergeVerdictResponse(selected_candidate="关羽", supporting_entry_index=1, supporting_quote="")

    monkeypatch.setattr(card_merge, "_card_merge_verdict", fake_verdict)
    dossier, _chapters = card_merge._card_merge_dossier(conn, "p1", "长生")
    assert "姓关，名羽" in dossier[0]["text"]

    merged = asyncio.run(card_merge.resolve_card_merge_target(conn, "p1", "长生", _bible("关羽")))

    assert merged is not None
    owner, alias = merged
    assert owner == "关羽" and alias["text"] == "长生"
