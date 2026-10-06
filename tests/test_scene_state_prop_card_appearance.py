"""场景状态图道具卡外观接线单测（2026-10-05，《顾念长安》EP1 第 15/16/19 段
真实故障：场景状态图把道具卡「绿萝」画成酒红陶盆、「鞋柜」画成高木柜，压过了
同时发出的道具卡）。

覆盖三个点：
① ``app.video_modes.scene_state_views.prop_appearance_notes_for_description``——
  状态描述原文逐字命中道具卡名/别名才追加外观陈述；重叠匹配取最长；不含卡名
  的描述不受扰动；
② ``scene_state_ensure.scene_state_prompt``/``scene_state_views.
  scene_state_input_fingerprint`` 接住这段文字：提示词里带出外观陈述，卡外观
  变了指纹跟着变；
③ ``seedance_reference_notes.build_seedance_reference_prompt_notes``：本段同时
  发了道具参考图时，场景/场景状态图的用途说明补一句"外观以道具参考图为准"，
  没有道具参考图时文案逐字不变。

不测试真实供应商往返/真实数据库，全部是纯函数输入输出断言。
"""
from __future__ import annotations

from app.schemas import Prop
from app.video_modes.scene_state_ensure import scene_state_prompt
from app.video_modes.scene_state_views import (
    prop_appearance_notes_for_description,
    scene_state_input_fingerprint,
)
from app.video_modes.seedance_reference_notes import build_seedance_reference_prompt_notes

_FLOOD_DESC = "出租屋被水淹透：地板积着半掌深的水，原木色矮鞋柜歪倒斜靠在门边墙上，绿萝侧倒在积水里。"


def _prop(name: str, appearance: str, *, aliases: list[str] | None = None) -> Prop:
    return Prop(name=name, appearance_canonical=appearance, aliases=aliases or [])


# ---------- prop_appearance_notes_for_description ----------

def test_notes_include_card_matched_by_literal_name():
    props = [_prop("绿萝", "搭配口径15cm左右米白色哑光塑料花盆，原生心形翠绿色叶片，盆内填充褐色腐殖土")]

    notes = prop_appearance_notes_for_description(_FLOOD_DESC, props)

    assert "「绿萝」" in notes
    assert "米白色哑光塑料花盆" in notes
    assert "它此刻的位置与状态按上面的描述" in notes


def test_notes_include_card_matched_by_alias():
    """卡的正名「出租屋矮柜」没在描述里逐字出现（描述里写的是别名「矮鞋柜」），
    必须靠别名命中——用与正名不同字面的卡名，确保命中来自 aliases 而非偶然
    与正名撞上描述里的子串。"""
    props = [_prop("出租屋矮柜", "低矮开放搁板架，原木色", aliases=["矮鞋柜"])]

    notes = prop_appearance_notes_for_description(_FLOOD_DESC, props)

    assert "「出租屋矮柜」" in notes
    assert "低矮开放搁板架" in notes

    # 去掉别名后这张卡的正名在描述里完全不出现——如果上面的命中其实来自正名
    # 而不是别名，这里应该仍能命中；它必须变成空字符串，才证明上面那次命中
    # 确实是 aliases 起的作用。
    props_without_alias = [_prop("出租屋矮柜", "低矮开放搁板架，原木色")]
    assert prop_appearance_notes_for_description(_FLOOD_DESC, props_without_alias) == ""


def test_notes_longest_match_does_not_double_count_shorter_alias_card():
    """描述里写「顾屿外套」时，不能把恰好是子串的「外套」卡也套上——重叠匹配
    取最长，整段只讲一次「顾屿外套」的外观。"""
    description = "顾屿外套搭在椅背上，墙角堆着纸箱。"
    props = [
        _prop("外套", "黑色羊毛外套，圆领"),
        _prop("顾屿外套", "深灰色风衣外套，领口绣着暗纹"),
    ]

    notes = prop_appearance_notes_for_description(description, props)

    assert "「顾屿外套」" in notes
    assert "深灰色风衣外套，领口绣着暗纹" in notes
    assert "黑色羊毛外套" not in notes
    assert notes.count("外观（颜色、材质、款式）按道具卡画") == 1


def test_notes_empty_when_description_mentions_no_card():
    props = [_prop("凝灵丹", "半透明淡金色药丸，表面有细小光斑")]

    notes = prop_appearance_notes_for_description(_FLOOD_DESC, props)

    assert notes == ""


def test_notes_empty_without_props_or_description():
    assert prop_appearance_notes_for_description(_FLOOD_DESC, []) == ""
    assert prop_appearance_notes_for_description("", [_prop("绿萝", "米白色花盆")]) == ""


# ---------- scene_state_prompt 接住这段文字 ----------

def test_scene_state_prompt_carries_prop_appearance_notes():
    props = [_prop("绿萝", "米白色哑光塑料花盆，原生心形翠绿色叶片")]
    notes = prop_appearance_notes_for_description(_FLOOD_DESC, props)

    prompt = scene_state_prompt("写实", "温念的出租屋", _FLOOD_DESC, "9:16", notes)

    assert "米白色哑光塑料花盆" in prompt
    assert "它此刻的位置与状态按上面的描述" in prompt
    assert "画面中没有任何人物" in prompt


