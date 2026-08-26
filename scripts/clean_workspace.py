"""Remove disposable files that accumulate in the repository root.

Only root-level ``_*`` entries and legacy root ``*.log`` files are removed by
default. Use ``--caches`` to also clear reproducible tool caches/build output.

Also sweeps ``/tmp`` for ``manju-pytest-*`` and ``manju-verify-*`` sandboxes
left behind by hard-killed ``pytest`` / ``scripts/verify.py`` runs. Staleness
is judged primarily by process ownership (see ``scripts/sandbox_lifecycle``):
a sandbox is only removed once the PID recorded in its ``owner.pid`` marker
is no longer alive, so one still owned by a currently running process is
never touched, regardless of how new or old it is. An age cutoff is only a
fallback for sandboxes with no readable marker.
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.sandbox_lifecycle import DEFAULT_MAX_AGE_HOURS, find_stale_sandboxes  # noqa: E402

CACHE_PATHS = (
    ROOT / ".pytest_cache",
    ROOT / ".ruff_cache",
    ROOT / "frontend" / "dist",
)


def disposable_paths(include_caches: bool = False) -> list[Path]:
    paths = [
        path
        for path in ROOT.iterdir()
        if path.name.startswith("_") or (path.is_file() and path.suffix.lower() == ".log")
    ]
    if include_caches:
        paths.extend(path for path in CACHE_PATHS if path.exists())
    return sorted(set(paths), key=lambda path: str(path).lower())


def _assert_safe(path: Path) -> None:
    resolved = path.resolve()
    root = ROOT.resolve()
    if resolved == root or (resolved.parent != root and resolved not in {item.resolve() for item in CACHE_PATHS}):
        raise RuntimeError(f"refusing to remove unexpected path: {resolved}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--caches", action="store_true", help="also remove reproducible caches and frontend/dist")
    parser.add_argument("--dry-run", action="store_true", help="show targets without removing them")
    args = parser.parse_args()

    paths = disposable_paths(args.caches)
    tmp_paths = find_stale_sandboxes()
    if not paths and not tmp_paths:
        print("Workspace is already clean.")
        return 0
    for path in paths:
        _assert_safe(path)
        print(f"{'would remove' if args.dry_run else 'removing'} {path.relative_to(ROOT)}")
        if args.dry_run:
            continue
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)
    for path in tmp_paths:
        print(f"{'would remove' if args.dry_run else 'removing'} {path} (orphaned; owner dead or >{DEFAULT_MAX_AGE_HOURS:.0f}h with no owner marker)")
        if args.dry_run:
            continue
        shutil.rmtree(path, ignore_errors=True)
    total = len(paths) + len(tmp_paths)
    print(f"{'Found' if args.dry_run else 'Removed'} {total} disposable item(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
