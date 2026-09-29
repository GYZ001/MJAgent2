"""旁白固定音色角色（narrator_voice_character，2026-09-28）在分镜提示词渲染层的
落地：app.production.storyboard_speech_render.rendered_utterance/render_segment_speech。

背景：《顾念长安（第二版）》第 1 集真实回归——忠实档台词台账把叙述句登记为
delivery_kind=narration、说话人字面量「旁白」，但视频模型每段给旁白随机配声音，
前后不一致。项目设置了旁白固定音色角色后，声道标签括号里写成「{角色}的声音」
而不是字面量「旁白」（例如「旁白（温念的声音）」），与
app.voice.segment_refs._with_narrator_entry / app.video_modes.seedance_reference_
notes._audio_note_part 共用同一个「{name}的声音」短语，三处对上模型才知道这段
参考音频对应哪句台词。

``narrator_voice_character`` 空串（默认）时行为与改动前逐字相同——覆盖回归。
"""
from __future__ import annotations

from app.production.storyboard_speech_render import (
    SPEECH_TOKEN,
    render_segment_speech,
    rendered_utterance,
)

_NAMES = {"旁白": "旁白", "bible:温念": "温念", "bible:顾屿": "顾屿"}


def _narration_line(text: str = "这是旁白") -> dict:
    return {
        "utterance_id": "U01", "speaker_identity_id": "旁白", "line": text,
        "delivery": "offscreen_voice", "delivery_kind": "narration",
    }


def _spoken_line(identity: str, text: str = "你好") -> dict:
    return {
        "utterance_id": "U02", "speaker_identity_id": identity, "line": text,
        "delivery": "spoken_dialogue", "delivery_kind": "spoken_dialogue",
    }


# ---------------------------------------------------------------------------
# rendered_utterance：Seedance（中文自由散文）方言
# ---------------------------------------------------------------------------


def test_narrator_voice_character_empty_leaves_narration_label_unchanged():
    """空串（默认，未接入该设置的旧调用方）：逐字不变，仍是字面量「旁白」。"""
    default_result = rendered_utterance(_narration_line(), _NAMES, dialect="")
    explicit_empty_result = rendered_utterance(_narration_line(), _NAMES, dialect="", narrator_voice_character="")
    assert default_result == explicit_empty_result
    assert default_result.startswith("旁白（旁白）：")


def test_narrator_voice_character_set_rewrites_narration_label():
    result = rendered_utterance(_narration_line("这是旁白"), _NAMES, dialect="", narrator_voice_character="温念")
    assert result.startswith("旁白（温念的声音）：")
    assert "这是旁白" in result


def test_narrator_voice_character_does_not_affect_spoken_dialogue_label():
    """只影响 narration 声道；本人开口的画内对白标签不受影响。"""
    result = rendered_utterance(_spoken_line("bible:顾屿"), _NAMES, dialect="", narrator_voice_character="温念")
    assert result.startswith("画内对白（顾屿）：")


def test_narrator_voice_character_does_not_change_speaker_names_mapping():
    """覆盖只发生在渲染出的自由文本里，不改变 speaker_names() 本身的身份映射——
    字幕（app.subtitles.episode）与发声者校验都直接读这份映射，不受影响。"""
    names_before = dict(_NAMES)
    rendered_utterance(_narration_line(), _NAMES, dialect="", narrator_voice_character="温念")
    assert _NAMES == names_before


# ---------------------------------------------------------------------------
# rendered_utterance：MiniMax H3（英文固定字段语法）方言
# ---------------------------------------------------------------------------


def test_narrator_voice_character_h3_dialect_writes_voice_source_in_speaker_slot():
    """H3 方言不强行把旁白的 S 编号并到该角色本人的编号上（两者是否同框、编号是否
    稳定另有既有机制管），而是把声音来源写进 speaker 槽位本身——「写清声音来源」
    是任务允许的两种做法之一，S1/S2 的具体数字只取决于 sorted(names) 的既有排序，
    不是本次改动关心的东西。"""
    result = rendered_utterance(_narration_line("这是旁白"), _NAMES, dialect="minimax_h3_native_fields", narrator_voice_character="温念")
    assert "温念的声音) narrates in an off-screen voiceover" in result
    assert "旁白)" not in result


def test_narrator_voice_character_h3_dialect_empty_keeps_literal_narrator_speaker():
    result = rendered_utterance(_narration_line(), _NAMES, dialect="minimax_h3_native_fields")
    assert ", 旁白) narrates in an off-screen voiceover" in result


# ---------------------------------------------------------------------------
# render_segment_speech：占位符展开端到端
# ---------------------------------------------------------------------------


def test_render_segment_speech_expands_narration_placeholder_with_narrator_voice():
    segment = {
        "speech_template": "镜头1：{{speech:U01}}",
        "dialogue": [_narration_line("小区物业正给那栋楼换水管")],
        "resources": {"characters": [{"identity_id": "bible:温念", "display_name": "温念"}]},
    }
    render_segment_speech(segment, dialect="", narrator_voice_character="温念")
    assert not SPEECH_TOKEN.search(segment["prompt_text"])
    assert "旁白（温念的声音）：" in segment["prompt_text"]
    assert "小区物业正给那栋楼换水管" in segment["prompt_text"]


