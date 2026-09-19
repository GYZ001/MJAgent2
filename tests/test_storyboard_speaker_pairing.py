"""发声者校验：不许误报拦住正确片段，也不许放走真冲突。

真实事故 ERR-20260919-5a4c99（龙猫出爪 EP6 段 9）：一问一答复述同一个词时，
``textmatch.condense`` 把标点一起去掉，「……八折的？」（周晚）与「八折的。」（小李）
归一后都成了「八折的」，双重循环交叉配对、两句双双报冲突，而数据完全正确。
全库当时 3 段中招（另两段：「善」关羽/刘备、「签了」豪尔赫/里奥）。

两类错误代价不对称，测试按这个来写：漏掉一个发声者写错，人在成片里听得出来；
误报直接阻断正确产出，而且按提示改不动——数据本来就是对的。
"""
from __future__ import annotations

from app.production.storyboard_speech_render import explicit_prompt_speaker_errors

_CHARS = [
    {"identity_id": "bible:周晚", "display_name": "周晚"},
    {"identity_id": "bible:小李", "display_name": "小李"},
]


def _legacy_segment(prompt: str, dialogue: list[dict]) -> dict:
    """旧产物：没有 speech_template，只能靠显式声道标签做文本配对。"""
    return {"prompt_text": prompt, "dialogue": dialogue, "resources": {"characters": _CHARS}}


def _line(speaker: str, text: str) -> dict:
    return {"speaker_identity_id": speaker, "line": text, "delivery": "spoken_dialogue"}


def test_near_identical_lines_do_not_cross_match() -> None:
    """问句与答句只差标点时，不能交叉配对报错——这就是那次真实误报。"""
    prompt = (
        "镜头3：@周晚 开口发问画内对白（周晚）：“……八折的？”，"
        "小李应声画内对白（小李）：“八折的。”"
    )
    segment = _legacy_segment(prompt, [
        _line("bible:周晚", "……八折的？"),
        _line("bible:小李", "八折的。"),
    ])
    assert explicit_prompt_speaker_errors(segment) == []


def test_real_speaker_swap_is_still_caught() -> None:
    """只压误报，不削弱检查：真把发声者写反了仍要报。"""
    prompt = "镜头1：@小李 开口画内对白（小李）：“它没拨错。”"
    segment = _legacy_segment(prompt, [_line("bible:周晚", "它没拨错。")])
    errors = explicit_prompt_speaker_errors(segment)
    assert len(errors) == 1
    assert "小李" in errors[0] and "周晚" in errors[0]


def test_ambiguous_pairing_is_skipped_not_reported() -> None:
    """归一后一对多时不判定。

    同一句台词被两个不同的人各说一次，文本配对无法确定哪个标签对应哪一条；
    此时报错必然有一半是错的，宁可不判——漏报可由人在成片里发现，误报会把正确
    片段锁死。
    """
    prompt = (
        "画内对白（周晚）：“好。”，画内对白（小李）：“好。”"
    )
    segment = _legacy_segment(prompt, [
        _line("bible:周晚", "好。"),
        _line("bible:小李", "好。"),
    ])
    assert explicit_prompt_speaker_errors(segment) == []


def test_punctuation_only_difference_still_matched_loosely() -> None:
    """真排版差异（全角半角、空格）仍要能配上，condense 的本意不能丢。"""
    prompt = "画内对白（小李）：“八折的 。”"
    segment = _legacy_segment(prompt, [_line("bible:周晚", "八折的。")])
    errors = explicit_prompt_speaker_errors(segment)
    assert len(errors) == 1, "只差空格应归一后配上并报出真冲突"


def test_template_segment_skips_text_pairing_entirely() -> None:
    """有 speech_template 时只走模板判据——文本配对是多余门禁，也是误报来源。

    模板判据「按合同重新展开一次、与成品逐字比对」对发声者错位是完备的。叠加
    文本配对不增加保证，只会让近似台词重新触发误报（本次事故的直接形态）。
    """
    template = (
        "镜头3：@周晚 开口发问{{speech:U01}}，小李应声{{speech:U02}}"
    )
    dialogue = [
        dict(_line("bible:周晚", "……八折的？"), utterance_id="U01"),
        dict(_line("bible:小李", "八折的。"), utterance_id="U02"),
    ]
    segment = {
        "prompt_text": (
            "镜头3：@周晚 开口发问画内对白（周晚）：“……八折的？”（发声者开口，其他可见人物不跟随口型），"
            "小李应声画内对白（小李）：“八折的。”（发声者开口，其他可见人物不跟随口型）"
        ),
        "speech_template": template,
        "speech_dialect": "",
        "dialogue": dialogue,
        "resources": {"characters": _CHARS},
    }
    assert explicit_prompt_speaker_errors(segment) == []


def test_template_segment_catches_prompt_tampering() -> None:
    """模板判据没被削弱：成品与按合同展开的结果对不上仍要报。"""
    template = "镜头1：{{speech:U01}}"
    segment = {
        "prompt_text": "镜头1：画内对白（小李）：“它没拨错。”（发声者开口，其他可见人物不跟随口型）",
        "speech_template": template,
        "speech_dialect": "",
        "dialogue": [dict(_line("bible:周晚", "它没拨错。"), utterance_id="U01")],
        "resources": {"characters": _CHARS},
    }
    errors = explicit_prompt_speaker_errors(segment)
    assert errors and "发声模板" in errors[0]
