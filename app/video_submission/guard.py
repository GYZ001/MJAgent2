"""图片/视频/音频输入角色的本地合法性校验（纯判定函数，无 app 内运行时依赖）。

从 ``app.hiagent.create_video_task`` 搬出（原逻辑逐字保留，见
``assert_image_video_roles_legal``），2026-09-24 新增 ``assert_audio_roles_legal``
支持 Seedance 2.0 的 ``reference_audio`` 角色。L1，同 ``app.chat_response_probe``
的先例——同样是从 hiagent 搬出的纯判定函数。

本模块不认识 ``ProviderError``（那是 ``app.hiagent`` L3 的类型，本模块 L1
不能反向依赖）：失败一律抛 :class:`SubmissionRoleError`，调用方捕获后自行
包装成面向供应商调用的错误类型。
"""
from __future__ import annotations

import base64
import binascii

VALID_IMAGE_ROLES = frozenset({"first_frame", "last_frame", "reference_image"})
AUDIO_ROLE = "reference_audio"


class SubmissionRoleError(ValueError):
    """角色/形态非法；调用方负责翻译成面向用户的错误。"""


def assert_image_video_roles_legal(image_roles: list[str], video_roles: list[str]) -> None:
    """图片与 ``reference_video`` 角色互斥校验（逐字搬自 ``app.hiagent``）。"""
    if any(role not in VALID_IMAGE_ROLES for role in image_roles):
        raise SubmissionRoleError(f"非法视频图片输入角色：{image_roles}")
    if any(role != "reference_video" for role in video_roles):
        raise SubmissionRoleError(f"非法视频输入角色：{video_roles}")
    if video_roles and image_roles:
        raise SubmissionRoleError("reference_video 不能与 reference_image/first_frame/last_frame 混用")
    if "reference_image" in image_roles and (
        "first_frame" in image_roles or "last_frame" in image_roles
    ):
        raise SubmissionRoleError("reference_image 不能与 first_frame/last_frame 混用")
    if "last_frame" in image_roles and "first_frame" not in image_roles:
        raise SubmissionRoleError("last_frame 不能脱离 first_frame 单独提交")


#: 读不出 WAV 头时的兜底格式，即参考片段的固定输出格式
#: （``app.voice.clipping.cut_and_normalize_clip``：24kHz、单声道、16-bit PCM）。
_CLIP_SAMPLE_RATE_HZ = 24_000
_CLIP_BYTES_PER_SAMPLE = 2
_WAV_HEADER_BYTES = 44


def _wav_byte_rate(encoded: str) -> int:
    """只解码 base64 开头 64 个字符（48 字节）读 WAV 头的字节率（fmt 块第 28-31
    字节）；不是标准 WAV 头时返回 0，由调用方兜底。"""
    try:
        head = base64.b64decode(encoded[:64])
    except (ValueError, binascii.Error):
        return 0
    if len(head) < 32 or head[:4] != b"RIFF" or head[8:12] != b"WAVE" or head[12:16] != b"fmt ":
        return 0
    return int.from_bytes(head[28:32], "little")


def _clip_duration_estimate_s(data_url: str) -> float:
    """按 WAV 头里的字节率换算时长，不解码整段音频；片段格式将来变了也不会
    误判（写死 24kHz 的旧估算遇到 48kHz 片段会把时长算成两倍而误拒）。"""
    _, _, encoded = data_url.partition(";base64,")
    if not encoded:
        return 0.0
    payload_bytes = max(0.0, len(encoded) * 3 / 4 - _WAV_HEADER_BYTES)
    byte_rate = _wav_byte_rate(encoded) or _CLIP_SAMPLE_RATE_HZ * _CLIP_BYTES_PER_SAMPLE
    return payload_bytes / byte_rate


def assert_audio_roles_legal(
    audio_urls: list[tuple[str, str]],
    *,
    image_roles: list[str],
    video_roles: list[str],
    max_count: int,
    max_total_duration_s: float,
) -> None:
    """``reference_audio`` 本地校验：角色名合法、不与首尾帧同用、必须有图或
    视频、段数与总时长不超过能力快照给出的上限（上限由调用方传入，本函数
    不写死 3、15 这类具体数字）。空列表直接放行，不产生任何限制。
    """
    if not audio_urls:
        return
    audio_roles = [role for _url, role in audio_urls]
    if any(role != AUDIO_ROLE for role in audio_roles):
        raise SubmissionRoleError(f"非法视频音频输入角色：{audio_roles}")
    if "first_frame" in image_roles or "last_frame" in image_roles:
        raise SubmissionRoleError("reference_audio 不能与 first_frame/last_frame 混用")
    if not image_roles and not video_roles:
        raise SubmissionRoleError("reference_audio 不能单独提交，至少需要一张参考图或一段参考视频")
    if len(audio_urls) > max_count:
        raise SubmissionRoleError(f"参考音频段数 {len(audio_urls)} 超过供应商上限 {max_count}")
    total = sum(_clip_duration_estimate_s(url) for url, _role in audio_urls)
    if total > max_total_duration_s:
        raise SubmissionRoleError(f"参考音频总时长约 {total:.1f}s 超过供应商上限 {max_total_duration_s}s")
