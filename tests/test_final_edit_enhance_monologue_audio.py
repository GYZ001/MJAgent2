"""``app.final_edit_enhance.monologue_audio``：合成一句独白后按真实（此处打桩
的）音频时长核验静默窗口容量。语音供应商网络请求与真实 ffmpeg 探测各自已在
``tests/test_voice_qwen_synthesize_speech.py``/``tests/test_final_edit_enhance_
apply.py`` 里单独验证过，这里只验证"合成时长超出窗口时明确跳过、不裁剪"这条
既有逻辑（``apply.MONOLOGUE_MIN_WINDOW_S`` 从固定 30s 改为数据推导之后，
候选窗口更短、这条防线更容易被真实触发，需要有直接覆盖它的单测）。
"""
from __future__ import annotations

from pathlib import Path

from app.db import get_conn
from app.final_edit_enhance import monologue_audio
from app.final_edit_enhance.plan_generate import ResolvedMonologueLine
from app.voice import store
from app.voice.providers import dispatch
from app.voice.providers.base import SpeechSynthesisResult


def _seed_voice(conn, project_id: str, character_name: str) -> None:
    store.ensure_tables_on_connection(conn)
    conn.execute(
        "INSERT INTO character_voices(id, project_id, character_name, anchor_key, status, "
        "source, model_id, provider_voice_id, created_at, updated_at) "
        "VALUES('v1', ?, ?, '', 'current', 'design', 'model-1', 'voice-1', 0, 0)",
        (project_id, character_name),
    )
    conn.commit()


async def _fake_synthesize(_model_id, _text, _voice_id, *, call_meta=None):
    return SpeechSynthesisResult(
        audio=b"RIFFxxxxWAVEfake", audio_format="wav", sample_rate=16000, request_id="r1", latency_ms=1,
    )


async def test_synthesized_line_longer_than_window_is_skipped_with_reason(tmp_path: Path, monkeypatch) -> None:
    conn = get_conn()
    _seed_voice(conn, "p1", "顾屿")
    monkeypatch.setattr(dispatch, "synthesize_speech_for_voice", _fake_synthesize)
    monkeypatch.setattr(monologue_audio, "probe_duration_s", lambda _path: 6.0)  # 合成出的语音实际时长 6s

    line = ResolvedMonologueLine(start_s=0.0, end_s=5.0, character_name="顾屿", text="我不会认输")  # 窗口只有 5s
    applied, skipped = await monologue_audio.synthesize_monologue_lines(conn, "p1", (line,), tmp_path)

    assert applied == []
    assert len(skipped) == 1
    assert "超出静默窗口" in skipped[0]["reason"]
    assert "不裁剪" in skipped[0]["reason"]


async def test_synthesized_line_within_window_is_applied(tmp_path: Path, monkeypatch) -> None:
    conn = get_conn()
    _seed_voice(conn, "p1", "顾屿")
    monkeypatch.setattr(dispatch, "synthesize_speech_for_voice", _fake_synthesize)
    monkeypatch.setattr(monologue_audio, "probe_duration_s", lambda _path: 3.0)  # 合成出的语音实际时长 3s，窗口 5s 够放

    line = ResolvedMonologueLine(start_s=0.0, end_s=5.0, character_name="顾屿", text="我不会认输")
    applied, skipped = await monologue_audio.synthesize_monologue_lines(conn, "p1", (line,), tmp_path)

    assert len(applied) == 1
    assert applied[0].duration_s == 3.0
    assert skipped == []
