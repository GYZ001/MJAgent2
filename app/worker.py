"""Compatibility facade for media workers.

Implementation chunks live under ``app.media_exec``.  They are executed in this
module namespace so existing ``import app.worker`` monkeypatches and attributes
continue to target the same globals the functions use.
"""
from __future__ import annotations

from pathlib import Path as _Path

_BASE = _Path(__file__).resolve().parent
_MEDIA_MODULES = (
    "media_exec/common.py",
    "media_exec/enqueue.py",
    "media_exec/legacy_keyframes.py",
    "media_exec/run_job.py",
    "media_exec/concat.py",
)
for _rel in _MEDIA_MODULES:
    _path = _BASE / _rel
    exec(compile(_path.read_text(encoding="utf-8"), str(_path), "exec"), globals())

del _BASE, _MEDIA_MODULES, _Path, _path, _rel
