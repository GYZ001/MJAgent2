"""分镜台「分镜正文复核」（``app.production.storyboard_prose_review``）。

真人短剧《顾念长安》第 1 集多代理逐段核查成立的七类真实缺陷（单镜动作过载、写实
画风脸红措辞、无台词的持续说话、跨段左右站位翻转、道具凭空出现、单镜大跨度时间
跳跃、否定句写动作）各用一条真实形状的句子覆盖代码核验；再覆盖「模型提名、代码
核验」的丢弃路径、按升序定向重写、重写仍违规写 degraded_capabilities、重写抛异常
保留原稿、开关关闭逐字不变。

monkeypatch 策略：``storyboard_prose_review`` 是普通 Python 包内模块（不是
``app/stages``/``app/portraits`` 那类 ``exec()`` 聚合外观），``_generate_all_
segment_prompts``/``_review_segment`` 都通过 ``from x import y`` 在本模块里持有
自己的名字绑定；因此直接 ``monkeypatch.setattr(prose_review, "_xxx", stub)``
打在这个模块自己的命名空间上就对本模块唯一的调用点生效，不需要
``tests/conftest.py`` 的 ``patch_*_everywhere``（那是给 exec() 外观包准备的，
见其文档）。``model_gateway`` 走的是 ``from app.harness import model_gateway``
模块引用（不是函数名绑定），打在 ``model_gateway.chat_structured`` 上对全仓所有
消费方都生效，同一先例见 ``tests/test_storyboard_short_drama_review.py``。
"""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from app.db import set_setting
from app.harness import model_gateway
from app.production import storyboard_prose_review as prose_review
from app.production.storyboard_action_density import MAX_KEY_ACTIONS_PER_SHOT, key_action_definition, over_limit_remedy
from app.production.storyboard_pack import _AiStoryboardSegmentDraft
from app.production.storyboard_skin_blush import SEEDANCE_SKIN_BLUSH_RULE
from app.schemas.segment_identity import SegmentDialogue
from app.visual_styles import VISUAL_STYLE_PRESETS


def _draft(prompt_text: str, *, dialogue: list | None = None, degraded_capabilities: list[str] | None = None) -> _AiStoryboardSegmentDraft:
    return _AiStoryboardSegmentDraft(
        prompt_text=prompt_text, shot_count=3, dialogue=dialogue or [], degraded_capabilities=degraded_capabilities or [],
    )


# ---------------------------------------------------------------------------
# 七类真实形状的违规：代码核验应当保留（quote/previous_quote 逐字核验通过）
# ---------------------------------------------------------------------------

_REAL_VIOLATION_CASES = [
    (
        "action_density",
        "镜头2：在床上猛地坐起，光着双脚跳下单人床，冲到窗台下单膝半蹲，右手捏住白色插头一把拔出，"
        "指尖碰到发烫的插头外壳猛地一缩，手指收回胸前，眉头拧紧。",
        "右手捏住白色插头一把拔出，指尖碰到发烫的插头外壳猛地一缩，手指收回胸前，眉头拧紧",
        None, "",
    ),
    ("skin_blush", "镜头3：她低头浅笑，耳根浅浅地红透。", "耳根浅浅地红透", None, ""),
    (
        "unvoiced_speech",
        "镜头3：她嘴唇轻轻张合像在说起小时候的事，说着说着肩膀轻轻一颤笑出声来。",
        "嘴唇轻轻张合像在说起小时候的事，说着说着肩膀轻轻一颤笑出声来", None, "",
    ),
    (
        "screen_side",
        "镜头1：人物位置承接上一段末镜，温念站在画面右侧，顾屿站在画面左侧。", "温念站在画面右侧",
        "镜头4：温念在画面左侧、顾屿在画面右侧，两人对视。", "温念在画面左侧、顾屿在画面右侧",
    ),
    (
        "prop_appearance",
        "镜头1：直接接上一段结尾的同一状态，桌上已经放着那部手机。", "桌上已经放着那部手机",
        "镜头4：她转身离开房间，桌上空无一物。", "桌上空无一物",
    ),
    (
        "time_jump",
        "镜头4：镜头从深夜接水的水龙头缓缓横摇到天亮时分她已沉沉睡去的床头。",
        "镜头从深夜接水的水龙头缓缓横摇到天亮时分她已沉沉睡去的床头", None, "",
    ),
    ("negated_action", "镜头2：她站在门口，她没有往里走。", "她没有往里走", None, ""),
]


