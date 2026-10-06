"""视频参考说明给道具卡补"此刻穿在谁身上"（2026-10-05，《顾念长安》EP1 第15
段真实故障：温念与顾屿同框，顾屿从头到尾穿着温念的「外套」——道具参考图的
用途说明只写「道具参考，只用来锁定外观与材质」，没说这件衣物是谁穿的，视频
模型把唯一一张单独的外套图分给了画面里的男主角）。

信息其实已经在 ``prompt_text`` 里：``app.production.storyboard_continuity_
memo.ensure_wardrobe_continuity_in_prompt`` 写入的「续接服装：@人物 ……。」
整行。``app.video_modes.seedance_reference_notes.build_seedance_reference_
prompt_notes`` 现在从这行原文反推每张道具参考图（单张/拼图成员）此刻的唯一
穿着者，匹配判据复用 ``app.video_modes.prop_references``（覆盖率/逐字命中/
重叠卡名取最长）。

覆盖：
1. 单张道具卡，命中唯一归属人物时补「「label」是{人}的，本段怎么穿戴或拿着以正文为准」
   （只标归属，不断言此刻穿在身上）；
2. 拼图成员同样逐一标注，覆盖率路径（深灰色围巾 vs 深灰色针织长围巾）与
   零匹配（浅蓝色碎花长裙，两人续接服装都没点到）并存；
3. 两个人都匹配同一 label（同款同名服装）时不标，宁缺不错；
4. 重叠卡名（"外套" 是 "顾屿外套" 的子串）取最长，不把短卡误判给长卡的主人；
5. 没有「续接服装」行、或没有道具参考图时，说明文本逐字不变（回归钉子）；
6. label 带末尾括号注释（如"浅蓝色碎花长裙（裙摆）"）剥注释后仍能匹配到唯一
   穿着者（2026-10-05 第19轮审片返工 #0：带括号注释的真实 label 覆盖率被
   注释字符拉低，本该唯一确定的穿着者被漏标）；
7. 续接服装原文写的是搭在手臂上（携带而非穿着）时仍标归属——措辞只说「是
   谁的」、穿戴方式以正文为准，与原文不矛盾；不靠维护一张「携带动词」表去
   区分穿着与携带（2026-10-05 第19轮审片返工 #1 的取舍，CLAUDE.md「禁止黑白
   名单与枚举穷举」）。
"""
from __future__ import annotations

from app.video_modes.prop_references import label_matches_wardrobe_text, unique_owners_for_labels
from app.video_modes.seedance_reference_notes import build_seedance_reference_prompt_notes

# 任务原文（第1集第15段真实续接服装）：温念、顾屿同框时各自的续接服装整句。
_WEN_WARDROBE = (
    "米白色灯芯绒翻领单排扣外套5颗扣子全部扣合，领口内露出一圈米白针织开衫领口，"
    "外套下摆以下露出一截浅天蓝底奶白小雏菊碎花裙摆，米白色平底单鞋。"
)
_GU_WARDROBE = (
    "深炭灰色羊毛双排扣长大衣，内搭深色毛衫，颈间系深灰色针织长围巾，深色长裤。"
)


def _wardrobe_lines(pairs: list[tuple[str, str]]) -> str:
    return "".join(f"\n续接服装：@{name} {text}。" for name, text in pairs)


def _body(pairs: list[tuple[str, str]]) -> str:
    return "镜头15：@温念 与 @顾屿 并肩走在雨后的街道上。" + _wardrobe_lines(pairs)


def _char_ref(name: str) -> dict:
    return {"type": "character", "entity_name": name, "relatedCharacterIds": [name]}


def _prop_ref(label: str) -> dict:
    return {"type": "prop", "entity_name": label}


def _composite_ref(labels: list[str]) -> dict:
    return {"type": "prop", "view_role": "prop_composite", "composite_member_labels": labels}


_REAL_PAIRS = [("温念", _WEN_WARDROBE), ("顾屿", _GU_WARDROBE)]


# ---------------------------------------------------------------------------
# 1) 单张道具卡：外套→温念、大衣→顾屿
# ---------------------------------------------------------------------------