def test_scene_state_prompt_unchanged_when_no_prop_notes():
    """不含卡名的描述：提示词与改动前逐字一致——描述后面直接紧跟"画面中没有
    任何人物"，中间没有被插入任何道具陈述。"""
    prompt = scene_state_prompt("写实", "温念的出租屋", _FLOOD_DESC, "9:16", "")
    prompt_default = scene_state_prompt("写实", "温念的出租屋", _FLOOD_DESC, "9:16")

    assert prompt == prompt_default
    assert f"{_FLOOD_DESC}。画面中没有任何人物" in prompt


# ---------- 指纹随卡外观变化 ----------

def test_fingerprint_changes_when_prop_card_appearance_changes():
    old_notes = prop_appearance_notes_for_description(
        _FLOOD_DESC, [_prop("绿萝", "米白色哑光塑料花盆，原生心形翠绿色叶片")],
    )
    new_notes = prop_appearance_notes_for_description(
        _FLOOD_DESC, [_prop("绿萝", "酒红色陶土花盆，叶片泛黄卷边")],
    )
    assert old_notes != new_notes

    common = {
        "scene_reference_id": "scene_1", "establishing_image_path": "/a.jpg",
        "description": _FLOOD_DESC, "visual_style": "写实",
    }
    fp_old = scene_state_input_fingerprint(**common, prop_appearance_notes=old_notes, prop_state_notes="")
    fp_new = scene_state_input_fingerprint(**common, prop_appearance_notes=new_notes, prop_state_notes="")

    assert fp_old != fp_new


def test_fingerprint_unchanged_when_prop_notes_identical():
    """同一段文字与同一批道具卡重复计算必须拿到同一个指纹——否则每次扫描都
    会误判为"又变了"而重出。"""
    notes = prop_appearance_notes_for_description(
        _FLOOD_DESC, [_prop("绿萝", "米白色哑光塑料花盆")],
    )
    common = {
        "scene_reference_id": "scene_1", "establishing_image_path": "/a.jpg",
        "description": _FLOOD_DESC, "visual_style": "写实",
    }
    assert scene_state_input_fingerprint(**common, prop_appearance_notes=notes, prop_state_notes="") == \
        scene_state_input_fingerprint(**common, prop_appearance_notes=notes, prop_state_notes="")


def test_fingerprint_of_state_without_prop_mention_is_unchanged_from_before_rule():
    """描述里没点到任何道具卡的状态图，提示词与引入道具外观陈述之前逐字相同，
    指纹也必须与引入之前逐字相同——否则这次规则上线会让所有项目的全部状态图
    白白判过期重画，并在重画完成前挡住各自的视频生成。期望值按引入前的
    material 形状独立手算（不调用被测函数），作为独立观察点。"""
    import hashlib
    import json

    from app.video_modes.scene_state_views import PROMPT_VERSION

    common = {
        "scene_reference_id": "scene_1", "establishing_image_path": "/a.jpg",
        "description": "深夜断电后的出租屋，雨夜暗蓝光线", "visual_style": "写实",
    }
    before = hashlib.sha256(json.dumps(
        {**common, "version": PROMPT_VERSION}, ensure_ascii=False, sort_keys=True,
    ).encode("utf-8")).hexdigest()[:32]
    assert PROMPT_VERSION == "v1"
    assert scene_state_input_fingerprint(**common, prop_appearance_notes="", prop_state_notes="") == before
    with_notes = scene_state_input_fingerprint(**common, prop_appearance_notes="画面里的「绿萝」外观按道具卡画。", prop_state_notes="")
    assert with_notes != before


# ---------- seedance 参考图用途说明 ----------

def _scene_state_ref(name: str) -> dict:
    return {"type": "scene", "view_role": "scene_state", "entity_name": name}


def _prop_ref(label: str) -> dict:
    return {"type": "prop", "entity_name": label, "relatedCharacterIds": [label]}


def test_reference_notes_mention_prop_card_authority_when_prop_ref_present():
    refs = [_scene_state_ref("温念的出租屋"), _prop_ref("绿萝")]

    result = build_seedance_reference_prompt_notes("镜头18：积水没过脚踝。", refs, aspect_ratio="9:16")

    assert "「绿萝」" in result
    assert "外观以对应的道具参考图为准" in result
    assert "这张图只决定它们在哪里、此刻处于什么状态" in result


def test_reference_notes_list_composite_member_labels_too():
    """拼图里的道具成员同样要被场景说明点名——不是只认非拼图道具。"""
    refs = [
        _scene_state_ref("温念的出租屋"),
        {"type": "prop", "view_role": "prop_composite", "composite_member_labels": ["绿萝", "鞋柜"]},
    ]

    result = build_seedance_reference_prompt_notes("镜头18：积水没过脚踝。", refs, aspect_ratio="9:16")

    assert "「绿萝」" in result and "「鞋柜」" in result


def test_reference_notes_unchanged_without_any_prop_ref():
    """没有道具参考图时，场景状态图的用途说明逐字不变——不凭空追加这句话。"""
    refs = [_scene_state_ref("温念的出租屋")]

    result = build_seedance_reference_prompt_notes("镜头18：积水没过脚踝。", refs, aspect_ratio="9:16")

    assert "外观以对应的道具参考图为准" not in result
    assert "图片1：场景「温念的出租屋」当前状态参考" in result
