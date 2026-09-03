"""WS9：分镜台落库时把映射台 ``payload["timeline"]["segments"]`` 接进
``shots.scene_time`` 与 ``storyboard_pack_segment.timeline_anchors``。

覆盖：
1. ``_segment_matched_timeline_anchors``/``_timeline_anchor_scene_time`` 两个
   纯函数（去重合并、kind 优先级、无锚点时保持空串）。
2. ``persist_storyboard_pack`` 端到端：payload 带 timeline 时 scene_time 写入
   逐字锚点值，storyboard_pack_segment.timeline_anchors 携带完整锚点详情；
   payload 不带 timeline（旧数据/未接线路径）时行为不变，scene_time 仍是空串。
"""
from __future__ import annotations

import json

from app import db
from app.production.storyboard_pack import (
    _segment_matched_timeline_anchors,
    _timeline_anchor_scene_time,
    persist_storyboard_pack,
)
from tests.test_storyboard_pack import _pack, _prep_pack_2_0_0_payload, _real_segments, _seed_episode


def test_segment_matched_timeline_anchors_dedupes_across_overlapping_segments():
    timeline_by_segment = {
        1: [{"kind": "age", "value": "八岁", "chapter_index": 1, "anchor_key": "age:8"}],
        2: [{"kind": "age", "value": "八岁", "chapter_index": 1, "anchor_key": "age:8"}],
    }
    matched = _segment_matched_timeline_anchors(timeline_by_segment, [1, 2])
    assert matched == [{"kind": "age", "value": "八岁", "chapter_index": 1, "anchor_key": "age:8"}]


def test_segment_matched_timeline_anchors_empty_when_no_coverage():
    assert _segment_matched_timeline_anchors({}, [1, 2]) == []


def test_timeline_anchor_scene_time_empty_without_anchors():
    assert _timeline_anchor_scene_time([]) == ""


def test_timeline_anchor_scene_time_prefers_age_over_era():
    anchors = [
        {"kind": "era", "value": "东汉末年"},
        {"kind": "age", "value": "八岁"},
    ]
    assert _timeline_anchor_scene_time(anchors) == "八岁"


def _isolated_db(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "storyboard-pack-timeline.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()


def test_persist_storyboard_pack_writes_scene_time_from_matched_timeline_anchor(tmp_path, monkeypatch):
    _isolated_db(tmp_path, monkeypatch)
    conn = db.get_conn()
    episode_id = "ep-pack-timeline-1"
    _seed_episode(conn, episode_id=episode_id)
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    payload = _prep_pack_2_0_0_payload()
    payload["timeline"] = {
        "era": "2004年",
        "segments": [
            {
                "index": 1,
                "anchors": [{
                    "kind": "age", "value": "八岁", "subject": "少年", "evidence": "他八岁那年",
                    "chapter_index": 1, "anchor_key": "age:8", "label": "8岁",
                }],
            },
        ],
    }
    segments = _real_segments(conn, ep)
    persist_storyboard_pack(conn, episode_id, ep, payload, _pack(), segments=segments)

    row = conn.execute("SELECT * FROM shots WHERE episode_id=?", (episode_id,)).fetchone()
    assert row["scene_time"] == "八岁"
    segment_record = json.loads(row["shot_contract_json"])["storyboard_pack_segment"]
    assert segment_record["timeline_anchors"] == [{
        "kind": "age", "value": "八岁", "subject": "少年", "evidence": "他八岁那年",
        "chapter_index": 1, "anchor_key": "age:8", "label": "8岁",
    }]


def test_persist_storyboard_pack_scene_time_stays_empty_without_timeline_field(tmp_path, monkeypatch):
    _isolated_db(tmp_path, monkeypatch)
    conn = db.get_conn()
    episode_id = "ep-pack-timeline-2"
    _seed_episode(conn, episode_id=episode_id)
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    payload = _prep_pack_2_0_0_payload()  # 没有 "timeline" 键，模拟旧 payload/未接线路径
    segments = _real_segments(conn, ep)
    persist_storyboard_pack(conn, episode_id, ep, payload, _pack(), segments=segments)

    row = conn.execute("SELECT * FROM shots WHERE episode_id=?", (episode_id,)).fetchone()
    assert row["scene_time"] == ""
    segment_record = json.loads(row["shot_contract_json"])["storyboard_pack_segment"]
    assert segment_record["timeline_anchors"] == []
