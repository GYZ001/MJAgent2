"""试听音频内容核验：语音识别转写 + 台词账本对齐阈值判断（复用
``app.subtitles``）。

最佳努力——识别引擎不可用或任何异常都归为「未核验」，不拦生成流程：这里恰恰
是「检查不可用时不能假装通过也不能假装拒绝」，如实记「未核验」，与 CLAUDE.md
「空集合不等于无需检查」是同一枚硬币的两面（这里检查确实跑不了，不是伪装
成"无需检查"）。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.subtitles.align import AsrToken, LineSpec, align_shot
from app.subtitles.engine import AsrEngineError, transcribe_media

MATCH_RATIO_PASS = 0.60
_JOB_KEY = "preview"


@dataclass(frozen=True)
class VoiceCheckResult:
    check_status: str  # "passed" | "failed" | "unchecked"
    check_reason: str
    asr_text: str
    asr_match: float | None


def check_preview_match(audio_path: Path, preview_text: str) -> VoiceCheckResult:
    """把 ``audio_path``（试听音频全量文件，不是裁出的短片段）转写后与
    ``preview_text`` 按拼音同音对齐比较，匹配率 ≥0.60 判通过。"""
    try:
        results = transcribe_media({_JOB_KEY: audio_path})
    except AsrEngineError as exc:
        return VoiceCheckResult("unchecked", f"语音识别引擎不可用，未核验：{exc}", "", None)
    except Exception as exc:  # noqa: BLE001 -- 校验本身绝不能拖垮生成流程，如实标注未核验
        return VoiceCheckResult("unchecked", f"语音识别校验异常，未核验：{exc}", "", None)
    result = results.get(_JOB_KEY)
    if result is None:
        return VoiceCheckResult("unchecked", "语音识别未返回结果，未核验", "", None)
    tokens = [AsrToken(text=text, start_s=start_s) for text, start_s in result.tokens]
    alignment = align_shot([LineSpec(utterance_id=_JOB_KEY, text=preview_text)], tokens)
    line = alignment.lines[0]
    ratio = round(float(line.match_ratio), 4)
    if ratio >= MATCH_RATIO_PASS:
        return VoiceCheckResult("passed", "", alignment.asr_text, ratio)
    return VoiceCheckResult(
        "failed", f"语音识别文本与试听文本差异较大（匹配率 {ratio:.2f}）", alignment.asr_text, ratio,
    )
