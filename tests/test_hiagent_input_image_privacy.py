"""Real incident (2026-08-31): 8 of 10 episodes of a《我欲封天》EP1-EP10
second-pass regression run were rejected by the video provider at HTTP 400
under the photographic visual style ("真人摄影风") with the Ark/Seedance-
native error body ``{"error": {"code":
"InputImageSensitiveContentDetected.PrivacyInformation", "message": "The
request failed because the input image 'content[2]' may contain real
person"}}``. Before ``app.harness.hiagent_input_image_privacy`` existed, that
body fell through ``app.hiagent._classify_http_error``'s generic fallback
into TECHNICAL/``provider_rejected``/MANUAL_REVIEW with the raw English body
quoted verbatim -- retryable-looking, when retrying the same input image
against the same provider policy cannot succeed.

This file covers the pure detector (``is_input_image_privacy_rejection``)
directly: it must key off the provider's structured ``error.code`` field
only, never the English ``message`` prose (CLAUDE.md「不要匹配错误消息的自然
语言」) -- so a body carrying the exact real-world message text but a
different/absent code must NOT match, and a body carrying the exact code
with arbitrary/absent message text must match.
``tests/test_provider_call_lifecycle.py`` covers the ``_classify_http_error``
wiring end-to-end (the resulting ``ProviderFailure`` shape); ``tests/
test_seedance_safety.py`` covers the resulting user-facing guidance text.
"""
from __future__ import annotations

from app.harness.hiagent_input_image_privacy import (
    INPUT_IMAGE_PRIVACY_CODE,
    INPUT_IMAGE_PRIVACY_REJECTED_KIND,
    input_image_privacy_rejection_guidance,
    is_input_image_privacy_rejection,
)

REAL_PRIVACY_BODY = (
    '{"error":{"code":"InputImageSensitiveContentDetected.PrivacyInformation",'
    '"message":"The request failed because the input image \'content[2]\' may '
    'contain real person","param":"","type":"BadRequest"}}'
)

# 真实生产轮询失败原文（2026-10-02，ERR-20261002-de0b34），已经过
# app.seedance.SeedanceAdapter.poll_video_task 剥掉外层 {"error": {...}} 包装
# 之后留在 result["error"]/exc.raw 里的样子：供应商一次点名了两张图。
REAL_POLL_PRIVACY_TEXT = (
    'Error code: 400 - {"message":"The request failed because the input image '
    "'content[2]' 'content[3]' may contain real person. Request id: "
    '0217886459998243462842069ee0828bfbcf630188a27c118fa59",'
    '"type":"BadRequest","code":"InputImageSensitiveContentDetected.PrivacyInformation",'
    '"param":"","request_id":""}'
)


def test_matches_the_real_world_privacy_rejection_body() -> None:
    assert is_input_image_privacy_rejection(REAL_PRIVACY_BODY) is True


def test_matches_on_code_alone_regardless_of_message_text() -> None:
    """结构判据只看 code；message 换成任意无关文字（甚至为空）依然命中。"""
    body = '{"error":{"code":"' + INPUT_IMAGE_PRIVACY_CODE + '","message":""}}'
    assert is_input_image_privacy_rejection(body) is True
    body_unrelated = '{"error":{"code":"' + INPUT_IMAGE_PRIVACY_CODE + '","message":"unrelated noise"}}'
    assert is_input_image_privacy_rejection(body_unrelated) is True


def test_does_not_match_real_message_text_with_a_different_code() -> None:
    """核心防呆：拒绝按英文措辞猜分类。同一条真实 message 文案，只要 code
    不是这个确切值，就不得命中——防止有人把这里悄悄改回关键词匹配。"""
    body = (
        '{"error":{"code":"InputImageSensitiveContentDetected.Violence",'
        '"message":"The request failed because the input image \'content[2]\' '
        'may contain real person"}}'
    )
    assert is_input_image_privacy_rejection(body) is False


def test_does_not_match_sibling_subcodes() -> None:
    """同一 code 家族的其它子类型（供应商对别的问题下的判断）不被当成同一件
    事——禁止按前缀/家族做枚举穷举扩大匹配面。"""
    body = '{"error":{"code":"InputImageSensitiveContentDetected"}}'
    assert is_input_image_privacy_rejection(body) is False


def test_does_not_match_missing_or_malformed_body() -> None:
    assert is_input_image_privacy_rejection("") is False
    assert is_input_image_privacy_rejection("not json") is False
    assert is_input_image_privacy_rejection("[]") is False
    assert is_input_image_privacy_rejection('{"error": "plain string, not an object"}') is False
    assert is_input_image_privacy_rejection("{}") is False


