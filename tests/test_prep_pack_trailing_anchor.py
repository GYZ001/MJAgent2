"""原文写法尾部退让锚定（``app.production.prep_pack.trailing_anchor``，
2026-10-01，PREP_PACK_VERSION 2.0.10）。

真实形状（见该模块 docstring，我欲封天系列 EP1 用 2.0.9 重跑映射实测）：
label/source_wording 都不是声明段落里的原文字面，但存在一个尾部子串（退到
2 字为止，口径照抄 ``app/production/scene_evidence.py::scene_label_
evidence``）逐字命中——"旧笔记本"→"笔记本"、"顾屿家书房"→"书房"、
"小木星星"→"木星星"。这里既测纯函数本身，也测道具/场景两处接线
（discovery.py/resolve_assets.py）确实用上了它、且退让到 2 字仍不命中时
诚实地保持未锚定。
"""
from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest

from app.production import prep_pack
from app.production.prep_pack.prop_manifest import _prep_pack_build_prop_manifest
from app.production.prep_pack.trailing_anchor import (
    prop_literal_or_trailing_anchor,
    scene_anchor_with_trailing_fallback,
    trailing_anchor_phrase,
)
from app.source_excerpt import index_source_segments

# ---------------------------------------------------------------------------
# trailing_anchor_phrase：核心尾部退让原语
# ---------------------------------------------------------------------------


def test_trailing_anchor_phrase_notebook_shape() -> None:
    """真实形状①："旧笔记本"→"笔记本"。"""
    text = "一本封面陈旧、锁扣锈迹斑斑的笔记本静静躺在抽屉里。"
    assert trailing_anchor_phrase(["旧笔记本", ""], text) == "笔记本"


def test_trailing_anchor_phrase_study_shape() -> None:
    """真实形状②："顾屿家书房"→"书房"。"""
    text = "她路过一间半开着门的书房，脚步顿了顿。"
    assert trailing_anchor_phrase(["顾屿家书房", ""], text) == "书房"


def test_trailing_anchor_phrase_star_shape() -> None:
    """真实形状③："小木星星"→"木星星"。"""
    text = "手心攥着那枚旧旧的木星星。"
    assert trailing_anchor_phrase(["小木星星", ""], text) == "木星星"


def test_trailing_anchor_phrase_stops_at_two_chars_not_one() -> None:
    """退到 2 字为止，不会再退到 1 字——同 scene_label_evidence 的既有口径。
    候选"神秘的西"（4 字）退让序列是 4/3/2 字，1 字的"西"从不会被尝试，
    即便文本里确实有"西"这个字。"""
    text = "桌上有个东西。"
    assert trailing_anchor_phrase(["神秘的西"], text) == ""


def test_trailing_anchor_phrase_no_match_returns_empty() -> None:
    """退到 2 字仍不命中——候选跟文本完全不沾边，诚实返回空串，不是"反正
    给个锚点"。"""
    assert trailing_anchor_phrase(["妈妈的字条"], "桌上放着一只杯子。") == ""


def test_trailing_anchor_phrase_skips_blank_candidates() -> None:
    assert trailing_anchor_phrase(["", "   ", "木星星"], "旧旧的木星星。") == "木星星"


# ---------------------------------------------------------------------------
# prop_literal_or_trailing_anchor：道具侧接线原语
# ---------------------------------------------------------------------------


def test_prop_literal_match_is_preferred_and_not_marked_trailing() -> None:
    """label 本身整串命中时，既有判据不变——trailing 必须是 False。"""
    segments = index_source_segments("她鬓边斜插着一支缠着细银丝的木簪。")
    result = prop_literal_or_trailing_anchor(
        "缠着细银丝的木簪", "", [1], segments, segments[0].text, "",
    )
    assert result == ([1], "缠着细银丝的木簪", False)


def test_prop_literal_or_trailing_anchor_retreats_to_tail_substring() -> None:
    segments = index_source_segments("一本封面陈旧、锁扣锈迹斑斑的笔记本静静躺在抽屉里。")
    indexes, phrase, trailing = prop_literal_or_trailing_anchor(
        "旧笔记本", "", [1], segments, segments[0].text, False,
    )
    assert indexes == [1]
    assert phrase == "笔记本"
    assert trailing is True


def test_prop_literal_or_trailing_anchor_skips_retreat_when_card_already_bound() -> None:
    """``card_bound=True`` 时不在这里退让——已绑定既有卡的提及改由
    ``discovery._prep_pack_prop_card_anchor`` 自己对 card.name/aliases 做
    退让，不跟 card_match 自身的单一胜者/消歧判据赛跑（真实回归，见
    tests/test_prop_card_binding.py::
    test_manifest_card_match_branch_anchors_within_its_own_segment）。"""
    segments = index_source_segments("那只行李箱靠在墙角里好多年了。")
    result = prop_literal_or_trailing_anchor(
        "行李箱三件套", "", [1], segments, segments[0].text, True,
    )
    assert result == ([], "", False)


