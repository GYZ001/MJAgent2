"""provider_calls 落库前的 base64 压缩与幂等漂移核对（2026-09-06 第 13 轮写锁风暴根因）。"""
from __future__ import annotations

from app.observability.provider_call_payload import compact_exact_request, compact_provider_payload

_PNG_A = "data:image/png;base64," + "QUJD" * 2000
_PNG_B = "data:image/png;base64," + "WFla" * 2000


def _request(url: str, prompt: str = "镜头1：固定全景") -> dict:
    return {"model": "m", "content": [{"type": "text", "text": prompt}, {"type": "image_url", "image_url": {"url": url}}]}


def test_base64_becomes_length_and_digest_placeholder() -> None:
    compacted = compact_exact_request(_request(_PNG_A))
    url = compacted["content"][1]["image_url"]["url"]
    assert url.startswith("data:image/png;base64,[omitted 8022 chars sha256:") and len(url) < 80
    assert compacted["content"][0]["text"] == "镜头1：固定全景"


def test_compaction_is_idempotent_and_distinguishes_images() -> None:
    once = compact_exact_request(_request(_PNG_A))
    assert compact_exact_request(once) == once
    assert compact_exact_request(_request(_PNG_B)) != once  # 换了一张图仍能被漂移核对识别
    assert compact_exact_request(_request(_PNG_A, "镜头2")) != once


def test_legacy_exact_row_compares_equal_to_current_payload() -> None:
    legacy_saved = _request(_PNG_A)  # 改版前按原样落库的历史行
    assert compact_exact_request(legacy_saved) == compact_exact_request(_request(_PNG_A))


def test_exact_form_keeps_long_text_while_log_form_truncates() -> None:
    long_text = "甲" * 130_000
    assert compact_exact_request({"t": long_text})["t"] == long_text
    assert "[truncated 10000 chars]" in compact_provider_payload({"t": long_text})["t"]