def test_single_prop_wearer_noted_for_unique_match():
    prompt = _body(_REAL_PAIRS)
    refs = [_char_ref("温念"), _char_ref("顾屿"), _prop_ref("外套"), _prop_ref("大衣")]

    result = build_seedance_reference_prompt_notes(prompt, refs, aspect_ratio="9:16")

    assert "「外套」是温念的，本段怎么穿戴或拿着以正文为准" in result
    assert "「大衣」是顾屿的，本段怎么穿戴或拿着以正文为准" in result


# ---------------------------------------------------------------------------
# 2) 拼图成员：覆盖率路径 + 零匹配不强行标注
# ---------------------------------------------------------------------------

def test_composite_member_wearer_noted_for_unique_matches_only():
    prompt = _body(_REAL_PAIRS)
    labels = ["深色毛衫", "深灰色围巾", "米白色针织开衫", "浅蓝色碎花长裙"]
    refs = [_char_ref("温念"), _char_ref("顾屿"), _composite_ref(labels)]

    result = build_seedance_reference_prompt_notes(prompt, refs, aspect_ratio="9:16")

    assert "深色毛衫（顾屿的）" in result
    # "深灰色围巾" 不是 "深灰色针织长围巾" 的逐字子串，走覆盖率判据命中顾屿。
    assert "深灰色围巾（顾屿的）" in result
    assert "米白色针织开衫（温念的）" in result
    # 两人的续接服装原文都没点到"浅蓝色碎花长裙"（覆盖率不足），不标注、也不报错。
    assert "浅蓝色碎花长裙（" not in result
    assert "浅蓝色碎花长裙" in result


# ---------------------------------------------------------------------------
# 3) 两人都穿同名件：无法唯一确定，不标
# ---------------------------------------------------------------------------

def test_two_wearers_match_same_label_not_noted():
    prompt = _body([("温念", "头戴黑色棒球帽，身穿白色卫衣。"), ("顾屿", "也戴着黑色棒球帽，身穿黑色外套。")])
    refs = [_char_ref("温念"), _char_ref("顾屿"), _prop_ref("黑色棒球帽")]

    result = build_seedance_reference_prompt_notes(prompt, refs, aspect_ratio="9:16")

    assert "以正文为准" not in result and "的）" not in result
    assert "图片3：道具参考，只用来锁定外观与材质" in result


# ---------------------------------------------------------------------------
# 4) 重叠卡名取最长：短卡不因为是长卡的子串被误判
# ---------------------------------------------------------------------------

def test_overlapping_label_substring_prefers_longest_card():
    prompt = _body([("温念", _WEN_WARDROBE), ("顾屿", "顾屿外套扣得严实，内搭深色毛衫。")])
    refs = [_char_ref("温念"), _char_ref("顾屿"), _prop_ref("外套"), _prop_ref("顾屿外套")]

    result = build_seedance_reference_prompt_notes(prompt, refs, aspect_ratio="9:16")

    # "外套"只在温念的续接服装原文里单独出现过；它在顾屿原文里只是更长、更
    # 具体的"顾屿外套"的子串，不能因为子串命中就也判给顾屿（否则会变成两人都
    # 命中、本该标注的温念反而被误判成歧义不标）。
    assert "「外套」是温念的，本段怎么穿戴或拿着以正文为准" in result
    assert "「顾屿外套」是顾屿的，本段怎么穿戴或拿着以正文为准" in result


# ---------------------------------------------------------------------------
# 5) 回归钉子：没有续接服装行 / 没有道具参考图时逐字不变
# ---------------------------------------------------------------------------

def test_unchanged_without_wardrobe_line():
    prompt = "镜头15：@温念 与 @顾屿 并肩走在雨后的街道上。"
    refs = [_char_ref("温念"), _char_ref("顾屿"), _prop_ref("外套"), _composite_ref(["深灰色围巾", "大衣"])]

    result = build_seedance_reference_prompt_notes(prompt, refs, aspect_ratio="9:16")

    assert "以正文为准" not in result and "的）" not in result
    assert "图片3：道具参考，只用来锁定外观与材质" in result
    assert "深灰色围巾、大衣" in result


