"""P0-F 道具外观全集锁定（``prop_appearance_locks``）：规则文案、有卡逐字核验、
未知 beat_id 剔除、按 beat_id 跨段分发（不依赖原文段号/映射过滤）、"入场计划
缺锁定"的可见信号、连贯性备忘 props.form 沿用锁定外观。与
``tests/test_storyboard_prop_entrance.py`` 同构。

真实回归数据（proj_ca86b15ab7d7 EP1，见派单只读核实结论）：温念的手机没有
道具卡（第 1-3 段「黑色手机」、第 6 段起「白色手机壳的智能手机」，各段各编）；
行李箱有道具卡（appearance「24寸竖款哑光深卡其色ABS硬壳拉杆箱……」），但
``asset_manifest.props`` 里这条记录的 ``segment_indexes`` 只有 ``[31]``，本集
分镜段 9/10/12 的 ``source_segment_indexes`` 都不包含 31，旧的
``_segment_relevant_assets`` 交集过滤会让这三段拿不到卡片、模型各自现编
「中号深蓝色帆布面软壳拉杆箱」，唯独包含 31 的段 11 见到真卡片、写出与卡片
逐字一致的外观。``test_moments_for_segment_reaches_segments_outside_mapped_
source_indexes`` 用这组真实的段号/beat_id 关系复现「跨段下发不依赖映射的
原文段过滤」这条修复。
"""
from __future__ import annotations

from types import SimpleNamespace

from app.production.storyboard_beat_sheet_schemas import _AiPropAppearanceLock, _AiPropEntrance
from app.production.storyboard_continuity_memo import (
    _AiContinuityMemo,
    _AiPropState,
    ensure_prop_form_matches_lock,
)
from app.production.storyboard_prop_appearance_lock import (
    known_prop_card_appearance_index,
    locks_by_label,
    log_missing_appearance_locks,
    moments_for_segment,
    prop_appearance_lock_beat_sheet_rules,
    segment_advisories,
    segment_rule_text,
    verify_and_lock_appearances,
)

_CARD_APPEARANCE = (
    "24寸竖款哑光深卡其色ABS硬壳拉杆箱，箱体边角带有轻微磕碰掉漆痕迹，侧面悬挂空白"
    "米白色帆布行李牌，箱体下半部分留有水渍干涸形成的不均匀浅褐色印记"
)


def _lock(**overrides) -> _AiPropAppearanceLock:
    defaults = dict(label="水泡坏的行李箱", appearance=_CARD_APPEARANCE, beat_ids=["B9", "B10", "B11", "B12"])
    defaults.update(overrides)
    return _AiPropAppearanceLock(**defaults)


def _payload_with_card(label: str = "水泡坏的行李箱", appearance: str = _CARD_APPEARANCE) -> dict:
    return {"asset_manifest": {"props": [{"label": label, "segment_indexes": [31], "appearance": appearance}]}}


# ---------------------------------------------------------------------------
# prop_appearance_lock_beat_sheet_rules：正面陈述
# ---------------------------------------------------------------------------

def test_rules_are_positive_statements():
    rules = prop_appearance_lock_beat_sheet_rules()
    assert rules and all(isinstance(r, str) and r for r in rules)
    joined = "".join(rules)
    assert "prop_appearance_locks" in joined
    assert "beat_ids" in joined
    assert "逐字复制" in joined


# ---------------------------------------------------------------------------
# known_prop_card_appearance_index：只收有真实卡片外观的道具
# ---------------------------------------------------------------------------

def test_card_index_collects_real_appearance():
    index = known_prop_card_appearance_index(_payload_with_card())
    assert index == {"水泡坏的行李箱": _CARD_APPEARANCE}


def test_card_index_skips_placeholder_note():
    from app.production.storyboard_prop_assets import _NO_CANONICAL_PROP_APPEARANCE_NOTE
    payload = _payload_with_card(appearance=_NO_CANONICAL_PROP_APPEARANCE_NOTE)
    assert known_prop_card_appearance_index(payload) == {}


def test_card_index_empty_for_unmentioned_prop():
    """手机在映射台从未被提名成道具提及——asset_manifest.props 里压根没有这条
    记录，card_index 结构上查不到，不是"查了没找到"（真实回归的另一半根因）。"""
    assert known_prop_card_appearance_index(_payload_with_card()).get("手机") is None


