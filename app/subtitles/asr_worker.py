"""ASR 子进程 CLI：在独立进程里跑 sherpa-onnx，主进程永不 import 它的依赖。

零 ``app.*`` import，只用 stdlib；``sherpa_onnx`` 只在 ``main()`` 内延迟
import——这样即使宿主环境没装 sherpa-onnx，``app.subtitles.engine`` 的预检
（``importlib.util.find_spec``）依然能在主进程里安全跑，不会因为模块级 import
就先炸；真正需要这个包的只有本文件，且只在它作为子进程被启动、且模型文件确
认存在之后才会走到 import 这一步（见 ``main()``）。

用法：
    .venv/bin/python -m app.subtitles.asr_worker --model-dir DIR --jobs jobs.json
        --out out.json [--threads 2] [--language zh]

``jobs.json`` 形如 ``[{"key": "ver_x", "wav": "/abs/a.wav"}, ...]``；wav 必须是
16 kHz 单声道 s16le（用 stdlib ``wave``+``array`` 读；采样率/声道不符的单个
job 记 error，不让整批失败——一镜格式异常不该拖累同批其它镜头）。

退出码：0 正常（个别 job 出错也 0，错误在 out.json 的 ``errors`` 里）；
2 参数/模型文件缺失；3 ``import sherpa_onnx`` 失败。
"""
from __future__ import annotations

import argparse
import array
import json
import sys
import wave
from pathlib import Path
from typing import Any

_REQUIRED_MODEL_FILES = ("model.int8.onnx", "model.onnx")


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", required=True, type=Path)
    parser.add_argument("--jobs", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--threads", type=int, default=2)
    parser.add_argument("--language", default="zh")
    return parser.parse_args(argv)


def _resolve_model_file(model_dir: Path) -> Path | None:
    for name in _REQUIRED_MODEL_FILES:
        candidate = model_dir / name
        if candidate.is_file():
            return candidate
    return None


def _resolve_model_id(model_dir: Path) -> str:
    marker = model_dir / "model_id.txt"
    if marker.is_file():
        text = marker.read_text(encoding="utf-8").strip()
        if text:
            return text
    return model_dir.name


def _read_pcm16_mono_16k(wav_path: Path) -> list[float]:
    """读 16kHz 单声道 s16le wav，返回归一化到 [-1, 1) 的浮点样本。

    采样率/声道/位宽不符时抛 ``ValueError``，由调用方记进该 job 的 error，不让
    整批失败。
    """
    with wave.open(str(wav_path), "rb") as handle:
        rate = handle.getframerate()
        channels = handle.getnchannels()
        sampwidth = handle.getsampwidth()
        if rate != 16000 or channels != 1 or sampwidth != 2:
            raise ValueError(
                "wav 不是 16kHz 单声道 s16le（实际 rate="
                f"{rate} channels={channels} sampwidth={sampwidth}）"
            )
        raw = handle.readframes(handle.getnframes())
    samples = array.array("h")
    samples.frombytes(raw)
    return [x / 32768.0 for x in samples]


def _decode_job(recognizer: Any, wav_path: Path) -> dict[str, Any]:
    samples = _read_pcm16_mono_16k(wav_path)
    stream = recognizer.create_stream()
    stream.accept_waveform(16000, samples)
    recognizer.decode_stream(stream)
    result = stream.result
    tokens = list(result.tokens)
    timestamps = list(result.timestamps)
    return {
        "text": result.text,
        "tokens": [[tok, float(t)] for tok, t in zip(tokens, timestamps)],
    }


def _run_all_jobs(recognizer: Any, jobs: list[dict[str, str]]) -> tuple[dict[str, Any], dict[str, str]]:
    results: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for index, job in enumerate(jobs):
        key = job.get("key") or f"job_{index}"
        try:
            results[key] = _decode_job(recognizer, Path(job["wav"]))
        except Exception as exc:  # noqa: BLE001 单 job 失败不能拖垮整批解码
            errors[key] = str(exc)
    return results, errors


def _load_jobs(jobs_path: Path) -> list[dict[str, str]] | None:
    try:
        return json.loads(jobs_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    model_dir: Path = args.model_dir
    model_file = _resolve_model_file(model_dir)
    tokens_file = model_dir / "tokens.txt"
    if model_file is None or not tokens_file.is_file():
        print(
            f"模型文件缺失：需要 {model_dir}/model.int8.onnx（或 model.onnx）"
            f"以及 {tokens_file}；请先运行 scripts/fetch_asr_model.py",
            file=sys.stderr,
        )
        return 2
    jobs = _load_jobs(args.jobs)
    if jobs is None:
        print(f"无法读取 jobs 文件 {args.jobs}", file=sys.stderr)
        return 2

    try:
        # sherpa_onnx 只在这里、确认模型文件齐全之后才 import：它是本文件唯一
        # 需要的重依赖，主进程（app.subtitles.engine）永不触碰它，见模块文档。
        import sherpa_onnx
    except ImportError as exc:
        print(
            f"import sherpa_onnx 失败（{exc}）；请先安装：pip install sherpa-onnx==1.13.8",
            file=sys.stderr,
        )
        return 3

    import importlib.metadata  # 只有 sherpa_onnx 已装、真正跑到这里时才需要取版本号

    recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
        model=str(model_file), tokens=str(tokens_file),
        num_threads=args.threads, language=args.language, use_itn=False,
    )
    results, errors = _run_all_jobs(recognizer, jobs)
    engine_id = f"sherpa-onnx/{importlib.metadata.version('sherpa-onnx')}"
    out = {
        "engine_id": engine_id,
        "model_id": _resolve_model_id(model_dir),
        "results": results,
        "errors": errors,
    }
    args.out.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
