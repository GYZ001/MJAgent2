"""身份工作台「修订本段」预览路径的两个真实缺陷（2026-09-29，proj_ca86b15ab7d7 EP1）：

1. 场景绑定读到映射时刻冻结的历史行。``episode.screenplay_json`` 是映射包建包
   那一刻的一次性快照，之后场景若被整包重生（旧行挪进历史槽），生成/重生成两条
   路径都会在喂给模型之前重新解析成当前生效行（``_enrich_asset_manifest_
   canonical_visuals``），但身份工作台读的是这份从未重新解析过的快照——
   ``canonical_segment_identities`` 的 ``_backfill_scene_reference_binding``
   无条件按 manifest 值覆盖段落，于是把已经作废的历史行写回了预览结果。
   见 app.production.storyboard_scene_binding.rebind_manifest_scene_references。

2. 旁白固定音色标签被改动时机丢弃。``prepare_identity_candidate``/
   ``revise_segment_dialogue`` 曾经读段落里生成时刻留下的 ``narrator_voice_
   character`` 旧字段，而不是现查项目当前设置——项目设置改过之后，编辑这一段
   会把旁白声道标签冻结在旧值上，与「重新生成」这一段会产出的标签不一致。

夹具沿用 tests/test_segment_identity_workspace.py 的 shots/episodes 结构，场景
历史行/当前行的写法沿用 tests/test_storyboard_scene_binding_current_row.py。
"""
from __future__ import annotations

import json

from app import db
from app.domain.storyboard_ops import identity_workspace as workspace
from app.production.storyboard_dialogue_revision import revise_segment_dialogue
from app.production.storyboard_identity_contract import stamp_identity_contract
from app.production.storyboard_speech_render import render_segment_speech

PROJECT_ID = "p"
EPISODE_ID = "ep"
SOURCE_TEXT = "温念看着黑色手机。我一定会回来。"


def _seed_project(conn, *, narrator_voice_character: str = "") -> None:
    conn.execute(
        "INSERT INTO projects(id,name,bible_json,narrator_voice_character,created_at) VALUES(?,?,?,?,?)",
        (PROJECT_ID, "回归项目", "{}", narrator_voice_character, db.now()),
    )


def _seed_scene_row(conn, *, row_id: str, scene_name: str, ep_start: int, ep_end: int | None, scene_canonical: str) -> None:
    conn.execute(
        "INSERT INTO scene_references(id, project_id, scene_name, ep_start, ep_end, scene_canonical, created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (row_id, PROJECT_ID, scene_name, ep_start, ep_end, scene_canonical, db.now()),
    )


def _build_segment(*, scene_reference_id: str | None) -> dict:
    """一段：温念看着黑色手机（镜头描述）+ 一句旁白台词（原文逐字来自 SOURCE_TEXT）。"""
    segment = dict(
        segment_no=1, synopsis="温念自述", source_segment_indexes=[1], beat_ids=["B1"],
        beats=[{"beat_id": "B1", "summary": "温念自述", "segment_indexes": [1]}], shot_count=2, duration_s=15,
        target_model="seedance_2", degraded_capabilities=[],
        speech_template="镜头1：黑色手机。{{speech:U01}} 镜头2：山路空寂。",
        prompt_text="镜头1：黑色手机。{{speech:U01}} 镜头2：山路空寂。",
        dialogue=[dict(
            utterance_id="U01", speaker_identity_id="旁白", line="我一定会回来。",
            source_segment_index=1, delivery="offscreen_voice", delivery_kind="narration",
        )],
        resources={
            "characters": [dict(identity_id="bible:温念", display_name="温念", subject_kind="character", visibility="visible")],
            "scenes": [{"scene_id": "scene:温念的出租屋", "scene_reference_id": scene_reference_id}],
            "props": [],
        },
    )
    render_segment_speech(segment, dialect="seedance_compact_director_brief", narrator_voice_character="")
    stamp_identity_contract(segment)
    return segment


def _seed_episode_and_shot(conn, *, segment: dict, scene_reference_id: str | None) -> None:
    payload = {
        "prep_pack_version": "2.0.0", "episode_no": 5,
        "asset_manifest": {
            "characters": [{"identity_id": "bible:温念", "display_name": "温念", "segment_indexes": [1]}],
            "functional_extras": [], "props": [],
            "scenes": [{
                "scene_id": "scene:温念的出租屋", "display_name": "温念的出租屋",
                "scene_reference_id": scene_reference_id, "segment_indexes": [1],
            }],
        },
    }
    conn.execute("INSERT INTO chapters(project_id,idx,title,content) VALUES(?,1,'第五章',?)", (PROJECT_ID, SOURCE_TEXT))
    conn.execute(
        "INSERT INTO episodes(id,project_id,episode_no,title,source_chapters,status,screenplay_json,created_at) "
        "VALUES(?,?,5,'第五集','[1]','confirmed',?,?)",
        (EPISODE_ID, PROJECT_ID, json.dumps(payload), db.now()),
    )
    conn.execute(
        "INSERT INTO shots(id,episode_id,shot_no,duration_s,shot_size,camera_move,scene_setting,action_desc,"
        "narration,characters,dialogues,source_excerpt,shot_contract_json,adopted_version_id) "
        "VALUES('s1',?,1,15,'','','','','','[]','[]',?,?,NULL)",
        (EPISODE_ID, SOURCE_TEXT, json.dumps({"storyboard_pack_segment": segment})),
    )


