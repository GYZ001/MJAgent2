"""道具图按世界书别名归正名查、没有区间覆盖时回退到最早登记（2026-09-05 第 5 集分镜台占位）。

并行跑集时第 14 集先登记了「凝灵丹」（ep_start=14），第 5 集按区间查不到就显示「物」占位；
「灵石」在世界书里是「半块灵石」的别名，按字面查也查不到。道具的样子不取决于它在第几集被
登记，别名与正名指同一件东西。
"""
from __future__ import annotations

import json

from app.db import get_conn
from app.props.store import ensure_schema, prop_reference_for_episode, upsert_prop_reference


def _seed() -> None:
    conn = get_conn()
    ensure_schema()
    bible = {"characters": [], "scenes": [], "props": [
        {"name": "半块灵石", "aliases": ["灵石"], "appearance_canonical": "白玉质地"},
        {"name": "凝灵丹", "aliases": [], "appearance_canonical": "乳白釉层"},
    ]}
    conn.execute("INSERT INTO projects(id,name,bible_json,created_at) VALUES('p','P',?,0)",
                 (json.dumps(bible, ensure_ascii=False),))
    conn.commit()
    upsert_prop_reference(conn, "p", "半块灵石", 7, appearance="白玉", image_path="/tmp/lingshi.png",
                          prompt="x", status="ready", qa={})
    upsert_prop_reference(conn, "p", "凝灵丹", 14, appearance="乳白", image_path="/tmp/dan.png",
                          prompt="x", status="ready", qa={})
    conn.commit()


def test_alias_resolves_to_canonical_prop_reference() -> None:
    _seed()
    row = prop_reference_for_episode(get_conn(), "p", "灵石", 9)
    assert row is not None and row["prop_name"] == "半块灵石"


def test_episode_before_first_registration_falls_back_to_earliest_reference() -> None:
    _seed()
    row = prop_reference_for_episode(get_conn(), "p", "凝灵丹", 5)
    assert row is not None and row["ep_start"] == 14


def test_unknown_prop_still_returns_none() -> None:
    _seed()
    assert prop_reference_for_episode(get_conn(), "p", "紫阳剑", 5) is None
