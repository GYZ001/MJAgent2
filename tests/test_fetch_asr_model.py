"""``scripts/fetch_asr_model.py``：sha256 校验失败、成功解压落位、幂等第二次不动。

现造一个假 tar.bz2（含最小 model.int8.onnx/tokens.txt 假内容），不依赖真实
163MB 模型包——sha256 只是内容摘要，用假内容配上通过 monkeypatch 换上的假
``EXPECTED_SHA256`` 一样能验证解压/落位/幂等的全部逻辑。
"""
from __future__ import annotations

import tarfile
from pathlib import Path

from scripts import fetch_asr_model


def _make_fake_tar(tar_path: Path, *, model_bytes: bytes = b"FAKE-MODEL-BYTES") -> Path:
    src_dir = tar_path.parent / "src_for_tar"
    src_dir.mkdir(exist_ok=True)
    (src_dir / "model.int8.onnx").write_bytes(model_bytes)
    (src_dir / "tokens.txt").write_text("a 0\nb 1\n", encoding="utf-8")
    (src_dir / "LICENSE").write_text("MIT-like", encoding="utf-8")
    (src_dir / "README.md").write_text("fake readme", encoding="utf-8")
    with tarfile.open(tar_path, "w:bz2") as tar:
        tar.add(src_dir, arcname="sherpa-onnx-fake-model")
    return tar_path


def test_sha_mismatch_exits_1_and_leaves_no_dest(tmp_path, capsys):
    tar_path = _make_fake_tar(tmp_path / "fake.tar.bz2")
    dest = tmp_path / "dest"

    code = fetch_asr_model.main(["--from-file", str(tar_path), "--dest", str(dest)])

    assert code == 1
    assert not (dest / "model.int8.onnx").exists()
    err = capsys.readouterr().err
    assert "sha256" in err


def test_from_file_missing_returns_1(tmp_path, capsys):
    code = fetch_asr_model.main([
        "--from-file", str(tmp_path / "no-such-file.tar.bz2"), "--dest", str(tmp_path / "dest"),
    ])
    assert code == 1
    assert "不存在" in capsys.readouterr().err


def test_extract_installs_and_second_run_is_idempotent(tmp_path, monkeypatch, capsys):
    tar_path = _make_fake_tar(tmp_path / "fake.tar.bz2")
    actual_sha = fetch_asr_model._sha256_of(tar_path)
    monkeypatch.setattr(fetch_asr_model, "EXPECTED_SHA256", actual_sha)
    dest = tmp_path / "dest"

    code = fetch_asr_model.main(["--from-file", str(tar_path), "--dest", str(dest)])
    assert code == 0
    assert (dest / "model.int8.onnx").read_bytes() == b"FAKE-MODEL-BYTES"
    assert (dest / "tokens.txt").is_file()
    assert (dest / "LICENSE").is_file()
    assert (dest / "README.md").is_file()
    assert (dest / "model_id.txt").read_text(encoding="utf-8").strip() == fetch_asr_model.MODEL_ID

    mtime_before = (dest / "model.int8.onnx").stat().st_mtime
    out_before = capsys.readouterr().out
    assert "sha256" not in out_before  # 正常路径不该打印校验失败字样

    code_again = fetch_asr_model.main(["--from-file", str(tar_path), "--dest", str(dest)])
    assert code_again == 0
    mtime_after = (dest / "model.int8.onnx").stat().st_mtime
    assert mtime_before == mtime_after, "幂等第二次运行不应重新落位（mtime 改变）"
    out_again = capsys.readouterr().out
    assert "已就绪" in out_again


def test_force_reinstalls_even_when_dest_complete(tmp_path, monkeypatch):
    tar_path = _make_fake_tar(tmp_path / "fake.tar.bz2")
    actual_sha = fetch_asr_model._sha256_of(tar_path)
    monkeypatch.setattr(fetch_asr_model, "EXPECTED_SHA256", actual_sha)
    dest = tmp_path / "dest"
    fetch_asr_model.main(["--from-file", str(tar_path), "--dest", str(dest)])
    mtime_before = (dest / "model.int8.onnx").stat().st_mtime

    code = fetch_asr_model.main(["--from-file", str(tar_path), "--dest", str(dest), "--force"])

    assert code == 0
    mtime_after = (dest / "model.int8.onnx").stat().st_mtime
    assert mtime_after >= mtime_before
