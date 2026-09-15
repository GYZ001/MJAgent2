"""真模型冒烟：不 mock，直接跑 sherpa-onnx + SenseVoice int8 识别真实语音。

``engine_status().ok`` 为假（未装 sherpa-onnx 或模型未下载）就 skip——这条测试
存在的意义就是「确实跑通了」，环境不具备时如实跳过，不允许靠 mock 蒙混过关
（CLAUDE.md「验证要有独立观察点」「查不清就说查不清」）。

fixture ``tests/fixtures/subtitles/speech_zh_3s.wav`` 是 B 现网真实语音截取
（第 10 集镜 10，3.2 秒），已知内容「没错，一旦王师兄进入内宗，用不了多久」。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.subtitles import engine

_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "subtitles" / "speech_zh_3s.wav"


def test_real_transcription_matches_known_speech():
    status = engine.engine_status()
    if not status.ok:
        pytest.skip(f"ASR 引擎不可用，跳过真模型冒烟：{'；'.join(status.problems)}")
    if not _FIXTURE.is_file():
        pytest.skip(f"缺少真实语音 fixture：{_FIXTURE}")

    results = engine.transcribe_media({"ver_real": _FIXTURE}, threads=2)
    result = results["ver_real"]

    assert "王师兄" in result.text, f"识别文本未命中已知台词：{result.text!r}"
    assert len(result.tokens) >= 12, f"token 数过少：{len(result.tokens)}"
    times = [t for _, t in result.tokens]
    assert times == sorted(times), f"时间戳必须单调递增：{times}"
    assert all(0.0 <= t <= 3.3 for t in times), f"时间戳越界 [0, 3.3]：{times}"
