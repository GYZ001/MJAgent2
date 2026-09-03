"""app.production.prep_pack.timeline_segments.attach_episode_timeline（WS9）。

覆盖：
1. 端到端：episode_prep_pack payload 拿到 timeline.segments（按段号匹配）与
   timeline.era；world.era 被写回 bible_json。
2. 按章缓存：章节内容不变时第二次调用不重新发起提取（不重复调用模型）；
   章节内容变化后重新提取。
3. fail-soft：提取抛异常时原样返回 payload，不向上抛。
"""
from __future__ import annotations

import json

import pytest

from app import db
from app.portraits.timeline_anchors import TimelineAnchor
from app.production.prep_pack import timeline_segments as ts
from app.source_chapters import _episode_chapters, _episode_source_blocks


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "prep-pack-timeline.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()


def _seed_project_and_chapter(conn, *, project_id="p1", content="他八岁那年第一次走进土场。") -> str:
    conn.execute(
        "INSERT INTO projects(id,name,bible_json,created_at) VALUES(?,?,?,?)",
        (project_id, "timeline fixture", json.dumps({"world": {"era": "", "genre": "", "visual_style_canonical": ""}}), db.now()),
    )
    conn.execute(
        "INSERT INTO chapters(project_id, idx, title, content) VALUES(?,?,?,?)",
        (project_id, 1, "第一章", content),
    )
    conn.commit()
    chapter_rows = _episode_chapters(conn, {"source_chapters": [1], "project_id": project_id})
    source_text, _offsets = _episode_source_blocks(chapter_rows)
    return source_text


def _fake_extract(anchors_by_chapter: dict[int, list[TimelineAnchor]], calls: list[int]):
    async def fake(chapter, **kwargs):
        idx = int(chapter["idx"])
        calls.append(idx)
        return anchors_by_chapter.get(idx, [])
    return fake


async def test_attach_episode_timeline_matches_segments_and_writes_era(monkeypatch):
    conn = db.get_conn()
    source_text = _seed_project_and_chapter(conn)
    anchors = [TimelineAnchor(kind="age", value="八岁", subject="他", evidence="他八岁那年", chapter_index=1)]
    calls: list[int] = []
    monkeypatch.setattr(ts, "extract_chapter_timeline_anchors", _fake_extract({1: anchors}, calls))

    payload = await ts.attach_episode_timeline(
        {"episode_no": 1}, project_id="p1", chapter_indexes=[1], source_text=source_text, conn=conn,
    )

    assert calls == [1]
    timeline = payload["timeline"]
    assert timeline["segments"] == [{
        "index": 1,
        "anchors": [{
            "kind": "age", "value": "八岁", "subject": "他", "evidence": "他八岁那年",
            "chapter_index": 1, "anchor_key": "age:8", "label": "8岁",
        }],
    }]
    assert timeline["era"] == ""  # 只有一条 age 锚点，没有 year/era，era 保持空（不臆造）
    bible = json.loads(conn.execute("SELECT bible_json FROM projects WHERE id='p1'").fetchone()["bible_json"])
    assert bible["world"]["era"] == ""  # 同上：没有可推导出的 era，不写


async def test_attach_episode_timeline_writes_era_from_year_anchor(monkeypatch):
    conn = db.get_conn()
    source_text = _seed_project_and_chapter(conn, content="那一年是2004年，一切开始了。")
    anchors = [TimelineAnchor(kind="year", value="2004年", evidence="那一年是2004年", chapter_index=1)]
    monkeypatch.setattr(ts, "extract_chapter_timeline_anchors", _fake_extract({1: anchors}, []))

    payload = await ts.attach_episode_timeline(
        {}, project_id="p1", chapter_indexes=[1], source_text=source_text, conn=conn,
    )

    assert payload["timeline"]["era"] == "2004年"
    bible = json.loads(conn.execute("SELECT bible_json FROM projects WHERE id='p1'").fetchone()["bible_json"])
    assert bible["world"]["era"] == "2004年"


async def test_attach_episode_timeline_caches_unchanged_chapter_across_calls(monkeypatch):
    conn = db.get_conn()
    source_text = _seed_project_and_chapter(conn)
    anchors = [TimelineAnchor(kind="age", value="八岁", subject="他", evidence="他八岁那年", chapter_index=1)]
    calls: list[int] = []
    monkeypatch.setattr(ts, "extract_chapter_timeline_anchors", _fake_extract({1: anchors}, calls))

    await ts.attach_episode_timeline({}, project_id="p1", chapter_indexes=[1], source_text=source_text, conn=conn)
    await ts.attach_episode_timeline({}, project_id="p1", chapter_indexes=[1], source_text=source_text, conn=conn)

    assert calls == [1]  # 第二次未重新发起模型调用


async def test_attach_episode_timeline_reextracts_after_chapter_content_changes(monkeypatch):
    conn = db.get_conn()
    source_text = _seed_project_and_chapter(conn)
    anchors = [TimelineAnchor(kind="age", value="八岁", subject="他", evidence="他八岁那年", chapter_index=1)]
    calls: list[int] = []
    monkeypatch.setattr(ts, "extract_chapter_timeline_anchors", _fake_extract({1: anchors}, calls))
    await ts.attach_episode_timeline({}, project_id="p1", chapter_indexes=[1], source_text=source_text, conn=conn)

    conn.execute("UPDATE chapters SET content=? WHERE project_id='p1' AND idx=1", ("他九岁那年又去了土场。",))
    conn.commit()
    chapter_rows = _episode_chapters(conn, {"source_chapters": [1], "project_id": "p1"})
    new_source_text, _ = _episode_source_blocks(chapter_rows)
    await ts.attach_episode_timeline(
        {}, project_id="p1", chapter_indexes=[1], source_text=new_source_text, conn=conn,
    )

    assert calls == [1, 1]  # 内容变了，第二次重新提取


async def test_attach_episode_timeline_fails_soft_on_extraction_error(monkeypatch):
    conn = db.get_conn()
    source_text = _seed_project_and_chapter(conn)

    async def boom(chapter, **kwargs):
        raise RuntimeError("model gateway exploded")

    monkeypatch.setattr(ts, "extract_chapter_timeline_anchors", boom)
    payload = {"episode_no": 1}
    result = await ts.attach_episode_timeline(
        payload, project_id="p1", chapter_indexes=[1], source_text=source_text, conn=conn,
    )
    assert result == payload
    assert "timeline" not in result
