"""下载/校验/解压 SenseVoice int8 ASR 模型到 ``data/models/sense-voice-int8/``。

必须用 Python ``tarfile``（``r:bz2``）解压——生产机 B 没有 ``bzip2`` 可执行文
件（实测，见 PRD/成片台字幕嵌入_台词对齐字幕PRD.md §1）。

用法：
    .venv/bin/python scripts/fetch_asr_model.py [--dest DIR] [--url URL]
        [--from-file local.tar.bz2] [--force]

幂等：``dest`` 已完整（``model.int8.onnx``/``tokens.txt``/``model_id.txt`` 三
件套齐全）且未传 ``--force`` 时直接打印「已就绪」退出 0，不重新下载。
"""
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.subtitles.engine import model_dir as default_model_dir  # noqa: E402

DEFAULT_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"
    "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17.tar.bz2"
)
EXPECTED_SHA256 = "7d1efa2138a65b0b488df37f8b89e3d91a60676e416f515b952358d83dfd347e"
MODEL_ID = "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17"
_REQUIRED_MEMBERS = ("model.int8.onnx", "tokens.txt")
_COPIED_MEMBERS = ("model.int8.onnx", "tokens.txt", "LICENSE", "README.md")


def _is_dest_complete(dest: Path) -> bool:
    return all((dest / name).is_file() for name in _REQUIRED_MEMBERS) and (dest / "model_id.txt").is_file()


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download(url: str, dest: Path) -> None:
    """按 10% 打印进度；``urllib`` 的默认 opener 自带 ``HTTPRedirectHandler``，
    301/302 会被自动跟随，不需要额外处理。"""
    last_pct = -1

    def _report(block_num: int, block_size: int, total_size: int) -> None:
        nonlocal last_pct
        if total_size <= 0:
            return
        pct = min(100, block_num * block_size * 100 // total_size)
        if pct >= last_pct + 10 or pct >= 100:
            print(f"下载进度 {pct}%")
            last_pct = pct

    urllib.request.urlretrieve(url, dest, reporthook=_report)


def _extract_required_members(tar_path: Path, work_dir: Path) -> None:
    """只把需要的 4 个文件直接读字节写到 ``work_dir`` 下、不带任何目录前缀
    ——不调用 ``TarFile.extract``，规避 tar 成员路径穿越（zip-slip 同类）风险，
    也顺带解决了压缩包内文件套在子目录下需要「拍平」的问题。"""
    with tarfile.open(tar_path, "r:bz2") as tar:
        found: dict[str, tarfile.TarInfo] = {}
        for member in tar.getmembers():
            if not member.isfile():
                continue
            base = Path(member.name).name
            if base in _COPIED_MEMBERS and base not in found:
                found[base] = member
        missing = [name for name in _REQUIRED_MEMBERS if name not in found]
        if missing:
            raise RuntimeError(f"压缩包内缺少必需文件：{'、'.join(missing)}")
        for base, member in found.items():
            source = tar.extractfile(member)
            if source is None:
                raise RuntimeError(f"压缩包内 {member.name} 不是常规文件")
            (work_dir / base).write_bytes(source.read())


def _install(work_dir: Path, dest: Path) -> None:
    """把 ``work_dir`` 原子换位成 ``dest``：先把旧 ``dest``（若存在）挪成
    备份，再把新目录挪进 ``dest``，最后删备份——每一步都是单次 ``os.replace``
    重命名（同一文件系统下是原子操作），中途失败旧内容不会丢失一半。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    (work_dir / "model_id.txt").write_text(MODEL_ID, encoding="utf-8")
    tmp_dest = dest.parent / f"{dest.name}.tmp-{os.getpid()}"
    if tmp_dest.exists():
        shutil.rmtree(tmp_dest)
    shutil.move(str(work_dir), str(tmp_dest))
    backup = None
    if dest.exists():
        backup = dest.parent / f"{dest.name}.bak-{os.getpid()}"
        os.replace(dest, backup)
    os.replace(tmp_dest, dest)
    if backup is not None:
        shutil.rmtree(backup)


def _fetch_and_install(args: argparse.Namespace, dest: Path) -> int:
    with tempfile.TemporaryDirectory(prefix="manju_asr_fetch_") as tmp:
        tmp_path = Path(tmp)
        tar_path = tmp_path / "model.tar.bz2"
        if args.from_file:
            if not args.from_file.is_file():
                print(f"--from-file 路径不存在：{args.from_file}", file=sys.stderr)
                return 1
            shutil.copy(args.from_file, tar_path)
        else:
            print(f"下载模型：{args.url}")
            _download(args.url, tar_path)
        actual_sha = _sha256_of(tar_path)
        if actual_sha != EXPECTED_SHA256:
            print(f"sha256 校验失败：期望 {EXPECTED_SHA256}，实际 {actual_sha}", file=sys.stderr)
            return 1
        work_dir = tmp_path / "extracted"
        work_dir.mkdir()
        try:
            _extract_required_members(tar_path, work_dir)
        except RuntimeError as exc:
            print(f"解压失败：{exc}", file=sys.stderr)
            return 1
        _install(work_dir, dest)
    print(f"模型已就绪：{dest}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dest", type=Path, default=None, help="默认 app.subtitles.engine.model_dir()")
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--from-file", type=Path, default=None, help="离线 tar.bz2 包路径，跳过下载")
    parser.add_argument("--force", action="store_true", help="即使 dest 已完整也重新下载/解压")
    args = parser.parse_args(argv)
    dest = args.dest or default_model_dir()

    if _is_dest_complete(dest) and not args.force:
        print(f"已就绪：{dest}")
        return 0
    return _fetch_and_install(args, dest)


if __name__ == "__main__":
    raise SystemExit(main())
