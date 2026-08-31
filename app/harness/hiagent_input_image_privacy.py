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
"""
from __future__ import annotations

import json

INPUT_IMAGE_PRIVACY_CODE = "InputImageSensitiveContentDetected.PrivacyInformation"
INPUT_IMAGE_PRIVACY_REJECTED_KIND = "input_image_privacy_rejected"


def is_input_image_privacy_rejection(body: str) -> bool:
    """True iff the raw HTTP error body carries the provider's own
    deterministic real-person-privacy rejection code for an input image.

    Same input always yields the same result (pure string/JSON parsing, no
    I/O) -- retrying the identical request against the identical provider
    policy cannot flip this, which is exactly why callers treat a match as
    an externally-terminal, non-retryable rejection rather than a transient
    fault.
    """
    try:
        payload = json.loads(body)
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict):
        return False
    error_payload = payload.get("error")
    code = error_payload.get("code") if isinstance(error_payload, dict) else None
    return str(code or "") == INPUT_IMAGE_PRIVACY_CODE
