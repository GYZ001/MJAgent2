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
    is_input_image_privacy_rejection,
)

REAL_PRIVACY_BODY = (
    '{"error":{"code":"InputImageSensitiveContentDetected.PrivacyInformation",'
    '"message":"The request failed because the input image \'content[2]\' may '
    'contain real person","param":"","type":"BadRequest"}}'
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
