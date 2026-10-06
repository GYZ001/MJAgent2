"""场景状态图：把上一段备忘里"道具此刻在哪、什么状态"画进状态图（2026-10-05，
《顾念长安》proj_ca86b15ab7d7 第1集第2段真实故障：温念拔下插头，场景状态图
四轮重抽都把插头画回插座里，见 ``app.video_modes.scene_state_prop_states``
模块文档）。

覆盖：
① ``prop_state_notes_for_run`` 第1→2段真实形态（同一场景，``_plug_prop()``
  默认文本逐字取自真实故障数据"插头已拔出，在温念右手附近"，不做任何简化
  替换）产出含位置/状态的正面陈述，且带动提示词与指纹变化；
② 上一段换了场景、串首是本集第一段 两种"宁缺不错"情形不取；
③ ``filter_prop_states_for_empty_room``：整条都在讲"被人拿着/穿着"的条目
  （没有任何一句独立空间信息）才剔除；只要有一句不提角色名（常见写法是用
  角色位置做相对空间锚点定位物件本身，真实故障数据就是这种写法）整条保留，
  不因为提了一次角色名就把整条物件状态滤掉（2026-10-05 返工：这正是审查
  发现的缺陷——旧判据会把真实故障数据本身滤掉，核心故障没被修到）；
④ 命中道具卡时带上卡面外观，但不重复场景描述自己已经讲过的那张卡；
⑤ 没有可取条目时提示词/指纹与改动前逐字一致（独立手算期望指纹）；
⑥ 扫描侧（``scan_episode_scene_state_needs``）与装配侧（``resolve_scene_
  state_view_for_shot``）对同一段算出同一个指纹。

不测试真实供应商往返；⑥ 用真实 sqlite（与 ``test_scene_state_assembly.py``
同一套 fixture 手法），其余全部是纯函数输入输出断言。
"""
from __future__ import annotations

import hashlib
import json

import pytest

from app import db
from app.schemas import Bible, Character, Prop, World
from app.video_modes.scene_state_ensure import scene_state_prompt
from app.video_modes.scene_state_prop_states import (
    character_display_names_from_bible,
    filter_prop_states_for_empty_room,
    prop_state_notes_for_run,
)
from app.video_modes.scene_state_views import (
    PROMPT_VERSION,
    load_episode_shot_rows,
    resolve_scene_state_view_for_shot,
    scan_episode_scene_state_needs,
    scene_state_input_fingerprint,
)
from app.video_modes.scene_state_views_store import ensure_tables_on_connection

_SCENE = "scene_ref_a"
_OTHER_SCENE = "scene_ref_b"
_DESC = "深夜断电后的出租屋，雨夜暗蓝光线"


def _row(shot_no: int, scenes: list[dict], continuity_props: list[dict] | None = None) -> dict:
    """照 ``shots`` 表一行的最小形状，与 ``tests/test_scene_state_grouping.py``
    的 ``_row`` 同一套手法：``prop_state_notes_for_run`` 只读
    ``shot_no``/``shot_contract_json`` 两个键。"""
    payload = {
        "storyboard_pack_segment": {
            "resources": {"scenes": scenes},
            "continuity_memo": {"props": continuity_props or []},
        },
    }
    return {"shot_no": shot_no, "shot_contract_json": json.dumps(payload, ensure_ascii=False)}


def _scene_entry(matches: str = "no", description: str = "", scene_reference_id: str = _SCENE) -> dict:
    return {
        "scene_id": "scene:温念的出租屋", "scene_reference_id": scene_reference_id,
        "scene_state_matches_card": matches, "description": description,
    }


def _plug_prop(location: str = "插座在床尾墙根贴近地板处；插头已拔出，在温念右手附近",
               state: str = "已拔下，插座墙根留一缕淡淡焦黑痕迹；屋内顶灯熄灭断电") -> dict:
    """文本逐字取自真实故障数据（第1集第2段 ``continuity_memo.props``），不做
    任何简化替换——``location`` 里的"在温念右手附近"正是会触发角色名过滤的
    那一句，必须保留才能验证返工后的判据真的在这条真实数据上生效。"""
    return {"name": "插座与插头", "form": "", "location": location, "state": state}