def test_unchanged_without_any_prop_ref():
    prompt = _body(_REAL_PAIRS)
    refs = [_char_ref("温念"), _char_ref("顾屿")]

    result = build_seedance_reference_prompt_notes(prompt, refs, aspect_ratio="9:16")

    assert "以正文为准" not in result and "的）" not in result
    assert "「外套」" not in result


# ---------------------------------------------------------------------------
# 纯函数单测：app.video_modes.prop_references 的匹配/唯一性判据
# ---------------------------------------------------------------------------

def test_label_matches_wardrobe_text_verbatim_and_coverage_paths():
    assert label_matches_wardrobe_text("外套", _WEN_WARDROBE) is True
    assert label_matches_wardrobe_text("深灰色围巾", _GU_WARDROBE) is True
    assert label_matches_wardrobe_text("浅蓝色碎花长裙", _WEN_WARDROBE) is False
    assert label_matches_wardrobe_text("", _WEN_WARDROBE) is False
    assert label_matches_wardrobe_text("大衣", "") is False


def test_unique_owners_for_labels_excludes_ambiguous_and_unmatched():
    wardrobe_by_name = {"温念": _WEN_WARDROBE, "顾屿": _GU_WARDROBE}
    labels = ["外套", "大衣", "浅蓝色碎花长裙"]

    result = unique_owners_for_labels(labels, wardrobe_by_name)

    assert result == {"外套": "温念", "大衣": "顾屿"}


# ---------------------------------------------------------------------------
# 6) 返工 #0 回归钉子：label 带末尾括号注释，剥注释后仍匹配到唯一穿着者
# ---------------------------------------------------------------------------

def test_label_matches_wardrobe_text_strips_trailing_bracket_annotation():
    text = "米白色针织开衫，浅蓝色碎花长裙，米白色平底单鞋。"
    assert label_matches_wardrobe_text("浅蓝色碎花长裙（裙摆）", text) is True


def test_bracket_annotated_label_wearer_noted_in_full_pipeline():
    prompt = _body([
        ("温念", "米白色针织开衫，浅蓝色碎花长裙，米白色平底单鞋。"),
        ("顾屿", _GU_WARDROBE),
    ])
    refs = [_char_ref("温念"), _char_ref("顾屿"), _prop_ref("浅蓝色碎花长裙（裙摆）")]

    result = build_seedance_reference_prompt_notes(prompt, refs, aspect_ratio="9:16")

    assert "「浅蓝色碎花长裙（裙摆）」是温念的，本段怎么穿戴或拿着以正文为准" in result


# ---------------------------------------------------------------------------
# 7) 返工 #1 回归钉子：续接服装原文写"搭/拎/背/挎/提/夹/抱"是携带不是穿着
# ---------------------------------------------------------------------------

def test_unique_owners_include_carried_items_as_ownership():
    """搭在手臂上的外套/围巾仍归温念——只标归属，不判断是否穿着。"""
    wardrobe_by_name = {
        "温念": "米白色针织开衫，浅蓝色碎花长裙，左小臂搭着对折的米白色灯芯绒外套与深灰色针织围巾。",
    }

    result = unique_owners_for_labels(["外套", "深灰色围巾"], wardrobe_by_name)

    assert result == {"外套": "温念", "深灰色围巾": "温念"}


def test_carried_item_noted_as_ownership_without_asserting_worn():
    prompt = _body([
        ("温念", "米白色针织开衫，浅蓝色碎花长裙，左小臂搭着对折的米白色灯芯绒外套与深灰色针织围巾。"),
        ("顾屿", _GU_WARDROBE),
    ])
    refs = [_char_ref("温念"), _char_ref("顾屿"), _prop_ref("外套")]

    result = build_seedance_reference_prompt_notes(prompt, refs, aspect_ratio="9:16")

    assert "「外套」是温念的，本段怎么穿戴或拿着以正文为准" in result
    assert "身上穿" not in result
