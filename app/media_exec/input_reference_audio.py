"""参考图模式下，说话角色声音参考的冻结编排（2026-09-24 角色固定音色 U3）。

由 ``app.media_exec.run_job`` 在 ``_prepare_planned_mode_inputs`` 返回之后调用
（不是 ``.input_reference``——那个文件与 ``.run_job`` 都在行数棘轮基线零余量，
这次改动索性放到独立新文件，两边各只需一行调用/一行回写）。

只在本次调用刚完成一次全新的参考图冻结时才计算音频清单：已经冻结过参考
图的版本原样跳过，不重新解析、不改写 meta，也不再往 prompt_text 追加声音
说明——这是防止在途/重试任务因为设置中途被打开而悄悄改变请求形状（触发
Seedance ``idempotency_request_mismatch``）的唯一判据，见
``app.voice.segment_refs`` 模块文档与 U3 派单第 4 条。
"""
from __future__ import annotations

import json
from typing import Any

from app import hiagent
from app.video_modes.seedance_reference_notes import append_audio_reference_note
from app.video_plan.capability_snapshot import current_capability_snapshot
from app.voice.segment_refs import (
    configured_max_speakers,
    reference_audio_enabled,
    resolve_segment_reference_audios,
)

from .enqueue import _load_shot_model
from .job_state import _set_version


def freeze_segment_reference_audios(
    conn, job, version, shot, meta: dict[str, Any], prompt_text: str, *, already_frozen: bool,
) -> str:
    """``already_frozen``：调用方在调用 ``_prepare_planned_mode_inputs`` 之前
    捕获的 ``meta.get("video_input_manifest_frozen")``——为真表示这个版本此前
    已经成功冻结过一次参考图（无论这次走的是快路复用还是重新命中同样结果），
    必须原样跳过，绝不给老版本"补上"音频键，避免同一版本重试时请求形状发生
    变化。只有本次调用让它从"未冻结"变为"已冻结"才计算并写入，并把声音说明
    追加进 prompt_text——返回值必须由调用方回写自己的 ``prompt_text`` 局部
    变量，否则实际提交的仍是没有声音说明的旧版本。
    """
    if already_frozen or not meta.get("video_input_manifest_frozen"):
        return prompt_text
    if not reference_audio_enabled():
        return prompt_text
    segment = getattr(_load_shot_model(shot), "storyboard_pack_segment", None)
    if segment is None:
        return prompt_text
    provider = hiagent.active_provider("video")
    capability = current_capability_snapshot(
        provider=provider, model=hiagent.active_model("video", provider), conn=conn,
    )
    refs, skips = resolve_segment_reference_audios(
        conn=conn, project_id=job["project_id"], segment=segment,
        has_visual_reference=bool(meta.get("reference_images")),
        max_speakers=configured_max_speakers(),
        supports_reference_audio=capability.supports_reference_audio,
        max_reference_audios=capability.max_reference_audios,
        max_reference_audio_total_s=capability.max_reference_audio_total_s,
    )
    meta["reference_audios"] = refs
    meta["reference_audio_skips"] = skips
    prompt_text = append_audio_reference_note(
        prompt_text, refs, aspect_ratio=str(meta.get("aspect_ratio") or "9:16"),
    )
    _set_version(
        version["id"], image_inputs=json.dumps(meta, ensure_ascii=False), prompt_text=prompt_text,
    )
    conn.commit()
    return prompt_text


__all__ = ["freeze_segment_reference_audios"]
