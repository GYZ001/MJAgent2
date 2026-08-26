"""Shared logic for reclaiming orphaned pytest/verify sandboxes under /tmp.

``scripts/verify.py`` and ``tests/conftest.py`` each create a sandbox
directory (``manju-verify-*`` / ``manju-pytest-*``) per run and normally
clean it up on exit (``TemporaryDirectory`` / ``pytest_unconfigure``).
Neither cleanup path runs when the owning process is hard-killed (SIGKILL,
or a default-action SIGTERM) -- that is how these accumulate in practice,
especially with several agents restarting runs against the same box.

Ownership is the primary staleness signal, not age: each sandbox records
its owning process's PID in an ``owner.pid`` marker file at creation time
(see ``mark_sandbox_owner``). A sandbox is orphaned -- and safe to remove
regardless of how recently it was created -- once that PID is no longer
alive. ``max_age_hours`` only matters as a fallback for sandboxes with no
readable marker (pre-existing dirs from before this module existed, or a
failed write) and as insurance against the unlikely case of PID reuse.

This module is intentionally dependency-free (stdlib only) so it can be
imported from ``scripts/`` and from ``tests/conftest.py`` without pulling
in application code.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import time
from pathlib import Path

SANDBOX_PREFIXES: tuple[str, ...] = ("manju-pytest-", "manju-verify-")
OWNER_FILE_NAME = "owner.pid"
DEFAULT_MAX_AGE_HOURS = 6.0


def mark_sandbox_owner(sandbox: Path, pid: int | None = None) -> None:
    """Record the owning process PID inside a freshly created sandbox.

    Best-effort: if the write fails, the sandbox just falls back to
    age-based staleness detection instead of ownership-based.
    """
    try:
        (sandbox / OWNER_FILE_NAME).write_text(str(pid if pid is not None else os.getpid()))
    except OSError:
        pass


def pid_is_alive(pid: int) -> bool:
    """Best-effort liveness check for a PID recorded in an owner.pid file."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # process exists (just not ours) -- can't prove it's dead
    except OSError:
        return True  # unexpected; don't risk deleting a live sandbox
    return True


def sandbox_owner_pid(entry: Path) -> int | None:
    """Read the owner PID marker for ``entry``, or None if absent/unreadable."""
    try:
        return int((entry / OWNER_FILE_NAME).read_text().strip())
    except (OSError, ValueError):
        return None


def is_stale_sandbox(
    entry: Path,
    *,
    max_age_hours: float = DEFAULT_MAX_AGE_HOURS,
    now: float | None = None,
) -> bool:
    """True if ``entry`` is orphaned and safe to remove.

    Primary check: the recorded owner PID is dead (age is irrelevant then --
    a sandbox from a run that was hard-killed a minute ago is exactly as
    orphaned as one from a week ago). Fallback: no readable marker, in which
    case the age cutoff is the only signal available.
    """
    owner_pid = sandbox_owner_pid(entry)
    if owner_pid is not None:
        return not pid_is_alive(owner_pid)
    cutoff = (now if now is not None else time.time()) - max_age_hours * 3600
    try:
        return entry.stat().st_mtime < cutoff
    except OSError:
        return False


def find_stale_sandboxes(
    tmp_root: Path | None = None,
    prefixes: tuple[str, ...] = SANDBOX_PREFIXES,
    *,
    max_age_hours: float = DEFAULT_MAX_AGE_HOURS,
) -> list[Path]:
    """List orphaned sandbox dirs under ``tmp_root`` without removing them."""
    root = tmp_root if tmp_root is not None else Path(tempfile.gettempdir())
    try:
        entries = list(root.iterdir())
    except OSError:
        return []
    now = time.time()
    stale = [
        entry
        for entry in entries
        if entry.name.startswith(prefixes) and is_stale_sandbox(entry, max_age_hours=max_age_hours, now=now)
    ]
    return sorted(stale, key=lambda path: str(path).lower())


def purge_stale_sandboxes(
    prefix: str | tuple[str, ...],
    *,
    max_age_hours: float = DEFAULT_MAX_AGE_HOURS,
    tmp_root: Path | None = None,
) -> list[Path]:
    """Remove orphaned sandbox dirs matching ``prefix`` under /tmp.

    Returns the list of paths that were removed (best-effort; a removal
    failure for one entry does not stop the sweep).
    """
    prefixes = (prefix,) if isinstance(prefix, str) else prefix
    stale = find_stale_sandboxes(tmp_root, prefixes, max_age_hours=max_age_hours)
    for entry in stale:
        shutil.rmtree(entry, ignore_errors=True)
    return stale
