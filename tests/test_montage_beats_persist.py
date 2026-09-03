"""WS11：蒙太奇拍点（``montage_beats``）真正落地的回归测试。

根因（WS9 之后发现，从未真正落地过）：``StoryboardPackSegment`` 曾经只有一个
``beats`` 字段——生成期写进模型自报的 ``MontageBeat`` 列表，``persist_storyboard_
pack`` 又把同一个键无条件改写成叙事节拍摘要（``beat_id``/``summary``/
``segment_indexes``），``app.continuity.apply_shot_contract`` 重建
``Shot.beats`` 时读到的永远是摘要形状，产出全是空的 ``MontageBeat``。

覆盖：
1. ``form="montage"`` 的段落，从 pack -> persist -> apply_shot_contract 全链路，
   ``Shot.beats`` 非空且 time_anchor/scene_name/visual/source_span 齐全。
2. 模型没填的 time_anchor 由本段确定性时间线锚点（WS9 ``payload["timeline"]``）
   回填，逐字取值；模型已自报的 time_anchor 不被覆盖。
3. 叙事节拍摘要仍写在 ``beats`` 键，形状与既有测试
   （``test_persist_storyboard_pack_segment_carries_beat_summary_self_contained``）
   完全一致，两个键互不干扰。
4. ``form="scene"`` 的段落零变化（``Shot.beats == []``）。
"""
from __future__ import annotations

import json

from app import db
from app.continuity import apply_shot_contract
from app.production.storyboard_pack import (
    StoryboardPack,
    StoryboardPackBeat,
    StoryboardPackSegment,
    persist_storyboard_pack,
)
from app.schemas import Shot
from tests.test_storyboard_pack import _prep_pack_2_0_0_payload, _real_segments, _seed_episode


