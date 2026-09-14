"""prep_pack 2.0.x 清单按身份键收录时，准备包对照审计不得误报「未收录」。

拆自 tests/test_episode_source_audit.py（该文件已在行数基线上限）；夹具助手直接复用那边的。
"""
from __future__ import annotations

from tests.test_episode_source_audit import (
    _base_pack,
    _insert_chapter,
    _insert_character,
    _insert_episode,
    _insert_pack_artifact,
    _insert_project,
    _make_db,
    _writer,
    audit,
)


def test_identity_keyed_manifest_counts_registered_character_as_covered(tmp_path):
    """prep_pack 2.0.x 清单按 identity_id 收录、portrait_id 留空：不能再报 B1_character_missing
    （2026-09-14 第 1–3 集实测：四个主角全被误报「未收录」）。"""
    db_path = _make_db(tmp_path)
    conn = _writer(db_path)
    _insert_project(conn, "proj_c")
    _insert_chapter(
        conn, "proj_c", 1, "第一章",
        "许清站在广场上，看着人群。\n\n王有材：“等等我！”他快步追了上去。",
    )
    _insert_character(conn, "portrait_xu", "proj_c", "许清")
    _insert_character(conn, "portrait_wang", "proj_c", "王有材")
    pack = _base_pack(
        chapter_indexes=[1],
        characters=[
            {"identity_id": "bible:许清", "display_name": "许清", "portrait_id": "portrait_xu",
             "event_ids": ["ev_001"], "aliases": []},
            {"identity_id": "bible:王有材", "portrait_id": None},
        ],
        scenes=[],
        event_chain=[{
            "event_id": "ev_001", "order": 1, "summary": "许清站在广场上",
            "source_span": {"from_segment": 1, "to_segment": 1},
            "source_evidence": [{"segment_index": 1, "quote": "许清站在广场上，看着人群。"}],
            "key_lines": [],
        }],
    )
    _insert_episode(conn, "ep_c1", "proj_c", 1, [1], "art_c1")
    _insert_pack_artifact(conn, "art_c1", "ep_c1", pack)
    conn.commit()
    conn.close()

    ro = audit.readonly_connection(db_path)
    result = audit.audit_episode(ro, "proj_c", 1)
    ro.close()

    assert [i.code for i in result.b_issues if i.code == "B1_character_missing"] == []
