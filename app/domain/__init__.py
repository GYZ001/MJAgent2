"""Domain modules split out of :mod:`app.api`.

Import public API through ``app.api`` for compatibility.  This package mirrors
that aggregate namespace for direct domain-level imports.
"""
from __future__ import annotations

from pathlib import Path as _Path

_BASE = _Path(__file__).resolve().parent
_DOMAIN_MODULES = (
    "common.py",
    "projects.py",
    "bible_ops.py",
    "screenplay_ops.py",
    "storyboard_ops.py",
    "video_ops.py",
)
for _rel in _DOMAIN_MODULES:
    _path = _BASE / _rel
    exec(compile(_path.read_text(encoding="utf-8"), str(_path), "exec"), globals())

__all__ = [name for name in globals() if not name.startswith("__")]
del _BASE, _DOMAIN_MODULES, _Path, _path, _rel
