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


def ensure_schema_respecting_caller_transaction(
    conn: sqlite3.Connection,
    *,
    on_caller_connection: Callable[[sqlite3.Connection], None],
    run_independent: Callable[[], None],
) -> None:
    """Route a lazy-schema call away from a write-lock deadlock.

    Six business packages (``orgs``/``models_registry``/``provisioning``/
    ``sso``/``quota_policy``/``audit``) each keep a pair of lazy
    table-creation entry points: an independent-connection variant
    (conventionally named ``ensure_schema``) that opens its own connection
    and its own ``BEGIN IMMEDIATE`` transaction, and a same-connection
    variant (``ensure_tables_on_connection``) that runs on a connection the
    caller already owns and never opens a new one.

    Calling the independent variant while the caller's own connection
    already holds an open write transaction makes the new connection
    contend for the very same SQLite write lock the caller is holding, time
    out after ``WRITE_TXN_BUSY_TIMEOUT_S`` seconds, and get silently
    swallowed by ``ensure_schema``'s own ``except Exception`` — leaving the
    table or column missing and surfacing far away, several call frames
    removed from the actual cause, as ``no such table``/``no such column``.
    This exact shape has recurred four times across four unrelated call
    sites (EP-04 tier quota, an ``executescript`` implicit-commit variant of
    the same race, ``create_session``/``resolve_session``, and a batch of
    remaining ``ensure_schema()`` call sites in ``models_registry``/
    ``provisioning``) — every prior fix patched the call site instead of the
    primitive, so a fifth call site making the same choice was only a matter
    of time.

    This helper removes the choice instead of trusting every call site to
    make it correctly: if ``conn`` (normally the caller's thread/task-local
    ``app.db.get_conn()``) is already mid-transaction, run
    ``on_caller_connection`` on that same connection — no new connection, no
    lock contention, whichever of the two entry points the call site
    happened to invoke. Otherwise fall back to ``run_independent`` — today's
    independent-connection behaviour, unchanged for the bootstrap path and
    background tasks that are never nested inside another write transaction.

    ``conn`` is passed in by the caller rather than resolved here: this
    module is imported by ``app.db`` itself and must keep zero dependency on
    it (see the module docstring, "Zero business dependencies") — reaching
    for ``app.db.get_conn()`` inside this module would be an upward layer
    edge (``app.db_schema`` is declared a lower layer than ``app.db`` in
    ``app/LAYERS.toml``).
    """
    if conn.in_transaction:
        on_caller_connection(conn)
        return
    run_independent()

