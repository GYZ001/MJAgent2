"""续接服装文本反推道具卡（2026-10-05，《顾念长安》proj_ca86b15ab7d7 第1集真实
回归）：21/35 段 ``continuity_memo.characters[].wardrobe`` 写了具体服装外观，
但模型没把对应服装道具卡 label 写进 ``resources.props``，这些服装的参考图
没进参考图池、视频模型自由换装。wardrobe 是模型早已写出的"提名"，本模块只
核验它是否逐字点名了一张世界书里真实存在的道具卡——不新增模型调用、不改
schema，详见 ``app.video_modes.prop_references`` 模块 docstring。

本文件覆盖五层：
1. ``_strip_trailing_annotation``——剥末尾括号注释。
2. ``_char_coverage``——分离度回归（用任务原文真实字符串钉住真匹配/碰撞的
   数值边界，防止以后被"优化"破坏分离边际）。
3. ``infer_wardrobe_prop_labels``——正例/已声明排除/歧义不绑/短词门槛/多人物
   去重。
4. ``_declared_prop_names``——括号变体对齐声明项。
5. ``storyboard_pack_prop_entries`` 全链路——含"无 continuity_memo 旧调用
   形状零回归"关键用例。
"""
from __future__ import annotations

import logging

from app.schemas import Bible, Prop, World
from app.video_modes.prop_references import (
    _char_coverage,
    _declared_prop_names,
    _strip_trailing_annotation,
    _visible_wardrobe_texts,
    infer_wardrobe_prop_labels,
    resolve_segment_prop_manifest_entries,
    storyboard_pack_prop_entries,
)

import app.video_modes.prop_references as prop_references

# 任务原文：第2段温念续接服装（米白色针织开衫/浅蓝色碎花长裙两张卡都没进
# resources.props 的真实案例）。
_WENNIAN_WARDROBE_TEXT = (
    "米白色灯芯绒翻领单排扣外套敞着前襟罩在米白色宽松针织开衫外，"
    "藏青色罗纹袖口，内搭浅蓝色细碎花棉质长裙，赤脚"
)


def _bible(props: list[Prop]) -> Bible:
    return Bible(characters=[], world=World(visual_style_canonical="写实"), props=props)


def _prop(name: str, aliases: list[str] | None = None) -> Prop:
    return Prop(name=name, appearance_canonical=name, aliases=list(aliases or []))


# ---------------------------------------------------------------------------
# _strip_trailing_annotation
# ---------------------------------------------------------------------------

def test_strip_trailing_annotation_removes_bracket_suffix() -> None:
    assert _strip_trailing_annotation("浅蓝色碎花长裙（裙摆）") == "浅蓝色碎花长裙"


def test_strip_trailing_annotation_noop_without_brackets() -> None:
    assert _strip_trailing_annotation("旧猫包") == "旧猫包"


def test_strip_trailing_annotation_keeps_original_when_stripped_to_empty() -> None:
    assert _strip_trailing_annotation("（）") == "（）"


# ---------------------------------------------------------------------------
# _char_coverage：分离度回归（任务原文真实字符串）
# ---------------------------------------------------------------------------

def test_char_coverage_true_match_full_name_is_one() -> None:
    assert _char_coverage("米白色针织开衫", _WENNIAN_WARDROBE_TEXT) == 1.0


def test_char_coverage_true_match_alias_is_one() -> None:
    assert _char_coverage("米白针织开衫", _WENNIAN_WARDROBE_TEXT) == 1.0


def test_char_coverage_color_collision_stays_below_threshold() -> None:
    coverage = _char_coverage("米白色针织开衫", "她换上了藏青色针织开衫")
    assert coverage < 0.8
    assert round(coverage, 2) == 0.71


def test_char_coverage_suffix_collision_stays_below_threshold() -> None:
    coverage = _char_coverage("顾屿外套", "她把外套搭在肩上走了出去")
    assert coverage < 0.8
    assert coverage == 0.5


def test_char_coverage_short_identifier_literal_hit_is_not_gated_here() -> None:
    """长度门槛在 ``infer_wardrobe_prop_labels`` 里挡，``_char_coverage`` 本身
    对短识别串逐字出现仍如实返回 1.0——两层职责分开验证。"""
    assert _char_coverage("围裙", "她系着围裙在厨房里忙") == 1.0


# ---------------------------------------------------------------------------
# _declared_prop_names
# ---------------------------------------------------------------------------

