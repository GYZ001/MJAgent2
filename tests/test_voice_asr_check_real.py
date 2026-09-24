"""真模型冒烟：``app.voice.asr_check.check_preview_match`` 接的是真实
sherpa-onnx + SenseVoice 识别，不 mock（同 ``tests/test_subtitles_engine_
real.py`` 的纪律：环境不具备就 skip，不允许靠 mock 蒙混过关）。

复用同一条已知语音 fixture：``speech_zh_3s.wav`` 已知内容「没错，一旦王师兄
进入内宗，用不了多久」。用它验证——
1) preview_text 与已知台词逐字一致时，check_status=passed；
2) preview_text 换成完全不相关的内容时，check_status=failed，且不是靠
   异常兜底成 unchecked（区分"识别不可用"与"识别到了但对不上"两种失败）。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.subtitles import engine
from app.voice.asr_check import check_preview_match

_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "subtitles" / "speech_zh_3s.wav"
_KNOWN_TEXT = "没错，一旦王师兄进入内宗，用不了多久"


def _skip_if_engine_unavailable() -> None:
    status = engine.engine_status()
    if not status.ok:
        pytest.skip(f"ASR 引擎不可用，跳过真模型冒烟：{'；'.join(status.problems)}")
    if not _FIXTURE.is_file():
        pytest.skip(f"缺少真实语音 fixture：{_FIXTURE}")


def test_check_preview_match_passes_on_known_matching_speech() -> None:
    _skip_if_engine_unavailable()

    result = check_preview_match(_FIXTURE, _KNOWN_TEXT)

    assert result.check_status == "passed", result.check_reason
    assert result.asr_match is not None and result.asr_match >= 0.60
    assert "王师兄" in result.asr_text


def test_check_preview_match_fails_on_unrelated_preview_text() -> None:
    _skip_if_engine_unavailable()

    result = check_preview_match(_FIXTURE, "今天的天气真不错，我们一起去公园散步吧")

    assert result.check_status == "failed"
    assert "匹配率" in result.check_reason
    assert result.asr_match is not None and result.asr_match < 0.60
