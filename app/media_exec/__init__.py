"""Media execution modules split out of :mod:`app.worker`.

The package re-exports the public worker API while keeping shared queue state in
one aggregate namespace.
"""
from __future__ import annotations

from pathlib import Path as _Path

_BASE = _Path(__file__).resolve().parent
_MEDIA_MODULES = (
    "common.py",
    "enqueue.py",
    "legacy_keyframes.py",
    "run_job.py",
    "concat.py",
)
for _rel in _MEDIA_MODULES:
    _path = _BASE / _rel
    exec(compile(_path.read_text(encoding="utf-8"), str(_path), "exec"), globals())

__all__ = [name for name in globals() if not name.startswith("__")]
del _BASE, _MEDIA_MODULES, _Path, _path, _rel