def test_declared_prop_names_matches_bracket_stripped_label() -> None:
    bible_props = [_prop("浅蓝色碎花长裙")]
    excluded = _declared_prop_names(
        [{"label": "浅蓝色碎花长裙（裙摆）"}], bible_props,
    )
    assert excluded == {"浅蓝色碎花长裙"}


def test_declared_prop_names_ignores_unrelated_props() -> None:
    bible_props = [_prop("浅蓝色碎花长裙"), _prop("米白色针织开衫")]
    excluded = _declared_prop_names([{"label": "脸盆"}], bible_props)
    assert excluded == set()


# ---------------------------------------------------------------------------
# infer_wardrobe_prop_labels
# ---------------------------------------------------------------------------

def test_infer_wardrobe_prop_labels_matches_both_cards_in_order() -> None:
    bible_props = [
        _prop("米白色针织开衫", aliases=["米白针织开衫"]),
        _prop("浅蓝色碎花长裙", aliases=["浅蓝碎花长裙"]),
    ]
    matched = infer_wardrobe_prop_labels(
        bible_props=bible_props, wardrobe_texts=[_WENNIAN_WARDROBE_TEXT], already_declared=set(),
    )
    assert matched == ["米白色针织开衫", "浅蓝色碎花长裙"]


def test_infer_wardrobe_prop_labels_skips_already_declared() -> None:
    bible_props = [
        _prop("米白色针织开衫", aliases=["米白针织开衫"]),
        _prop("浅蓝色碎花长裙", aliases=["浅蓝碎花长裙"]),
    ]
    matched = infer_wardrobe_prop_labels(
        bible_props=bible_props, wardrobe_texts=[_WENNIAN_WARDROBE_TEXT],
        already_declared={"米白色针织开衫"},
    )
    assert matched == ["浅蓝色碎花长裙"]


def test_infer_wardrobe_prop_labels_ambiguous_hit_is_not_bound(caplog) -> None:
    bible_props = [_prop("顾屿外套"), _prop("温念厚外套")]
    text = "顾屿和温念都换了新外套"
    # 两张卡各自对这句文本的覆盖率都先验证过 >=0.8，确保本用例真的构造出了
    # 歧义场景，不是因为识别串本身就打不到阈值而巧合返回空列表。
    assert _char_coverage("顾屿外套", text) >= 0.8
    assert _char_coverage("温念厚外套", text) >= 0.8
    with caplog.at_level(logging.WARNING, logger="app.video_modes.prop_references"):
        matched = infer_wardrobe_prop_labels(
            bible_props=bible_props, wardrobe_texts=[text], already_declared=set(),
        )
    assert matched == []
    assert any("STORYBOARD_WARDROBE_PROP_AMBIGUOUS" in record.message for record in caplog.records)


def test_infer_wardrobe_prop_labels_short_identifier_is_gated() -> None:
    """卡名"围裙"只有 2 字，低于 ``MIN_WARDROBE_PROP_IDENTIFIER_LEN``=4，即便
    原文逐字出现也不进入覆盖率判断、不命中。"""
    bible_props = [_prop("围裙")]
    matched = infer_wardrobe_prop_labels(
        bible_props=bible_props, wardrobe_texts=["她系着围裙在厨房里忙"], already_declared=set(),
    )
    assert matched == []


def test_infer_wardrobe_prop_labels_dedup_across_multiple_characters() -> None:
    bible_props = [_prop("米白色针织开衫", aliases=["米白针织开衫"])]
    matched = infer_wardrobe_prop_labels(
        bible_props=bible_props,
        wardrobe_texts=[_WENNIAN_WARDROBE_TEXT, _WENNIAN_WARDROBE_TEXT],
        already_declared=set(),
    )
    assert matched == ["米白色针织开衫"]


# ---------------------------------------------------------------------------
# _visible_wardrobe_texts
# ---------------------------------------------------------------------------

def test_visible_wardrobe_texts_excludes_voice_only_and_narrator() -> None:
    resources = {
        "characters": [
            {"identity_id": "温念", "visibility": "visible"},
            {"identity_id": "场外音", "visibility": "voice_only"},
            {"identity_id": "旁白", "visibility": "visible"},
        ],
    }
    continuity_memo = {
        "characters": [
            {"identity_id": "温念", "wardrobe": "米白色针织开衫"},
            {"identity_id": "场外音", "wardrobe": "不应该出现"},
            {"identity_id": "旁白", "wardrobe": "不应该出现"},
        ],
    }
    assert _visible_wardrobe_texts(resources, continuity_memo) == ["米白色针织开衫"]