# ---------------------------------------------------------------------------
# verify_and_lock_appearances：未知 beat_id 剔除 + 有卡逐字核验
# ---------------------------------------------------------------------------

def test_unknown_beat_ids_are_dropped_from_lock():
    kept = verify_and_lock_appearances([_lock(beat_ids=["B9", "B_GHOST"])], {"B9", "B10"}, {})
    assert kept[0].beat_ids == ["B9"]


def test_lock_dropped_entirely_when_all_beat_ids_unknown_and_logs(caplog):
    with caplog.at_level("WARNING"):
        kept = verify_and_lock_appearances([_lock(beat_ids=["B_GHOST"])], {"B9", "B10"}, {})
    assert kept == []
    assert "已剔除" in caplog.text


def test_appearance_forced_to_card_text_when_model_paraphrased():
    """真实回归：段 9/10/12 各自现编「中号深蓝色帆布面软壳拉杆箱」，与卡片
    「24寸……ABS硬壳……」不一致——代码核验必须强制改回卡片原文，不信任模型的转述。"""
    lock = _lock(appearance="中号深蓝色帆布面软壳拉杆行李箱，下半截泡在积水里")
    kept = verify_and_lock_appearances([lock], {"B9", "B10", "B11", "B12"}, {"水泡坏的行李箱": _CARD_APPEARANCE})
    assert kept[0].appearance == _CARD_APPEARANCE


def test_appearance_kept_as_self_authored_when_no_card_match():
    """手机没有卡——card_index 里查不到，模型自己写的这一次原样保留。"""
    lock = _lock(label="手机", appearance="白色手机壳的智能手机")
    kept = verify_and_lock_appearances([lock], {"B9", "B10", "B11", "B12"}, {})
    assert kept[0].appearance == "白色手机壳的智能手机"


# ---------------------------------------------------------------------------
# verify_and_lock_appearances：『无卡』分支不能原样采信占位说明文字（评审
# Finding 2）——同一类失败模式已在 wardrobe 字段真实发生过一次。
# ---------------------------------------------------------------------------

def test_lock_dropped_when_appearance_echoes_no_canonical_placeholder(caplog):
    from app.production.storyboard_prop_assets import _NO_CANONICAL_PROP_APPEARANCE_NOTE
    lock = _lock(label="手机", appearance=_NO_CANONICAL_PROP_APPEARANCE_NOTE)
    with caplog.at_level("WARNING"):
        kept = verify_and_lock_appearances([lock], {"B9", "B10", "B11", "B12"}, {})
    assert kept == []
    assert "PLACEHOLDER_ECHOED" in caplog.text
    assert "手机" in caplog.text


def test_lock_dropped_when_appearance_wraps_placeholder_with_extra_text(caplog):
    """模型没有逐字照抄，只是把占位说明文字整段嵌进了自己写的句子里——仍要拦。"""
    from app.production.storyboard_prop_assets import _NO_CANONICAL_PROP_APPEARANCE_NOTE
    lock = _lock(label="手机", appearance=f"（备注）{_NO_CANONICAL_PROP_APPEARANCE_NOTE}")
    with caplog.at_level("WARNING"):
        kept = verify_and_lock_appearances([lock], {"B9", "B10", "B11", "B12"}, {})
    assert kept == []


# ---------------------------------------------------------------------------
# verify_and_lock_appearances：『有卡』分支 label 排版归一后的兜底匹配 +
# 卡片未被任何锁定命中的可见信号（评审 Finding 2）。
# ---------------------------------------------------------------------------

def test_card_matched_after_label_whitespace_normalization():
    """模型把 label 多写了首尾空格——精确匹配会 miss，归一化兜底仍要命中卡片
    并强制改写成卡片原文，不能静默退化成『无卡』分支放行模型自编文字。"""
    lock = _lock(label=" 水泡坏的行李箱 ", appearance="随便写的一段话")
    kept = verify_and_lock_appearances(
        [lock], {"B9", "B10", "B11", "B12"}, {"水泡坏的行李箱": _CARD_APPEARANCE},
    )
    assert kept[0].appearance == _CARD_APPEARANCE


