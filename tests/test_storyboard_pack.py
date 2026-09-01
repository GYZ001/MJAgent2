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
    CAMERA_DIGEST_WINDOW,
    STORYBOARD_PACK_CONTRACT_MARKER,
    STORYBOARD_PACK_VERSION,
    MINIMAX_H3_DIALECT_INSTRUCTIONS,
    SEGMENT_PROMPT_ANSWER_TOKENS,
    StoryboardPack,
    StoryboardPackBeat,
    StoryboardPackSegment,
    _AiBeat,
    _AiBeatSheetDraft,
    _AiCameraDigest,
    _AiDialogueLine,
    _AiSegmentPlan,
    _AiSegmentResources,
    _AiStoryboardSegmentDraft,
    _camera_digest_window_payload,
    _dialect_for_target_video_model,
    _enrich_asset_manifest_canonical_visuals,
    _ensure_segment_prompt_budget,
    _generate_all_segment_prompts,
    _paratext_exclusion_rule,
    _paratext_segment_indexes,
    _segment_content_advisories,
    _segment_continuity_rules,
    _segment_source_block,
    StoryboardPackBudgetError,
    _source_block_for_prompt,
    _strip_paratext_from_beat_draft,
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


def test_segment_content_advisories_flags_dialogue_that_cannot_fit_in_fifteen_seconds():
    """台词写超 15 秒的口播容量要留下可见信号。

    真实产出（EP1，提示词收窄之前）：段 6 写了 172 字、段 11 写了 175 字，
    都是 config.MAX_SPOKEN_CHARS_PER_SHOT(=54) 的三倍多。视频模型只能抢读或
    整句吞掉超出的部分，而它吞哪一句无法预测——静默通过等于把这个损失藏起来。

    容量口径不在这里另发明：字数用 spoken_contract.content_char_count，上限用
    config.MAX_SPOKEN_CHARS_PER_SHOT，两者都是全仓库既有的唯一口径。
    """
    over = _draft(dialogue=[
        _AiDialogueLine(
            speaker_identity_id="id_a",
            line="你还没说，你们到底怎么下去的？飞！扯淡，飞什么飞，你能飞下去，现在怎么不飞上来。",
            source_segment_index=1,
        ),
        _AiDialogueLine(
            speaker_identity_id="id_a",
            line="我们是被一个会飞的女人抓过来的，说是要带我们去什么宗做杂役。会飞？那是传说中的仙人，谁信啊。",
            source_segment_index=1,
        ),
    ])
    advisories = _segment_content_advisories(
        over, known_character_ids={"id_a"}, known_scene_ids=set(),
        source_segment_indexes=[1, 2],
    )
    flagged = [a for a in advisories if "STORYBOARD_PACK_DIALOGUE_OVER_CAPACITY" in a]
    assert len(flagged) == 1, "超容量必须留下且只留下一条信号"
    assert "[未拦截]" in flagged[0], "用户拍板第一版分镜提示词不设门禁，这条只能是信息"
    assert str(config.MAX_SPOKEN_CHARS_PER_SHOT) in flagged[0], "要告诉人上限是多少"


def test_segment_content_advisories_silent_when_dialogue_fits_the_shot():
    """容量之内不出声——只有真超了才提示，否则这条信号会被当成噪音忽略掉。"""
    fits = _draft(dialogue=[
        _AiDialogueLine(speaker_identity_id="id_a", line="靠山宗。", source_segment_index=1),
        _AiDialogueLine(
            speaker_identity_id="id_a", line="这……这里是什么地方？", source_segment_index=1,
        ),
    ])
    advisories = _segment_content_advisories(
        fits, known_character_ids={"id_a"}, known_scene_ids=set(),
        source_segment_indexes=[1, 2],
    )
    assert not any("OVER_CAPACITY" in a for a in advisories)


def test_dialect_instructions_carry_the_real_spoken_capacity_number():
    """两种方言都要把真实上限数字写给模型，而不是留下未插值的模板占位。

    这两段指令是 f-string；漏掉 f 前缀会让「{config.MAX_SPOKEN_CHARS_PER_SHOT}」
    原样发给模型，模型只会照抄一个花括号表达式。这条测试同时守住插值发生了、
    并且插的是全仓库那个唯一口径的值。
    """
    from app.production.storyboard_pack import SEEDANCE_DIALECT_INSTRUCTIONS

    limit = str(config.MAX_SPOKEN_CHARS_PER_SHOT)
    for name, text in (
        ("seedance", SEEDANCE_DIALECT_INSTRUCTIONS),
        ("minimax_h3", MINIMAX_H3_DIALECT_INSTRUCTIONS),
    ):
        assert "{" not in text and "}" not in text, f"{name} 有未插值的占位符"
        assert limit in text, f"{name} 没把口播上限 {limit} 写给模型"


