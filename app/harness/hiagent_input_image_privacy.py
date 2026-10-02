"""Structural detection of the video provider's deterministic real-person-
privacy rejection code, split out of app/hiagent.py purely for file-size
budget (same reasoning as app/harness/hiagent_stream_evidence.py's module
docstring: that file is pinned exactly at its FILE_CONVENTIONS.toml
line-count baseline and CLAUDE.md's ratchet forbids raising it -- adding
this logic inline would have pushed it over).

Real-world incident (2026-08-31, 《我欲封天》EP1-EP10 second pass): 8 of 10
episodes under the photographic visual style ("真人摄影风"/"精修真人风", see
``app.visual_styles.VisualStylePreset.photographic``) were rejected by the
video provider at HTTP 400 with the Ark/Seedance-native error body
``{"error": {"code": "InputImageSensitiveContentDetected.PrivacyInformation",
"message": "The request failed because the input image 'content[2]' may
contain real person"}}`` -- structurally distinct from HiAgent's own
gateway-wrapped ``error.failure`` shape that
``app.hiagent.provider_failure_from_http_payload`` already handles (that one
requires an ``error.failure`` *object* with its own ``category``/``kind``;
this is a plain ``error.code`` *string* one layer up). Before this module
existed, that body fell through ``_classify_http_error``'s generic fallback
into TECHNICAL/``provider_rejected``/MANUAL_REVIEW with the raw English body
quoted verbatim as the user-facing message -- the same "结果不确定，可重试"
failure mode CLAUDE.md already retired for HiAgent's SSE stream refusals
(see hiagent_stream_evidence.py's module docstring): retrying the same input
image against the same provider policy cannot succeed, so surfacing it as
retryable, or silent, is a lie.

Detection reads only the provider's own structured ``error.code`` field --
never the ``message`` prose (CLAUDE.md「不要匹配错误消息的自然语言」: an
existing regression test, ``tests/test_provider_call_lifecycle.py::
test_http_400_rejection_is_typed_without_parsing_body_words``, already pins
that a *different* code, ``InputTextSensitiveContentDetected``, must stay
generic/``provider_rejected`` -- this module must not widen that) and never
a visual-style keyword/name blacklist (CLAUDE.md「禁止黑名单与枚举穷举」):
the code is provider-issued taxonomy, already a discrete, closed value
before this function ever looks at it. Only the exact
``.PrivacyInformation`` subtype is matched -- sibling
``InputImageSensitiveContentDetected.*`` subtypes (e.g. violence/other
categories) are a different judgment by the provider and are deliberately
left unclassified rather than guessed to also be about real-person privacy.

Kept dependency-free of ``app.hiagent`` (only plain values, no
``ProviderError``/``ProviderFailure`` construction) so it cannot form an
import cycle with it -- callers turn ``INPUT_IMAGE_PRIVACY_REJECTED_KIND``
into a typed ``ProviderFailure.model_rejection(...)`` themselves.

2026-10-02（ERR-20261002-de0b34，真实生产失败）: the **poll** path hits the
same rejection but in a different envelope than the HTTP-400-body shape this
module was written against. ``app.seedance.SeedanceAdapter.poll_video_task``
already unwraps one layer (``result["error"] = error_obj.get("message", "")``
-- see its module), so by the time detection runs here the text is either
the flat ``{"message": ..., "type": ..., "code": ...}`` object with no
``error`` wrapper at all, or that object prefixed with HiAgent's own
``"Error code: 400 - "`` banner. Both are read by
``app.harness.hiagent_input_image_rejection._embedded_error_object``
(already written to peel exactly these two shapes for the sibling
``rejected_input_image_index`` detector), so detection here now delegates to
it instead of re-parsing with a bare ``json.loads`` that only understood the
create-stage ``{"error": {...}}`` shape. One parser, both call sites --
not a second implementation that could drift out of sync.
"""
from __future__ import annotations

from app.harness.hiagent_input_image_rejection import (
    _embedded_error_object, rejected_reference_labels,
)
from app.visual_styles import VISUAL_STYLE_PRESETS

INPUT_IMAGE_PRIVACY_CODE = "InputImageSensitiveContentDetected.PrivacyInformation"
INPUT_IMAGE_PRIVACY_REJECTED_KIND = "input_image_privacy_rejected"


def is_input_image_privacy_rejection(body: str) -> bool:
    """True iff the raw error body/text carries the provider's own
    deterministic real-person-privacy rejection code for an input image, in
    any of the three shapes seen in production: the create-stage
    ``{"error": {"code": ..., ...}}`` HTTP body, the flat poll-stage
    ``{"message": ..., "type": ..., "code": ...}`` object, or that object
    prefixed with ``"Error code: 400 - "``.

    Same input always yields the same result (pure string/JSON parsing, no
    I/O) -- retrying the identical request against the identical provider
    policy cannot flip this, which is exactly why callers treat a match as
    an externally-terminal, non-retryable rejection rather than a transient
    fault.
    """
    payload = _embedded_error_object(body)
    if payload is None:
        return False
    return str(payload.get("code") or "") == INPUT_IMAGE_PRIVACY_CODE


def input_image_privacy_rejection_guidance(provider_text: str, labels: list[dict]) -> str:
    """把真人隐私拒收翻成面向用户的中文落地文案：逐字转述供应商原文、点名被判
    疑似真人的参考图（拿不到标签时如实说明，不编造）、声明系统已停止自动重
    试、再按被拒图的类型给出路（CLAUDE.md「拦住用户时必须给出路」）。

    ``provider_text`` 是调用方已经取好的 ``(exc.raw or "").strip()``（创建阶段
    是整段 HTTP 400 body，轮询阶段是 ``error.message`` 里嵌的那段结构化文本）；
    ``labels`` 是本镜 ``image_inputs._seedance_image_input_labels``，可能为空
    （旧数据、或这次失败发生在还没写入标签的阶段）。
    """
    rejected = rejected_reference_labels(provider_text, labels)
    quote = f"供应商原文：{provider_text}。" if provider_text else ""
    if rejected:
        named = "、".join(
            f"「{item.get('label') or item.get('entity_name') or '未标注参考图'}」"
            for item in rejected
        )
        pointer = f"系统比对本次任务的输入参数，判定疑似真人的是：{named}。"
    else:
        pointer = "供应商未指明是哪一张输入图，系统也无法对应到具体参考图（可能是旧数据或下标越界）。"
    character_names = [
        str(item.get("entity_name") or "").strip()
        for item in rejected
        if str(item.get("type") or "") == "character" and str(item.get("entity_name") or "").strip()
    ]
    non_photographic = "、".join(preset.name for preset in VISUAL_STYLE_PRESETS if not preset.photographic)
    if character_names:
        who = "、".join(f"「{name}」" for name in character_names)
        exit_path = (
            f"请到人物谱为{who}重新生成定妆照（换一张全身照，脸部占比小通常能通过供应商判定）；"
            f"或到项目设置改用非真人画风（{non_photographic}）后重新生成定妆照与本镜。"
        )
    else:
        exit_path = (
            f"请到项目设置改用非真人画风（{non_photographic}）后重新生成定妆照与本镜；"
            "若需继续保留当前摄影类画风，可仅保留图片产出、不生成视频。"
        )
    return (
        f"视频供应商判定本镜输入图疑似真人肖像，按隐私政策拒收（供应商错误码 {INPUT_IMAGE_PRIVACY_CODE}）。"
        f"{quote}{pointer}同一输入对同一供应商政策必然复现，系统已停止对本镜的自动付费重试。{exit_path}"
    )
