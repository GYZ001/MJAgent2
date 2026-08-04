"""REST API compatibility facade.

Domain implementation lives under ``app.domain``.  The chunks are executed in
this module namespace to preserve historical ``app.api`` monkeypatching and
``api.xxx`` access while keeping the source split by domain.
"""
from __future__ import annotations

from pathlib import Path as _Path

_BASE = _Path(__file__).resolve().parent
_DOMAIN_MODULES = (
    "domain/common.py",
    "domain/projects.py",
    "domain/bible_ops.py",
    "domain/screenplay_ops.py",
    "domain/storyboard_ops.py",
    "domain/video_ops.py",
    "domain/review_wall.py",
    "domain/narrative_calibration_ops.py",
)
for _rel in _DOMAIN_MODULES:
    _path = _BASE / _rel
    exec(compile(_path.read_text(encoding="utf-8"), str(_path), "exec"), globals())

del _BASE, _DOMAIN_MODULES, _Path, _path, _rel