@pytest.mark.parametrize("kind,prompt_text,quote,previous_prompt_text,previous_quote", _REAL_VIOLATION_CASES)
def test_real_shaped_violation_survives_code_verification(kind, prompt_text, quote, previous_prompt_text, previous_quote):
    draft = _draft(prompt_text)
    previous = _draft(previous_prompt_text) if previous_prompt_text else None
    violation = prose_review.ProseViolation(kind=kind, shot_label="镜头1", quote=quote, previous_quote=previous_quote, fix="按判据改写")
    verified = prose_review._verified_violations([violation], segment_no=1, draft=draft, previous_draft=previous)
    assert verified == [violation]


# ---------------------------------------------------------------------------
# 代码核验：丢弃路径
# ---------------------------------------------------------------------------

def test_unverifiable_quote_is_discarded_and_logged(caplog):
    draft = _draft("镜头1：她安静地坐着看向窗外。")
    violation = prose_review.ProseViolation(kind="negated_action", quote="这句话根本不在正文里出现过", fix="x")
    with caplog.at_level(logging.WARNING):
        verified = prose_review._verified_violations([violation], segment_no=5, draft=draft, previous_draft=None)
    assert verified == []
    assert "[STORYBOARD_PROSE_REVIEW_UNVERIFIED]" in caplog.text


def test_invalid_kind_is_discarded_and_logged(caplog):
    draft = _draft("镜头1：她没有往里走。")
    violation = prose_review.ProseViolation(kind="color_grading", quote="她没有往里走", fix="x")
    with caplog.at_level(logging.WARNING):
        verified = prose_review._verified_violations([violation], segment_no=2, draft=draft, previous_draft=None)
    assert verified == []
    assert "[STORYBOARD_PROSE_REVIEW_UNVERIFIED]" in caplog.text


def test_screen_side_without_previous_draft_is_discarded():
    draft = _draft("镜头1：温念站在画面右侧。")
    violation = prose_review.ProseViolation(kind="screen_side", quote="温念站在画面右侧", previous_quote="温念在画面左侧", fix="x")
    verified = prose_review._verified_violations([violation], segment_no=1, draft=draft, previous_draft=None)
    assert verified == []


def test_prop_appearance_with_unverifiable_previous_quote_is_discarded():
    draft = _draft("镜头1：桌上已经放着那部手机。")
    previous = _draft("镜头4：她转身离开房间。")
    violation = prose_review.ProseViolation(kind="prop_appearance", quote="桌上已经放着那部手机", previous_quote="编造的上一段原文", fix="x")
    verified = prose_review._verified_violations([violation], segment_no=2, draft=draft, previous_draft=previous)
    assert verified == []


# ---------------------------------------------------------------------------
# 判据文本：action_density 复用常量、skin_blush 只在写实画风出现
# ---------------------------------------------------------------------------

def test_review_rules_text_includes_skin_blush_only_when_photographic():
    text_on = prose_review._review_rules_text(photographic=True, max_shots=4)
    text_off = prose_review._review_rules_text(photographic=False, max_shots=4)
    assert "skin_blush" in text_on and SEEDANCE_SKIN_BLUSH_RULE in text_on
    assert "skin_blush" not in text_off
    assert str(MAX_KEY_ACTIONS_PER_SHOT) in text_on
    # 计数口径与满镜应对和生成侧共用同一份原文（两侧不各数各的、满镜时有可执行的改法）
    assert key_action_definition() in text_on
    assert over_limit_remedy(max_shots=4) in text_on
    assert "口型说明" in text_on, "系统写入的口型说明不算否定句违规"


# ---------------------------------------------------------------------------
# 占位符清单 / 上一段末镜文字 / 写实画风判定
# ---------------------------------------------------------------------------

