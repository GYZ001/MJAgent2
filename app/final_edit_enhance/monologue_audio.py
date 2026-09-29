"""主角内心独白：按角色现有固定音色合成计划里核验过的原文引用，落盘、探测
真实时长，超出静默窗口的直接跳过（不裁剪、不兜底）——裁剪会把一句完整的话
截断成语义不通的残句，比"这句话这次没有配上音频"更糟。

角色音色只读现有 ``app.voice.store`` 数据结构（``character_voices`` 表的
``current`` 行），不新造第二份音色查找逻辑；不支持"用已有音色合成任意文本"
的供应商（当前只有千问）明确跳过并写明原因。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.final_edit_enhance.ffutil import probe_duration_s, run_ffmpeg
from app.final_edit_enhance.plan_generate import ResolvedMonologueLine
from app.media_pipeline.delivery_encode import encode_timeout_s
from app.media_pipeline.loudness import FINAL_AUDIO_RATE
from app.voice import store
from app.voice.providers import dispatch
from app.voice.providers.base import VoiceProviderError, detect_audio_format

ANCHOR_KEY_DEFAULT = ""
_START_LEAD_S = 0.3


@dataclass(frozen=True)
class MonologueAudioItem:
    start_s: float
    duration_s: float
    text: str
    character_name: str
    audio_path: Path


def _placed_start_s(line: ResolvedMonologueLine, duration_s: float) -> float:
    lead = min(_START_LEAD_S, max(0.0, (line.end_s - line.start_s) - duration_s))
    return line.start_s + lead


async def _synthesize_one(
    conn: Any, project_id: str, line: ResolvedMonologueLine, index: int, work_dir: Path,
) -> tuple[MonologueAudioItem | None, dict[str, Any] | None]:
    item_summary = {"character_name": line.character_name, "text": line.text, "window_s": [line.start_s, line.end_s]}
    row = store.current_for(conn, project_id, line.character_name, ANCHOR_KEY_DEFAULT)
    if row is None or not str(row["model_id"]) or not str(row["provider_voice_id"]):
        return None, {"item": item_summary, "reason": f"角色「{line.character_name}」尚未设置固定音色"}
    try:
        result = await dispatch.synthesize_speech_for_voice(
            str(row["model_id"]), line.text, str(row["provider_voice_id"]),
            call_meta={"project_id": project_id, "purpose": "monologue"},
        )
    except VoiceProviderError as exc:
        return None, {"item": item_summary, "reason": f"语音合成失败：{exc}"}
    ext = detect_audio_format(result.audio)
    audio_path = work_dir / f"monologue-{index}.{ext if ext != 'bin' else 'wav'}"
    audio_path.write_bytes(result.audio)
    try:
        duration_s = probe_duration_s(str(audio_path))
    except ValueError as exc:
        return None, {"item": item_summary, "reason": f"合成音频无法探测时长：{exc}"}
    window_span = line.end_s - line.start_s
    if duration_s > window_span + 1e-6:
        return None, {
            "item": item_summary,
            "reason": f"合成语音时长 {duration_s:.1f}s 超出静默窗口 {window_span:.1f}s，不裁剪、直接跳过",
        }
    return MonologueAudioItem(
        start_s=_placed_start_s(line, duration_s), duration_s=duration_s,
        text=line.text, character_name=line.character_name, audio_path=audio_path,
    ), None


async def synthesize_monologue_lines(
    conn: Any, project_id: str, lines: tuple[ResolvedMonologueLine, ...], work_dir: Path,
) -> tuple[list[MonologueAudioItem], list[dict[str, Any]]]:
    applied: list[MonologueAudioItem] = []
    skipped: list[dict[str, Any]] = []
    for index, line in enumerate(lines):
        item, reason = await _synthesize_one(conn, project_id, line, index, work_dir)
        if item is not None:
            applied.append(item)
        elif reason is not None:
            skipped.append(reason)
    return applied, skipped


def build_monologue_track(items: list[MonologueAudioItem], total_duration_s: float, work_dir: Path) -> Path | None:
    """把各独白音频按 ``start_s`` 摆进一条与全片等长的音轨；没有可用条目返回 None。"""
    if not items:
        return None
    cmd = ["ffmpeg", "-y", "-loglevel", "error"]
    for item in items:
        cmd += ["-i", str(item.audio_path)]
    labels = []
    filters = []
    for i, item in enumerate(items):
        delay_ms = max(0, round(item.start_s * 1000))
        filters.append(f"[{i}:a]aresample={FINAL_AUDIO_RATE},adelay={delay_ms}|{delay_ms}[m{i}]")
        labels.append(f"[m{i}]")
    filters.append(f"{''.join(labels)}amix=inputs={len(items)}:duration=longest:normalize=0[mixed]")
    # amix 的 duration=longest 只取"最长输入"的时长，短于全片时长时要靠 apad
    # 补齐——atrim 只会裁短不会补长，两者顺序必须是 apad 在前。
    filters.append(f"[mixed]apad=whole_dur={total_duration_s:.6f},atrim=duration={total_duration_s:.6f}[out]")
    out_path = work_dir / "monologue_track.wav"
    cmd += ["-filter_complex", ";".join(filters), "-map", "[out]", str(out_path)]
    run_ffmpeg(cmd, timeout=encode_timeout_s(total_duration_s), context="独白音轨拼接")
    return out_path
