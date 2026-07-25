from pathlib import Path
import zipfile

import pytest

from app import atomic_io


def test_atomic_write_replaces_only_after_complete_commit(tmp_path: Path) -> None:
    target = tmp_path / "artifact.bin"
    target.write_bytes(b"old")

    atomic_io.atomic_write_bytes(target, b"new-complete")

    assert target.read_bytes() == b"new-complete"
    assert list(tmp_path.glob(".*.part")) == []


def test_atomic_write_failure_preserves_previous_target(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "artifact.bin"
    target.write_bytes(b"old")

    def fail_replace(_source, _target):
        raise OSError("simulated crash before rename")

    monkeypatch.setattr(atomic_io.os, "replace", fail_replace)

    with pytest.raises(OSError, match="simulated crash"):
        atomic_io.atomic_write_bytes(target, b"new-incomplete")

    assert target.read_bytes() == b"old"
    assert list(tmp_path.glob(".*.part")) == []


def test_atomic_copy_commits_complete_contents(tmp_path: Path) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"video-bytes")
    target = tmp_path / "final.mp4"

    atomic_io.atomic_copy(source, target)

    assert target.read_bytes() == b"video-bytes"
    assert list(tmp_path.glob(".*.part")) == []


def test_fsync_file_accepts_read_only_windows_paths(tmp_path: Path) -> None:
    """Regression: Windows EBADF when fsync'ing a read-only open (ERR-20260725-2b2321)."""
    path = tmp_path / "payload.bin"
    path.write_bytes(b"payload")
    atomic_io._fsync_file(path)
    assert path.read_bytes() == b"payload"


def test_cleanup_removes_only_atomic_part_files(tmp_path: Path) -> None:
    committed = tmp_path / "video.mp4"
    committed.write_bytes(b"ok")
    abandoned = tmp_path / ".video.mp4.deadbeef.part"
    abandoned.write_bytes(b"partial")

    assert atomic_io.cleanup_abandoned_parts(tmp_path) == 1
    assert committed.read_bytes() == b"ok"
    assert not abandoned.exists()


def test_atomic_zip_is_committed_with_complete_contents(tmp_path: Path) -> None:
    source = tmp_path / "package"
    source.mkdir()
    (source / "manifest.json").write_text('{"ok":true}', encoding="utf-8")
    nested = source / "media"
    nested.mkdir()
    (nested / "shot.mp4").write_bytes(b"video")
    target = tmp_path / "package.zip"

    atomic_io.atomic_zip_directory(source, target)

    with zipfile.ZipFile(target) as archive:
        assert archive.namelist() == ["manifest.json", "media/shot.mp4"]
        assert archive.read("media/shot.mp4") == b"video"
    assert list(tmp_path.glob(".*.part")) == []
