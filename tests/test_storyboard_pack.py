"""app.production.storyboard_pack -- 分镜台 2.0.0 核心（docs/STORYBOARD_PROMPT_IR_DESIGN.md）。

覆盖不需要真正调用模型的部分：结构校验函数、方言解析、持久化落库形状、
台词闸门（唯一从 F1-F6 幸存的闸门）、对白构图硬规则在新架构行上的显式退役、
以及 project_prep_pack_to_screenplay 对真正 2.0.0（无 event_chain）payload
的分支（此前只有 1.x 形态的 EP6 历史夹具覆盖，2.0.0 分支此前无测试）。

不覆盖：_generate_beat_sheet / _generate_segment_prompt 真正的模型调用
（需要 mock model_gateway.chat_structured，属于更大规模的集成测试，此文件
只测纯函数与 DB 落库/读回，不引入 mock 基础设施）。
"""
from __future__ import annotations

import json

import pytest

from app import config, db
from app.continuity import dialogue_framing_errors
from app.domain.video_ops import _storyboard_structural_errors
from app.production.screenplay_authority import project_prep_pack_to_screenplay
from app.production.storyboard_pack import (
    STORYBOARD_PACK_CONTRACT_MARKER,
    MINIMAX_H3_DIALECT_INSTRUCTIONS,
    StoryboardPack,
    StoryboardPackBeat,
    StoryboardPackSegment,
    _AiBeat,
    _AiBeatSheetDraft,
    _AiDialogueLine,
    _AiSegmentPlan,
    _AiSegmentResources,
    _AiStoryboardSegmentDraft,
    _dialect_for_target_video_model,
    _segment_content_advisories,
    _validate_beat_sheet_draft,
    _validate_segment_draft,
    persist_storyboard_pack,
)
from app.schemas import Bible, Shot, Storyboard, World
from app.source_excerpt import SourceSegment
from app.validators import storyboard_pack_dialogue_errors, validate_storyboard


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "storyboard-pack.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()