def test_render_segment_speech_narrator_voice_empty_is_byte_identical_to_before():
    """设置为空时逐字不变的回归：同一份 segment，narrator_voice_character 默认
    （未传）与显式传入非空值必须产出不同结果；默认与显式空串必须逐字相同。"""
    def _fresh_segment() -> dict:
        return {
            "speech_template": "镜头1：{{speech:U01}}",
            "dialogue": [_narration_line("小区物业正给那栋楼换水管")],
            "resources": {"characters": [{"identity_id": "bible:温念", "display_name": "温念"}]},
        }

    default_segment = _fresh_segment()
    render_segment_speech(default_segment, dialect="")
    explicit_empty_segment = _fresh_segment()
    render_segment_speech(explicit_empty_segment, dialect="", narrator_voice_character="")
    assert default_segment["prompt_text"] == explicit_empty_segment["prompt_text"]
    assert "旁白（旁白）：" in default_segment["prompt_text"]

    narrator_segment = _fresh_segment()
    render_segment_speech(narrator_segment, dialect="", narrator_voice_character="温念")
    assert narrator_segment["prompt_text"] != default_segment["prompt_text"]


# ---------------------------------------------------------------------------
# 评审复现（2026-09-28，blocking）：narrator_voice_character 必须随 segment 持久化，
# 否则任何「只拿到 segment 本身、重渲染再比对」的一致性检查都会拿到默认空串，
# 判定与已保存的 prompt_text 不同，把设置了该功能的段永久挡在提交闸门外。
# ---------------------------------------------------------------------------


def test_render_segment_speech_persists_narrator_voice_character_on_segment():
    """narrator_voice_character 必须像 speech_dialect 一样写回 segment 本身，
    供之后任何只拿到 segment（没有 conn/project_id）的重渲染复现同一次生成。"""
    segment = {
        "speech_template": "镜头1：{{speech:U01}}",
        "dialogue": [_narration_line("小区物业正给那栋楼换水管")],
        "resources": {"characters": [{"identity_id": "bible:温念", "display_name": "温念"}]},
    }
    render_segment_speech(segment, dialect="", narrator_voice_character="温念")
    assert segment["narrator_voice_character"] == "温念"


def test_explicit_prompt_speaker_errors_does_not_false_positive_after_narrator_voice_generation():
    """红：修复前 render_segment_speech 不写回 narrator_voice_character，
    explicit_prompt_speaker_errors 内部重渲染时拿到默认空串，与已保存的正文
    （带「温念的声音」）逐字不等，误判为「提示词与已保存的发声模板/台词合同不同」，
    把这段永久挡在提交闸门外。绿：当前实现不再误判。"""
    from app.production.storyboard_speech_render import explicit_prompt_speaker_errors

    segment = {
        "speech_template": "镜头1：{{speech:U01}}",
        "dialogue": [_narration_line("小区物业正给那栋楼换水管")],
        "resources": {"characters": [{"identity_id": "bible:温念", "display_name": "温念"}]},
    }
    render_segment_speech(segment, dialect="", narrator_voice_character="温念")
    assert explicit_prompt_speaker_errors(segment) == []

    def _old_explicit_prompt_speaker_errors_without_persistence(segment: dict) -> list[str]:
        """修复前的行为：重渲染时不读 segment 自带的 narrator_voice_character（因为
        当时这个字段根本不存在），永远按默认空串重渲染。"""
        prompt = str(segment.get("prompt_text") or "")
        candidate = dict(segment)
        render_segment_speech(candidate, dialect=str(segment.get("speech_dialect") or ""))
        if candidate["prompt_text"] != prompt:
            return ["提示词与已保存的发声模板/台词合同不同，请重新生成该片段的提示词"]
        return []

    assert _old_explicit_prompt_speaker_errors_without_persistence(segment) != [], (
        "旧实现确实会对使用了旁白固定音色的段误判——红态验证成立"
    )


def test_dialogue_revision_helpers_preserve_narrator_voice_character():
    """storyboard_dialogue_revision 的三个重渲染函数同一根因：台词人工修订/溯源
    核验不能因为丢了 narrator_voice_character 而把旁白标签打回默认字面量，
    也不能在核验时产生假阳性。"""
    from app.production.storyboard_dialogue_revision import (
        revise_segment_dialogue, revision_errors, source_faithful_copy,
    )

    segment = {
        "speech_template": "镜头1：{{speech:U01}}",
        "prompt_text": "",
        "speech_dialect": "",
        "dialogue": [_narration_line("小区物业正给那栋楼换水管")],
        "resources": {"characters": [{"identity_id": "bible:温念", "display_name": "温念"}]},
    }
    render_segment_speech(segment, dialect="", narrator_voice_character="温念")
    assert revision_errors(segment) == []  # 没有修订时永远放行，先确认基线本身不报错

    revised = revise_segment_dialogue(segment, {"U01": "水管换完了"}, reason="供应商拒收原句")
    assert "温念的声音" in revised["prompt_text"]
    assert revision_errors(revised) == []

    restored = source_faithful_copy(revised)
    assert "温念的声音" in restored["prompt_text"]
    assert restored["dialogue"][0]["line"] == "小区物业正给那栋楼换水管"