def _bible(props: list[Prop] | None = None) -> Bible:
    return Bible(
        characters=[Character(name="温念", role="主角", appearance_canonical="年轻女性，黑长直发")],
        world=World(visual_style_canonical="写实"), props=props or [],
    )


# ---------- ① 第1→2段真实形态 ----------

def test_real_shape_segment1_to_segment2_same_scene_produces_notes():
    shot_rows = [
        _row(1, [_scene_entry("yes", "")], continuity_props=[_plug_prop()]),
        _row(2, [_scene_entry("no", _DESC)]),
    ]
    bible = _bible()

    notes = prop_state_notes_for_run(
        shot_rows=shot_rows, description=_DESC, start_shot_no=2, scene_reference_id=_SCENE,
        props=bible.props, character_display_names=character_display_names_from_bible(bible),
    )

    assert "「插座与插头」" in notes
    assert "位置：插座在床尾墙根贴近地板处；插头已拔出，在温念右手附近" in notes
    assert "状态：已拔下，插座墙根留一缕淡淡焦黑痕迹；屋内顶灯熄灭断电" in notes


def test_notes_drive_prompt_and_fingerprint_changes():
    shot_rows = [
        _row(1, [_scene_entry("yes", "")], continuity_props=[_plug_prop()]),
        _row(2, [_scene_entry("no", _DESC)]),
    ]
    bible = _bible()
    notes = prop_state_notes_for_run(
        shot_rows=shot_rows, description=_DESC, start_shot_no=2, scene_reference_id=_SCENE,
        props=bible.props, character_display_names=character_display_names_from_bible(bible),
    )
    assert notes

    common = {
        "scene_reference_id": _SCENE, "establishing_image_path": "/est.jpg",
        "description": _DESC, "visual_style": "写实", "prop_appearance_notes": "",
    }
    fp_with_notes = scene_state_input_fingerprint(**common, prop_state_notes=notes)
    fp_without_notes = scene_state_input_fingerprint(**common, prop_state_notes="")
    assert fp_with_notes != fp_without_notes

    prompt_with_notes = scene_state_prompt("写实", "温念的出租屋", _DESC, "9:16", "", notes)
    prompt_without_notes = scene_state_prompt("写实", "温念的出租屋", _DESC, "9:16")
    assert prompt_with_notes != prompt_without_notes
    assert "插头已拔出" in prompt_with_notes
    assert prompt_with_notes.index(_DESC) < prompt_with_notes.index("插头已拔出")
    assert prompt_with_notes.index("插头已拔出") < prompt_with_notes.index("画面中没有任何人物")


# ---------- ② 宁缺不错：换场 / 串首是第一段 ----------

def test_previous_segment_different_scene_is_not_taken():
    shot_rows = [
        _row(1, [_scene_entry("yes", "", scene_reference_id=_OTHER_SCENE)], continuity_props=[_plug_prop()]),
        _row(2, [_scene_entry("no", _DESC)]),
    ]
    bible = _bible()

    notes = prop_state_notes_for_run(
        shot_rows=shot_rows, description=_DESC, start_shot_no=2, scene_reference_id=_SCENE,
        props=bible.props, character_display_names=character_display_names_from_bible(bible),
    )

    assert notes == ""


def test_first_segment_of_episode_has_no_previous_segment_to_take():
    shot_rows = [_row(1, [_scene_entry("no", _DESC)])]
    bible = _bible()

    notes = prop_state_notes_for_run(
        shot_rows=shot_rows, description=_DESC, start_shot_no=1, scene_reference_id=_SCENE,
        props=bible.props, character_display_names=character_display_names_from_bible(bible),
    )

    assert notes == ""


# ---------- ③ 过滤：location/state 点了人物名的条目不取 ----------