def test_prop_literal_or_trailing_anchor_no_match_returns_empty() -> None:
    segments = index_source_segments("桌上放着一只杯子。")
    result = prop_literal_or_trailing_anchor(
        "妈妈的字条", "妈妈写的字条", [1], segments, segments[0].text, False,
    )
    assert result == ([], "", False)


# ---------------------------------------------------------------------------
# discovery._prep_pack_prop_card_anchor：已绑定卡的尾部退让（单独在这里做，
# 不经 prop_literal_or_trailing_anchor，见上一节两个测试）
# ---------------------------------------------------------------------------


def test_prop_card_anchor_retreats_to_tail_substring_of_card_name() -> None:
    from app.production.prep_pack.prop_manifest import _prep_pack_prop_card_anchor
    from app.schemas import Prop

    segments = index_source_segments("那只行李箱靠在墙角里好多年了。")
    card = Prop(name="旧行李箱", appearance_canonical="一只深灰色旧行李箱")
    result = _prep_pack_prop_card_anchor(card, [1], segments)
    assert result == (1, "行李箱", True)


def test_prop_card_anchor_prefers_direct_hit_over_retreat() -> None:
    """card.name 本身整串就命中时不需要退让，trailing 必须是 False。"""
    from app.production.prep_pack.prop_manifest import _prep_pack_prop_card_anchor
    from app.schemas import Prop

    segments = index_source_segments("画面里那只行李箱边角磕碰。")
    card = Prop(name="行李箱", appearance_canonical="一只旧行李箱")
    result = _prep_pack_prop_card_anchor(card, [1], segments)
    assert result == (1, "行李箱", False)


# ---------------------------------------------------------------------------
# scene_anchor_with_trailing_fallback：场景侧接线原语
# ---------------------------------------------------------------------------


def test_scene_anchor_with_trailing_fallback_retreats_for_direct_method() -> None:
    segments = index_source_segments("她路过一间半开着门的书房，脚步顿了顿。")
    anchor_segments, anchor_phrase, trailing = scene_anchor_with_trailing_fallback(
        "direct", [], "", segments, [1], "顾屿家书房", "",
    )
    assert anchor_segments == [1]
    assert anchor_phrase == "书房"
    assert trailing is True


def test_scene_anchor_with_trailing_fallback_skips_when_already_anchored() -> None:
    """既有候选已经找到锚点就原样返回，不重复退让、trailing 置 False。"""
    segments = index_source_segments("她路过一间半开着门的书房。")
    result = scene_anchor_with_trailing_fallback(
        "direct", [1], "已有锚点", segments, [1], "顾屿家书房", "",
    )
    assert result == ([1], "已有锚点", False)


def test_scene_anchor_with_trailing_fallback_does_not_touch_alias_inherited() -> None:
    """alias_inherited 的空锚是刻意设计（``_prep_pack_scene_alias_
    provenance`` 故意不拿 name 自身当候选以避免同义反复）——即便声明段落里
    确实存在能命中的尾部子串，这里也不该被意外回填。"""
    segments = index_source_segments("她路过一间半开着门的书房。")
    result = scene_anchor_with_trailing_fallback(
        "alias_inherited", [], "", segments, [1], "顾屿家书房", "",
    )
    assert result == ([], "", False)


def test_scene_anchor_with_trailing_fallback_no_match_stays_empty() -> None:
    segments = index_source_segments("桌上放着一只杯子。")
    result = scene_anchor_with_trailing_fallback(
        "direct", [], "", segments, [1], "顾屿家书房", "",
    )
    assert result == ([], "", False)


# ---------------------------------------------------------------------------
# 道具侧端到端接线：_prep_pack_build_prop_manifest
# ---------------------------------------------------------------------------


def test_prop_manifest_rescues_notebook_via_trailing_anchor() -> None:
    segments = index_source_segments("一本封面陈旧、锁扣锈迹斑斑的笔记本静静躺在抽屉里。")
    mention = {
        "label": "旧笔记本", "description": "一本旧笔记本", "segment_indexes": [1],
        "plot_significant": False, "plot_significant_quote": "", "source_wording": "",
        "known_prop_name": "",
    }
    unanchored: list[dict] = []
    props = _prep_pack_build_prop_manifest([mention], segments, cards=[], unanchored=unanchored)
    assert len(props) == 1
    entry = props[0]
    assert entry["label"] == "旧笔记本"
    assert entry["provenance"]["method"] == "direct"
    assert entry["provenance"]["anchor_phrase"] == "笔记本"
    assert entry["provenance"]["trailing_anchor"] is True
    assert unanchored == []