def test_visible_wardrobe_texts_matches_identity_id_with_missing_prefix() -> None:
    """真实回归（见 storyboard_continuity_memo.continuity_memo_character_
    advisories docstring）：模型在 continuity_memo.characters 里偶尔省略
    resources.characters 已解析出的 bible:/entity: 前缀。resources 侧带前缀、
    continuity_memo 侧不带时仍要判定成同一个可见人物，精确字符串比对会把
    这句本该发出的 wardrobe 文本静默漏掉。"""
    resources = {"characters": [{"identity_id": "bible:温念", "visibility": "visible"}]}
    continuity_memo = {"characters": [{"identity_id": "温念", "wardrobe": "米白色针织开衫"}]}
    assert _visible_wardrobe_texts(resources, continuity_memo) == ["米白色针织开衫"]


def test_visible_wardrobe_texts_skips_empty_wardrobe() -> None:
    resources = {"characters": [{"identity_id": "温念", "visibility": "visible"}]}
    continuity_memo = {"characters": [{"identity_id": "温念", "wardrobe": "  "}]}
    assert _visible_wardrobe_texts(resources, continuity_memo) == []


# ---------------------------------------------------------------------------
# storyboard_pack_prop_entries 全链路
# ---------------------------------------------------------------------------

def test_storyboard_pack_prop_entries_appends_inferred_cards_after_declared(monkeypatch) -> None:
    monkeypatch.setattr(prop_references, "_prop_reference_lookup", lambda *a, **k: None)
    segment = {
        "resources": {
            "characters": [{"identity_id": "温念", "visibility": "visible"}],
            "props": [{"label": "外套"}],
        },
        "continuity_memo": {
            "characters": [{"identity_id": "温念", "wardrobe": _WENNIAN_WARDROBE_TEXT}],
        },
    }
    bible = _bible([
        _prop("米白色针织开衫", aliases=["米白针织开衫"]),
        _prop("浅蓝色碎花长裙", aliases=["浅蓝碎花长裙"]),
    ])
    entries = storyboard_pack_prop_entries(
        segment=segment, bible=bible, conn=object(), project_id="proj-1", episode_no=1,
    )
    labels = [e["label"] for e in entries]
    assert labels == ["外套", "米白色针织开衫", "浅蓝色碎花长裙"]
    # 声明项优先：resources_order 必须保持 0/1/2 递增，推导项排在声明项之后。
    assert [e["resources_order"] for e in entries] == [0, 1, 2]


def test_storyboard_pack_prop_entries_excludes_already_declared_card(monkeypatch) -> None:
    monkeypatch.setattr(prop_references, "_prop_reference_lookup", lambda *a, **k: None)
    segment = {
        "resources": {
            "characters": [{"identity_id": "温念", "visibility": "visible"}],
            "props": [{"label": "米白色针织开衫"}],
        },
        "continuity_memo": {
            "characters": [{"identity_id": "温念", "wardrobe": _WENNIAN_WARDROBE_TEXT}],
        },
    }
    bible = _bible([
        _prop("米白色针织开衫"),
        _prop("浅蓝色碎花长裙", aliases=["浅蓝碎花长裙"]),
    ])
    entries = storyboard_pack_prop_entries(
        segment=segment, bible=bible, conn=object(), project_id="proj-1", episode_no=1,
    )
    labels = [e["label"] for e in entries]
    assert labels == ["米白色针织开衫", "浅蓝色碎花长裙"]


def test_storyboard_pack_prop_entries_no_continuity_memo_matches_legacy_call_shape(monkeypatch) -> None:
    """零回归用例：``segment`` 不带 ``continuity_memo`` 键（现有存量调用形状）
    时，结果必须与直接调用旧 ``resolve_segment_prop_manifest_entries`` 逐字
    一致——证明旧调用形状下行为不变。"""
    monkeypatch.setattr(prop_references, "_prop_reference_lookup", lambda *a, **k: None)
    segment = {
        "resources": {
            "characters": [],
            "scenes": [],
            "props": [
                {"label": "旧猫包", "description": "破猫包"},
                {"label": "没图的道具", "description": "x"},
            ],
        },
    }
    bible = _bible([_prop("米白色针织开衫")])
    via_new = storyboard_pack_prop_entries(
        segment=segment, bible=bible, conn=object(), project_id="proj-1", episode_no=3,
    )
    via_old = resolve_segment_prop_manifest_entries(
        segment["resources"]["props"], conn=object(), project_id="proj-1", episode_no=3,
    )
    assert via_new == via_old