def test_unmatched_card_logs_visible_signal(caplog):
    """素材库有一张卡片，但没有任何一条锁定命中它——记一条可见信号，不是静默
    放过（可能模型漏报了这件道具）。"""
    lock = _lock(label="完全不相关的道具", appearance="随便写的一段话")
    with caplog.at_level("WARNING"):
        verify_and_lock_appearances(
            [lock], {"B9", "B10", "B11", "B12"}, {"水泡坏的行李箱": _CARD_APPEARANCE},
        )
    assert "CARD_UNUSED" in caplog.text
    assert "水泡坏的行李箱" in caplog.text


def test_matched_card_does_not_log_unused_signal(caplog):
    with caplog.at_level("WARNING"):
        verify_and_lock_appearances(
            [_lock()], {"B9", "B10", "B11", "B12"}, {"水泡坏的行李箱": _CARD_APPEARANCE},
        )
    assert "CARD_UNUSED" not in caplog.text


def test_no_unused_signal_when_no_cards_at_all(caplog):
    """card_index 为空（映射台压根没建卡）不是『卡片没被用上』，不能报噪音。"""
    with caplog.at_level("WARNING"):
        verify_and_lock_appearances(
            [_lock(label="手机", appearance="白色手机壳")], {"B9", "B10", "B11", "B12"}, {},
        )
    assert "CARD_UNUSED" not in caplog.text


# ---------------------------------------------------------------------------
# segment_advisories：prompt_text 是否真的写成了锁定外观（评审 Finding 1/3）
# ensure_prop_form_matches_lock 只纠正 continuity_memo 的旁路记账字段，真正
# 发给视频模型的 prompt_text 靠这条非阻断核对补上可见信号。
# ---------------------------------------------------------------------------

def test_segment_advisories_flags_lock_not_shown_in_prompt():
    advisories = segment_advisories([_lock()], prompt_text="镜头1：两人站在门槛外说话。")
    assert advisories
    assert "STORYBOARD_PROP_APPEARANCE_LOCK_NOT_SHOWN" in advisories[0]
    assert "水泡坏的行李箱" in advisories[0]
    assert "[未拦截]" in advisories[0]


def test_segment_advisories_silent_when_appearance_shown_in_prompt():
    advisories = segment_advisories(
        [_lock()], prompt_text=f"镜头1：她把{_CARD_APPEARANCE}的箱子拖到门口。",
    )
    assert advisories == []


def test_segment_advisories_empty_when_no_locks_here():
    assert segment_advisories([], prompt_text="随便什么提示词") == []


# ---------------------------------------------------------------------------
# locks_by_label / moments_for_segment：按 beat_id 跨段分发，不是认领一次
# ---------------------------------------------------------------------------

def test_locks_by_label_maps_label_to_appearance():
    assert locks_by_label([_lock()]) == {"水泡坏的行李箱": _CARD_APPEARANCE}


def test_moments_for_segment_hits_on_beat_id_overlap():
    assert moments_for_segment(["B11"], [_lock()]) == [_lock()]


def test_moments_for_segment_empty_when_no_overlap():
    assert moments_for_segment(["B_OTHER"], [_lock()]) == []


def test_moments_for_segment_is_not_claim_once():
    """与 storyboard_prop_entrance.moments_for_segment 的关键差异：同一件道具的
    锁定要能被它出现的每一个段重复取用，不能被第一个段"认领走"。"""
    locks = [_lock()]
    first = moments_for_segment(["B9"], locks)
    second = moments_for_segment(["B10"], locks)
    assert first == [locks[0]]
    assert second == [locks[0]]


def test_moments_for_segment_reaches_segments_outside_mapped_source_indexes():
    """跨段下发不依赖映射的原文段过滤（真实回归核心修复）：行李箱道具卡在
    asset_manifest 里的 segment_indexes 只有 [31]，本集分镜段 9/10/12 的
    source_segment_indexes（[23,24,25]/[26,27,28]/[34,35,36,37,38]）都不包含
    31——旧的按原文段号交集过滤（storyboard_pack._segment_relevant_assets）
    结构上不可能让这三段拿到卡片；本函数只看 beat_id，与 source_segment_
    indexes/asset_manifest.segment_indexes 完全无关，四个段都能拿到同一份
    锁定外观。"""
    lock = _lock(beat_ids=["B9", "B10", "B11", "B12"])
    segment_beat_ids = {9: ["B9"], 10: ["B10"], 11: ["B11"], 12: ["B12"]}
    segment_source_indexes = {9: [23, 24, 25], 10: [26, 27, 28], 11: [29, 30, 31, 32, 33], 12: [34, 35, 36, 37, 38]}
    manifest_segment_indexes = {31}  # 卡片记录里唯一登记过的原文段号
    for segment_no, beat_ids in segment_beat_ids.items():
        assert moments_for_segment(beat_ids, [lock]) == [lock]
        would_have_matched_old_filter = bool(manifest_segment_indexes & set(segment_source_indexes[segment_no]))
        if segment_no != 11:
            assert not would_have_matched_old_filter  # 旧口径下这三段本该拿不到