def test_prop_mentioning_character_name_in_location_is_excluded():
    shot_rows = [
        _row(1, [_scene_entry("yes", "")], continuity_props=[
            {"name": "手机", "form": "", "location": "手机在温念左手", "state": ""},
        ]),
        _row(2, [_scene_entry("no", _DESC)]),
    ]
    bible = _bible()

    notes = prop_state_notes_for_run(
        shot_rows=shot_rows, description=_DESC, start_shot_no=2, scene_reference_id=_SCENE,
        props=bible.props, character_display_names=character_display_names_from_bible(bible),
    )

    assert notes == ""


def test_filter_prop_states_for_empty_room_keeps_only_unmentioned_and_nonempty():
    props = [
        {"name": "手机", "location": "手机在温念左手", "state": ""},
        {"name": "台灯", "location": "", "state": ""},
        {"name": "插座与插头", "location": "插座在床尾墙根", "state": "已拔下"},
    ]
    kept = filter_prop_states_for_empty_room(props, {"温念"})
    assert [p["name"] for p in kept] == ["插座与插头"]


def test_filter_keeps_item_when_character_name_is_relative_spatial_anchor():
    """2026-10-05 返工：旧判据按"整条文字出现过角色名就剔除"实现，会把真实
    故障数据本身滤掉——"在温念右手附近"只是借角色位置做相对空间锚点定位
    物件本身，同一条目里"插座在床尾墙根贴近地板处""插头已拔出"是不提角色的
    独立空间信息，整条应当保留、不删改任何一句。"""
    props = [_plug_prop()]
    kept = filter_prop_states_for_empty_room(props, {"温念"})
    assert kept == props


def test_filter_drops_held_item_even_when_state_clause_omits_character():
    """拿在手里的物件，状态分句（屏幕亮着）不提人物名也不能因此被保留——
    「是否被人拿着」只看位置分句，否则空房间状态图会多画一部手机。"""
    held = {"name": "手机", "form": "", "location": "在温念左手里", "state": "屏幕亮着，冷白微光"}
    assert filter_prop_states_for_empty_room([held], {"温念"}) == []
    floor = {"name": "手机", "form": "", "location": "床单上", "state": "屏幕朝下，边缘透出冷白光"}
    assert filter_prop_states_for_empty_room([floor], {"温念"}) == [floor]


def test_character_display_names_from_bible_collects_name_and_aliases():
    from app.schemas import CharacterAlias

    bible = Bible(
        characters=[Character(
            name="温念", role="主角", appearance_canonical="年轻女性",
            aliases=[CharacterAlias(text="小温", name_kind="personal_name", evidence_chapter_index=1, evidence_quote="小温")],
        )],
        world=World(visual_style_canonical="写实"),
    )
    names = character_display_names_from_bible(bible)
    assert names == {"温念", "小温"}


# ---------- ④ 命中道具卡：带外观但不重复场景描述已讲过的那张卡 ----------

def test_matched_prop_card_appearance_included_when_not_covered_by_description():
    card = Prop(name="插座与插头", appearance_canonical="白色双孔墙壁插座，白色电线插头")
    bible = _bible(props=[card])
    shot_rows = [
        _row(1, [_scene_entry("yes", "")], continuity_props=[_plug_prop()]),
        _row(2, [_scene_entry("no", _DESC)]),
    ]

    notes = prop_state_notes_for_run(
        shot_rows=shot_rows, description=_DESC, start_shot_no=2, scene_reference_id=_SCENE,
        props=bible.props, character_display_names=character_display_names_from_bible(bible),
    )

    assert "白色双孔墙壁插座" in notes
    assert notes.count("「插座与插头」") == 1


def test_matched_prop_card_appearance_not_duplicated_when_already_covered_by_description():
    card = Prop(name="插座与插头", appearance_canonical="白色双孔墙壁插座，白色电线插头")
    bible = _bible(props=[card])
    description = "深夜断电后的出租屋，墙根的插座与插头泛着冷光"
    shot_rows = [
        _row(1, [_scene_entry("yes", "")], continuity_props=[_plug_prop()]),
        _row(2, [_scene_entry("no", description)]),
    ]

    notes = prop_state_notes_for_run(
        shot_rows=shot_rows, description=description, start_shot_no=2, scene_reference_id=_SCENE,
        props=bible.props, character_display_names=character_display_names_from_bible(bible),
    )

    assert "白色双孔墙壁插座" not in notes
    assert "位置：" in notes and "状态：" in notes


