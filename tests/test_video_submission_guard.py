"""``app.video_submission.guard``：图片/视频/音频输入角色的本地合法性校验。

纯函数单测，不涉及数据库或网络。``assert_image_video_roles_legal`` 的规则
逐字搬自 ``app.hiagent.create_video_task``（见该函数当前 docstring），这里
把移动后的行为钉死；``assert_audio_roles_legal`` 是 U3 新增的三类本地校验：
段数/总时长上限、必须有图或视频、不与 first_frame/last_frame 同用。
"""
from __future__ import annotations

import pytest

from app.video_submission.guard import (
    SubmissionRoleError,
    _clip_duration_estimate_s,
    assert_audio_roles_legal,
    assert_image_video_roles_legal,
)


def _audio_url_for_duration(seconds: float) -> str:
    """构造一个 base64 长度对应约 ``seconds`` 秒（24kHz/16-bit/单声道）的音频
    data URL；内容不是真实 wav 字节，``_clip_duration_estimate_s`` 只按字符串
    长度换算，不解码。"""
    raw_bytes = int(round(seconds * 24_000 * 2)) + 44
    encoded_len = -(-raw_bytes * 4 // 3)
    encoded_len += (4 - encoded_len % 4) % 4
    return f"data:audio/wav;base64,{'A' * encoded_len}"


# --------------------------- assert_image_video_roles_legal ---------------------------

def test_legal_reference_image_only_passes() -> None:
    assert_image_video_roles_legal(["reference_image", "reference_image"], [])


def test_legal_first_last_frame_passes() -> None:
    assert_image_video_roles_legal(["first_frame", "last_frame"], [])


def test_legal_reference_video_only_passes() -> None:
    assert_image_video_roles_legal([], ["reference_video"])


def test_illegal_image_role_rejected() -> None:
    with pytest.raises(SubmissionRoleError, match="非法视频图片输入角色"):
        assert_image_video_roles_legal(["not_a_role"], [])


def test_illegal_video_role_rejected() -> None:
    with pytest.raises(SubmissionRoleError, match="非法视频输入角色"):
        assert_image_video_roles_legal([], ["not_a_role"])


def test_reference_video_cannot_mix_with_image_roles() -> None:
    with pytest.raises(SubmissionRoleError, match="不能与 reference_image/first_frame/last_frame 混用"):
        assert_image_video_roles_legal(["reference_image"], ["reference_video"])


def test_reference_image_cannot_mix_with_first_frame() -> None:
    with pytest.raises(SubmissionRoleError, match="reference_image 不能与 first_frame/last_frame 混用"):
        assert_image_video_roles_legal(["reference_image", "first_frame"], [])


def test_last_frame_requires_first_frame() -> None:
    with pytest.raises(SubmissionRoleError, match="last_frame 不能脱离 first_frame 单独提交"):
        assert_image_video_roles_legal(["last_frame"], [])


# ------------------------------ assert_audio_roles_legal ------------------------------

def test_empty_audio_urls_always_passes() -> None:
    assert_audio_roles_legal(
        [], image_roles=[], video_roles=[], max_count=0, max_total_duration_s=0.0,
    )


def test_legal_audio_with_reference_image_passes() -> None:
    assert_audio_roles_legal(
        [(_audio_url_for_duration(3.0), "reference_audio")],
        image_roles=["reference_image"], video_roles=[],
        max_count=3, max_total_duration_s=15.0,
    )


def test_illegal_audio_role_name_rejected() -> None:
    with pytest.raises(SubmissionRoleError, match="非法视频音频输入角色"):
        assert_audio_roles_legal(
            [("data:audio/wav;base64,AAAA", "wrong_role")],
            image_roles=["reference_image"], video_roles=[],
            max_count=3, max_total_duration_s=15.0,
        )


def test_audio_cannot_mix_with_first_frame() -> None:
    with pytest.raises(SubmissionRoleError, match="不能与 first_frame/last_frame 混用"):
        assert_audio_roles_legal(
            [(_audio_url_for_duration(2.0), "reference_audio")],
            image_roles=["first_frame"], video_roles=[],
            max_count=3, max_total_duration_s=15.0,
        )


def test_audio_cannot_mix_with_last_frame() -> None:
    with pytest.raises(SubmissionRoleError, match="不能与 first_frame/last_frame 混用"):
        assert_audio_roles_legal(
            [(_audio_url_for_duration(2.0), "reference_audio")],
            image_roles=["first_frame", "last_frame"], video_roles=[],
            max_count=3, max_total_duration_s=15.0,
        )


def test_audio_requires_at_least_one_image_or_video() -> None:
    with pytest.raises(SubmissionRoleError, match="不能单独提交"):
        assert_audio_roles_legal(
            [(_audio_url_for_duration(2.0), "reference_audio")],
            image_roles=[], video_roles=[],
            max_count=3, max_total_duration_s=15.0,
        )


def test_audio_with_reference_video_passes() -> None:
    assert_audio_roles_legal(
        [(_audio_url_for_duration(2.0), "reference_audio")],
        image_roles=[], video_roles=["reference_video"],
        max_count=3, max_total_duration_s=15.0,
    )


def test_audio_count_over_cap_rejected() -> None:
    urls = [(_audio_url_for_duration(1.0), "reference_audio") for _ in range(4)]
    with pytest.raises(SubmissionRoleError, match="段数 4 超过供应商上限 3"):
        assert_audio_roles_legal(
            urls, image_roles=["reference_image"], video_roles=[],
            max_count=3, max_total_duration_s=60.0,
        )


def test_audio_total_duration_over_cap_rejected() -> None:
    urls = [(_audio_url_for_duration(5.0), "reference_audio") for _ in range(3)]
    with pytest.raises(SubmissionRoleError, match="总时长约.*超过供应商上限 10.0s"):
        assert_audio_roles_legal(
            urls, image_roles=["reference_image"], video_roles=[],
            max_count=3, max_total_duration_s=10.0,
        )


def test_audio_total_duration_exactly_at_cap_passes() -> None:
    """3 段各 5 秒＝15 秒，等于上限不算超过（"不超过"含等于）。"""
    urls = [(_audio_url_for_duration(5.0), "reference_audio") for _ in range(3)]
    assert_audio_roles_legal(
        urls, image_roles=["reference_image"], video_roles=[],
        max_count=3, max_total_duration_s=15.5,  # 留一点浮点误差余量
    )


def test_clip_duration_estimate_matches_known_wav_size() -> None:
    url = _audio_url_for_duration(4.0)
    assert abs(_clip_duration_estimate_s(url) - 4.0) < 0.01


def test_clip_duration_estimate_handles_url_without_base64_marker() -> None:
    assert _clip_duration_estimate_s("not-a-data-url") == 0.0


def _wav_data_url(seconds: float, sample_rate: int) -> str:
    import base64
    import io
    import wave

    buf = io.BytesIO()
    with wave.open(buf, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(b"\x00\x00" * int(seconds * sample_rate))
    return "data:audio/wav;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def test_duration_estimate_reads_wav_header_not_fixed_24k() -> None:
    """48kHz 片段按 WAV 头的字节率换算，不会被当成 24kHz 算成两倍时长而误拒。"""
    from app.video_submission.guard import _clip_duration_estimate_s

    assert abs(_clip_duration_estimate_s(_wav_data_url(5.0, 48_000)) - 5.0) < 0.05
    assert abs(_clip_duration_estimate_s(_wav_data_url(4.0, 24_000)) - 4.0) < 0.05
    assert _clip_duration_estimate_s("data:audio/wav;base64,") == 0.0

