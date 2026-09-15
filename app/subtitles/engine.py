"""ASR 引擎：模型探测、wav 抽取、子进程调度、全机串行锁。

主进程永不 import ``sherpa_onnx``——是否可用只用 ``importlib.util.find_spec``
探测（``engine_status()``），真正的识别工作全部丢给
``app.subtitles.asr_worker`` 子进程，主进程与子进程之间只通过 jobs.json /
out.json 两份文件通信（见该模块文档）。

失败即报错、不降级：PRD/成片台字幕嵌入_台词对齐字幕PRD.md §11「本功能不允许
静默降级」——``engine_status()`` 不 ok，或子进程报任何单个 job 的 error，都
直接抛 ``AsrEngineError``，绝不产出无字幕/错时间戳的成片。

全机串行：``_ASR_LOCK`` 照抄 ``app/domain/series_ops/merge.py::_MERGE_LOCK``
的做法——SenseVoice int8 子进程峰值约 500 MB，多个合成任务并发跑 ASR 会跟其它
CPU 密集环节抢核。
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from app import config

DEFAULT_MODEL_SUBDIR = "models/sense-voice-int8"

_ASR_LOCK = threading.Lock()


class AsrEngineError(RuntimeError):
    """message 必须含具体出路：要跑什么脚本、装什么包、设什么环境变量。"""


@dataclass(frozen=True)
class EngineStatus:
    ok: bool
    model_dir: Path
    model_id: str
    problems: tuple[str, ...]
    engine_id: str = ""


@dataclass(frozen=True)
class AsrResult:
    text: str
    tokens: tuple[tuple[str, float], ...]


def model_dir() -> Path:
    override = os.environ.get("MANJU_ASR_MODEL_DIR", "").strip()
    if override:
        return Path(override)
    return config.DATA_DIR / DEFAULT_MODEL_SUBDIR


def _missing_model_file_problems(directory: Path) -> list[str]:
    problems: list[str] = []
    has_model = any((directory / name).is_file() for name in ("model.int8.onnx", "model.onnx"))
    if not has_model:
        problems.append(
            f"模型目录 {directory} 缺 model.int8.onnx/model.onnx；"
            f"请运行 .venv/bin/python scripts/fetch_asr_model.py --dest {directory}"
        )
    if not (directory / "tokens.txt").is_file():
        problems.append(
            f"模型目录 {directory} 缺 tokens.txt；"
            f"请运行 .venv/bin/python scripts/fetch_asr_model.py --dest {directory}"
        )
    return problems


def _read_model_id(directory: Path) -> str:
    marker = directory / "model_id.txt"
    if marker.is_file():
        text = marker.read_text(encoding="utf-8").strip()
        if text:
            return text
    return directory.name


def _installed_engine_id() -> str:
    """读已安装包的元数据取真实版本号；只查元数据、不 import sherpa_onnx 本体
    （不执行包代码），与模块文档「主进程永不 import 它的依赖」不冲突。U3 需要
    这个值写进 ``subtitle_alignments`` 缓存行与字幕报告的 ``engine_id``——此前
    只有子进程 ``asr_worker`` 算过它，主进程 ``transcribe_media`` 把它丢在了
    worker 输出里没往外传，调用方拿不到，是本次接线时发现并补上的缺口。"""
    import importlib.metadata

    try:
        return f"sherpa-onnx/{importlib.metadata.version('sherpa-onnx')}"
    except importlib.metadata.PackageNotFoundError:
        return "sherpa-onnx/unavailable"


def engine_status() -> EngineStatus:
    problems: list[str] = []
    if importlib.util.find_spec("sherpa_onnx") is None:
        problems.append("未安装 sherpa-onnx；请先运行 .venv/bin/pip install sherpa-onnx==1.13.8")
    directory = model_dir()
    problems.extend(_missing_model_file_problems(directory))
    return EngineStatus(
        ok=not problems, model_dir=directory, model_id=_read_model_id(directory), problems=tuple(problems),
        engine_id=_installed_engine_id(),
    )


def _low_priority() -> None:
    """ASR/ffmpeg 子进程自降 CPU 优先级（nice 10），后端进程的响应优先；本包 L1 不依赖 app.media_pipeline。"""
    try:
        os.nice(10)
    except OSError:
        pass


def extract_wav(source: Path, wav_path: Path, *, timeout_s: float) -> None:
    """ffmpeg 抽 16kHz 单声道 s16le wav；失败/超时都抛 ``AsrEngineError``。"""
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error", "-i", str(source),
        "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav_path),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout_s, preexec_fn=_low_priority)
    except subprocess.TimeoutExpired as exc:
        raise AsrEngineError(f"ffmpeg 抽取音轨超时（{source}）；请检查源文件是否损坏") from exc
    except FileNotFoundError as exc:
        raise AsrEngineError("未找到 ffmpeg 可执行文件；请安装 ffmpeg 并确保在 PATH 中") from exc
    if proc.returncode != 0:
        stderr_tail = proc.stderr.decode("utf-8", errors="replace")[-800:]
        raise AsrEngineError(f"ffmpeg 抽取音轨失败（{source}），退出码 {proc.returncode}：{stderr_tail}")


def _run_worker(cmd: list[str], *, timeout_s: float) -> subprocess.CompletedProcess:
    """子进程调用的唯一入口——测试通过 monkeypatch 本函数注入假输出，不 patch
    ``subprocess.run`` 全局（那会连累其它模块共用的 subprocess 调用）。"""
    return subprocess.run(cmd, cwd=str(config.ROOT), capture_output=True, timeout=timeout_s, preexec_fn=_low_priority)


def _extract_all_wavs(jobs: Mapping[str, Path], tmp_dir: Path, timeout_s: float) -> list[dict[str, str]]:
    job_specs: list[dict[str, str]] = []
    for key, source in jobs.items():
        wav_path = tmp_dir / f"{key}.wav"
        extract_wav(Path(source), wav_path, timeout_s=timeout_s)
        job_specs.append({"key": key, "wav": str(wav_path)})
    return job_specs


def _build_worker_cmd(
    model_directory: Path, jobs_path: Path, out_path: Path, threads: int,
) -> list[str]:
    return [
        sys.executable, "-m", "app.subtitles.asr_worker",
        "--model-dir", str(model_directory),
        "--jobs", str(jobs_path), "--out", str(out_path),
        "--threads", str(threads),
    ]


def _invoke_worker(cmd: list[str], timeout_s: float) -> None:
    try:
        proc = _run_worker(cmd, timeout_s=timeout_s)
    except subprocess.TimeoutExpired as exc:
        raise AsrEngineError(
            f"ASR 子进程超时（{timeout_s:.0f}s）；镜头数较多可适当调大 timeout_s"
        ) from exc
    if proc.returncode != 0:
        stderr_tail = proc.stderr.decode("utf-8", errors="replace")[-800:]
        raise AsrEngineError(
            f"ASR 子进程失败（退出码 {proc.returncode}）：{stderr_tail}；"
            "请检查模型文件是否完整（.venv/bin/python scripts/fetch_asr_model.py）"
            "或重新安装 pip install sherpa-onnx==1.13.8"
        )


def _load_worker_output(out_path: Path) -> dict:
    try:
        out = json.loads(out_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AsrEngineError(f"ASR 子进程输出不是合法 JSON（{out_path}）：{exc}") from exc
    errors = out.get("errors") or {}
    if errors:
        detail = "；".join(f"{key}: {reason}" for key, reason in errors.items())
        raise AsrEngineError(f"以下镜头语音识别失败，不允许静默降级为无字幕：{detail}")
    return out


def _results_from_output(out: dict) -> dict[str, AsrResult]:
    results = out.get("results") or {}
    return {
        key: AsrResult(
            text=item.get("text", ""),
            tokens=tuple((tok, float(ts)) for tok, ts in item.get("tokens", [])),
        )
        for key, item in results.items()
    }


def transcribe_media(
    jobs: Mapping[str, Path], *, threads: int = 2, timeout_s: float | None = None,
) -> dict[str, AsrResult]:
    status = engine_status()
    if not status.ok:
        raise AsrEngineError("字幕语音识别引擎不可用：" + "；".join(status.problems))
    if not jobs:
        return {}
    effective_timeout = timeout_s if timeout_s is not None else 60.0 + 5.0 * len(jobs)
    with _ASR_LOCK:  # 全机一次只跑一个 ASR 批次，见模块 docstring
        with tempfile.TemporaryDirectory(prefix="manju_asr_") as tmp:
            tmp_dir = Path(tmp)
            job_specs = _extract_all_wavs(jobs, tmp_dir, effective_timeout)
            jobs_path, out_path = tmp_dir / "jobs.json", tmp_dir / "out.json"
            jobs_path.write_text(json.dumps(job_specs, ensure_ascii=False), encoding="utf-8")
            cmd = _build_worker_cmd(status.model_dir, jobs_path, out_path, threads)
            _invoke_worker(cmd, effective_timeout)
            out = _load_worker_output(out_path)
            return _results_from_output(out)
