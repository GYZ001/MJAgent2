"""``app.portraits.card_rebind.rebind_character_card`` 与
``app.portraits.portrait_io._append_character_to_bible`` 的别名感知去重回归。

任务范围：真名揭示后旧称谓卡就地改名，不新建第二张卡（见
``app/portraits/card_rebind.py`` 模块 docstring）；以及 ``_append_character_
to_bible`` 收敛到 ``card_owner.resolve_card_owner`` 后不再对别名标签重复建卡。
"""

import asyncio
import json
import sqlite3

import pytest

from app.errors import ContentGenerationError
from app.portraits.card_rebind import rebind_character_card
from app.portraits.portrait_io import _append_character_to_bible
from app.schemas import Bible, Character, CharacterAlias, World
from tests.conftest import patch_portraits_everywhere


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE projects(id TEXT PRIMARY KEY, bible_json TEXT, "
        "bible_version INTEGER DEFAULT 0)"
    )
    conn.execute(
        "CREATE TABLE character_portraits(id TEXT, project_id TEXT, "
        "character_name TEXT, ep_start INTEGER, ep_end INTEGER, "
        "appearance TEXT, prompt TEXT, image_path TEXT, "
        "base_portrait_id TEXT, bible_version INTEGER, created_at REAL)"
    )
    return conn


def _seed_bible_project(conn: sqlite3.Connection, bible: Bible, *, version: int = 1) -> None:
    conn.execute(
        "INSERT INTO projects(id, bible_json, bible_version) VALUES('p1', ?, ?)",
        (json.dumps(bible.model_dump(), ensure_ascii=False), version),
    )
    conn.commit()


def test_rebind_character_card_renames_and_preserves_portrait_assets(monkeypatch) -> None:
    """改绑必须原地保留定妆资产：ref_image_path/portrait_prompt_override 不动，
    character_portraits 的当前生效行随改名同步，负数 ep_start 的作废历史槽位
    必须保持原样不被卷入（见模块 docstring 的 ep_start>=0 约束）。"""
    conn = _make_conn()
    _seed_bible_project(conn, Bible(
        world=World(visual_style_canonical="国风"),
        characters=[Character(
            name="许师姐", role="重要配角", appearance_canonical="青衣女子，剑眉星目",
            ref_image_path="/assets/xu.jpg",
            portrait_prompt_override="国风水墨，青衣女子",
        )],
    ))
    conn.execute(
        "INSERT INTO character_portraits VALUES"
        "('port1','p1','许师姐',3,NULL,'anchor','prompt','/assets/xu.jpg',NULL,1,0)"
    )
    conn.execute(
        "INSERT INTO character_portraits VALUES"
        "('port0','p1','许师姐',-1,0,'stale anchor','stale prompt','/assets/xu_old.jpg',NULL,1,0)"
    )
    conn.commit()
    patch_portraits_everywhere(monkeypatch, "get_conn", lambda: conn)

    ok = asyncio.run(rebind_character_card("p1", "许师姐", "许清"))

    assert ok is True
    row = conn.execute("SELECT bible_json, bible_version FROM projects WHERE id='p1'").fetchone()
    assert row["bible_version"] == 2
    data = json.loads(row["bible_json"])
    assert len(data["characters"]) == 1
    character = data["characters"][0]
    assert character["name"] == "许清"
    assert character["ref_image_path"] == "/assets/xu.jpg"
    assert character["portrait_prompt_override"] == "国风水墨，青衣女子"
    alias_texts = {a["text"]: a for a in character["aliases"]}
    assert "许师姐" in alias_texts
    assert alias_texts["许师姐"]["is_exclusive"] is False

    current = conn.execute(
        "SELECT character_name FROM character_portraits WHERE id='port1'"
    ).fetchone()
    assert current["character_name"] == "许清"
    stale = conn.execute(
        "SELECT character_name FROM character_portraits WHERE id='port0'"
    ).fetchone()
    assert stale["character_name"] == "许师姐"  # 作废历史槽位不参与改名


