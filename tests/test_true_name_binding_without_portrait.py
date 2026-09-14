"""真名假设的目标没有定妆照也能绑定（2026-09-14）：出图解耦到后台后，同轮映射刚建的卡此刻没图，
旧判据 _resolve_portrait_id 为 None 就整段放弃真名假设，与 persistent_appellation 的「没图不绑」是同一族缺陷。
"""
from __future__ import annotations

import json
import sqlite3

from app import db
from app.production.prep_pack.asset_lookup import _resolve_character_binding
from app.schemas import Bible, Character, World


def _conn(bible: dict | None, *, portrait: bool) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    for statement in db.MIGRATIONS:
        try:
            conn.execute(statement)
        except sqlite3.OperationalError:
            pass
    conn.execute(
        "INSERT INTO projects(id,name,created_at,bible_json) VALUES('p','P',0,?)",
        (json.dumps(bible, ensure_ascii=False) if bible is not None else None,),
    )
    if portrait:
        conn.execute(
            "INSERT INTO character_portraits(id,project_id,character_name,ep_start,ep_end,created_at) "
            "VALUES('cp1','p','曹阳',1,NULL,0)",
        )
    conn.commit()
    return conn


_BIBLE = Bible(
    world=World(visual_style_canonical="国风"), characters=[Character(name="曹阳", role="反派", appearance_canonical="身材高大")],
).model_dump(mode="json")


def test_card_without_portrait_is_a_valid_true_name_target() -> None:
    assert _resolve_character_binding(_conn(_BIBLE, portrait=False), "p", "曹阳", 1) == "bible:曹阳"


def test_portrait_id_wins_when_present() -> None:
    assert _resolve_character_binding(_conn(_BIBLE, portrait=True), "p", "曹阳", 1) == "cp1"


def test_unknown_name_or_missing_bible_is_not_a_target() -> None:
    assert _resolve_character_binding(_conn(_BIBLE, portrait=False), "p", "许师姐", 1) is None
    assert _resolve_character_binding(_conn(None, portrait=False), "p", "曹阳", 1) is None
