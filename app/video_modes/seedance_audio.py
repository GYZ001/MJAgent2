"""Seedance 参考音频输入组装：读冻结清单、核对 sha256、拼 data URL。

由 ``app.media_exec.run_job`` 在提交前调用（与 ``build_seedance_video_inputs``
同一层次的入口），只读 ``meta["reference_audios"]``（冻结阶段写入，见
``app.media_exec.input_reference_audio``），不重新解析、不重新选声音——选择
规则只在冻结那一刻跑一次（``app.voice.segment_refs.resolve_segment_reference_
audios``），这里只负责把冻结结果转成供应商能吃的 data URL，并核对片段文件
此刻是否仍与冻结时记录的哈希一致（CLAUDE.md「核对 sha256，不一致就明确
失败，不许偷换成别的声音」）。
"""
from __future__ import annotations

import base64
import hashlib
from pathlib import Path
from typing import Any

from app.hiagent import ProviderError

AUDIO_MIME = "audio/wav"
# 这些失败都发生在组装请求阶段，请求没有发出、不会扣费；按「未发出」标记，
# 与 hiagent.create_video_task 的 reject_before_create 同一口径。
_NOT_SENT = {"delivery_state": "not_sent", "replay_safe": True, "create_not_accepted": True}


def _audio_data_url(clip_path: str, expected_sha256: str, *, character_name: str) -> str:
    label = character_name or "（未知角色）"
    path = Path(clip_path) if clip_path else None
    if path is None or not path.is_file():
        raise ProviderError(f"角色「{label}」的声音参考片段文件不存在：{clip_path or '（路径为空）'}", **_NOT_SENT)
    if not expected_sha256:
        raise ProviderError(f"角色「{label}」的声音参考片段缺少冻结哈希记录，拒绝提交", **_NOT_SENT)
    raw = path.read_bytes()
    actual = hashlib.sha256(raw).hexdigest()
    if actual != expected_sha256:
        raise ProviderError(f"角色「{label}」的声音参考片段哈希与冻结记录不一致，文件可能已被替换，拒绝提交", **_NOT_SENT)
    return f"data:{AUDIO_MIME};base64,{base64.b64encode(raw).decode('ascii')}"


def build_seedance_audio_inputs(meta: dict[str, Any]) -> list[tuple[str, str]]:
    """冻结清单为空（含开关关闭、老版本没有这个键）时返回空列表，不报错；
    每一项都必须能核对哈希，任何一项不通过都整体失败（不静默跳过坏项）。
    """
    refs = meta.get("reference_audios") or []
    out: list[tuple[str, str]] = []
    for item in refs:
        if not isinstance(item, dict):
            continue
        url = _audio_data_url(
            str(item.get("clip_path") or ""),
            str(item.get("clip_sha256") or ""),
            character_name=str(item.get("character_name") or ""),
        )
        out.append((url, "reference_audio"))
    return out