# ---------- ⑤ 没有可取条目：提示词/指纹与改动前逐字一致 ----------

def test_fingerprint_and_prompt_unchanged_when_nothing_to_take():
    common = {
        "scene_reference_id": "scene_1", "establishing_image_path": "/a.jpg",
        "description": _DESC, "visual_style": "写实",
    }
    before = hashlib.sha256(json.dumps(
        {**common, "version": PROMPT_VERSION}, ensure_ascii=False, sort_keys=True,
    ).encode("utf-8")).hexdigest()[:32]

    shot_rows = [_row(1, [_scene_entry("no", _DESC)])]  # 本集第一段，没有上一段可取
    notes = prop_state_notes_for_run(
        shot_rows=shot_rows, description=_DESC, start_shot_no=1, scene_reference_id="scene_1",
        props=[], character_display_names=set(),
    )
    assert notes == ""

    fp = scene_state_input_fingerprint(**common, prop_appearance_notes="", prop_state_notes=notes)
    assert fp == before

    prompt_before = scene_state_prompt("写实", "温念的出租屋", _DESC, "9:16")
    prompt_after = scene_state_prompt("写实", "温念的出租屋", _DESC, "9:16", "", notes)
    assert prompt_before == prompt_after


# ---------- ⑥ 扫描侧与装配侧对同一段算出同一指纹（真实 sqlite） ----------

@pytest.fixture
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "scene-state-prop-states.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()


def test_scan_and_assembly_sides_compute_identical_fingerprint(tmp_path, _isolated_db):
    conn = db.get_conn()
    est = tmp_path / "est.png"
    est.write_bytes(b"dry-room")
    conn.execute(
        "INSERT INTO projects(id,name,bible_json,created_at) VALUES(?,?,?,?)",
        ("proj-1", "出租屋 fixture", "{}", db.now()),
    )
    conn.execute(
        "INSERT INTO scene_references(id, project_id, scene_name, ep_start, ep_end, image_path, created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (_SCENE, "proj-1", "温念的出租屋", 1, None, str(est), db.now()),
    )
    conn.execute(
        "INSERT INTO episodes(id, project_id, episode_no, status, created_at) VALUES(?,?,?,?,?)",
        ("ep-1", "proj-1", 1, "scripted", db.now()),
    )
    for shot_id, shot_no, scenes, props in (
        ("shot-1", 1, [_scene_entry("yes", "")], [_plug_prop()]),
        ("shot-2", 2, [_scene_entry("no", _DESC)], None),
    ):
        row = _row(shot_no, scenes, props)
        conn.execute(
            "INSERT INTO shots(id, episode_id, shot_no, duration_s, shot_contract_json) VALUES(?,?,?,?,?)",
            (shot_id, "ep-1", shot_no, 15, row["shot_contract_json"]),
        )
    conn.commit()

    bible = _bible()
    shot_rows = load_episode_shot_rows(conn, "ep-1")
    items = scan_episode_scene_state_needs(
        conn=conn, bible=bible, project_id="proj-1", episode_id="ep-1", shot_rows=shot_rows,
    )
    item = next(i for i in items if i["shot_nos"] == [2])
    assert item["prop_state_notes"]
    scan_fp = item["fingerprint"]

    ensure_tables_on_connection(conn)
    conn.execute(
        """INSERT INTO scene_state_views(
               id, project_id, episode_id, scene_reference_id, state_key, description, image_path, prompt,
               status, error, input_fingerprint, created_at, updated_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        ("scstate-1", "proj-1", "ep-1", _SCENE, item["state_key"], item["description"], str(est), "prompt",
         "ready", None, scan_fp, db.now(), db.now()),
    )
    conn.commit()

    result = resolve_scene_state_view_for_shot(
        conn=conn, episode_id="ep-1", shot_no=2, scene_reference_id=_SCENE,
        establishing_image_path=str(est), visual_style="写实", props=bible.props,
        character_display_names=character_display_names_from_bible(bible),
    )

    assert result is not None
    assert result["input_fingerprint"] == scan_fp
