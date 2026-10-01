"""口型标注去重（app.production.storyboard_speech_render）。

2026-09-30 真实回归（B 机 provider_calls id=81314，第 1 集第 20 段 opus 原始输出
逐字核实）：``……先停顿一拍，再压低声音连贯快速地答：{{speech:U02}}（发声者开口，
其他可见人物不跟随口型）光影：台灯暖黄光……``——模型在占位符**紧后**自己又写了
一遍系统本该生成的口型标注，而 ``dialogue[].line`` 里 U02 的原话是干净的「没
什么，工作上的旧资料。」，不含任何标注。``render_segment_speech`` 原先只替换
``{{speech:U02}}`` 这四个字本身，模型紧跟着写的那份标注原样留在旁边，
``rendered_utterance`` 又在展开结果末尾追加一份，两份紧挨着重复。
"""
from __future__ import annotations

from copy import deepcopy

from app.production.storyboard_speech_render import render_segment_speech

DIALECT = "seedance_compact_director_brief"


def _segment(template: str, *, delivery_kind: str = "spoken_dialogue") -> dict:
    return {
        "speech_dialect": DIALECT,
        "speech_template": template,
        "prompt_text": "",
        "resources": {"characters": [{"identity_id": "bible:听听", "display_name": "听听"}]},
        "dialogue": [{
            "utterance_id": "U02", "speaker_identity_id": "bible:听听", "line": "没什么，工作上的旧资料。",
            "delivery": "spoken_dialogue" if delivery_kind == "spoken_dialogue" else "offscreen_voice",
            "delivery_kind": delivery_kind,
        }],
    }


def _render(segment: dict) -> str:
    return render_segment_speech(deepcopy(segment), dialect=segment["speech_dialect"])["prompt_text"]


def test_baseline_no_trailing_annotation_renders_exactly_once():
    """基线：占位符后面没有任何标注时，展开结果只有本函数自己生成的那一份。"""
    prompt = _render(_segment("镜头1：压低声音，一字一顿：{{speech:U02}}光影：台灯暖黄光。"))
    assert prompt == "镜头1：压低声音，一字一顿：画内对白（听听）：“没什么，工作上的旧资料。”（发声者开口，其他可见人物不跟随口型）光影：台灯暖黄光。"
    assert prompt.count("发声者开口，其他可见人物不跟随口型") == 1


def test_dedupes_when_model_writes_matching_annotation_right_after_placeholder():
    """2026-09-30 真实回归逐字复现：占位符紧后跟着模型自己写的同一条标注，
    dialogue[].line 本身干净、不含标注——重复完全是模型在正文里紧跟占位符写出来的，
    不是台词原话带来的。"""
    prompt = _render(_segment(
        "镜头1：先停顿一拍，再压低声音连贯快速地答：{{speech:U02}}（发声者开口，其他可见人物不跟随口型）光影：台灯暖黄光。",
    ))
    assert prompt == (
        "镜头1：先停顿一拍，再压低声音连贯快速地答："
        "画内对白（听听）：“没什么，工作上的旧资料。”（发声者开口，其他可见人物不跟随口型）光影：台灯暖黄光。"
    )
    assert prompt.count("发声者开口，其他可见人物不跟随口型") == 1


def test_dedupes_when_model_writes_mismatched_annotation_after_placeholder():
    """占位符对应的是画外对白（该用闭口标注），但模型紧跟着写的是另一种（开口）标注：
    以 rendered_utterance 按 delivery_kind 生成的那条（闭口）为准，模型写错的那条去掉，
    不是简单看字符串相等。"""
    prompt = _render(_segment(
        "镜头1：画外音渐起：{{speech:U02}}（发声者开口，其他可见人物不跟随口型）光影：台灯暖黄光。",
        delivery_kind="offscreen_dialogue",
    ))
    assert prompt == (
        "镜头1：画外音渐起："
        "人物画外对白（听听）：“没什么，工作上的旧资料。”（画面人物嘴唇闭合无张合动作）光影：台灯暖黄光。"
    )
    assert "发声者开口，其他可见人物不跟随口型" not in prompt
    assert prompt.count("画面人物嘴唇闭合无张合动作") == 1
