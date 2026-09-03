"""app.video_modes.seedance_reference_notes 单测（2026-09-03 新拆出模块）。

覆盖三件事：① prompt 正文里完全匹配的 @角色名/@场景名确定性替换成
@图片N，且不吃掉名字后面的空格、不误伤更短名字恰好是更长名字前缀的情况；
② 追加在正文之后的参考图用途说明改成中文、按类型给出不同措辞；
③ REFERENCE_PROMPT_NOTE_MARKER 的幂等语义（已经带过说明的 prompt 不重复
加）在新模块里原样保留。不测试真实供应商往返，只测这个模块的纯函数行为。

app.video_modes.seedance_pack.append_reference_prompt_notes_from_dicts 现在
只是这个模块的一行委托，另有一个用例直接校验这层委托没有走样。
"""
from __future__ import annotations

from app.video_modes import seedance_pack
from app.video_modes.seedance_reference_notes import (
    REFERENCE_PROMPT_NOTE_MARKER,
    REFERENCE_SINGLE_INSTANCE_NOTE,
    build_seedance_reference_prompt_notes,
)


def _character_ref(name: str) -> dict:
    return {
        "type": "character",
        "entity_name": name,
        "relatedCharacterIds": [name],
    }


def _scene_ref() -> dict:
    return {"type": "scene"}


def test_at_mention_replaced_with_picture_number_and_trailing_space_kept():
    prompt = "镜头1：固定远景镜头，@李麦麦 20多岁职场女性，蹲在门口。"
    refs = [_character_ref("李麦麦")]

    result = build_seedance_reference_prompt_notes(prompt, refs)

    assert "@图片1 20多岁职场女性" in result
    assert "@李麦麦" not in result


def test_at_mention_replacement_prefers_longest_name_match():
    """"李麦"是"李麦麦"的前缀；两个角色都在场时，@李麦麦 必须整体替换成
    它自己对应的图号，不能被短名字"李麦"先吃掉一部分。"""
    prompt = "镜头2：@李麦麦 转身看向 @李麦。"
    refs = [_character_ref("李麦麦"), _character_ref("李麦")]

    result = build_seedance_reference_prompt_notes(prompt, refs)

    assert "@图片1 转身看向 @图片2" in result
    assert "@李麦" not in result and "@李麦麦" not in result


def test_no_reference_images_leaves_at_mentions_untouched():
    """text-only 回退：没有任何参考图时，正文里的 @名字 原样保留，也不
    追加任何用途说明。"""
    prompt = "镜头1：@橘座 蹲坐在窗台上。"

    result = build_seedance_reference_prompt_notes(prompt, [])

    assert result == prompt
    assert REFERENCE_PROMPT_NOTE_MARKER not in result


def test_purpose_note_is_chinese_marker_and_covers_scene_and_character():
    prompt = "镜头1：@橘座 蹲坐在窗台上。"
    refs = [_scene_ref(), _character_ref("橘座")]

    result = build_seedance_reference_prompt_notes(prompt, refs)

    assert REFERENCE_PROMPT_NOTE_MARKER in result
    assert "图片1：场景参考，只用来锁定环境外观" in result
    assert "图片2：角色橘座的人物参考，只用来锁定长相与服装" in result
    assert REFERENCE_SINGLE_INSTANCE_NOTE in result
    assert "Reference image" not in result
    assert "use as" not in result


def test_plot_key_frame_purpose_includes_progress_and_target_in_chinese():
    prompt = "镜头1：固定远景。"
    refs = [{
        "type": "plot_key_frame",
        "relatedCharacterIds": ["橘座"],
        "keyframe_index": 1,
        "keyframe_total": 3,
        "keyframe_time_ratio": 0.5,
        "keyframe_target_desc": "橘座抬头看向镜头",
    }]

    result = build_seedance_reference_prompt_notes(prompt, refs)

    assert "图片1：橘座的关键帧参考" in result
    assert "进度约50%" in result and "第1/3拍" in result
    assert "目标画面：橘座抬头看向镜头" in result


def test_marker_makes_note_idempotent_on_second_call():
    prompt = "镜头1：@橘座 蹲坐在窗台上。"
    refs = [_character_ref("橘座")]

    once = build_seedance_reference_prompt_notes(prompt, refs)
    twice = build_seedance_reference_prompt_notes(once, refs)

    assert once == twice
    assert twice.count(REFERENCE_PROMPT_NOTE_MARKER) == 1


def test_duration_suffix_uses_explicit_shot_duration_when_prompt_lacks_dur():
    """分镜台 2.0.0 段落式 prompt_text 从不内嵌 --dur；调用方必须显式传
    shot.duration_s，否则会静默落到 5 秒兜底而不是这一段真正的时长（回归
    EP1 第 3 段 duration_s=15 却提交 --dur 5 的真实故障）。"""
    prompt = "镜头1：@橘座 蹲坐在窗台上。"
    refs = [_character_ref("橘座")]

    result = build_seedance_reference_prompt_notes(prompt, refs, duration_s=15)

    assert result.endswith("--ratio 9:16 --dur 15")


def test_subject_definitions_branch_inserts_note_right_after_heading():
    prompt = "subject_definitions:\nReference subjects follow contract. --ratio 9:16 --dur 5"
    refs = [_character_ref("A")]

    result = build_seedance_reference_prompt_notes(prompt, refs)

    assert result.startswith("subject_definitions:\n图片1：角色A的人物参考")
    assert result.endswith("--ratio 9:16 --dur 5")


def test_seedance_pack_wrapper_delegates_to_new_module():
    """app.video_modes.seedance_pack.append_reference_prompt_notes_from_dicts
    现在只是这个模块的一行委托，两条路径的输出必须逐字相同。"""
    prompt = "镜头1：@橘座 蹲坐在窗台上。"
    refs = [_character_ref("橘座")]

    direct = build_seedance_reference_prompt_notes(prompt, refs)
    via_pack = seedance_pack.append_reference_prompt_notes_from_dicts(prompt, refs)

    assert direct == via_pack