def test_dialogue_placeholders_format_matches_prompt_token():
    line = SegmentDialogue(utterance_id="U01", speaker_identity_id="id_a", line="走吧", source_segment_index=1)
    assert prose_review._dialogue_placeholders([line]) == [
        {"utterance_id": "U01", "placeholder": "{{speech:U01}}", "speaker_identity_id": "id_a", "line": "走吧"}
    ]


def test_previous_shot_text_takes_last_shot():
    previous = _draft("镜头1：甲。\n镜头2：乙。")
    assert prose_review._previous_shot_text(previous) == "乙。"


def test_previous_shot_text_empty_without_previous_draft():
    assert prose_review._previous_shot_text(None) == ""


def test_is_photographic_true_for_real_photo_preset():
    preset = next(p for p in VISUAL_STYLE_PRESETS if p.photographic)
    bible = SimpleNamespace(world=SimpleNamespace(visual_style_canonical=preset.prompt))
    assert prose_review._is_photographic(bible) is True


def test_is_photographic_false_without_bible():
    assert prose_review._is_photographic(None) is False


# ---------------------------------------------------------------------------
# 开关登记：两处声明必须一致，未声明的设置键写接口会拒写
# ---------------------------------------------------------------------------

def test_setting_registered_in_default_settings_and_schema():
    from app.config import DEFAULT_SETTINGS
    from app.monitoring import SETTINGS_SCHEMA

    assert DEFAULT_SETTINGS[prose_review.PROSE_REVIEW_SETTING_KEY] == "true"
    spec = SETTINGS_SCHEMA[prose_review.PROSE_REVIEW_SETTING_KEY]
    assert spec["type"] == "boolean" and spec["default"] == "true"


def test_enabled_defaults_true_on_empty_value(monkeypatch):
    monkeypatch.setattr(prose_review, "get_setting", lambda key: "")
    assert prose_review.storyboard_prose_review_enabled() is True


@pytest.mark.parametrize("raw", ["0", "false", "off", "no", "FALSE", "Off"])
def test_enabled_false_on_explicit_negative_values(monkeypatch, raw):
    monkeypatch.setattr(prose_review, "get_setting", lambda key: raw)
    assert prose_review.storyboard_prose_review_enabled() is False


# ---------------------------------------------------------------------------
# review_and_revise_segments：按升序只重写违规段
# ---------------------------------------------------------------------------

def _three_segment_drafts() -> dict[int, _AiStoryboardSegmentDraft]:
    return {
        1: _draft("镜头1：A动作一，动作二，动作三。"),
        2: _draft("镜头1：B干净无问题。"),
        3: _draft("镜头1：C动作一，动作二，动作三。"),
    }


@pytest.mark.asyncio
async def test_only_violated_segments_rewritten_in_ascending_order(monkeypatch):
    drafts = _three_segment_drafts()
    review_counts: dict[int, int] = {}

    async def fake_review_segment(*, episode_id, segment_no, draft, previous_draft, photographic, max_shots):
        review_counts[segment_no] = review_counts.get(segment_no, 0) + 1
        if segment_no in (1, 3) and review_counts[segment_no] == 1:
            return [prose_review.ProseViolation(kind="action_density", shot_label="镜头1", quote="动作一，动作二，动作三", fix="拆镜")]
        return []

    regenerated_order: list[int] = []

    async def fake_generate_all(reuse, notes):
        no = next(n for n in drafts if n not in reuse)
        regenerated_order.append(no)
        fixed = dict(reuse)
        fixed[no] = _draft(f"镜头1：{no}号已改好，干净。")
        return fixed

    monkeypatch.setattr(prose_review, "_review_segment", fake_review_segment)
    result = await prose_review.review_and_revise_segments(drafts, episode_id="ep1", bible=None, max_shots=4, regenerate=fake_generate_all)

    assert regenerated_order == [1, 3], "只有违规段被重写，且按段号升序"
    assert result[1].prompt_text == "镜头1：1号已改好，干净。"
    assert result[3].prompt_text == "镜头1：3号已改好，干净。"
    assert result[2] is drafts[2], "干净段原样保留，未被重写"


