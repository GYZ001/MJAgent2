"""分镜台「分镜正文复核」（``app.production.storyboard_prose_review``）。

真人短剧《顾念长安》第 1 集多代理逐段核查成立的九类真实缺陷（单镜动作过载、写实
画风脸红措辞、无台词的持续说话、跨段左右站位翻转、道具凭空出现、跨段重复的动作
转换、单镜大跨度时间跳跃、运镜物理不可达、否定句写动作）各用一条真实形状的句子
覆盖代码核验；再覆盖「模型提名、代码核验」的丢弃路径。

2026-10-01 边写边审：``review_and_revise_segments``/``_run_batch_review``/
``_regenerate_segment`` 批量后处理已退场（见 ``storyboard_prose_review`` 模块
docstring「边写边审」），本文件对应的「按升序只重写违规段」整集级测试改为
``review_segment_inline``（没有违规/有违规未到最后一次尝试/最后一次尝试仍违规
写 degraded_capabilities/复核调用失败不重写不阻断/开关关闭不发起复核调用）与
``log_review_summary``（汇总日志）两组单段级测试；「第 N 段重写后第 N+1 段衔接
的是修正稿」「开关关闭产物逐字不变」这两条需要 ``_generate_all_segment_prompts``
自己的逐段循环才能验证，见 ``tests/test_storyboard_pack.py``。

2026-10-01 新增 ``repeated_transition_action``（第 1 集重做第二轮分镜独立核查，
``/tmp/mjtest/ep1_redo/segments_r2.json`` 第 30/31 段）：上一段末镜已写温念转身
背对镜头望向窗户，硬切到下一段开场又写她顺着视线回过头、肩膀侧转——同一个转身
在剪辑点两侧被演了两次，``screen_side`` 只管左右站位管不到这种重复；同一批次还
把 ``time_jump`` 的 fix 文案从「硬切或叠化」改成只建议硬切——``storyboard_
dialects`` 贯穿全片「镜头之间硬切」的全局规则下叠化不是段内可选项。

2026-10-01 新增 ``impossible_camera_move``（第 1 集重做第三轮分镜独立核查，
``/tmp/mjtest/ep1_redo/segments_r3.json`` 第 26 段）：收尾镜写「镜头从餐桌上方
缓慢升起并向后拉远，越过窗台退到窗外巷子上空，透过窗户俯看整间餐厅」——真人
实拍摄影机做不到穿过墙体/玻璃从室内直接运动到室外；只在写实画风启用，与
``skin_blush`` 同一开关先例（``_PHOTOGRAPHIC_ONLY_KINDS``）。

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

import pytest

from app.harness import model_gateway
from app.production import storyboard_prose_review as prose_review
from app.production.storyboard_action_density import MAX_KEY_ACTIONS_PER_SHOT, key_action_definition, over_limit_remedy
from app.production.storyboard_continuity_memo import _AiContinuityMemo, _AiPropState
from app.production.storyboard_pack import _AiStoryboardSegmentDraft
from app.production.storyboard_skin_blush import SEEDANCE_SKIN_BLUSH_RULE
from app.schemas.segment_identity import SegmentDialogue


def _draft(prompt_text: str, *, dialogue: list | None = None, degraded_capabilities: list[str] | None = None) -> _AiStoryboardSegmentDraft:
    return _AiStoryboardSegmentDraft(
        prompt_text=prompt_text, shot_count=3, dialogue=dialogue or [], degraded_capabilities=degraded_capabilities or [],
    )


def _draft_with_props(prompt_text: str, props: list[_AiPropState]) -> _AiStoryboardSegmentDraft:
    """供 prop_state_regression 用例：previous_draft 需要带 continuity_memo.props。"""
    return _AiStoryboardSegmentDraft(prompt_text=prompt_text, shot_count=3, continuity_memo=_AiContinuityMemo(time_of_day="白天", props=props))


# ---------------------------------------------------------------------------
# 十类真实形状的违规：代码核验应当保留（quote/previous_quote 逐字核验通过）
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
        "prop_duplication",
        "镜头1：温念拖着那只深卡其色行李箱走在前面，顾屿空手跟在她身侧。镜头4：顾屿手里也拉着"
        "一只与温念同款的深卡其色行李箱，两人并肩往前走。",
        "顾屿手里也拉着一只与温念同款的深卡其色行李箱", None, "",
    ),
    (
        "repeated_transition_action",
        "镜头1：她顺着对面的视线缓缓回过头，肩膀随之侧转，望向顾屿。",
        "她顺着对面的视线缓缓回过头，肩膀随之侧转",
        "镜头4：温念转身，背对镜头，望向窗户。", "温念转身，背对镜头，望向窗户",
    ),
    (
        "time_jump",
        "镜头4：镜头从深夜接水的水龙头缓缓横摇到天亮时分她已沉沉睡去的床头。",
        "镜头从深夜接水的水龙头缓缓横摇到天亮时分她已沉沉睡去的床头", None, "",
    ),
    ("negated_action", "镜头2：她站在门口，她没有往里走。", "她没有往里走", None, ""),
    (
        "impossible_camera_move",
        "镜头4：镜头从餐桌上方缓慢升起并向后拉远，越过窗台退到窗外巷子上空，透过窗户俯看整间餐厅。",
        "镜头从餐桌上方缓慢升起并向后拉远，越过窗台退到窗外巷子上空，透过窗户俯看整间餐厅", None, "",
    ),
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


def test_repeated_transition_action_without_previous_draft_is_discarded():
    """本集第一段没有上一段可比对时，这类违规结构上不可能成立，与 screen_side/
    prop_appearance 同一取舍。"""
    draft = _draft("镜头1：她顺着对面的视线回过头。")
    violation = prose_review.ProseViolation(
        kind="repeated_transition_action", quote="她顺着对面的视线回过头",
        previous_quote="温念转身，背对镜头", fix="x",
    )
    verified = prose_review._verified_violations([violation], segment_no=1, draft=draft, previous_draft=None)
    assert verified == []


# ---------------------------------------------------------------------------
# prop_state_regression（2026-10-04，真实案例：第 1→2 段插座/插头）：
# previous_quote 核验对照上一段 continuity_memo.props，不是 prompt_text
# ---------------------------------------------------------------------------

def test_prop_state_regression_survives_code_verification():
    previous = _draft_with_props(
        "镜头4：她拔下插头。",
        [_AiPropState(name="插座与插头", location="插座在床尾墙根，插头已拔出，躺在地板上", state="已拔下，插座两孔空着")],
    )
    draft = _draft("镜头1：墙根插座上插着白色插头。")
    violation = prose_review.ProseViolation(
        kind="prop_state_regression", prop_name="插座与插头",
        quote="墙根插座上插着白色插头", previous_quote="插座两孔空着", fix="改回插头已拔下、插座两孔空着",
    )
    verified = prose_review._verified_violations([violation], segment_no=2, draft=draft, previous_draft=previous)
    assert verified == [violation]


def test_prop_state_regression_without_previous_draft_is_discarded():
    draft = _draft("镜头1：墙根插座上插着白色插头。")
    violation = prose_review.ProseViolation(
        kind="prop_state_regression", prop_name="插座与插头",
        quote="墙根插座上插着白色插头", previous_quote="插座两孔空着", fix="x",
    )
    verified = prose_review._verified_violations([violation], segment_no=1, draft=draft, previous_draft=None)
    assert verified == []


def test_prop_state_regression_with_unknown_prop_name_is_discarded():
    previous = _draft_with_props("镜头4：她拔下插头。", [_AiPropState(name="插座与插头", location="插座两孔空着", state="已拔下")])
    draft = _draft("镜头1：墙根插座上插着白色插头。")
    violation = prose_review.ProseViolation(
        kind="prop_state_regression", prop_name="一个不存在的道具名",
        quote="墙根插座上插着白色插头", previous_quote="插座两孔空着", fix="x",
    )
    verified = prose_review._verified_violations([violation], segment_no=2, draft=draft, previous_draft=previous)
    assert verified == []


def test_prop_state_regression_with_unverifiable_previous_quote_is_discarded():
    previous = _draft_with_props("镜头4：她拔下插头。", [_AiPropState(name="插座与插头", location="插座两孔空着", state="已拔下")])
    draft = _draft("镜头1：墙根插座上插着白色插头。")
    violation = prose_review.ProseViolation(
        kind="prop_state_regression", prop_name="插座与插头",
        quote="墙根插座上插着白色插头", previous_quote="编造的备忘原文", fix="x",
    )
    verified = prose_review._verified_violations([violation], segment_no=2, draft=draft, previous_draft=previous)
    assert verified == []


def test_review_rules_text_includes_prop_state_regression_regardless_of_photographic():
    text_on = prose_review._review_rules_text(photographic=True, max_shots=4)
    text_off = prose_review._review_rules_text(photographic=False, max_shots=4)
    assert "prop_state_regression" in text_on and "prop_state_regression" in text_off
    assert "previous_continuity_memo.props" in text_on


def test_repeated_transition_action_with_unverifiable_previous_quote_is_discarded():
    draft = _draft("镜头1：她顺着对面的视线回过头。")
    previous = _draft("镜头4：温念站在窗边。")
    violation = prose_review.ProseViolation(
        kind="repeated_transition_action", quote="她顺着对面的视线回过头",
        previous_quote="编造的上一段原文", fix="x",
    )
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
    assert "repeated_transition_action" in text_on
    assert "叠化" not in text_on, "段内只用硬切，time_jump 的 fix 不得再建议叠化（与全局硬切规则冲突）"


def test_review_rules_text_includes_impossible_camera_move_only_when_photographic():
    text_on = prose_review._review_rules_text(photographic=True, max_shots=4)
    text_off = prose_review._review_rules_text(photographic=False, max_shots=4)
    assert "impossible_camera_move" in text_on
    assert "穿过门、窗、墙体、玻璃" in text_on
    assert "之间硬切" in text_on
    assert "impossible_camera_move" not in text_off, "非写实画风项目不应收到这条规则"


def test_review_rules_text_includes_prop_duplication_regardless_of_photographic():
    """2026-10-01（第 1 集修订本段验收后第二轮逐帧复查新增第十类）：道具分身与
    写实/非写实画风无关，两种画风都应该收到这条规则，判据文本单源指向
    ``storyboard_prop_count.SEEDANCE_PROP_COUNT_RULE``。"""
    from app.production.storyboard_prop_count import SEEDANCE_PROP_COUNT_RULE

    text_on = prose_review._review_rules_text(photographic=True, max_shots=4)
    text_off = prose_review._review_rules_text(photographic=False, max_shots=4)
    assert "prop_duplication" in text_on and "prop_duplication" in text_off
    assert SEEDANCE_PROP_COUNT_RULE in text_on and SEEDANCE_PROP_COUNT_RULE in text_off


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
# review_segment_inline：边写边审，调用方每生成一次草稿后调用一次
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_violation_returns_empty_and_records_clean_outcome(monkeypatch):
    draft = _draft("镜头1：她安静地坐着看向窗外。")

    async def clean(*, episode_id, segment_no, draft, previous_draft, photographic, max_shots):
        return []

    monkeypatch.setattr(prose_review, "_review_segment", clean)
    outcomes: list[dict] = []
    revision_text = await prose_review.review_segment_inline(
        draft, previous_draft=None, episode_id="ep1", segment_no=1, photographic=False, max_shots=4,
        attempt=0, enabled=True, outcomes=outcomes,
    )
    assert revision_text == ""
    assert draft.degraded_capabilities == []
    assert outcomes == [{"segment_no": 1, "rewritten": False, "remaining": 0}]


@pytest.mark.asyncio
async def test_violation_on_first_attempt_returns_revision_notes_without_degrading(monkeypatch):
    draft = _draft("镜头1：A动作一，动作二，动作三。")

    async def violating(*, episode_id, segment_no, draft, previous_draft, photographic, max_shots):
        return [prose_review.ProseViolation(kind="action_density", shot_label="镜头1", quote="动作一，动作二，动作三", fix="拆镜")]

    monkeypatch.setattr(prose_review, "_review_segment", violating)
    outcomes: list[dict] = []
    revision_text = await prose_review.review_segment_inline(
        draft, previous_draft=None, episode_id="ep1", segment_no=1, photographic=False, max_shots=4,
        attempt=0, enabled=True, outcomes=outcomes,
    )
    assert "action_density" in revision_text and "拆镜" in revision_text
    assert draft.degraded_capabilities == [], "还没到最后一次尝试，不写 degraded_capabilities"
    assert outcomes == [], "还会再来一轮，这次不是终态，不记汇总"


@pytest.mark.asyncio
async def test_violation_on_last_attempt_writes_degraded_and_stops(monkeypatch):
    draft = _draft("镜头1：依然是动作一，动作二，动作三。")

    async def always_violating(*, episode_id, segment_no, draft, previous_draft, photographic, max_shots):
        return [prose_review.ProseViolation(kind="action_density", shot_label="镜头1", quote="动作一，动作二，动作三", fix="拆镜")]

    monkeypatch.setattr(prose_review, "_review_segment", always_violating)
    outcomes: list[dict] = []
    revision_text = await prose_review.review_segment_inline(
        draft, previous_draft=None, episode_id="ep1", segment_no=1, photographic=False, max_shots=4,
        attempt=prose_review.INLINE_MAX_ATTEMPTS - 1, enabled=True, outcomes=outcomes,
    )
    assert revision_text == "", "没有下一轮了，返回空串收尾，不阻断"
    assert any("[STORYBOARD_PROSE_REVIEW_REMAINING][未拦截]" in note for note in draft.degraded_capabilities)
    assert outcomes == [{"segment_no": 1, "rewritten": True, "remaining": 1}]


@pytest.mark.asyncio
async def test_review_call_failure_does_not_rewrite_or_block(monkeypatch, caplog):
    """复核调用失败（供应商错误）时 _review_segment 记
    [STORYBOARD_PROSE_REVIEW_FAILED] 并返回 None（「未完成复核」，区别于
    「复核成功、没有违规」的空列表）；生成主链路的 review_segment_inline 把
    None 按「没有违规」处理，不重写不阻断。"""
    draft = _draft("镜头1：她没有往里走。")

    async def failing(*args, **kwargs):
        raise RuntimeError("供应商 500")

    monkeypatch.setattr(model_gateway, "chat_structured", failing)
    with caplog.at_level(logging.WARNING):
        revision_text = await prose_review.review_segment_inline(
            draft, previous_draft=None, episode_id="ep1", segment_no=1, photographic=False, max_shots=4,
            attempt=0, enabled=True,
        )
    assert revision_text == ""
    assert draft.degraded_capabilities == []
    assert "[STORYBOARD_PROSE_REVIEW_FAILED]" in caplog.text


@pytest.mark.asyncio
async def test_disabled_skips_review_call_entirely(monkeypatch):
    draft = _draft("镜头1：她没有往里走。")

    async def _must_not_be_called(*args, **kwargs):
        raise AssertionError("enabled=False 时不应发起任何复核调用")

    monkeypatch.setattr(prose_review, "_review_segment", _must_not_be_called)
    revision_text = await prose_review.review_segment_inline(
        draft, previous_draft=None, episode_id="ep1", segment_no=1, photographic=False, max_shots=4,
        attempt=0, enabled=False,
    )
    assert revision_text == ""
    assert draft.degraded_capabilities == []


@pytest.mark.asyncio
async def test_review_segment_inline_calls_chat_structured_when_enabled(monkeypatch):
    """复核确实走 model_gateway.chat_structured（而不是绕过它），且 kind 来自
    schema 允许的同一份取值——两侧对齐，不靠本文件自己猜一份。"""
    draft = _draft("镜头1：她没有往里走。")
    calls = []

    async def fake_chat_structured(*args, **kwargs):
        calls.append(kwargs)
        model_type = kwargs["model_type"]
        return model_type(violations=[{"kind": "negated_action", "shot_label": "镜头1", "quote": "她没有往里走", "fix": "改成正面写法"}])

    monkeypatch.setattr(model_gateway, "chat_structured", fake_chat_structured)
    revision_text = await prose_review.review_segment_inline(
        draft, previous_draft=None, episode_id="ep1", segment_no=1, photographic=False, max_shots=4,
        attempt=0, enabled=True,
    )
    assert len(calls) == 1
    assert calls[0]["call_meta"]["stage_key"] == "storyboard_prose_review"
    assert "negated_action" in revision_text


# ---------------------------------------------------------------------------
# log_review_summary
# ---------------------------------------------------------------------------

def test_log_review_summary_prints_aggregate_counts(caplog):
    outcomes = [
        {"segment_no": 1, "rewritten": True, "remaining": 0},
        {"segment_no": 2, "rewritten": False, "remaining": 0},
        {"segment_no": 3, "rewritten": True, "remaining": 2},
    ]
    with caplog.at_level(logging.INFO):
        prose_review.log_review_summary(outcomes, episode_id="ep1", enabled=True)
    assert "[STORYBOARD_PROSE_REVIEW_SUMMARY]" in caplog.text
    assert "episode=ep1" in caplog.text
    assert "segments=3" in caplog.text and "rewritten=2" in caplog.text and "remaining_violations=2" in caplog.text


def test_log_review_summary_silent_when_disabled_or_empty(caplog):
    with caplog.at_level(logging.INFO):
        prose_review.log_review_summary([{"segment_no": 1, "rewritten": False, "remaining": 0}], episode_id="ep1", enabled=False)
        prose_review.log_review_summary([], episode_id="ep1", enabled=True)
    assert "[STORYBOARD_PROSE_REVIEW_SUMMARY]" not in caplog.text
