"""Crash-safe filesystem commits for generated artifacts."""
from __future__ import annotations

import os
import shutil
import tempfile
import zipfile
from pathlib import Path


def _sync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def atomic_write_bytes(path: str | Path, value: bytes) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".part", dir=target.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        _sync_directory(target.parent)
        return target
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def atomic_write_text(path: str | Path, value: str, *, encoding: str = "utf-8") -> Path:
    return atomic_write_bytes(path, value.encode(encoding))


def atomic_copy(source: str | Path, destination: str | Path) -> Path:
    source_path = Path(source)
    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".part", dir=target.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        shutil.copy2(source_path, temporary)
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        _sync_directory(target.parent)
        return target
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def atomic_zip_directory(source: str | Path, destination: str | Path) -> Path:
    """Create a deterministic file-order ZIP and expose it only after fsync."""
    source_path = Path(source)
    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".part", dir=target.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(source_path.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(source_path).as_posix())
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        _sync_directory(target.parent)
        return target
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def cleanup_abandoned_parts(root: str | Path) -> int:
    """Remove only internal atomic-write remnants; committed targets are untouched."""
    base = Path(root)
    if not base.exists():
        return 0
    removed = 0
    for path in base.rglob(".*.part"):
        if not path.is_file():
            continue
        try:
            path.unlink()
            removed += 1
        except OSError:
            pass
    return removed