def test_dialect_tells_the_model_what_to_do_with_a_character_that_has_no_reference():
    """@名字 的长相由参考图负责，所以没有参考图的角色必须有另一条明确写法。

    真实故障（《黄英》EP1 镜6）：吕氏是映射台的 functional_extras，
    relevant_assets.characters 里查不到她。模型在镜头2 写足了她的外观，
    镜头3 按「此后只 @点名」的规则改成「固定 @吕氏 把一小袋粮食递到黄英
    手里」——而 @吕氏 绑不到任何参考图，图没有、文字又不再描述长相，这一镜
    她的长相彻底没有来源。
    """
    from app.production.storyboard_pack import SEEDANCE_DIALECT_INSTRUCTIONS

    text = SEEDANCE_DIALECT_INSTRUCTIONS
    assert "relevant_assets.characters" in text, (
        "必须告诉模型判据从哪来：能不能用 @ 简写，取决于这个人在不在 "
        "relevant_assets.characters 里"
    )
    # 只说「有图的可以 @」不够，还要正面交代没图的那类人怎么写。
    head = text.index("relevant_assets.characters 里收录的")
    rule = text[head:head + 400]
    assert "没有任何" in rule and "参考图" in rule, "没有说明未收录角色缺的是参考图"
    assert "不要用 @" in rule or "不用 @" in rule, "没有正面说清未收录角色不加 @ 前缀"
    assert "每一个出现他的镜头" in rule, "没有要求未收录角色每镜自带外观特征"


def test_unknown_character_advisory_says_what_actually_happened() -> None:
    """降级信号不能声称做了它没做的事。

    这条 advisory 原文写「已按纯文字描述处理」，可代码只是记了一句话，
    prompt_text 里的 @吕氏 原样保留——承诺与行为不一致。
    """
    draft = _draft(
        resources=_AiSegmentResources(
            characters=[{"identity_id": "吕氏", "description": "做针线的妇人"}],
        ),
    )
    advisories = _segment_content_advisories(
        draft, known_character_ids={"bible:马子才"}, known_scene_ids=set(),
        source_segment_indexes=[1],
    )
    unknown = [a for a in advisories if "RESOURCE_CHARACTER_UNKNOWN" in a]
    assert unknown, "未知身份必须报出来"
    assert "已按纯文字描述处理" not in unknown[0], "不得声称做了未做的处理"
    assert "参考图" in unknown[0], "要说清缺的是什么：没有可绑定的人物参考图"


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


