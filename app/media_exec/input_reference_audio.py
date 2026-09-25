"""参考图模式下，说话角色声音参考的冻结编排（2026-09-24 角色固定音色 U3）。

由 ``app.media_exec.run_job`` 在 ``_prepare_planned_mode_inputs`` 返回之后调用
（不是 ``.input_reference``——那个文件与 ``.run_job`` 都在行数棘轮基线零余量，
这次改动索性放到独立新文件，两边各只需一行调用/一行回写）。

每个版本只决定一次声音清单：meta 里还没有 ``reference_audios``、且这个版本
还没向供应商发过创建请求时才解析并冻结；此后重试一律原样复用（不重新解析、
不改写 meta、不再追加声音说明），避免请求形状变化触发 Seedance
``idempotency_request_mismatch``。

判据**不看**参考图走的是哪条冻结路径：分镜台 2.x 的参考图在入队时就已从素材库
拼好，运行时走快路径，只写 ``reference_manifest_frozen``、不写
``video_input_manifest_frozen``——2026-09-24 线上测试集 9 段因旧判据一段都没
带上声音。
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
    conn, job, version, shot, meta: dict[str, Any], prompt_text: str, *, operation_id: str,
) -> str:
    """``operation_id``：本版本的供应商创建操作号（``run_job`` 里的
    ``provider_operation_id``）。meta 已有 ``reference_audios``（决定过，含决定为
    空）或该操作号已有创建请求落账（发过）时原样返回；否则解析、写 meta、把声音
    说明追加进 prompt_text 并落库——返回值必须由调用方回写自己的 ``prompt_text``
    局部变量，否则实际提交的仍是没有声音说明的旧版本。
    """
    if conn is None:
        # 连接必须由调用方显式给出（CLAUDE.md「Ownership Must Be Explicit」）：run_job 传的是
        # 任务自己的 conn，不是给续租心跳子任务用、生产上恒为 None 的 operation_conn。
        raise TypeError("freeze_segment_reference_audios 需要调用方显式传入数据库连接")
    if "reference_audios" in meta:
        return prompt_text
    if hiagent._latest_provider_operation_request("video_create", operation_id) is not None:
        return prompt_text
    if not reference_audio_enabled():
        return prompt_text
    segment = getattr(_load_shot_model(shot), "storyboard_pack_segment", None)
    if segment is None:
        return prompt_text
    provider = hiagent.active_provider("video")
    # 快照缺失/过期时会自建并自行提交：用它自己的连接语义，不在调用方连接上留下未提交的写入。
    capability = current_capability_snapshot(
        provider=provider, model=hiagent.active_model("video", provider), conn=None,
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
    return prompt_text


__all__ = ["freeze_segment_reference_audios"]
