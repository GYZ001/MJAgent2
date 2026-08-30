"""DDL / bootstrap registry — the only mechanism ``app.db.init_db()`` uses to
run per-table setup and idempotent one-time migrations owned by business
modules.

Zero business dependencies: only ``sqlite3`` and the standard library.
Business modules import this module and call :func:`register_table` once at
import time (module level, not inside a function). ``app.db`` never imports
those business modules back — see
``docs/coupling_review_2026-08-29.md`` 第2步 for why the import direction
matters: it used to be the single largest contributor to the 112-module
dependency cycle (``app.db`` is depended on by 82 modules; every business
module it imported back pulled all 82 of them, plus everything *they*
depend on, into one strongly connected component).

Something at the entry layer must import every registrant module at least
once before ``app.db.init_db()`` runs, or the registry stays empty and a
lookup raises ``KeyError``. ``app.main``'s lifespan does this for the running
service; ``tests/conftest.py`` does the same so ``db.init_db()`` also works
when a single test file is run in isolation, not just for a full-suite run.

A missing registration is a startup wiring bug, not a state to silently
swallow — :func:`get` and :func:`run` raise ``KeyError`` rather than no-op.
"""
from __future__ import annotations

import sqlite3
from typing import Any, Callable

_REGISTRY: dict[str, Callable[..., Any]] = {}


def register_table(name: str, fn: Callable[..., Any]) -> None:
    """Register a bootstrap callable under ``name``.

    Safe to call again for the same ``name`` — re-importing a module during
    test collection must not raise; the later registration simply wins.
    Callers own uniqueness of ``name`` within their own domain.
    """
    _REGISTRY[name] = fn


def get(name: str) -> Callable[..., Any]:
    """Look up a registered callable by name.

    Raises ``KeyError`` if nothing has registered under ``name`` yet: the
    owning business module was never imported before this call.
    """
    return _REGISTRY[name]


def run(conn: sqlite3.Connection, name: str) -> Any:
    """Sugar for ``get(name)(conn)`` — the common single-connection-arg case."""
    return get(name)(conn)


def registered_names() -> list[str]:
    return list(_REGISTRY.keys())