def test_kind_constant_is_a_plain_string_not_a_hiagent_enum_member() -> None:
    """本模块刻意不依赖 app.hiagent（避免与它形成循环 import，见模块文档字符
    串），``INPUT_IMAGE_PRIVACY_REJECTED_KIND`` 只是个普通字符串，调用方
    （app.hiagent._classify_http_error）自己把它包进
    ``ProviderFailure.model_rejection(...)``。"""
    assert INPUT_IMAGE_PRIVACY_REJECTED_KIND == "input_image_privacy_rejected"
    assert isinstance(INPUT_IMAGE_PRIVACY_REJECTED_KIND, str)


def test_matches_poll_stage_error_code_400_prefixed_text() -> None:
    """2026-10-02 ERR-20261002-de0b34：轮询路径给到的文本不是创建阶段那种
    ``{"error": {...}}`` HTTP body，而是 ``app.seedance`` 已经剥过一层外壳后
    剩下的 ``Error code: 400 - {…}`` 前缀文本——这是这次缺陷的根因：轮询从未
    走过这个检测器，只因为它从前只认得创建阶段那一种形态。"""
    assert is_input_image_privacy_rejection(REAL_POLL_PRIVACY_TEXT) is True


def test_matches_flat_object_without_error_wrapper() -> None:
    """同一个供应商错误体剥掉 ``Error code: 400 - `` 前缀、只剩裸 JSON 时同样
    命中——两种形态共用同一个解析（``_embedded_error_object``），不是两份判断。"""
    flat = (
        '{"message":"The request failed because the input image \'content[2]\' '
        'may contain real person","type":"BadRequest","code":"'
        + INPUT_IMAGE_PRIVACY_CODE + '","param":"","request_id":""}'
    )
    assert is_input_image_privacy_rejection(flat) is True


def test_guidance_quotes_provider_text_and_names_rejected_character_refs() -> None:
    """轮询路径的真实场景：供应商点名 content[2]/content[3]，两个下标都对应
    人物参考图——文案必须逐字带上供应商原文、点出具体是哪两张参考图、并给
    「去人物谱重出定妆照」这条人物向的出路，不能是旧版那句不指名的通用建议。"""
    # content[1] 是提示词之后的第一张图（未被点名，充当占位），content[2]/
    # content[3] 落在标签表的下标 1/2——与 ``rejected_reference_labels`` 的
    # "content[N] -> labels[N-1]" 约定对齐。
    labels = [
        {"type": "scene", "entity_name": "占位场景", "label": "场景参考 · 占位场景"},
        {"type": "character", "entity_name": "顾屿", "label": "角色参考 · 顾屿"},
        {"type": "character", "entity_name": "温念", "label": "角色参考 · 温念"},
    ]
    message = input_image_privacy_rejection_guidance(REAL_POLL_PRIVACY_TEXT, labels)

    assert "供应商原文：" in message and REAL_POLL_PRIVACY_TEXT in message
    assert "角色参考 · 顾屿" in message and "角色参考 · 温念" in message
    assert "已停止对本镜的自动付费重试" in message
    assert "人物谱" in message and "顾屿" in message and "温念" in message
    # 不能把用户指向错误的出路：这是人物参考图被拒，不是画面描述或台词问题。
    assert "改台词" not in message and "片段镜头稿" not in message


def test_guidance_is_honest_when_labels_are_unavailable() -> None:
    """标签越界/旧数据取不到标签时：如实说明对不上，不编造一个不存在的参考图
    名字（CLAUDE.md「不得兜底填充」）。"""
    message = input_image_privacy_rejection_guidance(REAL_POLL_PRIVACY_TEXT, [])

    assert "供应商未指明是哪一张输入图" in message or "无法对应到具体参考图" in message
    assert "顾屿" not in message and "温念" not in message


def test_guidance_falls_back_to_generic_style_switch_for_non_character_refs() -> None:
    """被拒的是场景参考图而非人物参考图时，不编造「去人物谱重出定妆照」这种
    文不对题的出路，走现有的「改画风」通用说法。"""
    # 只保留 content[2]（下标 1），去掉 content[3]，让唯一命中的是场景参考图。
    labels = [
        {"type": "character", "entity_name": "占位人物", "label": "角色参考 · 占位人物"},
        {"type": "scene", "entity_name": "城楼", "label": "场景参考 · 城楼"},
    ]
    message = input_image_privacy_rejection_guidance(
        REAL_POLL_PRIVACY_TEXT.replace("'content[3]'", ""), labels,
    )

    assert "场景参考 · 城楼" in message
    assert "人物谱" not in message
    assert "非真人画风" in message