def test_enrich_asset_manifest_canonical_visuals_falls_back_to_bible_appearance_without_portrait_row():
    """任务V收尾（真实 EP1 回归 ERR-20260831-63a9d2）：出图解耦到后台后，
    映射台可能已经把角色/场景解析到人物谱/场景库的卡，但 character_portraits/
    scene_references 还没有对应行（图还没出）。外观/场景锚点必须回退世界书
    原始 appearance_canonical/scene_canonical，不是 _NO_CANONICAL_APPEARANCE_
    NOTE 兜底——那句兜底只该留给素材库压根没有这张卡的群演/一次性人物。"""
    conn = db.get_conn()
    _seed_episode(conn, episode_id="ep-visuals-5")
    conn.commit()
    payload = _prep_pack_2_0_0_payload()  # portrait_id/scene_reference_id 均无对应行
    bible = Bible.model_validate({
        "characters": [{
            "name": "少年", "role": "主角",
            "appearance_canonical": "十六七岁少年，黑发碎短，蓝色文士长衫。",
        }],
        "scenes": [{"name": "山顶", "scene_canonical": "云雾缭绕的山顶。"}],
        "world": {"era": "", "genre": "", "visual_style_canonical": "测试画风"},
    })
    _enrich_asset_manifest_canonical_visuals(conn, payload, bible=bible)
    character = payload["asset_manifest"]["characters"][0]
    scene = payload["asset_manifest"]["scenes"][0]
    assert character["appearance"] == "十六七岁少年，黑发碎短，蓝色文士长衫。"
    assert scene["scene_canonical"] == "云雾缭绕的山顶。"


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
    from app.schemas import Bible, Character, Scene, World

    conn = db.get_conn()
    episode_id = "ep-pack-conn-regress"
    _seed_episode(conn, episode_id=episode_id)
    portrait_image = tmp_path / "portrait.png"
    portrait_image.write_bytes(b"fake-png")
    scene_image = tmp_path / "scene.png"
    scene_image.write_bytes(b"fake-png")
    # character_name/scene_name 对齐 _pack() 段落资源里实际的 identity_id/
    # scene_id（"id_a"/"scene_1"，见 _pack() fixture）——现在按名字实时查
    # library，不再按段落固化的 portrait_id/scene_reference_id 直接 SELECT
    # id=? 拿行，两边名字必须对得上。
    conn.execute(
        "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end, "
        "image_path, created_at) VALUES(?,?,?,?,?,?,?)",
        ("portrait_1", "proj-1", "id_a", 1, None, str(portrait_image), db.now()),
    )
    conn.execute(
        "INSERT INTO scene_references(id, project_id, scene_name, ep_start, ep_end, "
        "image_path, created_at) VALUES(?,?,?,?,?,?,?)",
        ("scene_ref_1", "proj-1", "scene_1", 1, None, str(scene_image), db.now()),
    )
    conn.commit()
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    payload = _prep_pack_2_0_0_payload()
    segments = _real_segments(conn, ep)
    shot_ids = persist_storyboard_pack(conn, episode_id, ep, payload, _pack(), segments=segments)
    row = conn.execute("SELECT * FROM shots WHERE id=?", (shot_ids[0],)).fetchone()
    shot = _load_shot_model(row)
    assert shot.storyboard_pack_segment is not None
    bible = Bible(
        characters=[Character(name="id_a", role="配角", appearance_canonical="少年，青衫")],
        world=World(visual_style_canonical="写实"),
        scenes=[Scene(name="scene_1", scene_canonical="山顶，日，云雾")],
    )

    manifest = resolve_shot_asset_dependencies(
        project_id="proj-1", episode_no=1, shot_id=row["id"], shot=shot, conn=conn, bible=bible,
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
    from app.schemas import Bible, Scene, World

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
    bible = Bible(
        characters=[], world=World(visual_style_canonical="写实"),
        scenes=[
            Scene(name="山顶", scene_canonical="山顶，日"),
            Scene(name="山脚", scene_canonical="山脚，日"),
        ],
    )
    manifest = _storyboard_pack_asset_dependencies(
        project_id="proj-1", episode_no=1, shot_id="shot-x", segment=segment, conn=conn, bible=bible,
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
    from app.schemas import Bible, Scene, World

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
    # 「失踪场景」已经在场景库建卡（bible.scenes 里有），只是 scene_references
    # 没有它的可用行——这是 has_card=True 但当前查不到图的真实缺口场景，跟
    # 「查无此卡」不是一回事，必须走同一个「已建卡但没图」的拦截分支。
    bible = Bible(
        characters=[], world=World(visual_style_canonical="写实"),
        scenes=[
            Scene(name="山顶", scene_canonical="山顶，日"),
            Scene(name="失踪场景", scene_canonical="失踪场景，日"),
        ],
    )
    manifest = _storyboard_pack_asset_dependencies(
        project_id="proj-1", episode_no=1, shot_id="shot-x", segment=segment, conn=conn, bible=bible,
    )
    blockers = manifest_production_blockers(manifest)
    assert any("失踪场景" in b for b in blockers)


# ---------------------------------------------------------------------------
# asset_required 判据挂人物谱/场景库的卡、不挂段落固化快照（出图解耦漏掉的
# 最后一环）：映射那一刻本集新发现的角色/场景还没出图，段落自己的
# portrait_id/scene_reference_id 快照恒为 null；旧判据 asset_required=
# bool(snapshot_id) 因此把它们判成"不需要参考图"，视频生成拿不到脸。
# EP2 实证：小胖子已建卡且定妆照在生成开始前已经落库，只因映射时快照是
# null 被判不需要。
# ---------------------------------------------------------------------------

def test_storyboard_pack_asset_required_carded_character_resolves_live_portrait_despite_null_snapshot(
    tmp_path,
):
    """已建卡角色：段落快照 portrait_id 为空（本集新发现、映射时还没出图），
    但按集号现查能找到真实定妆照——asset_required 必须为真，且必须真的把
    这张图选出来，不能因为快照是空就判成不需要。"""
    from app.multiview import _storyboard_pack_asset_dependencies
    from app.schemas import Bible, Character, World

    conn = db.get_conn()
    episode_id = "ep-pack-live-portrait"
    _seed_episode(conn, episode_id=episode_id)
    portrait_image = tmp_path / "portrait.png"
    portrait_image.write_bytes(b"fake-png")
    conn.execute(
        "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end, "
        "image_path, created_at) VALUES(?,?,?,?,?,?,?)",
        ("portrait_live", "proj-1", "孟浩", 1, None, str(portrait_image), db.now()),
    )
    conn.commit()

    segment = {
        "segment_no": 1,
        "resources": {
            "characters": [{"identity_id": "bible:孟浩", "portrait_id": None}],
            "scenes": [],
        },
    }
    bible = Bible(
        characters=[Character(name="孟浩", role="主角", appearance_canonical="孟浩，青年")],
        world=World(visual_style_canonical="写实"),
    )
    manifest = _storyboard_pack_asset_dependencies(
        project_id="proj-1", episode_no=1, shot_id="shot-x", segment=segment, conn=conn, bible=bible,
    )
    character = manifest["characters"][0]
    assert character["asset_required"] is True
    assert character["look_revision_id"] == "portrait_live"
    assert character["selected_views"][0]["image_path"] == str(portrait_image)
    assert character["missing_required"] == []


def test_storyboard_pack_asset_required_carded_character_without_any_portrait_blocks_gate(
    tmp_path,
):
    """已建卡角色，但当前真的没有任何可用视角（没有 character_portraits 行）——
    asset_required 仍是真，missing_required 必须非空，manifest_production_
    blockers 必须真的拦下来，不能形同虚设。"""
    from app.multiview import _storyboard_pack_asset_dependencies, manifest_production_blockers
    from app.schemas import Bible, Character, World

    conn = db.get_conn()
    episode_id = "ep-pack-no-portrait-yet"
    _seed_episode(conn, episode_id=episode_id)

    segment = {
        "segment_no": 1,
        "resources": {
            "characters": [{"identity_id": "bible:小胖子", "portrait_id": None}],
            "scenes": [],
        },
    }
    bible = Bible(
        characters=[Character(name="小胖子", role="配角", appearance_canonical="小胖子，圆脸")],
        world=World(visual_style_canonical="写实"),
    )
    manifest = _storyboard_pack_asset_dependencies(
        project_id="proj-1", episode_no=1, shot_id="shot-x", segment=segment, conn=conn, bible=bible,
    )
    character = manifest["characters"][0]
    assert character["asset_required"] is True
    assert character["look_revision_id"] is None
    assert character["missing_required"] == ["front_full"]
    blockers = manifest_production_blockers(manifest)
    assert any("小胖子" in b for b in blockers)


def test_storyboard_pack_asset_required_false_for_entity_prefixed_extra():
    """负控：群演/一次性人物（identity_id 是 entity: 前缀的视觉实体哈希，人物谱
    里查无此人）必须继续 asset_required=False、missing_required 为空——这条
    修复只该让"已建卡但快照为空"的角色变真，不能反过来把本来就没有卡的群演
    也误判成需要参考图。"""
    from app.multiview import _storyboard_pack_asset_dependencies, manifest_production_blockers
    from app.schemas import Bible, Character, World

    conn = db.get_conn()
    episode_id = "ep-pack-extra-stays-optional"
    _seed_episode(conn, episode_id=episode_id)

    segment = {
        "segment_no": 1,
        "resources": {
            "characters": [{"identity_id": "entity:fdd28fea634a6cdc", "portrait_id": None}],
            "scenes": [],
        },
    }
    bible = Bible(
        characters=[Character(name="孟浩", role="主角", appearance_canonical="孟浩，青年")],
        world=World(visual_style_canonical="写实"),
    )
    manifest = _storyboard_pack_asset_dependencies(
        project_id="proj-1", episode_no=1, shot_id="shot-x", segment=segment, conn=conn, bible=bible,
    )
    character = manifest["characters"][0]
    assert character["asset_required"] is False
    assert character["missing_required"] == []
    blockers = manifest_production_blockers(manifest)
    assert blockers == []


def test_storyboard_pack_asset_required_scene_resolves_live_reference_despite_null_snapshot(
    tmp_path,
):
    """场景侧同构：已建卡场景（bible.scenes 里有），段落快照 scene_reference_id
    为空，但按集号现查能找到真实场景图——asset_required 为真且必须选出这张图，
    跟角色侧同一个判据来源，不是另写一套。"""
    from app.multiview import _storyboard_pack_asset_dependencies
    from app.schemas import Bible, Scene, World

    conn = db.get_conn()
    episode_id = "ep-pack-live-scene"
    _seed_episode(conn, episode_id=episode_id)
    scene_image = tmp_path / "scene.png"
    scene_image.write_bytes(b"fake-png")
    conn.execute(
        "INSERT INTO scene_references(id, project_id, scene_name, ep_start, ep_end, "
        "image_path, created_at) VALUES(?,?,?,?,?,?,?)",
        ("scene_ref_live", "proj-1", "靠山宗山峦小路", 1, None, str(scene_image), db.now()),
    )
    conn.commit()

    segment = {
        "segment_no": 1,
        "resources": {
            "characters": [],
            "scenes": [{"scene_id": "scene:靠山宗山峦小路", "scene_reference_id": None}],
        },
    }
    bible = Bible(
        characters=[], world=World(visual_style_canonical="写实"),
        scenes=[Scene(name="靠山宗山峦小路", scene_canonical="靠山宗外围，山路，日")],
    )
    manifest = _storyboard_pack_asset_dependencies(
        project_id="proj-1", episode_no=1, shot_id="shot-x", segment=segment, conn=conn, bible=bible,
    )
    scene = manifest["scene"]
    assert scene["asset_required"] is True
    assert scene["scene_revision_id"] == "scene_ref_live"
    assert scene["selected_views"][0]["image_path"] == str(scene_image)
    assert scene["missing_required"] == []



# ---------------------------------------------------------------------------
# 2.0.8：阶段二改回逐段独立调用。答案预算的安全网从「一批装不装得下」简化
# 成「这一段装不装得下」，但 2.0.6 记录的生产事故本身（答案预算不够会撞
# finish_reason=length、这一段照失败且照常计费）与批不批无关，不能跟着
# 批量机制一起退场。
# ---------------------------------------------------------------------------

_LIMITS_32K = {
    "context_window_tokens": 131072,
    "max_output_tokens": 32768,
    "token_limits_source": "test",
}


def _patch_thinking_model(
    monkeypatch,
    *,
    max_output: int = 32768,
    reserve: int | None = None,
    observed_reasoning: int | None = None,
) -> None:
    from app import hiagent, model_runtime_profile

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
    # 思考预留现在会先问模型的观测画像（app/model_runtime_profile.py）。测试
    # 必须钉死这个输入，否则断言会随开发机 provider_calls 里的真实历史漂移。
    # 默认钉成「没有观测」，也就是回落全局默认——正是这些用例原本的语义。
    monkeypatch.setattr(
        model_runtime_profile,
        "model_runtime_profile",
        lambda model: model_runtime_profile.ModelRuntimeProfile(
            model=str(model or ""),
            sample_count=0 if observed_reasoning is None else model_runtime_profile.MIN_OBSERVATIONS,
            reasoning_ceiling=observed_reasoning,
            first_token_ceiling_s=None,
        ),
    )


def test_contract_marker_stays_on_2_0_5_so_existing_packs_resume():
    """marker 跟落库形状走，不跟提示词措辞走。

    2.0.6 起的约定：只改生成切分或提示词措辞时不动 marker，已成功的集不该被
    强迫重跑一次付费生成；需要用上新提示词的集走「删除 + 重新生成」。所以
    STORYBOARD_PACK_VERSION 会随每次修订往前走，marker 只在落库形状真变了
    的那次才动——断言版本号具体等于几，守的是前者而不是这条规则本身。
    """
    assert STORYBOARD_PACK_CONTRACT_MARKER == "storyboard_pack/2.0.5"
    assert STORYBOARD_PACK_VERSION > "2.0.6"


def test_ensure_segment_prompt_budget_passes_when_room_is_ample(monkeypatch):
    _patch_thinking_model(monkeypatch, max_output=131072)
    _ensure_segment_prompt_budget()  # 不应抛出


def test_ensure_segment_prompt_budget_raises_when_single_segment_does_not_fit(monkeypatch):
    """装不下就在发请求前停，并且说清该去动哪个旋钮。

    复用 2.0.6 记录的同一次真实生产事故指纹（reasoning_tokens=30839，
    max_output_tokens=32768）：批量退场后不再有"批次"概念，但同一个模型
    观测同样能把单段的答案预算（SEGMENT_PROMPT_ANSWER_TOKENS=2400）挤不
    下——30768 剩余空间 < 2400，说明这道安全网没有跟着批量机制一起失效。
    """
    _patch_thinking_model(monkeypatch, max_output=32768, observed_reasoning=30839)

    with pytest.raises(StoryboardPackBudgetError) as excinfo:
        _ensure_segment_prompt_budget()

    message = str(excinfo.value)
    assert "32768" in message and "30839" in message
    # 拦住人就得给出路，不能只说"不行"。
    assert "模型" in message


def test_budget_error_names_the_default_probe_cache_as_the_fixable_cause(monkeypatch):
    """上限还是兜底默认值时，要直说「去把真实能力填上」而不是让人换模型。

    这是同一条产品规矩的第二半：闸门可以拦，但不能把人晾在原地。真实成因
    多半是这个模型的能力还挂着系统探测不到时写下的兜底值，填对就能跑。
    """
    from app import model_capabilities

    _patch_thinking_model(monkeypatch, max_output=32768, observed_reasoning=30839)
    monkeypatch.setattr(
        model_capabilities,
        "active_model_token_limits",
        lambda *_a, **_k: {
            "context_window_tokens": 131072,
            "max_output_tokens": 32768,
            "token_limits_source": "default_128k_32k",
        },
    )

    with pytest.raises(StoryboardPackBudgetError) as excinfo:
        _ensure_segment_prompt_budget()

    assert "真实值" in str(excinfo.value)


def test_budget_error_points_elsewhere_when_capability_is_already_evidenced(monkeypatch):
    """能力已是实测值时就别再让人去改配置——那条路已经走过了。"""
    from app import model_capabilities

    _patch_thinking_model(monkeypatch, max_output=32768, observed_reasoning=30839)
    monkeypatch.setattr(
        model_capabilities,
        "active_model_token_limits",
        lambda *_a, **_k: {
            "context_window_tokens": 131072,
            "max_output_tokens": 32768,
            "token_limits_source": "configured",
        },
    )

    with pytest.raises(StoryboardPackBudgetError) as excinfo:
        _ensure_segment_prompt_budget()

    message = str(excinfo.value)
    assert "真实值" not in message
    assert "改配" in message


def test_ensure_segment_prompt_budget_follows_the_model_this_stage_actually_calls(monkeypatch):
    """预算核验必须按分镜台自己配的模型算，不能拿全局默认模型的上限。

    分镜台可在项目上配专属文本模型（board_text_provider）。这里原先不传
    provider，拿到的是全局默认文本模型的能力——生产里两者的 max_output_tokens
    恰好都是 32768 兜底默认值才没露馅，一旦有一边填成真实值就会拿 A 的上限
    去判 B 该不该发。
    """
    from app import hiagent
    from app.harness.text_provider_scope import stage_text_provider

    _patch_thinking_model(monkeypatch)
    seen: list[str | None] = []
    real = hiagent.text_request_token_limits

    def _recording(*args, **kwargs):
        seen.append(kwargs.get("provider"))
        return real(*args, **kwargs)

    monkeypatch.setattr(hiagent, "text_request_token_limits", _recording)

    with stage_text_provider("custom:board-only-model"):
        _ensure_segment_prompt_budget()

    assert seen == ["custom:board-only-model"]


# ---------------------------------------------------------------------------
# 2.0.8：一镜参考的三层载荷——本段自己的原文切片、镜头语言窗口、续接规则
# ---------------------------------------------------------------------------

def test_segment_source_block_uses_only_this_segments_indexes():
    """逐段调用只带这一段自己的原文，不是全集原文（承袭 2.0.3 之前的设计，
    全集原文对本段来说是白白抬高单次调用体量的多余上下文）。"""
    segments = [
        SourceSegment(segment_id="s1", text="第一段原文", start_offset=0, end_offset=5),
        SourceSegment(segment_id="s2", text="第二段原文", start_offset=5, end_offset=10),
        SourceSegment(segment_id="s3", text="第三段原文", start_offset=10, end_offset=15),
    ]
    block = _segment_source_block(segments, [2], paratext_indexes=set())
    assert block == "[段2] 第二段原文"
    assert "第一段" not in block and "第三段" not in block


def test_segment_source_block_swaps_paratext_placeholder():
    """paratext 段落即使落在本段引用范围内，也绝不能把原文本身发给模型——
    这条 2.0.4 的底线在逐段调用里同样成立。"""
    segments = [SourceSegment(segment_id="s1", text="求票求收藏", start_offset=0, end_offset=5)]
    block = _segment_source_block(segments, [1], paratext_indexes={1})
    assert "求票求收藏" not in block
    assert "[段1]" in block


def test_camera_digest_window_only_includes_recent_segments_within_window():
    """只给上一段（窗口=1）发现不了「第 1、3、5 段用同一机位」这种隔段
    重复，这正是 CAMERA_DIGEST_WINDOW > 1 要解决的问题。"""
    history = {
        1: _AiCameraDigest(opening_shot_size="远景", opening_camera_move="推近"),
        2: _AiCameraDigest(opening_shot_size="中景", opening_camera_move="横摇"),
        3: _AiCameraDigest(opening_shot_size="近景", opening_camera_move="固定"),
    }
    window = _camera_digest_window_payload(history, segment_no=4, window=2)
    assert [item["segment_no"] for item in window] == [2, 3]
    assert window[0]["opening_shot_size"] == "中景"


def test_camera_digest_window_empty_for_first_segment():
    """空列表是诚实的「还没有历史」，不是需要兜底填充的缺口。"""
    assert _camera_digest_window_payload({}, segment_no=1, window=CAMERA_DIGEST_WINDOW) == []


def test_continuity_rules_first_segment_has_no_previous_and_no_history():
    """本集第一段没有上一段、没有镜头语言历史，两种情况都要如实说清楚，
    不能假装存在一个不存在的参照（CLAUDE.md「Prompts」：确实没有时该怎么写）。"""
    rules = _segment_continuity_rules(previous_segment_no=None, camera_history=[])
    assert "没有上一段可参考" in rules[0]
    assert "没有可参考的镜头语言历史" in rules[1]


def test_continuity_rules_names_the_previous_segment_number():
    rules = _segment_continuity_rules(previous_segment_no=3, camera_history=[])
    assert "第 3 段" in rules[0]
    assert "previous_segment_prompt" in rules[0]


def test_continuity_rules_camera_history_gives_a_legitimate_repetition_escape_hatch():
    """允许重复，但要求说明理由——不是简单禁令（对应「给重复留合法出路」）。"""
    rules = _segment_continuity_rules(
        previous_segment_no=4,
        camera_history=[{"segment_no": 2, "opening_shot_size": "远景", "opening_camera_move": "推近"}],
    )
    assert "[2]" in rules[1]
    assert "camera_repetition_rationale" in rules[1]
    assert "留空即可" in rules[1]


# ---------------------------------------------------------------------------
# 2.0.8：_generate_all_segment_prompts 端到端——逐段独立调用、严格串行、
# 一镜参考三层载荷都出现在真正发给模型的 task_payload 里
# ---------------------------------------------------------------------------

def _segment_draft(
    prompt_text: str, *, camera_digest: _AiCameraDigest | None = None,
) -> _AiStoryboardSegmentDraft:
    return _AiStoryboardSegmentDraft(
        prompt_text=prompt_text,
        shot_count=3,
        camera_digest=camera_digest or _AiCameraDigest(),
    )


@pytest.mark.asyncio
async def test_generate_calls_model_once_per_segment_strictly_sequential(monkeypatch):
    """逐段独立调用——不是一次批量：调用次数必须恰好等于段数，且第 N 段的
    调用必须能看到第 N-1 段刚生成、定稿的 prompt_text（用户方案要求的
    "一镜参考"）。"""
    import app.production.storyboard_pack as storyboard_pack_module

    calls: list[dict] = []

    async def fake_chat_structured(messages, **kwargs):
        payload = json.loads(messages[1]["content"])
        calls.append(
            {"payload": payload, "max_tokens": kwargs["max_tokens"], "call_meta": kwargs["call_meta"]}
        )
        segment_no = payload["segment_no"]
        return _segment_draft(
            f"提示词-段{segment_no}",
            camera_digest=_AiCameraDigest(
                opening_shot_size=f"景别{segment_no}", opening_camera_move=f"运镜{segment_no}",
            ),
        )

    monkeypatch.setattr(storyboard_pack_module.model_gateway, "chat_structured", fake_chat_structured)
    monkeypatch.setattr(storyboard_pack_module, "_ensure_segment_prompt_budget", lambda: None)

    beat_draft = _AiBeatSheetDraft(
        beat_sheet=[_AiBeat(beat_id="B1", summary="他扔掉了理想", segment_indexes=[1])],
        segments=[
            _AiSegmentPlan(segment_no=index, synopsis=f"段{index}", source_segment_indexes=[1], beat_ids=["B1"])
            for index in range(1, 6)
        ],
    )
    source = [SourceSegment(segment_id="s1", text="少年站在山顶。", start_offset=0, end_offset=7)]

    result = await _generate_all_segment_prompts(
        episode_id="ep-sequential",
        episode_no=1,
        beat_draft=beat_draft,
        segments=source,
        payload={},
        target_video_model="hiagent",
        bible=None,
    )

    assert list(result) == [1, 2, 3, 4, 5]
    assert len(calls) == 5, "每段各一次独立调用，不是一次整集批量"
    assert calls[0]["payload"]["previous_segment_prompt"] is None
    for n in range(2, 6):
        assert calls[n - 1]["payload"]["previous_segment_prompt"] == f"提示词-段{n - 1}"
    for call in calls:
        assert call["max_tokens"] == SEGMENT_PROMPT_ANSWER_TOKENS
        assert call["call_meta"]["stage_key"] == "storyboard_pack_segment"
        assert call["call_meta"]["call_role"] == "storyboard_pack_segment_single"


@pytest.mark.asyncio
async def test_generate_camera_digest_window_excludes_segments_outside_window(monkeypatch):
    """镜头语言清单只带最近 CAMERA_DIGEST_WINDOW 段，不是全集累积清单。"""
    import app.production.storyboard_pack as storyboard_pack_module

    calls: list[dict] = []

    async def fake_chat_structured(messages, **kwargs):
        payload = json.loads(messages[1]["content"])
        calls.append(payload)
        segment_no = payload["segment_no"]
        return _segment_draft(
            f"提示词-段{segment_no}",
            camera_digest=_AiCameraDigest(opening_shot_size="远景", opening_camera_move="推近"),
        )

    monkeypatch.setattr(storyboard_pack_module.model_gateway, "chat_structured", fake_chat_structured)
    monkeypatch.setattr(storyboard_pack_module, "_ensure_segment_prompt_budget", lambda: None)
    monkeypatch.setattr(storyboard_pack_module, "CAMERA_DIGEST_WINDOW", 2)

    beat_draft = _AiBeatSheetDraft(
        beat_sheet=[_AiBeat(beat_id="B1", summary="x", segment_indexes=[1])],
        segments=[
            _AiSegmentPlan(segment_no=index, synopsis=f"段{index}", source_segment_indexes=[1], beat_ids=["B1"])
            for index in range(1, 5)
        ],
    )
    source = [SourceSegment(segment_id="s1", text="少年站在山顶。", start_offset=0, end_offset=7)]

    await _generate_all_segment_prompts(
        episode_id="ep-window",
        episode_no=1,
        beat_draft=beat_draft,
        segments=source,
        payload={},
        target_video_model="hiagent",
        bible=None,
    )

    assert calls[0]["recent_camera_language"] == []
    assert [item["segment_no"] for item in calls[1]["recent_camera_language"]] == [1]
    assert [item["segment_no"] for item in calls[2]["recent_camera_language"]] == [1, 2]
    assert [item["segment_no"] for item in calls[3]["recent_camera_language"]] == [2, 3]


@pytest.mark.asyncio
async def test_generate_appearance_rule_tells_model_to_copy_fresh_not_from_memory(monkeypatch):
    """专治角色换装的第三层：每次独立调用都要求逐字重抄世界书原文，不允许
    凭记忆复述——这是比"整集一次看见全部段落"更不容易漂移的机制（对应
    CLAUDE.md「接上世界书的 appearance 后逐字一致」的既有先例）。"""
    import app.production.storyboard_pack as storyboard_pack_module

    calls: list[dict] = []

    async def fake_chat_structured(messages, **kwargs):
        payload = json.loads(messages[1]["content"])
        calls.append(payload)
        return _segment_draft(f"提示词-段{payload['segment_no']}")

    monkeypatch.setattr(storyboard_pack_module.model_gateway, "chat_structured", fake_chat_structured)
    monkeypatch.setattr(storyboard_pack_module, "_ensure_segment_prompt_budget", lambda: None)

    beat_draft = _AiBeatSheetDraft(
        beat_sheet=[_AiBeat(beat_id="B1", summary="x", segment_indexes=[1])],
        segments=[
            _AiSegmentPlan(segment_no=1, synopsis="a", source_segment_indexes=[1], beat_ids=["B1"]),
            _AiSegmentPlan(segment_no=2, synopsis="b", source_segment_indexes=[1], beat_ids=["B1"]),
        ],
    )
    source = [SourceSegment(segment_id="s1", text="少年站在山顶。", start_offset=0, end_offset=7)]

    await _generate_all_segment_prompts(
        episode_id="ep-appearance",
        episode_no=1,
        beat_draft=beat_draft,
        segments=source,
        payload={},
        target_video_model="hiagent",
        bible=None,
    )

    assert len(calls) == 2
    for payload in calls:
        appearance_rule = next(
            r for r in payload["rules"] if "appearance" in r or "scene_canonical" in r
        )
        assert "不要凭记忆复述" in appearance_rule
        assert "每次都" in appearance_rule and "重新逐字抄一遍" in appearance_rule


@pytest.mark.asyncio
async def test_generate_raises_before_any_call_when_budget_cannot_fit_first_segment(monkeypatch):
    """预算核验必须在真正发出第一次请求之前就拦下——不能烧钱之后才发现。"""
    import app.production.storyboard_pack as storyboard_pack_module

    called = False

    async def fake_chat_structured(messages, **kwargs):
        nonlocal called
        called = True
        return _segment_draft("不应该被生成")

    monkeypatch.setattr(storyboard_pack_module.model_gateway, "chat_structured", fake_chat_structured)

    def _raise():
        raise StoryboardPackBudgetError(
            model="thinking-model", model_cap=32768, reserve=30839, needed=2400, provider=None,
        )

    monkeypatch.setattr(storyboard_pack_module, "_ensure_segment_prompt_budget", _raise)

    beat_draft = _AiBeatSheetDraft(
        beat_sheet=[_AiBeat(beat_id="B1", summary="x", segment_indexes=[1])],
        segments=[_AiSegmentPlan(segment_no=1, synopsis="a", source_segment_indexes=[1], beat_ids=["B1"])],
    )
    source = [SourceSegment(segment_id="s1", text="少年站在山顶。", start_offset=0, end_offset=7)]

    with pytest.raises(StoryboardPackBudgetError):
        await _generate_all_segment_prompts(
            episode_id="ep-budget-block",
            episode_no=1,
            beat_draft=beat_draft,
            segments=source,
            payload={},
            target_video_model="hiagent",
            bible=None,
        )

    assert called is False, "预算不够就不该真的发出请求"