# ---------------------------------------------------------------------------
# segment_rule_text
# ---------------------------------------------------------------------------

def test_segment_rule_text_contains_label_and_full_appearance():
    lines = segment_rule_text([_lock()])
    assert len(lines) == 1
    assert "水泡坏的行李箱" in lines[0]
    assert _CARD_APPEARANCE in lines[0]


def test_segment_rule_text_empty_when_no_locks():
    assert segment_rule_text([]) == []


def test_segment_rule_text_is_conditioned_on_visibility_not_asserted_as_fact():
    """2026-10-01（第 1 集第五版真实回归，见 ``storyboard_prop_visibility`` 模块
    docstring）：旧文案「在本段画面中出现」把 beat_id 命中（只说明"在场"）断言成了
    "可见"——星盘被卫衣完全遮住那一段同样命中了 beat_id，旧文案因此会让模型误以为
    即使画面写明道具被遮住，也必须写出完整外观。新文案改成条件句，可见时才要求
    逐字沿用，看不见时指向道具可见性规则处理，不再断言"在本段画面中出现"是既成事实。"""
    lines = segment_rule_text([_lock()])
    assert "在本段画面中出现" not in lines[0]
    assert "如果看得见" in lines[0]
    assert "被遮住、收起或根本不在画面中" in lines[0]
    assert "不写这段外观" in lines[0] and "resources.props" in lines[0]


# ---------------------------------------------------------------------------
# log_missing_appearance_locks：入场计划有提名但没锁定的可见信号
# ---------------------------------------------------------------------------

def _entrance(label: str) -> _AiPropEntrance:
    return _AiPropEntrance(label=label, beat_id="B9", entrance_description="从屋内拖出到门口")


def test_missing_lock_logs_warning_for_unlocked_entrance(caplog):
    with caplog.at_level("WARNING"):
        log_missing_appearance_locks([_entrance("水泡坏的行李箱")], [])
    assert "水泡坏的行李箱" in caplog.text
    assert "STORYBOARD_PROP_APPEARANCE_LOCK_MISSING" in caplog.text


def test_missing_lock_silent_when_entrance_has_matching_lock(caplog):
    with caplog.at_level("WARNING"):
        log_missing_appearance_locks([_entrance("水泡坏的行李箱")], [_lock()])
    assert "STORYBOARD_PROP_APPEARANCE_LOCK_MISSING" not in caplog.text


# ---------------------------------------------------------------------------
# ensure_prop_form_matches_lock（app.production.storyboard_continuity_memo）：
# 连贯性备忘 props.form 以锁定外观为准
# ---------------------------------------------------------------------------

def _draft_with_prop_form(name: str, form: str) -> SimpleNamespace:
    memo = _AiContinuityMemo(time_of_day="白天", props=[_AiPropState(name=name, form=form)])
    return SimpleNamespace(continuity_memo=memo)


def test_ensure_prop_form_matches_lock_overwrites_mismatched_form():
    draft = _draft_with_prop_form("水泡坏的行李箱", "中号深蓝色帆布面软壳拉杆箱")
    assert ensure_prop_form_matches_lock(draft, locks_by_label([_lock()])) == []
    assert draft.continuity_memo.props[0].form == _CARD_APPEARANCE


def test_ensure_prop_form_matches_lock_leaves_unlocked_prop_untouched():
    draft = _draft_with_prop_form("绿萝", "叶子发黑，倒在积水里")
    ensure_prop_form_matches_lock(draft, locks_by_label([_lock()]))
    assert draft.continuity_memo.props[0].form == "叶子发黑，倒在积水里"


def test_ensure_prop_form_matches_lock_noop_when_no_locks_visible_this_segment():
    draft = _draft_with_prop_form("水泡坏的行李箱", "中号深蓝色帆布面软壳拉杆箱")
    ensure_prop_form_matches_lock(draft, {})
    assert draft.continuity_memo.props[0].form == "中号深蓝色帆布面软壳拉杆箱"