def _isolated_db(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "montage-beats-persist.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()


def _montage_pack() -> StoryboardPack:
    return StoryboardPack(
        episode_no=1,
        target_model="seedance_2",
        beat_sheet=[StoryboardPackBeat(beat_id="B1", summary="他的一生", segment_indexes=[1, 2])],
        segments=[
            StoryboardPackSegment(
                segment_no=1, synopsis="他的一生闪回",
                source_segment_indexes=[1],
                beat_ids=["B1"],
                prompt_text="镜头1（约0-8秒）……镜头2（约8-15秒）……",
                shot_count=2,
                dialogue=[], resources={"characters": [], "scenes": [], "props": []},
                degraded_capabilities=[],
                form="montage",
                montage_beats=[
                    {
                        "time_anchor": "", "scene_name": "山顶",
                        "visual": "八岁的自己站在山顶", "source_span": "他八岁那年站在山顶",
                    },
                    {
                        "time_anchor": "三十五岁", "scene_name": "",
                        "visual": "长大后的自己", "source_span": "三十五岁的我",
                    },
                ],
            ),
            StoryboardPackSegment(
                segment_no=2, synopsis="他回过神来",
                source_segment_indexes=[2],
                beat_ids=[],
                prompt_text="占位提示词。",
                shot_count=3,
                dialogue=[], resources={"characters": [], "scenes": [], "props": []},
                degraded_capabilities=[],
            ),
        ],
    )


def _persisted_segment_records(conn, episode_id: str) -> dict[int, dict]:
    rows = conn.execute(
        "SELECT shot_no, shot_contract_json FROM shots WHERE episode_id=? ORDER BY shot_no", (episode_id,),
    ).fetchall()
    return {
        row["shot_no"]: json.loads(row["shot_contract_json"])["storyboard_pack_segment"] for row in rows
    }


def _shot_from_contract(shot_no: int, contract: dict) -> Shot:
    shot = Shot(
        shot_no=shot_no, duration_s=15, shot_size="", camera_move="",
        action_desc="占位", prompt_contract_version="storyboard_pack.v1",
    )
    apply_shot_contract(shot, contract)
    return shot


def test_montage_beats_key_is_not_overwritten_by_beat_summary(tmp_path, monkeypatch):
    _isolated_db(tmp_path, monkeypatch)
    conn = db.get_conn()
    episode_id = "ep-montage-1"
    _seed_episode(conn, episode_id=episode_id)
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    payload = _prep_pack_2_0_0_payload()
    segments = _real_segments(conn, ep)
    persist_storyboard_pack(conn, episode_id, ep, payload, _montage_pack(), segments=segments)

    records = _persisted_segment_records(conn, episode_id)
    montage_beats = records[1]["montage_beats"]
    assert len(montage_beats) == 2
    # 叙事节拍摘要（既有契约字段，见 test_storyboard_pack.py 同名历史用例）
    # 完全不受影响——两个键互不覆盖。
    assert records[1]["beats"] == [{"beat_id": "B1", "summary": "他的一生", "segment_indexes": [1, 2]}]


def test_montage_beat_time_anchor_backfilled_from_timeline_when_model_left_it_blank(tmp_path, monkeypatch):
    _isolated_db(tmp_path, monkeypatch)
    conn = db.get_conn()
    episode_id = "ep-montage-2"
    _seed_episode(conn, episode_id=episode_id)
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    payload = _prep_pack_2_0_0_payload()
    payload["timeline"] = {
        "segments": [{
            "index": 1,
            "anchors": [{
                "kind": "age", "value": "八岁", "subject": "少年", "evidence": "他八岁那年",
                "chapter_index": 1, "anchor_key": "age:8", "label": "8岁",
            }],
        }],
    }
    segments = _real_segments(conn, ep)
    persist_storyboard_pack(conn, episode_id, ep, payload, _montage_pack(), segments=segments)

    montage_beats = _persisted_segment_records(conn, episode_id)[1]["montage_beats"]
    # 拍 1 模型没填 time_anchor，source_span 含锚点原文 "八岁" -> 回填逐字值。
    assert montage_beats[0]["time_anchor"] == "八岁"
    # 拍 2 模型已自报 "三十五岁"，即使没有对应锚点也不得被覆盖或清空。
    assert montage_beats[1]["time_anchor"] == "三十五岁"


def test_apply_shot_contract_rebuilds_nonempty_montage_beats_after_persist(tmp_path, monkeypatch):
    """全链路回归：pack -> persist -> apply_shot_contract 后 Shot.beats 非空，
    time_anchor/scene_name/visual/source_span 四个字段都到位——这正是 WS9 之后
    发现的陈年 bug（``beats`` 键被叙事摘要覆盖）修复前必然全为空对象的地方。"""
    _isolated_db(tmp_path, monkeypatch)
    conn = db.get_conn()
    episode_id = "ep-montage-3"
    _seed_episode(conn, episode_id=episode_id)
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    payload = _prep_pack_2_0_0_payload()
    payload["timeline"] = {
        "segments": [{
            "index": 1,
            "anchors": [{
                "kind": "age", "value": "八岁", "subject": "少年", "evidence": "他八岁那年",
                "chapter_index": 1, "anchor_key": "age:8", "label": "8岁",
            }],
        }],
    }
    segments = _real_segments(conn, ep)
    persist_storyboard_pack(conn, episode_id, ep, payload, _montage_pack(), segments=segments)

    records = _persisted_segment_records(conn, episode_id)
    montage_shot = _shot_from_contract(1, {"storyboard_pack_segment": records[1]})
    assert montage_shot.form == "montage"
    assert len(montage_shot.beats) == 2
    first, second = montage_shot.beats
    assert first.time_anchor == "八岁"
    assert first.scene_name == "山顶"
    assert first.visual == "八岁的自己站在山顶"
    assert first.source_span == "他八岁那年站在山顶"
    assert second.time_anchor == "三十五岁"
    assert second.visual == "长大后的自己"

    # form=scene 段落零变化：既有断言（test_storyboard_pack_wiring.py 同类用例）不受影响。
    scene_shot = _shot_from_contract(2, {"storyboard_pack_segment": records[2]})
    assert scene_shot.form == "scene"
    assert scene_shot.beats == []