@pytest.mark.asyncio
async def test_remaining_violation_after_rewrite_is_recorded_not_blocking(monkeypatch):
    drafts = {1: _draft("镜头1：A动作一，动作二，动作三。")}

    async def always_violating(*, episode_id, segment_no, draft, previous_draft, photographic, max_shots):
        return [prose_review.ProseViolation(kind="action_density", shot_label="镜头1", quote="动作一，动作二，动作三", fix="拆镜")]

    async def fake_generate_all(reuse, notes):
        no = next(n for n in drafts if n not in reuse)
        fixed = dict(reuse)
        fixed[no] = _draft("镜头1：依然是动作一，动作二，动作三。")  # 重写后仍然超限
        return fixed

    monkeypatch.setattr(prose_review, "_review_segment", always_violating)
    result = await prose_review.review_and_revise_segments(drafts, episode_id="ep1", bible=None, max_shots=4, regenerate=fake_generate_all)

    assert result[1].shot_count == 3  # 没有抛异常，整集没被阻断
    assert any("[STORYBOARD_PROSE_REVIEW_REMAINING][未拦截]" in note for note in result[1].degraded_capabilities)


@pytest.mark.asyncio
async def test_rewrite_exception_keeps_original_draft(monkeypatch, caplog):
    original = _draft("镜头1：A动作一，动作二，动作三。")
    drafts = {1: original}

    async def violating_once(*, episode_id, segment_no, draft, previous_draft, photographic, max_shots):
        return [prose_review.ProseViolation(kind="action_density", shot_label="镜头1", quote="动作一，动作二，动作三", fix="拆镜")]

    async def failing_generate_all(reuse, notes):
        raise RuntimeError("供应商 500")

    monkeypatch.setattr(prose_review, "_review_segment", violating_once)
    with caplog.at_level(logging.WARNING):
        result = await prose_review.review_and_revise_segments(drafts, episode_id="ep1", bible=None, max_shots=4, regenerate=failing_generate_all)

    assert result[1] is original, "重写调用抛异常时保留原稿"
    assert "[STORYBOARD_PROSE_REVIEW_FAILED]" in caplog.text, "不吞异常信息"
    assert any("[STORYBOARD_PROSE_REVIEW_REMAINING][未拦截]" in note for note in result[1].degraded_capabilities)


@pytest.mark.asyncio
async def test_disabled_switch_skips_all_review_calls(monkeypatch):
    set_setting(prose_review.PROSE_REVIEW_SETTING_KEY, "false")
    drafts = _three_segment_drafts()

    async def _must_not_be_called(*args, **kwargs):
        raise AssertionError("开关关闭时不应发起任何复核调用")

    monkeypatch.setattr(model_gateway, "chat_structured", _must_not_be_called)
    async def _must_not_regenerate(reuse, notes):
        raise AssertionError("开关关闭时不应重写")

    result = await prose_review.review_and_revise_segments(drafts, episode_id="ep1", bible=None, max_shots=4, regenerate=_must_not_regenerate)

    assert result is drafts, "开关关闭时产物逐字不变（同一对象，未经任何改写）"


@pytest.mark.asyncio
async def test_batch_review_calls_chat_structured_when_enabled(monkeypatch):
    """复核确实走 model_gateway.chat_structured（而不是绕过它），且 kind 来自
    schema 允许的同一份取值——两侧对齐，不靠本文件自己猜一份。"""
    drafts = {1: _draft("镜头1：她没有往里走。")}
    calls = []

    async def fake_chat_structured(*args, **kwargs):
        calls.append(kwargs)
        model_type = kwargs["model_type"]
        return model_type(violations=[{"kind": "negated_action", "shot_label": "镜头1", "quote": "她没有往里走", "fix": "改成正面写法"}])

    monkeypatch.setattr(model_gateway, "chat_structured", fake_chat_structured)

    outstanding = await prose_review._run_batch_review(drafts, episode_id="ep1", photographic=False, max_shots=4)

    assert len(calls) == 1
    assert calls[0]["call_meta"]["stage_key"] == "storyboard_prose_review"
    assert outstanding[1][0].kind == "negated_action"