def _preview_with_edited_phone_description(conn) -> dict:
    """唯一的编辑动作：把镜头描述里的「黑色手机」换成「白色手机壳的智能手机」——
    与生产复现一致，台词/人物/场景都不碰。"""
    row = conn.execute("SELECT shot_contract_json FROM shots WHERE id='s1'").fetchone()
    stored = json.loads(row["shot_contract_json"])["storyboard_pack_segment"]
    candidate = dict(stored)
    candidate["speech_template"] = stored["speech_template"].replace("黑色手机", "白色手机壳的智能手机")
    return workspace.prepare_identity_candidate(conn, shot_id="s1", candidate=candidate)


def test_preview_rebinds_stale_history_scene_reference_to_current_row():
    """映射快照冻结的是已挪进历史槽的旧场景行；预览必须产出当前生效行，不能
    把已作废的历史行写回段落（真实回归：proj_ca86b15ab7d7 EP1「温念的出租屋」）。"""
    conn = db.get_conn()
    _seed_project(conn)
    _seed_scene_row(conn, row_id="scene_history", scene_name="温念的出租屋", ep_start=-2, ep_end=0, scene_canonical="旧：地面积水")
    _seed_scene_row(conn, row_id="scene_current", scene_name="温念的出租屋", ep_start=1, ep_end=None, scene_canonical="新：干净整洁")
    segment = _build_segment(scene_reference_id="scene_history")
    _seed_episode_and_shot(conn, segment=segment, scene_reference_id="scene_history")
    conn.commit()

    result = _preview_with_edited_phone_description(conn)

    assert result["resources"]["scenes"][0]["scene_reference_id"] == "scene_current"


def test_preview_keeps_history_reference_absent_when_no_current_row_exists():
    """当前生效行确实不存在时必须显式落空，不是把预览结果继续钉死在历史行上
    （CLAUDE.md「不得兜底填充」——旧值已被证明不是当前生效行）。"""
    conn = db.get_conn()
    _seed_project(conn)
    _seed_scene_row(conn, row_id="scene_history", scene_name="温念的出租屋", ep_start=-2, ep_end=0, scene_canonical="旧：地面积水")
    segment = _build_segment(scene_reference_id="scene_history")
    _seed_episode_and_shot(conn, segment=segment, scene_reference_id="scene_history")
    conn.commit()

    result = _preview_with_edited_phone_description(conn)

    assert result["resources"]["scenes"][0]["scene_reference_id"] is None


def test_preview_renders_narrator_voice_label_when_project_setting_is_on():
    """项目设置了旁白固定音色角色：预览必须渲染「旁白（温念的声音）」，与生成
    台产出的标签一致——即使编辑动作与台词/旁白毫不相关。"""
    conn = db.get_conn()
    _seed_project(conn, narrator_voice_character="温念")
    _seed_scene_row(conn, row_id="scene_current", scene_name="温念的出租屋", ep_start=1, ep_end=None, scene_canonical="新：干净整洁")
    segment = _build_segment(scene_reference_id="scene_current")
    _seed_episode_and_shot(conn, segment=segment, scene_reference_id="scene_current")
    conn.commit()

    result = _preview_with_edited_phone_description(conn)

    assert "旁白（温念的声音）：" in result["prompt_text"]
    assert "白色手机壳的智能手机" in result["prompt_text"]


def test_preview_keeps_literal_narrator_label_when_project_setting_is_off():
    """项目未设置旁白固定音色：预览渲染字面量「旁白」标签，不得凭空写出任何角色名。"""
    conn = db.get_conn()
    _seed_project(conn, narrator_voice_character="")
    _seed_scene_row(conn, row_id="scene_current", scene_name="温念的出租屋", ep_start=1, ep_end=None, scene_canonical="新：干净整洁")
    segment = _build_segment(scene_reference_id="scene_current")
    _seed_episode_and_shot(conn, segment=segment, scene_reference_id="scene_current")
    conn.commit()

    result = _preview_with_edited_phone_description(conn)

    assert "旁白（旁白）：" in result["prompt_text"]
    assert "的声音）：" not in result["prompt_text"]


def test_dialogue_revision_uses_caller_supplied_narrator_voice_not_stale_segment_field():
    """对话修订路径同一根因：段落生成时项目还没设置旁白音色（segment 自带字段是
    空串），后来项目开了这个设置；调用方（app.domain.storyboard_ops.
    mutation_primitives.apply_segment_dialogue_revision）现查后传入，修订必须
    用新值，不能被 segment 里的旧字段拖住。"""
    segment = {
        "speech_template": "镜头1：{{speech:U01}}",
        "speech_dialect": "",
        "prompt_text": "",
        "narrator_voice_character": "",  # 生成时的旧值：项目当时还没设置
        "dialogue": [{
            "utterance_id": "U01", "speaker_identity_id": "旁白", "line": "小区物业正给那栋楼换水管",
            "delivery": "offscreen_voice", "delivery_kind": "narration",
        }],
        "resources": {"characters": [{"identity_id": "bible:温念", "display_name": "温念"}]},
    }
    render_segment_speech(segment, dialect="", narrator_voice_character="")
    assert "旁白（旁白）：" in segment["prompt_text"]

    revised = revise_segment_dialogue(segment, {"U01": "水管已经换完了"}, reason="人工修订", narrator_voice_character="温念")

    assert "旁白（温念的声音）：" in revised["prompt_text"]
    assert revised["narrator_voice_character"] == "温念"
