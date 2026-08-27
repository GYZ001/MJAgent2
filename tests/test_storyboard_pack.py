"""app.production.storyboard_pack -- 分镜台 2.0.0 核心（docs/STORYBOARD_PROMPT_IR_DESIGN.md）。

覆盖不需要真正调用模型的部分：结构校验函数、方言解析、持久化落库形状、
台词闸门（唯一从 F1-F6 幸存的闸门）、对白构图硬规则在新架构行上的显式退役、
以及 project_prep_pack_to_screenplay 对真正 2.0.0（无 event_chain）payload
的分支（此前只有 1.x 形态的 EP6 历史夹具覆盖，2.0.0 分支此前无测试）。
阶段二分批切分与 already_written 衔接用 mock chat_structured 覆盖；不覆盖
真实供应商往返。
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app import config, db
from app.continuity import dialogue_framing_errors
from app.domain.video_ops import _storyboard_structural_errors
from app.production.screenplay_authority import project_prep_pack_to_screenplay
from app.production.storyboard_pack import (
    STORYBOARD_PACK_CONTRACT_MARKER,
    STORYBOARD_PACK_VERSION,
    MINIMAX_H3_DIALECT_INSTRUCTIONS,
    SEGMENT_BATCH_TOKENS_PER_SEGMENT,
    StoryboardPack,
    StoryboardPackBeat,
    StoryboardPackSegment,
    _AiAllSegmentsDraft,
    _AiBeat,
    _AiBeatSheetDraft,
    _AiDialogueLine,
    _AiSegmentPlan,
    _AiSegmentResources,
    _AiStoryboardSegmentDraft,
    _dialect_for_target_video_model,
    _enrich_asset_manifest_canonical_visuals,
    _generate_all_segment_prompts,
    _paratext_exclusion_rule,
    _paratext_segment_indexes,
    _segment_content_advisories,
    _segment_prompt_answer_budget,
    _segment_prompt_batch_capacity,
    _segment_prompt_task_text,
    _source_block_for_prompt,
    _split_segment_prompt_batches,
    _strip_paratext_from_beat_draft,
    _validate_all_segments_draft,
    _validate_beat_sheet_draft,
    _validate_segment_draft,
    persist_storyboard_pack,
)
from app.schemas import Bible, Shot, Storyboard, World
from app.source_excerpt import SourceSegment, index_source_segments
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


def _real_segments(conn, ep) -> list[SourceSegment]:
    """Real ``index_source_segments`` offsets for ``_seed_episode``'s chapter.

    persist_storyboard_pack now derives storyboard_source_bindings straight
    from segment start/end offsets (app.production.storyboard_pack.
    _resolve_segment_source_binding slices the joined episode source text and
    locates that literal slice inside the authorized chapter) -- a fixture
    with hand-picked offsets that don't correspond to any real slice of
    _seed_episode's chapter content would make every persist call fail to
    find a binding, not just look unrealistic. Computed the same way
    app.production.storyboard_pack._load_indexed_source_segments does it, so
    this always matches whatever _seed_episode's chapter text actually is.
    """
    from app.domain.common import _episode_source_text

    return index_source_segments(_episode_source_text(conn, ep))


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
# 作者的话（paratext）复用映射台已算好的账（2.0.4）
# ---------------------------------------------------------------------------

def test_paratext_segment_indexes_reads_coverage_ledger():
    payload = {"coverage_ledger": {"paratext": [1, 52, 53, 54]}}
    assert _paratext_segment_indexes(payload) == {1, 52, 53, 54}


def test_paratext_segment_indexes_empty_when_coverage_ledger_missing():
    """旧契约分集兜底：没有 coverage_ledger 时返回空集（退化为全量路径），
    不能把"账不存在"误读成"全部段落都是 paratext"。"""
    assert _paratext_segment_indexes({}) == set()


def test_paratext_segment_indexes_empty_when_paratext_key_missing():
    assert _paratext_segment_indexes({"coverage_ledger": {}}) == set()


def test_paratext_segment_indexes_empty_when_paratext_is_not_a_list():
    assert _paratext_segment_indexes({"coverage_ledger": {"paratext": "oops"}}) == set()


def test_paratext_segment_indexes_ignores_non_int_entries():
    payload = {"coverage_ledger": {"paratext": [1, "not-an-int", None, 3]}}
    assert _paratext_segment_indexes(payload) == {1, 3}


def test_source_block_for_prompt_omits_paratext_text_but_keeps_numbering():
    segments = [
        SourceSegment(segment_id="s1", text="【第八章】\n第八章", start_offset=0, end_offset=1),
        SourceSegment(segment_id="s2", text="孟浩推开院门。", start_offset=1, end_offset=2),
        SourceSegment(
            segment_id="s3", text="又是大章，求推荐票，谢谢诸位道友！",
            start_offset=2, end_offset=3,
        ),
    ]
    block = _source_block_for_prompt(segments, {1, 3})

    assert "[段1]" in block and "[段2]" in block and "[段3]" in block
    assert "孟浩推开院门。" in block
    # 作者的话的原文一个字都不能出现在喂给模型的文本里。
    assert "求推荐票" not in block
    assert "诸位道友" not in block
    assert "【第八章】" not in block
    # 段号不重新编号：段2（唯一的正文段）紧跟在 [段2] 后面，不是 [段1]。
    assert "[段2] 孟浩推开院门。" in block


def test_source_block_for_prompt_full_text_when_no_paratext():
    segments = [SourceSegment(segment_id="s1", text="正文。", start_offset=0, end_offset=1)]
    block = _source_block_for_prompt(segments, set())
    assert block == "[段1] 正文。"


def test_paratext_exclusion_rule_none_when_no_paratext():
    assert _paratext_exclusion_rule(set()) is None


def test_paratext_exclusion_rule_names_the_segment_numbers():
    rule = _paratext_exclusion_rule({3, 1})
    assert rule is not None
    assert "[1, 3]" in rule


def test_strip_paratext_from_beat_draft_removes_paratext_only_from_mixed_references():
    draft = _AiBeatSheetDraft(
        beat_sheet=[_AiBeat(beat_id="B1", summary="x", segment_indexes=[1, 2, 3])],
        segments=[
            _AiSegmentPlan(
                segment_no=1, synopsis="x",
                source_segment_indexes=[2, 3], beat_ids=["B1"],
            )
        ],
    )
    notes = _strip_paratext_from_beat_draft(draft, {3})

    assert draft.beat_sheet[0].segment_indexes == [1, 2]
    assert draft.segments[0].source_segment_indexes == [2]
    assert notes == []


def test_strip_paratext_from_beat_draft_keeps_reference_when_filtering_would_empty_it():
    """全部引用都落在 paratext 账内时保留原样，不产出空引用（那会让
    _resolve_segment_source_binding 直接抛错，整集生成失败）；但要留痕。"""
    draft = _AiBeatSheetDraft(
        beat_sheet=[_AiBeat(beat_id="B1", summary="x", segment_indexes=[9])],
        segments=[
            _AiSegmentPlan(
                segment_no=1, synopsis="x", source_segment_indexes=[9], beat_ids=["B1"],
            )
        ],
    )
    notes = _strip_paratext_from_beat_draft(draft, {9})

    assert draft.beat_sheet[0].segment_indexes == [9]
    assert draft.segments[0].source_segment_indexes == [9]
    assert len(notes) == 2
    assert any("beat B1" in n for n in notes)
    assert any("段 1" in n for n in notes)


def test_strip_paratext_from_beat_draft_noop_when_no_paratext():
    draft = _AiBeatSheetDraft(
        beat_sheet=[_AiBeat(beat_id="B1", summary="x", segment_indexes=[1])],
        segments=[_AiSegmentPlan(segment_no=1, synopsis="x", source_segment_indexes=[1])],
    )
    notes = _strip_paratext_from_beat_draft(draft, set())
    assert notes == []
    assert draft.beat_sheet[0].segment_indexes == [1]


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
        segment_no=1,
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
# _validate_all_segments_draft：2.0.3 整集批量调用的顶层校验——segment_no
# 完整性（新增，单段版本不需要）+ 逐项复用 _validate_segment_draft（不重复
# 发明格式检查）。
# ---------------------------------------------------------------------------

def test_validate_all_segments_draft_accepts_well_formed_batch():
    draft = _AiAllSegmentsDraft(segments=[_draft(segment_no=1), _draft(segment_no=2)])
    errors = _validate_all_segments_draft(
        draft, expected_segment_nos=[1, 2], dialect_render_format="seedance_compact_director_brief",
    )
    assert errors == []


def test_validate_all_segments_draft_rejects_missing_segment_no():
    draft = _AiAllSegmentsDraft(segments=[_draft(segment_no=1)])
    errors = _validate_all_segments_draft(
        draft, expected_segment_nos=[1, 2], dialect_render_format="seedance_compact_director_brief",
    )
    assert any("缺失 [2]" in e for e in errors)


def test_validate_all_segments_draft_rejects_extra_segment_no():
    draft = _AiAllSegmentsDraft(segments=[_draft(segment_no=1), _draft(segment_no=2), _draft(segment_no=3)])
    errors = _validate_all_segments_draft(
        draft, expected_segment_nos=[1, 2], dialect_render_format="seedance_compact_director_brief",
    )
    assert any("包含不存在的 [3]" in e for e in errors)


def test_validate_all_segments_draft_rejects_duplicate_segment_no():
    draft = _AiAllSegmentsDraft(segments=[_draft(segment_no=1), _draft(segment_no=1)])
    errors = _validate_all_segments_draft(
        draft, expected_segment_nos=[1, 2], dialect_render_format="seedance_compact_director_brief",
    )
    assert any("重复 [1]" in e for e in errors)


def test_validate_all_segments_draft_prefixes_per_item_format_errors_with_segment_no():
    draft = _AiAllSegmentsDraft(segments=[_draft(segment_no=1, prompt_text=" "), _draft(segment_no=2)])
    errors = _validate_all_segments_draft(
        draft, expected_segment_nos=[1, 2], dialect_render_format="seedance_compact_director_brief",
    )
    assert any(e.startswith("segment_no=1：") and "为空" in e for e in errors)


# ---------------------------------------------------------------------------
# _segment_content_advisories：内容判断照算，只是结论不再是拦截，而是
# 附在产物 degraded_capabilities[] 上的信息（不许用删掉校验函数来实现）。
# ---------------------------------------------------------------------------

def test_segment_content_advisories_empty_for_well_formed_draft():
    advisories = _segment_content_advisories(
        _draft(resources=_AiSegmentResources(
            characters=[{"identity_id": "id_a", "description": "x"}],
            scenes=[{"scene_id": "scene_a", "description": "y"}],
        )),
        known_character_ids={"id_a"},
        known_scene_ids={"scene_a"},
        source_segment_indexes=[1, 2],
        segment_relevant_scene_ids={"scene_a"},
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


def test_segment_content_advisories_flags_invented_identity_id_even_when_manifest_is_empty():
    """真实 EP7 回归的最小复现：本集 asset_manifest.characters/scenes 都恰好是
    空集（映射台这次没能解析出任何角色/场景），模型对同一个角色自造了三种
    不同前缀（character:/char:/ch:）。旧代码里 `known_character_ids and
    identity_id not in known_character_ids` 在 known_character_ids 为空集时
    整条判断短路成 False，八条越界引用一条告警都不产出——比 EP6 用旧映射包
    跑、至少还挂出 CHARACTER_UNKNOWN 标记的年代更静默。空取值域必须被判定
    成"取值域里什么都不合法"，而不是"这一项不用查"。"""
    draft = _draft(
        resources=_AiSegmentResources(
            characters=[
                {"identity_id": "character:孟浩", "description": "x"},
                {"identity_id": "char:孟浩", "description": "x"},
                {"identity_id": "ch:孟浩", "description": "x"},
            ],
            scenes=[{"scene_id": "scene:荒山", "description": "y"}],
        ),
        dialogue=[],
    )
    advisories = _segment_content_advisories(
        draft, known_character_ids=set(), known_scene_ids=set(), source_segment_indexes=[1, 2],
    )
    unknown_character_advisories = [
        a for a in advisories if "STORYBOARD_PACK_RESOURCE_CHARACTER_UNKNOWN" in a
    ]
    unknown_scene_advisories = [
        a for a in advisories if "STORYBOARD_PACK_RESOURCE_SCENE_UNKNOWN" in a
    ]
    assert len(unknown_character_advisories) == 3
    assert len(unknown_scene_advisories) == 1


def test_segment_content_advisories_flags_manifest_gap_when_no_relevant_scenes_exist():
    """真实 EP4 回归：asset_manifest.scenes 只覆盖原文段号 [2..20]，本集共
    54 段，段号落在 [24..51] 的那几段 relevant_assets.scenes 天生是空列表——
    模型没有任何合法 scene_id 可用，resources.scenes 留空是诚实的选择，不是
    这次分镜生成的遗漏，必须标记成映射台侧的缺口，不能和"模型偷懒"混为一谈。
    """
    draft = _draft(resources=_AiSegmentResources(
        characters=[{"identity_id": "id_a", "description": "x"}],
    ))
    advisories = _segment_content_advisories(
        draft, known_character_ids={"id_a"}, known_scene_ids=set(),
        source_segment_indexes=[24, 25], segment_relevant_scene_ids=set(),
    )
    assert any("STORYBOARD_PACK_RESOURCE_SCENE_MANIFEST_GAP" in a for a in advisories)
    assert not any("STORYBOARD_PACK_RESOURCE_SCENE_MISSING" in a for a in advisories)


def test_segment_content_advisories_flags_missing_when_relevant_scenes_available_but_unused():
    """区分于上一条：这次映射台确实给了候选场景（relevant_assets.scenes 非
    空），但模型的 resources.scenes 仍然是空——不能排除"这段确实没有独立
    场景"（例如纯特写/纯对话），所以只标记为需要人工核对，不是确定性错误，
    也不阻断生成。"""
    draft = _draft(resources=_AiSegmentResources(
        characters=[{"identity_id": "id_a", "description": "x"}],
    ))
    advisories = _segment_content_advisories(
        draft, known_character_ids={"id_a"}, known_scene_ids={"scene_a"},
        source_segment_indexes=[1, 2], segment_relevant_scene_ids={"scene_a"},
    )
    assert any("STORYBOARD_PACK_RESOURCE_SCENE_MISSING" in a for a in advisories)
    assert not any("STORYBOARD_PACK_RESOURCE_SCENE_MANIFEST_GAP" in a for a in advisories)


def test_segment_content_advisories_no_scene_advisory_when_scenes_declared():
    """场景确实被声明了（不管是否用满 relevant_assets 里的全部候选），两条
    新标记都不应该出现——它们只在 resources.scenes 为空时才有意义。"""
    draft = _draft(resources=_AiSegmentResources(
        characters=[{"identity_id": "id_a", "description": "x"}],
        scenes=[{"scene_id": "scene_a", "description": "y"}],
    ))
    advisories = _segment_content_advisories(
        draft, known_character_ids={"id_a"}, known_scene_ids={"scene_a"},
        source_segment_indexes=[1, 2], segment_relevant_scene_ids={"scene_a", "scene_b"},
    )
    assert not any("STORYBOARD_PACK_RESOURCE_SCENE_MISSING" in a for a in advisories)
    assert not any("STORYBOARD_PACK_RESOURCE_SCENE_MANIFEST_GAP" in a for a in advisories)


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
# _enrich_asset_manifest_canonical_visuals：世界书标准外观/场景锚点接入
# （问题一修复，真实 EP1 回归：孟浩在 10 段提示词里换了三套衣服。根因是
# asset_manifest.characters[] 从不带外观描述，模型只能从自己这一段的原文
# 现推；原文没写衣着的段落只能各段各编。世界书里的标准外观/场景锚点
# character_portraits.appearance / scene_references.scene_canonical 一直
# 都在，只是没被送给模型——这里补的是管道，不是"叮嘱模型别漂"。）
# ---------------------------------------------------------------------------

def test_enrich_asset_manifest_canonical_visuals_adds_appearance_from_character_portraits():
    conn = db.get_conn()
    _seed_episode(conn, episode_id="ep-visuals-1")
    conn.execute(
        "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end, "
        "appearance, created_at) VALUES(?,?,?,?,?,?,?)",
        (
            "portrait_1", "proj-1", "少年", 1, None,
            "十六七岁的少年男性，黑发碎短利落，皮肤偏黑身形瘦削，常年穿干净的蓝色文士长衫。",
            db.now(),
        ),
    )
    conn.commit()
    payload = _prep_pack_2_0_0_payload()
    _enrich_asset_manifest_canonical_visuals(conn, payload)
    character = payload["asset_manifest"]["characters"][0]
    assert character["appearance"] == (
        "十六七岁的少年男性，黑发碎短利落，皮肤偏黑身形瘦削，常年穿干净的蓝色文士长衫。"
    )


def test_enrich_asset_manifest_canonical_visuals_adds_scene_canonical_from_scene_references():
    conn = db.get_conn()
    _seed_episode(conn, episode_id="ep-visuals-2")
    conn.execute(
        "INSERT INTO scene_references(id, project_id, scene_name, ep_start, ep_end, "
        "scene_canonical, created_at) VALUES(?,?,?,?,?,?,?)",
        ("scene_ref_1", "proj-1", "山顶", 1, None, "云雾缭绕的山顶，一块巨石立于中央。", db.now()),
    )
    conn.commit()
    payload = _prep_pack_2_0_0_payload()
    _enrich_asset_manifest_canonical_visuals(conn, payload)
    scene = payload["asset_manifest"]["scenes"][0]
    assert scene["scene_canonical"] == "云雾缭绕的山顶，一块巨石立于中央。"


def test_enrich_asset_manifest_canonical_visuals_notes_functional_extras_have_no_canonical_appearance():
    # 群演/一次性人物没有 portrait_id，天生没有定妆照：必须给出显式说明，
    # 不能静默留空——留空会被模型读成"没有任何相关信息"，导致同一群演在
    # 不同段落里各编一套外观，这是问题一的同一种漂移换了个没有 portrait_id
    # 的马甲。
    conn = db.get_conn()
    _seed_episode(conn, episode_id="ep-visuals-3")
    conn.commit()
    payload = _prep_pack_2_0_0_payload()
    payload["asset_manifest"]["functional_extras"] = [
        {"label": "绿袍男子", "visual_entity_id": "entity:abc123", "segment_indexes": [1]},
    ]
    _enrich_asset_manifest_canonical_visuals(conn, payload)
    extra = payload["asset_manifest"]["functional_extras"][0]
    assert extra["appearance"]
    assert "没有为这个角色建立标准外观" in extra["appearance"]


def test_enrich_asset_manifest_canonical_visuals_notes_missing_portrait_row_instead_of_empty():
    # 边界：portrait_id 存在但 character_portraits 里查不到这一行（或该行
    # appearance 列本身为空）——同样不得静默留空，退回显式说明而不是空字符串。
    conn = db.get_conn()
    _seed_episode(conn, episode_id="ep-visuals-4")
    conn.commit()
    payload = _prep_pack_2_0_0_payload()  # portrait_id="portrait_1"，未插入任何 character_portraits 行
    _enrich_asset_manifest_canonical_visuals(conn, payload)
    character = payload["asset_manifest"]["characters"][0]
    assert character["appearance"]
    assert "没有为这个角色建立标准外观" in character["appearance"]


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
                beat_ids=["B1"],
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
    segments = _real_segments(conn, ep)
    shot_ids = persist_storyboard_pack(conn, episode_id, ep, payload, _pack(), segments=segments)
    assert len(shot_ids) == 1

    rows = conn.execute("SELECT * FROM shots WHERE episode_id=?", (episode_id,)).fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert row["shot_no"] == 1
    assert row["duration_s"] == 15
    assert row["shot_size"] == ""
    assert row["camera_move"] == ""
    # 逐字契约切片 -- 段落间的真实空白是 index_source_segments 的 "\n\s*\n"
    # 段落分隔，不是代码拼装用的单个 "\n"（persist_storyboard_pack 现在从
    # _resolve_segment_source_binding 取回可核验的原文切片，不再是
    # "\n".join(segment.text ...) 重组文本，见该函数文档）。
    assert row["source_excerpt"] == "少年站在山顶。\n\n他扔掉了葫芦。"
    # 落库时不再有任何"已采用"版本 -- 这一镜此刻还没有生成出任何视频，
    # adopted_version_id 必须诚实地保持空（现象1 的回归防线：分镜台写了
    # 提示词不等于「已采纳」，「已采纳」只能表示真的生成出可用视频并被选中）。
    assert row["adopted_version_id"] is None
    # 同理：落库不再插入占位 shot_versions 行 -- 第一次真实生成才应该是 v1
    # （现象2 的回归防线：占位行曾经白白占掉 version_no=1）。
    assert conn.execute(
        "SELECT COUNT(*) c FROM shot_versions WHERE shot_id=?", (row["id"],),
    ).fetchone()["c"] == 0

    contract = json.loads(row["shot_contract_json"])
    assert contract["prompt_contract_version"] == STORYBOARD_PACK_CONTRACT_MARKER
    assert contract["is_final"] is True
    # prompt_text 落库必须与模型草稿逐字一致 -- 中间没有任何代码重新拼装
    # （交付前必须回答 #2 的验证：见 persist_storyboard_pack 的文档）。落库
    # 前没有 shot_versions 行可读，prompt_text 唯一权威来源就是这里
    # （app.media_exec.enqueue 第一次生成时也是从这个字段读，不再经过
    # adopted_version_id 转一手）。
    assert contract["storyboard_pack_segment"]["prompt_text"] == _pack().segments[0].prompt_text


def test_enqueue_reads_prompt_text_from_contract_not_adopted_version():
    # app.media_exec.enqueue 的 is_storyboard_pack_shot 分支不再查
    # shot_versions（该行落库时已经没有任何版本了），而是直接读
    # _load_shot_model(shot_row).storyboard_pack_segment["prompt_text"]。
    # 这条测试锁定 enqueue 实际依赖的那条读取路径本身 -- 不是重复上面已经
    # 验过的落库内容，是验证 enqueue 真正会走的代码（app.media_exec.enqueue
    # 的 is_storyboard_pack_shot 分支）拿到的值。
    from app.media_exec.enqueue import _load_shot_model

    conn = db.get_conn()
    episode_id = "ep-pack-enqueue-prompt"
    _seed_episode(conn, episode_id=episode_id)
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    payload = _prep_pack_2_0_0_payload()
    segments = _real_segments(conn, ep)
    persist_storyboard_pack(conn, episode_id, ep, payload, _pack(), segments=segments)

    shot_row = conn.execute("SELECT * FROM shots WHERE episode_id=?", (episode_id,)).fetchone()
    shot = _load_shot_model(shot_row)
    assert shot.storyboard_pack_segment is not None
    assert shot.storyboard_pack_segment["prompt_text"] == _pack().segments[0].prompt_text


def test_persist_storyboard_pack_segment_carries_beat_summary_self_contained():
    # 段记录必须自包含：拿到这一个 shot 就能知道它承载的节拍在讲什么，不必
    # 反查一份独立的全集 beat_sheet 去 join。字段名照冻结契约
    # （beat_id/summary/segment_indexes），不发明新名。
    conn = db.get_conn()
    episode_id = "ep-pack-beats"
    _seed_episode(conn, episode_id=episode_id)
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    payload = _prep_pack_2_0_0_payload()
    segments = _real_segments(conn, ep)
    persist_storyboard_pack(conn, episode_id, ep, payload, _pack(), segments=segments)

    row = conn.execute("SELECT * FROM shots WHERE episode_id=?", (episode_id,)).fetchone()
    segment_record = json.loads(row["shot_contract_json"])["storyboard_pack_segment"]

    # 既有裸 ID 键原样保留，不贸然删 -- 老消费方（若有）不受影响。
    assert segment_record["beat_ids"] == ["B1"]
    # 新键携带完整节拍记录，字段名与冻结契约 beat_sheet[] 项一致。
    assert segment_record["beats"] == [
        {"beat_id": "B1", "summary": "他扔掉了理想", "segment_indexes": [1, 2]},
    ]


def test_persist_storyboard_pack_segment_beats_empty_when_declared_beat_ids_empty():
    # beat_ids 的真源是模型在节拍阶段自报的 segment.beat_ids（与该段自身提示词
    # beat_summaries 同源），不是 segment_indexes 与 beat.segment_indexes 的交集
    # 代理判定——交集只是代理，两个维度可能在边界处各说各话。
    conn = db.get_conn()
    episode_id = "ep-pack-beats-empty"
    _seed_episode(conn, episode_id=episode_id)
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    payload = _prep_pack_2_0_0_payload()
    segments = _real_segments(conn, ep)
    pack = _pack()
    # 模型自报本段不承载任何节拍：即使 source_segment_indexes 仍与 beat.segment_indexes
    # 重叠，落库也必须服从模型自报，不得靠交集"顺手"补回一个模型没声明的节拍。
    pack.segments[0].beat_ids = []
    persist_storyboard_pack(conn, episode_id, ep, payload, pack, segments=segments)

    row = conn.execute("SELECT * FROM shots WHERE episode_id=?", (episode_id,)).fetchone()
    segment_record = json.loads(row["shot_contract_json"])["storyboard_pack_segment"]
    assert segment_record["beat_ids"] == []
    assert segment_record["beats"] == []


def test_persist_storyboard_pack_segment_beats_follow_declared_ids_not_index_overlap():
    # 反向证明：即使 source_segment_indexes 挪到与 beat.segment_indexes 不再重叠的
    # 段号，只要模型自报的 beat_ids 仍引用该节拍，落库的 beats 就必须保留它——
    # 证明真源是 beat_ids，不是交集代理（交集判定会在这个场景下把它错误地判空）。
    conn = db.get_conn()
    episode_id = "ep-pack-beats-follow-declared"
    _seed_episode(conn, episode_id=episode_id)
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    payload = _prep_pack_2_0_0_payload()
    segments = _real_segments(conn, ep)
    pack = _pack()
    # 节拍 B1.segment_indexes=[1, 2]；把段的原文范围挪到不重叠的第 3 段，
    # 但仍保留 beat_ids=["B1"]（模型自报本段承载 B1）。
    pack.segments[0].source_segment_indexes = [3]
    persist_storyboard_pack(conn, episode_id, ep, payload, pack, segments=segments)

    row = conn.execute("SELECT * FROM shots WHERE episode_id=?", (episode_id,)).fetchone()
    segment_record = json.loads(row["shot_contract_json"])["storyboard_pack_segment"]
    assert segment_record["beat_ids"] == ["B1"]
    assert segment_record["beats"] == [
        {"beat_id": "B1", "summary": "他扔掉了理想", "segment_indexes": [1, 2]},
    ]


def test_persist_storyboard_pack_writes_standalone_beat_sheet_artifact():
    # 顺带确认：整份节拍表（含摘要）除了段记录之外，必须还有一个独立落点，
    # 否则事后无法复盘"段数是怎么定出来的"。
    conn = db.get_conn()
    episode_id = "ep-pack-artifact"
    _seed_episode(conn, episode_id=episode_id)
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    payload = _prep_pack_2_0_0_payload()
    segments = _real_segments(conn, ep)
    persist_storyboard_pack(conn, episode_id, ep, payload, _pack(), segments=segments)

    artifact_row = conn.execute(
        "SELECT content_json FROM artifacts WHERE type='storyboard_pack_beat_sheet' AND scope_id=?",
        (episode_id,),
    ).fetchone()
    assert artifact_row is not None
    content = json.loads(artifact_row["content_json"])
    assert content["segment_count"] == 1
    assert content["beat_sheet"] == [
        {"beat_id": "B1", "summary": "他扔掉了理想", "segment_indexes": [1, 2]},
    ]


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
    segments = _real_segments(conn, ep)
    pack = _pack_with_misattributed_speaker()
    # 顺利保存：persist 不抛异常、不丢段落。
    shot_ids = persist_storyboard_pack(conn, episode_id, ep, payload, pack, segments=segments)
    assert len(shot_ids) == 1

    row = conn.execute("SELECT * FROM shots WHERE episode_id=?", (episode_id,)).fetchone()
    contract = json.loads(row["shot_contract_json"])
    # prompt_text 原样落库，未被这条内容缺陷影响。
    assert contract["storyboard_pack_segment"]["prompt_text"] == pack.segments[0].prompt_text
    # 落库不等于已采纳：这一镜还没有生成任何视频。
    assert row["adopted_version_id"] is None

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


# ---------------------------------------------------------------------------
# run_storyboard_pack_generation 的 resume 短路：判据必须只看产物本身
# （回归锁 -- ep_3d523ff4d0a4 事故：10 段成品被 resume 悄悄吃成 7 段）
# ---------------------------------------------------------------------------

def test_resume_reuses_existing_shots_without_regenerating_despite_scripting_status(monkeypatch):
    """resume_storyboard()（app/domain/storyboard_ops.py）这个 HTTP 路由，在派发
    生成任务之前会先把 episodes.status 改成 'scripting' 并提交（供
    _storyboard_generation_is_live 之类的去重使用），然后才 spawn 任务；
    run_storyboard_supervisor 随后重新 SELECT 出来的 ep 快照因此必然是
    'scripting'。旧短路条件判 ``ep["status"] in ("scripted","confirmed",
    "generating","done")``，'scripting' 不在其中——短路对任何一次真实 resume
    请求都必然判不过（不是偶发），直接落到下面的全量重灌分支：DELETE 全部
    shots、重新调模型、段数因模型自由裁量而不稳定。真实事故（ep_3d523ff4d0a4，
    run_84f1d96f9963）就是这样把已通过、已采纳的 10 段吃成了 7 段。

    这里直接复现那个必然出现的中间状态（episodes.status='scripting'），锁死
    修复后的行为：判据只看产物本身（每行都带当前 STORYBOARD_PACK_CONTRACT_
    MARKER、尾镜 is_final=True），不再看 episodes.status——短路必须仍然生效：
    不调用 generate_storyboard_pack（不重新调模型/不产生新 provider_calls）、
    shots 行数与 id 原封不动。
    """
    import app.production.storyboard_pack as storyboard_pack_module

    conn = db.get_conn()
    episode_id = "ep-resume-guard"
    _seed_episode(conn, episode_id=episode_id)
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    payload = _prep_pack_2_0_0_payload()
    segments = _real_segments(conn, ep)
    shot_ids = persist_storyboard_pack(conn, episode_id, ep, payload, _pack(), segments=segments)
    assert shot_ids

    # 复现 resume_storyboard() 派发任务前必然造成的中间状态。
    conn.execute("UPDATE episodes SET status='scripting' WHERE id=?", (episode_id,))
    conn.commit()
    ep_mid_resume = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    assert ep_mid_resume["status"] == "scripting"

    before_shots = [
        dict(row) for row in conn.execute(
            "SELECT id, shot_no FROM shots WHERE episode_id=? ORDER BY shot_no", (episode_id,),
        ).fetchall()
    ]
    before_calls = conn.execute("SELECT COUNT(*) AS c FROM provider_calls").fetchone()["c"]

    def _must_not_call(*_args, **_kwargs):
        raise AssertionError(
            "generate_storyboard_pack 不应被调用：已有完整产物必须走 resume 短路，"
            "绝不重新调模型"
        )

    monkeypatch.setattr(storyboard_pack_module, "generate_storyboard_pack", _must_not_call)

    cp = asyncio.run(
        storyboard_pack_module.run_storyboard_pack_generation(
            episode_id, ep=ep_mid_resume, conn=conn, payload=payload, resume=True,
        )
    )

    assert cp.phase == "SUCCEEDED"
    assert cp.outcome == "SUCCEEDED_READY_FOR_CONFIRM"
    assert cp.validated_prefix_end == len(before_shots)
    assert cp.expected_total == len(before_shots)

    after_shots = [
        dict(row) for row in conn.execute(
            "SELECT id, shot_no FROM shots WHERE episode_id=? ORDER BY shot_no", (episode_id,),
        ).fetchall()
    ]
    assert after_shots == before_shots

    after_calls = conn.execute("SELECT COUNT(*) AS c FROM provider_calls").fetchone()["c"]
    assert after_calls == before_calls


# ---------------------------------------------------------------------------
# resolve_shot_asset_dependencies 分镜包分支的 conn 依赖
# （回归锁 -- ERR-20260826-99049d 事故：整集生成 500，
# AttributeError: 'NoneType' object has no attribute 'execute'。
# app/domain/video_ops.py:_generate_episode_core -> scan_episode_reference_
# asset_gaps -> resolve_shot_asset_dependencies(conn=None，默认值) ->
# _storyboard_pack_asset_dependencies 里对段落 portrait_id / scene_
# reference_id 无条件 conn.execute(...)。当天 EP2/3/4/7/8/9/10 的每个片段
# 都至少带一个非空 portrait_id/scene_reference_id，所以每一集都必崩。）
# ---------------------------------------------------------------------------

def test_scan_episode_reference_asset_gaps_requires_conn_kwarg():
    """scan_episode_reference_asset_gaps 的 conn 形参不再有默认值——两个真正
    调用方（video_ops._generate_episode_core、video_supervisor._reference_
    asset_scan）手上都已经有 conn，留 None 默认值只会邀请下一个调用方漏传，
    在分镜包分支里静默摔进三层深的 AttributeError。这里锁死：漏传必须在
    调用边界就地报错（TypeError），不能悄悄用 None 往下传。
    """
    from app.multiview import scan_episode_reference_asset_gaps

    with pytest.raises(TypeError):
        scan_episode_reference_asset_gaps(  # type: ignore[call-arg]
            project_id="proj-1", episode_no=1, shots=[],
        )


def test_resolve_shot_asset_dependencies_requires_conn_kwarg():
    """resolve_shot_asset_dependencies 的 conn 也不再有默认值——分镜包分支
    （segment 不为 None）对 conn 是硬依赖：段落里的 portrait_id/scene_
    reference_id 要求直接按 ID 查 character_portraits/scene_references，无
    条件 conn.execute(...)。曾经的 conn=None 默认值就是 ERR-20260826-99049d
    事故的根因：调用方漏传时不在调用点报错，而是拖到三层深处才炸出一个
    无法定位的 AttributeError。这里锁死：漏传必须在调用那一刻就是
    TypeError，不能悄悄用 None 往下传（也不允许回退到 get_conn() 静默补
    上——get_conn() 按 asyncio task 缓存连接，可能不是调用方事务里的那个
    连接，用它兜底等于把「少传一个参数」的缺陷永久藏起来）。
    """
    from app.multiview import resolve_shot_asset_dependencies
    from app.schemas import Shot

    shot = Shot(
        shot_no=1, duration_s=5, shot_size="近景", camera_move="固定",
        scene_setting="室内", characters=[], characters_visible=[],
        action_desc="x", first_frame_desc="x", last_frame_desc="x",
        state_in="x", primary_action="x", state_out="x", source_excerpt="x",
    )
    with pytest.raises(TypeError):
        resolve_shot_asset_dependencies(  # type: ignore[call-arg]
            project_id="proj-1", episode_no=1, shot_id="s", shot=shot,
        )


def test_resolve_shot_asset_dependencies_storyboard_pack_branch_resolves_real_assets_when_conn_passed(
    tmp_path,
):
    """正向路径：调用方按契约真正传了 conn 时，分镜包分支必须解析出刚插入
    的资产行——不是空/半成品 manifest（那正是这条修复要避免重蹈的旧覆
    辙：场景参考图从来没挂上、跑了无数次没人发现）。这里复现整集生成入口
    实际会构造的段落形状（真实 persist_storyboard_pack 落库），验证按正确
    契约调用能拿到与单段生成一致的数据。
    """
    from app.media_exec.enqueue import _load_shot_model
    from app.multiview import resolve_shot_asset_dependencies

    conn = db.get_conn()
    episode_id = "ep-pack-conn-regress"
    _seed_episode(conn, episode_id=episode_id)
    portrait_image = tmp_path / "portrait.png"
    portrait_image.write_bytes(b"fake-png")
    scene_image = tmp_path / "scene.png"
    scene_image.write_bytes(b"fake-png")
    conn.execute(
        "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end, "
        "image_path, created_at) VALUES(?,?,?,?,?,?,?)",
        ("portrait_1", "proj-1", "少年", 1, None, str(portrait_image), db.now()),
    )
    conn.execute(
        "INSERT INTO scene_references(id, project_id, scene_name, ep_start, ep_end, "
        "image_path, created_at) VALUES(?,?,?,?,?,?,?)",
        ("scene_ref_1", "proj-1", "山顶", 1, None, str(scene_image), db.now()),
    )
    conn.commit()
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    payload = _prep_pack_2_0_0_payload()
    segments = _real_segments(conn, ep)
    shot_ids = persist_storyboard_pack(conn, episode_id, ep, payload, _pack(), segments=segments)
    row = conn.execute("SELECT * FROM shots WHERE id=?", (shot_ids[0],)).fetchone()
    shot = _load_shot_model(row)
    assert shot.storyboard_pack_segment is not None

    manifest = resolve_shot_asset_dependencies(
        project_id="proj-1", episode_no=1, shot_id=row["id"], shot=shot, conn=conn,
    )
    character = manifest["characters"][0]
    assert character["look_revision_id"] == "portrait_1"
    assert character["selected_views"][0]["image_path"] == str(portrait_image)
    scene = manifest["scene"]
    assert scene["scene_revision_id"] == "scene_ref_1"
    assert scene["selected_views"][0]["image_path"] == str(scene_image)


def test_storyboard_pack_asset_dependencies_resolves_every_declared_scene_not_just_first(
    tmp_path,
):
    """多场景转场段（一段 = 多镜，段内转场到第二个地点）此前只挂第一个
    scene 的参考图：``_storyboard_pack_asset_dependencies`` 写死取
    ``resources.scenes[0]``，第二个及以后声明的场景连同它的参考图整个消
    失，没有任何可见信号。实测复现见 EP2 段2/shot_53d87e5d107d（两个 scene
    声明，镜头3/4 确实转场到了第二个地点）。这里构造同样形状的两场景段，
    验证修复后两个场景都进了 manifest，且都能展开成可提交的参考图锚点。
    """
    from app.multiview import _storyboard_pack_asset_dependencies, library_anchor_assets_from_manifest

    conn = db.get_conn()
    episode_id = "ep-pack-multiscene"
    _seed_episode(conn, episode_id=episode_id)
    scene_image_1 = tmp_path / "scene1.png"
    scene_image_1.write_bytes(b"fake-png-1")
    scene_image_2 = tmp_path / "scene2.png"
    scene_image_2.write_bytes(b"fake-png-2")
    conn.execute(
        "INSERT INTO scene_references(id, project_id, scene_name, ep_start, ep_end, "
        "image_path, created_at) VALUES(?,?,?,?,?,?,?)",
        ("scene_ref_a", "proj-1", "山顶", 1, None, str(scene_image_1), db.now()),
    )
    conn.execute(
        "INSERT INTO scene_references(id, project_id, scene_name, ep_start, ep_end, "
        "image_path, created_at) VALUES(?,?,?,?,?,?,?)",
        ("scene_ref_b", "proj-1", "山脚", 1, None, str(scene_image_2), db.now()),
    )
    conn.commit()

    segment = {
        "segment_no": 1,
        "resources": {
            "characters": [],
            "scenes": [
                {"scene_id": "scene:山顶", "scene_reference_id": "scene_ref_a"},
                {"scene_id": "scene:山脚", "scene_reference_id": "scene_ref_b"},
            ],
        },
    }
    manifest = _storyboard_pack_asset_dependencies(
        episode_no=1, shot_id="shot-x", segment=segment, conn=conn,
    )
    assert manifest["scene"]["name"] == "山顶"
    assert manifest["scene"]["scene_revision_id"] == "scene_ref_a"
    assert [s["name"] for s in manifest["additional_scenes"]] == ["山脚"]
    assert manifest["additional_scenes"][0]["scene_revision_id"] == "scene_ref_b"

    anchors = library_anchor_assets_from_manifest(manifest)
    scene_anchors = {a["entity_name"]: a["image_path"] for a in anchors if a["entity_type"] == "scene"}
    assert scene_anchors == {
        "山顶": str(scene_image_1),
        "山脚": str(scene_image_2),
    }


def test_storyboard_pack_asset_dependencies_second_scene_unresolvable_is_visible_not_silent(
    tmp_path,
):
    """第二个场景声明了但 scene_reference_id 查不到可用图（同样的失败模式，
    主场景一直就有这条检查）时，manifest_production_blockers 必须报出来——
    可见不拦截，不能像旧代码那样连声明都消失，事后无法核对。"""
    from app.multiview import _storyboard_pack_asset_dependencies, manifest_production_blockers

    conn = db.get_conn()
    episode_id = "ep-pack-multiscene-gap"
    _seed_episode(conn, episode_id=episode_id)
    scene_image_1 = tmp_path / "scene1.png"
    scene_image_1.write_bytes(b"fake-png-1")
    conn.execute(
        "INSERT INTO scene_references(id, project_id, scene_name, ep_start, ep_end, "
        "image_path, created_at) VALUES(?,?,?,?,?,?,?)",
        ("scene_ref_a", "proj-1", "山顶", 1, None, str(scene_image_1), db.now()),
    )
    conn.commit()

    segment = {
        "segment_no": 1,
        "resources": {
            "characters": [],
            "scenes": [
                {"scene_id": "scene:山顶", "scene_reference_id": "scene_ref_a"},
                {"scene_id": "scene:失踪场景", "scene_reference_id": "scene_ref_missing"},
            ],
        },
    }
    manifest = _storyboard_pack_asset_dependencies(
        episode_no=1, shot_id="shot-x", segment=segment, conn=conn,
    )
    blockers = manifest_production_blockers(manifest)
    assert any("失踪场景" in b for b in blockers)


# ---------------------------------------------------------------------------
# 2.0.6：阶段二答案预算必须给思考预留留位置，装不下就顺序分批
# ---------------------------------------------------------------------------

_LIMITS_32K = {
    "context_window_tokens": 131072,
    "max_output_tokens": 32768,
    "token_limits_source": "test",
}


def _patch_thinking_model(monkeypatch, *, max_output: int = 32768, reserve: int | None = None) -> None:
    from app import hiagent

    monkeypatch.setattr(hiagent, "active_provider", lambda _kind: "hiagent")
    monkeypatch.setattr(hiagent, "active_model", lambda *_args, **_kwargs: "thinking-model")
    monkeypatch.setattr(
        hiagent,
        "active_model_token_limits",
        lambda *_args, **_kwargs: {**_LIMITS_32K, "max_output_tokens": max_output},
    )
    monkeypatch.setattr(
        hiagent,
        "get_setting",
        lambda key: (
            str(reserve)
            if reserve is not None and key == "text_reasoning_token_reserve"
            else ""
        ),
    )


def test_contract_marker_stays_on_2_0_5_so_existing_packs_resume():
    """2.0.6 只改生成切分，不改落库形状；resume 仍认 2.0.5 marker。"""
    assert STORYBOARD_PACK_CONTRACT_MARKER == "storyboard_pack/2.0.5"
    assert STORYBOARD_PACK_VERSION == "2.0.6"


def test_twelve_segment_answer_budget_saturates_32k_with_reasoning(monkeypatch):
    """生产事故指纹：12 段一次调用的答案+思考预留会把 32768 打满。"""
    _patch_thinking_model(monkeypatch)
    answer = _segment_prompt_answer_budget(12)
    assert answer == 12 * SEGMENT_BATCH_TOKENS_PER_SEGMENT
    from app import hiagent

    _, _, effective = hiagent.text_request_token_limits(requested_max_tokens=answer)
    assert effective == 32768
    assert answer + hiagent.reasoning_token_reserve() > 32768


def test_batch_capacity_leaves_room_for_reasoning_under_32k_cap(monkeypatch):
    _patch_thinking_model(monkeypatch)
    from app import hiagent

    capacity = _segment_prompt_batch_capacity(12)
    assert 1 <= capacity < 12
    assert (
        _segment_prompt_answer_budget(capacity) + hiagent.reasoning_token_reserve()
        < 32768
    )
    assert (
        _segment_prompt_answer_budget(capacity + 1) + hiagent.reasoning_token_reserve()
        >= 32768
    )


def test_small_episode_stays_a_single_batch(monkeypatch):
    _patch_thinking_model(monkeypatch)
    assert _split_segment_prompt_batches([1, 2, 3, 4]) == [[1, 2, 3, 4]]


def test_twelve_segments_split_into_batches_that_fit(monkeypatch):
    _patch_thinking_model(monkeypatch)
    batches = _split_segment_prompt_batches(list(range(1, 13)))
    assert len(batches) >= 2
    assert [no for batch in batches for no in batch] == list(range(1, 13))
    from app import hiagent

    for batch in batches:
        assert (
            _segment_prompt_answer_budget(len(batch)) + hiagent.reasoning_token_reserve()
            < 32768
        )


def test_empty_segment_list_does_not_skip_the_split_check():
    """空集合不是『无需检查』：没有段落就不该假装切出一批。"""
    assert _split_segment_prompt_batches([]) == []


def test_non_thinking_model_keeps_twelve_segments_in_one_batch(monkeypatch):
    _patch_thinking_model(monkeypatch, reserve=0)
    assert _split_segment_prompt_batches(list(range(1, 13))) == [list(range(1, 13))]


def test_single_batch_task_text_keeps_whole_episode_wording():
    text = _segment_prompt_task_text(
        write_nos=[1, 2, 3], all_nos=[1, 2, 3], has_already_written=False,
    )
    assert "一次性联合产出" in text
    assert "already_written_segments" not in text


def test_followup_batch_task_text_points_at_already_written():
    text = _segment_prompt_task_text(
        write_nos=[7, 8],
        all_nos=list(range(1, 13)),
        has_already_written=True,
    )
    assert "already_written_segments" in text
    assert "[7, 8]" in text
    assert "一次性联合产出" not in text


def _segment_draft(segment_no: int) -> _AiStoryboardSegmentDraft:
    return _AiStoryboardSegmentDraft(
        segment_no=segment_no,
        prompt_text=f"电影级预告片质感，多镜头叙事。镜头1 段{segment_no}。",
        shot_count=3,
    )


@pytest.mark.asyncio
async def test_generate_splits_and_passes_already_written_to_later_batches(monkeypatch):
    import app.production.storyboard_pack as storyboard_pack_module

    monkeypatch.setattr(
        storyboard_pack_module, "_segment_prompt_batch_capacity", lambda _total: 2,
    )
    calls: list[dict] = []

    async def fake_chat_structured(messages, **kwargs):
        payload = json.loads(messages[1]["content"])
        write_nos = list(kwargs["call_meta"]["write_segment_nos"])
        calls.append(
            {
                "payload": payload,
                "max_tokens": kwargs["max_tokens"],
                "write_nos": write_nos,
            }
        )
        return _AiAllSegmentsDraft(
            segments=[_segment_draft(no) for no in write_nos]
        )

    monkeypatch.setattr(
        storyboard_pack_module.model_gateway, "chat_structured", fake_chat_structured,
    )
    beat_draft = _AiBeatSheetDraft(
        beat_sheet=[_AiBeat(beat_id="B1", summary="他扔掉了理想", segment_indexes=[1])],
        segments=[
            _AiSegmentPlan(
                segment_no=index,
                synopsis=f"段{index}",
                source_segment_indexes=[1],
                beat_ids=["B1"],
            )
            for index in range(1, 5)
        ],
    )
    source = [
        SourceSegment(segment_id="s1", text="少年站在山顶。", start_offset=0, end_offset=7),
    ]
    result = await _generate_all_segment_prompts(
        episode_id="ep-truncation",
        episode_no=1,
        beat_draft=beat_draft,
        segments=source,
        payload={},
        target_video_model="hiagent",
        bible=None,
    )
    assert list(result) == [1, 2, 3, 4]
    assert [call["write_nos"] for call in calls] == [[1, 2], [3, 4]]
    assert "already_written_segments" not in calls[0]["payload"]
    written = calls[1]["payload"]["already_written_segments"]
    assert [item["segment_no"] for item in written] == [1, 2]
    assert written[0]["prompt_text"] == result[1].prompt_text
    assert calls[0]["max_tokens"] == _segment_prompt_answer_budget(2)
    # 整集视野仍在每一次调用里：不是退回互不可见的逐段并行。
    assert [unit["segment_no"] for unit in calls[0]["payload"]["segments"]] == [1, 2, 3, 4]
    assert [unit["segment_no"] for unit in calls[1]["payload"]["segments"]] == [1, 2, 3, 4]


@pytest.mark.asyncio
async def test_generate_keeps_single_call_when_the_episode_fits(monkeypatch):
    import app.production.storyboard_pack as storyboard_pack_module

    monkeypatch.setattr(
        storyboard_pack_module, "_segment_prompt_batch_capacity", lambda total: total,
    )
    calls: list[dict] = []

    async def fake_chat_structured(messages, **kwargs):
        payload = json.loads(messages[1]["content"])
        calls.append(payload)
        write_nos = list(kwargs["call_meta"]["write_segment_nos"])
        return _AiAllSegmentsDraft(
            segments=[_segment_draft(no) for no in write_nos]
        )

    monkeypatch.setattr(
        storyboard_pack_module.model_gateway, "chat_structured", fake_chat_structured,
    )
    beat_draft = _AiBeatSheetDraft(
        beat_sheet=[_AiBeat(beat_id="B1", summary="x", segment_indexes=[1])],
        segments=[
            _AiSegmentPlan(segment_no=1, synopsis="a", source_segment_indexes=[1], beat_ids=["B1"]),
            _AiSegmentPlan(segment_no=2, synopsis="b", source_segment_indexes=[1], beat_ids=["B1"]),
        ],
    )
    source = [
        SourceSegment(segment_id="s1", text="少年站在山顶。", start_offset=0, end_offset=7),
    ]
    result = await _generate_all_segment_prompts(
        episode_id="ep-small",
        episode_no=1,
        beat_draft=beat_draft,
        segments=source,
        payload={},
        target_video_model="hiagent",
        bible=None,
    )
    assert set(result) == {1, 2}
    assert len(calls) == 1
    assert "already_written_segments" not in calls[0]
    assert "一次性联合产出" in calls[0]["task"]