def test_rebind_character_card_fails_closed_when_target_name_already_owned(monkeypatch) -> None:
    """目标真名在人物谱里已有归属时，这是"两张卡其实是同一个人"的合并信号，
    不是本原语的职责——必须 fail closed 报可见错误，旧卡原封不动（不是悄悄
    跳过也不是猜哪张卡该留下）。"""
    conn = _make_conn()
    _seed_bible_project(conn, Bible(
        world=World(visual_style_canonical="国风"),
        characters=[
            Character(
                name="许师姐", role="重要配角", appearance_canonical="青衣女子",
                ref_image_path="/assets/xu.jpg",
            ),
            Character(
                name="许清", role="重要配角", appearance_canonical="另一位角色，与许师姐同名冲突",
            ),
        ],
    ))
    patch_portraits_everywhere(monkeypatch, "get_conn", lambda: conn)

    with pytest.raises(ContentGenerationError):
        asyncio.run(rebind_character_card("p1", "许师姐", "许清"))

    row = conn.execute("SELECT bible_json, bible_version FROM projects WHERE id='p1'").fetchone()
    assert row["bible_version"] == 1  # 未被改写
    data = json.loads(row["bible_json"])
    names = {c["name"] for c in data["characters"]}
    assert names == {"许师姐", "许清"}  # 两张卡都原封不动


def test_rebind_character_card_allows_self_promotion_of_own_unverified_alias(
    monkeypatch,
) -> None:
    """目标真名若只是这张卡自己已登记的一条别名（如共现回填通道先记了这条
    未核验别名），owner 恰好就是 from_label 本身——这不是两张卡合并，只是把
    别名提升为主名，必须放行且不留重复别名。"""
    conn = _make_conn()
    _seed_bible_project(conn, Bible(
        world=World(visual_style_canonical="国风"),
        characters=[Character(
            name="许师姐", role="重要配角", appearance_canonical="青衣女子",
            aliases=[CharacterAlias(
                text="许清", name_kind="referential", is_exclusive=False,
                evidence_chapter_index=1, evidence_quote="有人唤她许清",
            )],
        )],
    ))
    patch_portraits_everywhere(monkeypatch, "get_conn", lambda: conn)

    ok = asyncio.run(rebind_character_card("p1", "许师姐", "许清"))

    assert ok is True
    data = json.loads(
        conn.execute("SELECT bible_json FROM projects WHERE id='p1'").fetchone()["bible_json"]
    )
    character = data["characters"][0]
    assert character["name"] == "许清"
    alias_texts = [a["text"] for a in character["aliases"]]
    assert alias_texts == ["许师姐"]  # 旧的"许清"别名被回收，不与新主名重复


def test_append_character_to_bible_skips_alias_label_duplicate() -> None:
    """``_append_character_to_bible`` 收敛到 ``resolve_card_owner`` 后，用一个
    已登记为别名（而非 name）的称呼去建卡必须识别出已有归属，不再建出第二张
    卡——这是 5e1f6d7 六处收敛点之外漏下的最后一处防线。"""
    conn = _make_conn()
    _seed_bible_project(conn, Bible(
        world=World(visual_style_canonical="国风"),
        characters=[Character(
            name="李富贵", role="重要配角", appearance_canonical="圆脸胖身",
            aliases=[CharacterAlias(
                text="小胖子", name_kind="honorific",
                evidence_chapter_index=1, evidence_quote="众人都唤他小胖子",
            )],
        )],
    ))

    appended = _append_character_to_bible(conn, "p1", {
        "name": "小胖子", "role": "重要配角", "appearance_canonical": "圆脸胖身",
    })

    assert appended is False
    data = json.loads(
        conn.execute("SELECT bible_json FROM projects WHERE id='p1'").fetchone()["bible_json"]
    )
    assert len(data["characters"]) == 1  # 没有建出第二张卡
