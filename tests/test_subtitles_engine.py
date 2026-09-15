"""``app.subtitles.engine``：模型探测、wav 抽取、子进程调度、全机串行锁。

子进程调用统一走 monkeypatch ``engine._run_worker``（模块级函数，不 patch
``subprocess.run`` 全局——那会连累其它模块共用的 subprocess 调用，见该函数
docstring）；``engine_status``/``extract_wav`` 同样用 monkeypatch 隔离真实
sherpa-onnx 安装状态与真实 ffmpeg 依赖，保证这里的用例在任何机器上都确定性
可跑，不依赖 CI 上是否已经 ``pip install sherpa-onnx``/下载过模型。

真模型端到端识别（不 mock）见 ``tests/test_subtitles_engine_real.py``。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
import wave
from pathlib import Path

import pytest

from app.subtitles import engine


def _ok_status(model_dir: Path) -> engine.EngineStatus:
    return engine.EngineStatus(ok=True, model_dir=model_dir, model_id="test-model", problems=())


def _noop_extract(source: Path, wav_path: Path, *, timeout_s: float) -> None:
    wav_path.write_bytes(b"")


# ---------------------------------------------------------------------------
# engine_status：三种缺失各报一条 problem
# ---------------------------------------------------------------------------


def test_engine_status_reports_missing_sherpa_onnx(tmp_path, monkeypatch):
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    (model_dir / "model.int8.onnx").write_bytes(b"fake")
    (model_dir / "tokens.txt").write_text("a 0\n", encoding="utf-8")
    monkeypatch.setenv("MANJU_ASR_MODEL_DIR", str(model_dir))
    monkeypatch.setattr(engine.importlib.util, "find_spec", lambda name: None)

    status = engine.engine_status()

    assert status.ok is False
    assert any("sherpa-onnx" in p for p in status.problems)


def test_engine_status_reports_missing_model_file(tmp_path, monkeypatch):
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    (model_dir / "tokens.txt").write_text("a 0\n", encoding="utf-8")
    monkeypatch.setenv("MANJU_ASR_MODEL_DIR", str(model_dir))
    monkeypatch.setattr(engine.importlib.util, "find_spec", lambda name: object())

    status = engine.engine_status()

    assert status.ok is False
    assert any("model.int8.onnx" in p for p in status.problems)


def test_engine_status_reports_missing_tokens_file(tmp_path, monkeypatch):
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    (model_dir / "model.int8.onnx").write_bytes(b"fake")
    monkeypatch.setenv("MANJU_ASR_MODEL_DIR", str(model_dir))
    monkeypatch.setattr(engine.importlib.util, "find_spec", lambda name: object())

    status = engine.engine_status()

    assert status.ok is False
    assert any("tokens.txt" in p for p in status.problems)


def test_engine_status_model_id_from_marker_file(tmp_path, monkeypatch):
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    (model_dir / "model.int8.onnx").write_bytes(b"fake")
    (model_dir / "tokens.txt").write_text("a 0\n", encoding="utf-8")
    (model_dir / "model_id.txt").write_text("my-model-id\n", encoding="utf-8")
    monkeypatch.setenv("MANJU_ASR_MODEL_DIR", str(model_dir))
    monkeypatch.setattr(engine.importlib.util, "find_spec", lambda name: object())

    status = engine.engine_status()

    assert status.ok is True
    assert status.model_id == "my-model-id"


# ---------------------------------------------------------------------------
# transcribe_media：monkeypatch _run_worker 注入各种 out.json
# ---------------------------------------------------------------------------


def test_transcribe_media_raises_when_engine_not_ok(tmp_path, monkeypatch):
    bad = engine.EngineStatus(ok=False, model_dir=tmp_path, model_id="x", problems=("缺模型文件",))
    monkeypatch.setattr(engine, "engine_status", lambda: bad)

    with pytest.raises(engine.AsrEngineError, match="缺模型文件"):
        engine.transcribe_media({"ver_x": Path("/fake/src.mp4")})


def test_transcribe_media_success(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "engine_status", lambda: _ok_status(tmp_path))
    monkeypatch.setattr(engine, "extract_wav", _noop_extract)

    def fake_run_worker(cmd, *, timeout_s):
        out_path = Path(cmd[cmd.index("--out") + 1])
        out_path.write_text(json.dumps({
            "engine_id": "sherpa-onnx/1.13.8", "model_id": "test-model",
            "results": {"ver_x": {"text": "你好", "tokens": [["你", 0.1], ["好", 0.3]]}},
            "errors": {},
        }), encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(engine, "_run_worker", fake_run_worker)

    result = engine.transcribe_media({"ver_x": Path("/fake/src.mp4")})

    assert result["ver_x"].text == "你好"
    assert result["ver_x"].tokens == (("你", 0.1), ("好", 0.3))


def test_transcribe_media_nonzero_exit_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "engine_status", lambda: _ok_status(tmp_path))
    monkeypatch.setattr(engine, "extract_wav", _noop_extract)

    def fake_run_worker(cmd, *, timeout_s):
        return subprocess.CompletedProcess(cmd, 2, stdout=b"", stderr=b"boom stderr detail")

    monkeypatch.setattr(engine, "_run_worker", fake_run_worker)

    with pytest.raises(engine.AsrEngineError) as exc_info:
        engine.transcribe_media({"ver_x": Path("/fake/src.mp4")})
    message = str(exc_info.value)
    assert "退出码" in message
    assert "boom stderr detail" in message
    assert "fetch_asr_model.py" in message or "sherpa-onnx" in message


def test_transcribe_media_job_errors_raise(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "engine_status", lambda: _ok_status(tmp_path))
    monkeypatch.setattr(engine, "extract_wav", _noop_extract)

    def fake_run_worker(cmd, *, timeout_s):
        out_path = Path(cmd[cmd.index("--out") + 1])
        out_path.write_text(json.dumps({
            "engine_id": "x", "model_id": "y", "results": {},
            "errors": {"ver_x": "wav 不是 16kHz 单声道 s16le"},
        }), encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(engine, "_run_worker", fake_run_worker)

    with pytest.raises(engine.AsrEngineError) as exc_info:
        engine.transcribe_media({"ver_x": Path("/fake/src.mp4")})
    message = str(exc_info.value)
    assert "ver_x" in message
    assert "不允许静默降级" in message


def test_transcribe_media_bad_json_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "engine_status", lambda: _ok_status(tmp_path))
    monkeypatch.setattr(engine, "extract_wav", _noop_extract)

    def fake_run_worker(cmd, *, timeout_s):
        out_path = Path(cmd[cmd.index("--out") + 1])
        out_path.write_text("not-json{{{", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(engine, "_run_worker", fake_run_worker)

    with pytest.raises(engine.AsrEngineError, match="JSON"):
        engine.transcribe_media({"ver_x": Path("/fake/src.mp4")})


# ---------------------------------------------------------------------------
# extract_wav：真跑 ffmpeg（缺 ffmpeg 则 skip）
# ---------------------------------------------------------------------------


def test_extract_wav_real_ffmpeg(tmp_path):
    if not shutil.which("ffmpeg"):
        pytest.skip("本机没有 ffmpeg，跳过真实抽取测试")
    source = tmp_path / "sine.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
            "-i", "sine=frequency=440:duration=1", "-c:a", "aac", str(source),
        ],
        check=True, timeout=30,
    )
    wav_path = tmp_path / "out.wav"

    engine.extract_wav(source, wav_path, timeout_s=30)

    assert wav_path.is_file()
    with wave.open(str(wav_path), "rb") as handle:
        assert handle.getframerate() == 16000
        assert handle.getnchannels() == 1
        assert handle.getsampwidth() == 2


def test_extract_wav_missing_source_raises(tmp_path):
    if not shutil.which("ffmpeg"):
        pytest.skip("本机没有 ffmpeg，跳过真实抽取测试")
    with pytest.raises(engine.AsrEngineError):
        engine.extract_wav(tmp_path / "does-not-exist.mp4", tmp_path / "out.wav", timeout_s=10)


# ---------------------------------------------------------------------------
# _ASR_LOCK：全机串行，两线程 + 假 runner 记录进入/退出时间，连跑 >= 10 次
# ---------------------------------------------------------------------------


def test_asr_lock_serializes_concurrent_calls(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "engine_status", lambda: _ok_status(tmp_path))
    monkeypatch.setattr(engine, "extract_wav", _noop_extract)

    intervals: list[tuple[float, float]] = []
    intervals_lock = threading.Lock()

    def fake_run_worker(cmd, *, timeout_s):
        start = time.monotonic()
        time.sleep(0.05)
        end = time.monotonic()
        with intervals_lock:
            intervals.append((start, end))
        out_path = Path(cmd[cmd.index("--out") + 1])
        out_path.write_text(
            json.dumps({"engine_id": "x", "model_id": "y", "results": {}, "errors": {}}),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(engine, "_run_worker", fake_run_worker)

    for attempt in range(10):
        intervals.clear()
        errors: list[BaseException] = []

        def _call() -> None:
            try:
                engine.transcribe_media({"v": Path("/fake/src.mp4")})
            except BaseException as exc:  # noqa: BLE001 主线程需要看到子线程异常
                errors.append(exc)

        threads = [threading.Thread(target=_call) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert not errors, f"attempt {attempt}: 子线程异常 {errors}"
        assert len(intervals) == 2, f"attempt {attempt}: 未收集到两次调用 {intervals}"
        (s1, e1), (s2, e2) = sorted(intervals)
        assert e1 <= s2, f"attempt {attempt}: 区间交叠 {intervals}——_ASR_LOCK 未生效"