def test_prop_manifest_still_unanchored_when_retreat_to_two_chars_fails() -> None:
    """退让到 2 字仍不命中——跟原有「两条判据都不满足」的 unanchored 处置
    完全一致（可见日志 + unanchored 出参），不是新增一条静默通道。"""
    segments = index_source_segments("桌上放着一只杯子。")
    mention = {
        "label": "妈妈的字条", "description": "一张字条", "segment_indexes": [1],
        "plot_significant": False, "plot_significant_quote": "",
        "source_wording": "妈妈写的字条", "known_prop_name": "",
    }
    unanchored: list[dict] = []
    props = _prep_pack_build_prop_manifest([mention], segments, cards=[], unanchored=unanchored)
    assert props == []
    assert len(unanchored) == 1
    assert unanchored[0]["label"] == "妈妈的字条"


# ---------------------------------------------------------------------------
# 场景侧端到端接线：_resolve_assets（同 test_prep_pack_source_wording.py
# 既有「④」测试同一套最小 conn 夹具）
# ---------------------------------------------------------------------------


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE projects(id TEXT PRIMARY KEY, bible_json TEXT)")
    conn.execute(
        "CREATE TABLE character_portraits(id TEXT, project_id TEXT, character_name TEXT, "
        "ep_start INTEGER, ep_end INTEGER)"
    )
    conn.execute(
        "CREATE TABLE scene_references(id TEXT, project_id TEXT, scene_name TEXT, "
        "ep_start INTEGER, ep_end INTEGER)"
    )
    conn.execute(
        "CREATE TABLE episodes(id TEXT, project_id TEXT, episode_no INTEGER, "
        "source_chapters TEXT, screenplay_json TEXT)"
    )
    conn.execute("CREATE TABLE chapters(project_id TEXT, idx INTEGER, content TEXT)")
    conn.execute(
        "INSERT INTO projects(id, bible_json) VALUES ('p1', ?)",
        (json.dumps({
            "characters": [], "scenes": [],
            "world": {"era": "", "genre": "", "visual_style_canonical": "测试画风"},
        }, ensure_ascii=False),),
    )
    conn.commit()
    return conn


def test_scene_trailing_anchor_rescues_newly_discovered_study(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实案例：场景「顾屿家书房」是本集新发现的地点，quote/source_wording
    都没能逐字命中（模型给的是概括写法），但声明段落里有「书房」——退让后
    必须成功锚定，不再判未解析，provenance 打 trailing_anchor 标记。"""
    from app import scenes as scenes_module

    conn = _make_conn()

    async def fake_ensure_scenes_for_labels(project_id, episode_no, labels, evidence=None):
        assert labels == ["顾屿家书房"]
        conn.execute(
            "INSERT INTO scene_references(id, project_id, scene_name, ep_start, ep_end) "
            "VALUES ('sr-study','p1','顾屿家书房',2,NULL)"
        )
        conn.commit()
        return {
            "added": [{"name": "顾屿家书房"}], "errors": [], "ready_scenes": ["顾屿家书房"],
            "resolved_names": {"顾屿家书房": "顾屿家书房"},
        }

    monkeypatch.setattr(scenes_module, "ensure_scenes_for_labels", fake_ensure_scenes_for_labels)

    source_text = (
        "占位第一段内容。"
        "\n\n她路过一间半开着门的书房，脚步顿了顿，没有进去。"
        "\n\n她继续往前走，脚步声渐渐远去。"
    )
    scene_mentions = [{
        "display_name": "顾屿家书房", "suspected_true_name": None,
        "segment_indexes": [2, 3], "quote": "", "source_wording": "",
    }]

    result = asyncio.run(prep_pack._resolve_assets(
        conn, project_id="p1", episode_id="ep-test", episode_no=2,
        source_text=source_text, character_mentions=[], scene_mentions=scene_mentions,
        prop_mentions=[], run_id=None,
    ))
    _characters, scene_list, _props, _functional_extras, errors, _stats = result[:6]

    assert errors == []
    assert len(scene_list) == 1
    entry = scene_list[0]
    assert entry["display_name"] == "顾屿家书房"
    assert entry["provenance"]["method"] == "discovery"
    assert entry["provenance"]["anchor_phrase"] == "书房"
    assert entry["provenance"]["anchor_segments"] == [2]
    assert entry["provenance"]["trailing_anchor"] is True