def _seed_episode(conn, *, episode_id: str, project_id: str = "proj-1") -> None:
    conn.execute(
        "INSERT INTO projects(id,name,bible_json,created_at) VALUES(?,?,?,?)",
        (project_id, "storyboard pack fixture", "{}", db.now()),
    )
    conn.execute(
        "INSERT INTO chapters(project_id, idx, title, content) VALUES(?,?,?,?)",
        (project_id, 1, "第一章", "少年站在山顶。\n\n他扔掉了葫芦。\n\n葫芦落入河中。"),
    )
    conn.execute(
        """INSERT INTO episodes(
               id,project_id,episode_no,title,source_chapters,target_duration_s,
               status,screenplay_status,target_video_model,
               screenplay_character_resolutions,created_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (
            episode_id, project_id, 1, "Fixture", "[1]", 1800,
            "scripted", "ready", "hiagent", "[]", db.now(),
        ),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# 方言解析
# ---------------------------------------------------------------------------

def test_dialect_maps_hiagent_to_seedance_2():
    profile, target_model, instructions = _dialect_for_target_video_model("hiagent")
    assert target_model == "seedance_2"
    assert "电影级预告片质感" in instructions


def test_dialect_maps_minimax_h3():
    profile, target_model, instructions = _dialect_for_target_video_model("minimax_h3")
    assert target_model == "minimax_h3"
    assert instructions is MINIMAX_H3_DIALECT_INSTRUCTIONS
    # 字段名是接口语法，锁字面量，防止被顺手改写
    # (docs/STORYBOARD_PROMPT_IR_DESIGN.md 「H3 的固定指令行是模式信标」)。
    for literal in (
        "integrated_multimodal_description:",
        "overall_soundscape:",
        "non_diegetic_music:",
        "[Shot 1]",
        "<d>",
    ):
        assert literal in instructions


# ---------------------------------------------------------------------------
# 阶段一：节拍表 + 分段的结构校验
# ---------------------------------------------------------------------------

def test_validate_beat_sheet_draft_accepts_well_formed_draft():
    draft = _AiBeatSheetDraft(
        beat_sheet=[_AiBeat(beat_id="B1", summary="他扔掉了理想", segment_indexes=[1, 2])],
        segments=[
            _AiSegmentPlan(
                segment_no=1, synopsis="他扔掉了理想",
                source_segment_indexes=[1, 2], beat_ids=["B1"],
            )
        ],
    )
    assert _validate_beat_sheet_draft(draft, total_segments=3) == []


def test_validate_beat_sheet_draft_rejects_out_of_range_segment_index():
    draft = _AiBeatSheetDraft(
        beat_sheet=[_AiBeat(beat_id="B1", summary="x", segment_indexes=[99])],
        segments=[_AiSegmentPlan(segment_no=1, synopsis="x", source_segment_indexes=[1])],
    )
    errors = _validate_beat_sheet_draft(draft, total_segments=3)
    assert any("不存在的原文段号" in e for e in errors)


def test_validate_beat_sheet_draft_rejects_non_contiguous_segment_no():
    draft = _AiBeatSheetDraft(
        beat_sheet=[_AiBeat(beat_id="B1", summary="x", segment_indexes=[1])],
        segments=[_AiSegmentPlan(segment_no=2, synopsis="x", source_segment_indexes=[1])],
    )
    errors = _validate_beat_sheet_draft(draft, total_segments=3)
    assert any("连续递增" in e for e in errors)


def test_validate_beat_sheet_draft_rejects_unknown_beat_id_reference():
    draft = _AiBeatSheetDraft(
        beat_sheet=[_AiBeat(beat_id="B1", summary="x", segment_indexes=[1])],
        segments=[
            _AiSegmentPlan(
                segment_no=1, synopsis="x", source_segment_indexes=[1], beat_ids=["B-GHOST"],
            )
        ],
    )
    errors = _validate_beat_sheet_draft(draft, total_segments=3)
    assert any("不存在的 beat_id" in e for e in errors)


# ---------------------------------------------------------------------------
# 阶段二：逐段提示词草稿的结构校验
# ---------------------------------------------------------------------------

def _draft(**overrides) -> _AiStoryboardSegmentDraft:
    base = dict(
        prompt_text="电影级预告片质感，多镜头叙事，镜头之间硬切。……",
        shot_count=3,
        dialogue=[_AiDialogueLine(speaker_identity_id="id_a", line="走吧", source_segment_index=1)],
        resources=_AiSegmentResources(),
    )
    base.update(overrides)
    return _AiStoryboardSegmentDraft(**base)


# 2026-08-26（用户拍板，第一版分镜提示词不设任何内容门禁）：
# _validate_segment_draft 现在只剩「下一环节会真的用不了」的形状检查
# （prompt_text 空/超限、H3 固定字段名）；内容判断（说话人在场、资源身份是否
# 映射台已知）移到 _segment_content_advisories，只计算、不参与
# model_gateway.chat_structured 的语义重试/失败判定。

def test_validate_segment_draft_accepts_well_formed_draft():
    errors = _validate_segment_draft(_draft(), dialect_render_format="seedance_compact_director_brief")
    assert errors == []


def test_validate_segment_draft_rejects_empty_prompt_text():
    errors = _validate_segment_draft(
        _draft(prompt_text=" "), dialect_render_format="seedance_compact_director_brief",
    )
    assert any("为空" in e for e in errors)


def test_validate_segment_draft_requires_h3_literal_fields():
    errors = _validate_segment_draft(
        _draft(prompt_text="没有按 H3 格式写的自由散文"),
        dialect_render_format="minimax_h3_native_fields",
    )
    assert any("integrated_multimodal_description:" in e for e in errors)


def test_validate_segment_draft_rejects_over_char_limit(monkeypatch):
    monkeypatch.setattr(config, "PROMPT_CHAR_LIMIT", 10)
    errors = _validate_segment_draft(_draft(), dialect_render_format="seedance_compact_director_brief")
    assert any("超过上限" in e for e in errors)


def test_validate_segment_draft_does_not_block_on_dialogue_source_outside_segment():
    # 这条以前会拦截/触发语义重试；用户拍板后必须完全不出现在这个函数里，
    # 即使内容明显有问题（source_segment_index=9 越界）。
    errors = _validate_segment_draft(
        _draft(dialogue=[_AiDialogueLine(speaker_identity_id="id_a", line="走吧", source_segment_index=9)]),
        dialect_render_format="seedance_compact_director_brief",
    )
    assert errors == []


def test_validate_segment_draft_does_not_block_on_unknown_character_resource():
    errors = _validate_segment_draft(
        _draft(resources=_AiSegmentResources(characters=[{"identity_id": "id_ghost", "description": "x"}])),
        dialect_render_format="seedance_compact_director_brief",
    )
    assert errors == []


# ---------------------------------------------------------------------------
# _segment_content_advisories：内容判断照算，只是结论不再是拦截，而是
# 附在产物 degraded_capabilities[] 上的信息（不许用删掉校验函数来实现）。
# ---------------------------------------------------------------------------

def test_segment_content_advisories_empty_for_well_formed_draft():
    advisories = _segment_content_advisories(
        _draft(resources=_AiSegmentResources(characters=[{"identity_id": "id_a", "description": "x"}])),
        known_character_ids={"id_a"},
        known_scene_ids=set(),
        source_segment_indexes=[1, 2],
    )
    assert advisories == []


def test_segment_content_advisories_flags_misattributed_speaker_but_does_not_raise():
    # 用户判据的原始场景：台词安给了当时不在场的人。必须能算出来、必须不抛异常。
    draft = _draft(
        dialogue=[_AiDialogueLine(speaker_identity_id="id_absent", line="走吧", source_segment_index=1)],
        resources=_AiSegmentResources(characters=[{"identity_id": "id_a", "description": "x"}]),
    )
    advisories = _segment_content_advisories(
        draft, known_character_ids={"id_a", "id_absent"}, known_scene_ids=set(),
        source_segment_indexes=[1, 2],
    )
    assert any("不在本段 resources.characters 内" in a for a in advisories)


def test_segment_content_advisories_flags_untraceable_dialogue_source():
    draft = _draft(dialogue=[_AiDialogueLine(speaker_identity_id="id_a", line="走吧", source_segment_index=9)])
    advisories = _segment_content_advisories(
        draft, known_character_ids={"id_a"}, known_scene_ids=set(), source_segment_indexes=[1, 2],
    )
    assert any("不在本段引用的原文段号" in a for a in advisories)


def test_segment_content_advisories_flags_unknown_character_and_scene_resource():
    draft = _draft(
        resources=_AiSegmentResources(
            characters=[{"identity_id": "id_ghost", "description": "x"}],
            scenes=[{"scene_id": "scene_ghost", "description": "y"}],
        ),
    )
    advisories = _segment_content_advisories(
        draft, known_character_ids={"id_a"}, known_scene_ids={"scene_1"}, source_segment_indexes=[1, 2],
    )
    assert any("不是映射台已知的人物身份" in a for a in advisories)
    assert any("不是映射台已知场景" in a for a in advisories)


# ---------------------------------------------------------------------------
# 台词闸门（说话人在场 + 出处可溯，唯一从 F1-F6 幸存的闸门）
# ---------------------------------------------------------------------------

def _shot_with_segment(**segment_overrides) -> Shot:
    segment = {
        "segment_no": 1, "duration_s": 15, "synopsis": "x",
        "source_segment_indexes": [1, 2], "prompt_text": "x", "shot_count": 3,
        "dialogue": [{"speaker_identity_id": "id_a", "line": "走吧", "source_segment_index": 1}],
        "resources": {"characters": [{"identity_id": "id_a", "description": "x"}], "scenes": [], "props": []},
        "degraded_capabilities": [],
    }
    segment.update(segment_overrides)
    return Shot(
        shot_no=1, duration_s=15, shot_size="", camera_move="", action_desc="x",
        storyboard_pack_segment=segment,
    )


def test_storyboard_pack_dialogue_errors_passes_when_speaker_present_and_traceable():
    assert storyboard_pack_dialogue_errors(_shot_with_segment()) == []


def test_storyboard_pack_dialogue_errors_flags_absent_speaker():
    shot = _shot_with_segment(
        dialogue=[{"speaker_identity_id": "id_ghost", "line": "走吧", "source_segment_index": 1}],
    )
    errors = storyboard_pack_dialogue_errors(shot)
    assert any("SPEAKER_ABSENT" in e for e in errors)


def test_storyboard_pack_dialogue_errors_flags_untraceable_source():
    shot = _shot_with_segment(
        dialogue=[{"speaker_identity_id": "id_a", "line": "走吧", "source_segment_index": 99}],
    )
    errors = storyboard_pack_dialogue_errors(shot)
    assert any("NO_SOURCE" in e for e in errors)


def test_storyboard_pack_dialogue_errors_noop_for_legacy_shot():
    legacy = Shot(shot_no=1, duration_s=5, shot_size="近景", camera_move="固定", action_desc="x" * 12)
    assert storyboard_pack_dialogue_errors(legacy) == []


# ---------------------------------------------------------------------------
# 对白构图硬规则的退役（app.continuity.dialogue_framing_errors）
# ---------------------------------------------------------------------------

def test_dialogue_framing_errors_retired_for_storyboard_pack_shot():
    # 会触发旧规则的一切条件都占齐（无 characters_visible、无 shot_size）——
    # 若退役失效，这里理应报错；断言它不报错才是这条测试的意义。
    shot = _shot_with_segment()
    shot.dialogues = [{"speaker": "id_a", "line": "走吧", "emotion": "平静", "delivery": "spoken_dialogue"}]
    assert dialogue_framing_errors(shot) == []


# ---------------------------------------------------------------------------
# validate_storyboard 的分镜台 2.0.0 短路分支
# ---------------------------------------------------------------------------

def _bible() -> Bible:
    return Bible(characters=[], world=World(visual_style_canonical=""))


def test_validate_storyboard_pack_branch_accepts_valid_board():
    board = Storyboard(episode_no=1, shots=[_shot_with_segment()])
    assert validate_storyboard(board, _bible(), 15) == []


def test_validate_storyboard_pack_branch_rejects_wrong_duration():
    shot = _shot_with_segment()
    shot.duration_s = 10
    board = Storyboard(episode_no=1, shots=[shot])
    errors = validate_storyboard(board, _bible(), 15)
    assert any("15s" in e for e in errors)


# ---------------------------------------------------------------------------
# _storyboard_structural_errors（app.domain.video_ops，真正的确认阻塞门）
# ---------------------------------------------------------------------------

def test_storyboard_structural_errors_does_not_require_shot_size_for_pack_rows():
    shot = _shot_with_segment()
    shot.scene_name = "山顶"
    board = Storyboard(episode_no=1, shots=[shot])
    assert _storyboard_structural_errors(board) == []


def test_storyboard_structural_errors_flags_missing_prompt_text():
    shot = _shot_with_segment(prompt_text="")
    shot.scene_name = "山顶"
    board = Storyboard(episode_no=1, shots=[shot])
    errors = _storyboard_structural_errors(board)
    assert any("prompt_text" in e for e in errors)


# ---------------------------------------------------------------------------
# project_prep_pack_to_screenplay：真正 2.0.0（无 event_chain）payload
# ---------------------------------------------------------------------------

def _prep_pack_2_0_0_payload() -> dict:
    return {
        "prep_pack_version": "2.0.0",
        "episode_no": 1,
        "episode_scope": {"chapter_indexes": [1], "source_segment_count": 3},
        "asset_manifest": {
            "characters": [
                {
                    "identity_id": "id_a", "display_name": "少年",
                    "display_appellation": "少年", "aliases": [],
                    "portrait_id": "portrait_1", "visual_entity_id": "id_a",
                    "segment_indexes": [1, 2],
                },
            ],
            "scenes": [
                {
                    "scene_id": "scene_1", "display_name": "山顶",
                    "scene_reference_id": "scene_ref_1", "segment_indexes": [1, 2],
                },
            ],
            "props": [], "functional_extras": [],
        },
        "appellation_map": [
            {"raw_mention": "他", "segment_index": 2, "identity_id": "id_a", "canonical_appellation": "少年"},
        ],
        "coverage_ledger": {
            "total_segments": 3, "delivered": [1, 2, 3], "merged": [],
            "retained_as_context": [], "proven_duplicates": [], "paratext": [], "uncovered": [],
        },
    }


def test_project_prep_pack_to_screenplay_2_0_0_has_no_event_chain_key():
    payload = _prep_pack_2_0_0_payload()
    assert "event_chain" not in payload


def test_project_prep_pack_to_screenplay_2_0_0_scene_outline_uses_segment_indexes():
    screenplay = project_prep_pack_to_screenplay(_prep_pack_2_0_0_payload())
    assert len(screenplay.scene_outline) == 1
    assert screenplay.scene_outline[0].scene_heading == "山顶"
    # 角色名单来自 segment_indexes 交集（1,2 ∩ 1,2），不是永远清空的 event_ids 交集。
    assert screenplay.scene_outline[0].characters == ["少年"]


def test_project_prep_pack_to_screenplay_2_0_0_leaves_dialogue_fields_empty_not_fabricated():
    screenplay = project_prep_pack_to_screenplay(_prep_pack_2_0_0_payload())
    assert screenplay.full_script_text == ""
    assert screenplay.dialogue_chains == []
    assert screenplay.key_lines == []
    assert screenplay.key_plot_points == []


# ---------------------------------------------------------------------------
# persist_storyboard_pack：DB 落库形状
# ---------------------------------------------------------------------------

def _pack() -> StoryboardPack:
    return StoryboardPack(
        episode_no=1,
        target_model="seedance_2",
        beat_sheet=[StoryboardPackBeat(beat_id="B1", summary="他扔掉了理想", segment_indexes=[1, 2])],
        segments=[
            StoryboardPackSegment(
                segment_no=1, synopsis="他扔掉了理想",
                source_segment_indexes=[1, 2],
                prompt_text="电影级预告片质感，多镜头叙事，镜头之间硬切。……",
                shot_count=3,
                dialogue=[{"speaker_identity_id": "id_a", "line": "走了", "source_segment_index": 1}],
                resources={
                    "characters": [{"identity_id": "id_a", "portrait_id": "portrait_1", "description": "少年"}],
                    "scenes": [{"scene_id": "scene_1", "scene_reference_id": "scene_ref_1", "description": "山顶"}],
                    "props": [],
                },
                degraded_capabilities=[],
            ),
        ],
    )


def test_persist_storyboard_pack_writes_one_shots_row_per_segment():
    conn = db.get_conn()
    episode_id = "ep-pack-1"
    _seed_episode(conn, episode_id=episode_id)
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    payload = _prep_pack_2_0_0_payload()
    segments = [
        SourceSegment(segment_id="SRC0001", text="少年站在山顶。", start_offset=0, end_offset=1),
        SourceSegment(segment_id="SRC0002", text="他扔掉了葫芦。", start_offset=1, end_offset=2),
        SourceSegment(segment_id="SRC0003", text="葫芦落入河中。", start_offset=2, end_offset=3),
    ]
    shot_ids = persist_storyboard_pack(conn, episode_id, ep, payload, _pack(), segments=segments)
    assert len(shot_ids) == 1

    rows = conn.execute("SELECT * FROM shots WHERE episode_id=?", (episode_id,)).fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert row["shot_no"] == 1
    assert row["duration_s"] == 15
    assert row["shot_size"] == ""
    assert row["camera_move"] == ""
    assert row["source_excerpt"] == "少年站在山顶。\n他扔掉了葫芦。"
    assert row["adopted_version_id"]

    contract = json.loads(row["shot_contract_json"])
    assert contract["prompt_contract_version"] == STORYBOARD_PACK_CONTRACT_MARKER
    assert contract["is_final"] is True
    assert contract["storyboard_pack_segment"]["prompt_text"] == _pack().segments[0].prompt_text

    version = conn.execute(
        "SELECT prompt_text FROM shot_versions WHERE id=?", (row["adopted_version_id"],),
    ).fetchone()
    # prompt_text 落库必须与模型草稿逐字一致 -- 中间没有任何代码重新拼装
    # （交付前必须回答 #2 的验证：见 persist_storyboard_pack 的文档）。
    assert version["prompt_text"] == _pack().segments[0].prompt_text


# ---------------------------------------------------------------------------
# 判据（用户 2026-08-26 原话）：一个「台词安给了当时不在场的人」的段落，必须
# 能顺利生成、顺利保存、顺利进入下一环节，同时那条不一致要能在产物里被看见
# （记录下来，不是消失）——两件事同时成立。
# ---------------------------------------------------------------------------

def _pack_with_misattributed_speaker() -> StoryboardPack:
    pack = _pack()
    segment = pack.segments[0]
    # 台词的说话人 "id_absent" 不在本段 resources.characters（只有 id_a）——
    # 这正是"当时不在场的人"这一情形的落库形态。
    segment.dialogue = [
        {"speaker_identity_id": "id_absent", "line": "我不该在这", "source_segment_index": 1},
    ]
    return pack


def test_misattributed_speaker_segment_persists_successfully_not_dropped():
    conn = db.get_conn()
    episode_id = "ep-pack-misattributed"
    _seed_episode(conn, episode_id=episode_id)
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    payload = _prep_pack_2_0_0_payload()
    segments = [
        SourceSegment(segment_id="SRC0001", text="少年站在山顶。", start_offset=0, end_offset=1),
        SourceSegment(segment_id="SRC0002", text="他扔掉了葫芦。", start_offset=1, end_offset=2),
        SourceSegment(segment_id="SRC0003", text="葫芦落入河中。", start_offset=2, end_offset=3),
    ]
    pack = _pack_with_misattributed_speaker()
    # 顺利保存：persist 不抛异常、不丢段落。
    shot_ids = persist_storyboard_pack(conn, episode_id, ep, payload, pack, segments=segments)
    assert len(shot_ids) == 1

    row = conn.execute("SELECT * FROM shots WHERE episode_id=?", (episode_id,)).fetchone()
    contract = json.loads(row["shot_contract_json"])
    # prompt_text 原样落库，未被这条内容缺陷影响。
    assert contract["storyboard_pack_segment"]["prompt_text"] == pack.segments[0].prompt_text
    assert row["adopted_version_id"]

    board = Storyboard(episode_no=1, shots=[_shot_with_segment(
        dialogue=contract["storyboard_pack_segment"]["dialogue"],
        resources=contract["storyboard_pack_segment"]["resources"],
    )])
    # 顺利进入下一环节：真正阻断确认的结构检查不受影响。
    assert _storyboard_structural_errors(board) == []
    # 不一致仍然算得出来、看得见——只是不再是 structural_errors。
    dialogue_errors = storyboard_pack_dialogue_errors(board.shots[0])
    assert any("SPEAKER_ABSENT" in e for e in dialogue_errors)


def test_evaluate_storyboard_pack_for_confirmation_passes_despite_misattributed_speaker():
    from app.domain.video_ops import _evaluate_storyboard_pack_for_confirmation

    shot = _shot_with_segment(
        dialogue=[{"speaker_identity_id": "id_absent", "line": "我不该在这", "source_segment_index": 1}],
        resources={"characters": [{"identity_id": "id_a", "description": "x"}], "scenes": [], "props": []},
    )
    shot.scene_name = "山顶"
    board = Storyboard(episode_no=1, shots=[shot])
    episode_row = {"id": "ep-x", "target_duration_s": 15}
    evaluation = _evaluate_storyboard_pack_for_confirmation(episode_row, board, _bible(), target_duration_s=15)
    # 顺利进入下一环节：确认必须通过。
    assert evaluation.passed is True
    assert evaluation.errors == []
    # 记录下来，不是消失：不一致出现在 warnings（进而是返回的 issues）里。
    assert any("SPEAKER_ABSENT" in w for w in evaluation.warnings)
    assert any("SPEAKER_ABSENT" in issue.message for issue in evaluation.issues)
