"""门禁裁定的纯文本回退必须能走到提交：``build_seedance_image_inputs`` 对
``reference_mode_text_only_fallback`` 返回空输入，而不是抛「禁止纯文本提交」。

373a7c7 只加了门禁侧（reference_pool_gate）的回退，打包侧没改，结果纯群演镜
在门禁标成 VIDEO_READY 后每次提交都被打回 waiting_human——2026-09-03 三国 ep1
第 1 镜重试 5 次全部如此。
"""
from __future__ import annotations

import pytest

from app import video_modes
from app.hiagent import ProviderError
from app.media_exec.job_state import _video_image_inputs_from_meta


def _text_only_meta() -> dict:
    return {
        "mode": video_modes.REFERENCE_IMAGE_MODE,
        "reference_images": [],
        "reference_mode_text_only_fallback": True,
        "reference_mode_text_only_reason": "empty_candidate_pool",
    }


def test_gate_approved_text_only_fallback_submits_without_images():
    assert video_modes.build_seedance_image_inputs(_text_only_meta()) == []
    assert _video_image_inputs_from_meta(_text_only_meta()) == []  # 不再升级成 VideoInputRepairRequired


def test_missing_references_without_gate_decision_still_refused():
    meta = _text_only_meta()
    meta.pop("reference_mode_text_only_fallback")
    with pytest.raises(ProviderError, match="禁止纯文本提交"):
        video_modes.build_seedance_image_inputs(meta)


def test_flag_must_be_literal_true_not_truthy_noise():
    meta = _text_only_meta()
    meta["reference_mode_text_only_fallback"] = "yes"
    with pytest.raises(ProviderError, match="禁止纯文本提交"):
        video_modes.build_seedance_image_inputs(meta)
