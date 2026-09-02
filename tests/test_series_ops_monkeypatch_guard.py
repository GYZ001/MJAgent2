"""Guard for the ``app.domain.series_ops`` package-split monkeypatch trap.

Unlike the 13 existing ``test_*_monkeypatch_guard.py`` files (which guard a
``patch_X_everywhere`` helper's usage in ``tests/`` after a *pre-existing*
single-file module was split into a package), ``app/domain/series_ops`` was
designed as a real package from day one specifically to avoid ever needing
that helper: every sibling submodule that a test might want to stub
(``stages``, ``merge``, ``orchestrator``, ``state``, ``tasks``, ``queue``,
``exports``) is reached exclusively via ``from . import x`` + ``x.name(...)``
attribute access, never ``from .x import name``. Because Python resolves
``x.name`` on the shared module object at *call time*, a single
``monkeypatch.setattr(x, "name", stub)`` reaches every call site in the
package -- there is no second, private copy for the patch to miss.

This guard is the structural half of that promise: it AST-scans every file
under ``app/domain/series_ops/`` and fails if any of them reintroduces
``from .stages import name`` / ``from .merge import name`` /
``from .orchestrator import name`` / ``from .state import name`` /
``from .tasks import name`` / ``from .queue import name`` /
``from .exports import name`` (a level-1 relative import that copies a
*name*, not the module, into the importer's own namespace) -- the exact
pattern that made ``patch_X_everywhere`` helpers necessary for the other
packages in this repo (see e.g. ``app/video_supervisor/__init__.py``'s
module docstring). ``from . import recovery`` (module-level) is fine
anywhere; ``__init__.py``'s own ``from .recovery import
recover_series_film_runs`` is exempt because nothing inside the package
calls back into that name -- it is a leaf export consumed only by
``app/recovery.py`` outside the package, so there is no internal call site a
patch could fail to reach.

If this test ever needs a real exemption (a genuine reason to copy a name
out of one of the four guarded modules), the fix is almost always "call
``x.name(...)`` instead", not widening this guard.
"""
from __future__ import annotations

import ast
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent.parent / "app" / "domain" / "series_ops"

# These hold state/behaviour tests stub out; they must only ever be
# reached via ``from . import <name>`` + attribute access.
GUARDED_MODULES = {"stages", "merge", "orchestrator", "state", "tasks", "queue", "exports"}


def _iter_package_files() -> list[Path]:
    return sorted(p for p in PACKAGE_DIR.glob("*.py") if p.name != "__pycache__")


def _violations_in_file(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    violations = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.level != 1:
            continue  # not a same-package relative import
        if node.module in GUARDED_MODULES:
            names = ", ".join(alias.name for alias in node.names)
            violations.append(
                f"{path.name}:{node.lineno}: `from .{node.module} import {names}` "
                f"copies a name out of a guarded module; use `from . import "
                f"{node.module}` and call `{node.module}.{names.split(',')[0].strip()}(...)` instead"
            )
    return violations


def test_series_ops_package_never_copies_names_out_of_guarded_modules() -> None:
    assert PACKAGE_DIR.is_dir(), f"expected package at {PACKAGE_DIR}"
    all_violations: list[str] = []
    for path in _iter_package_files():
        all_violations.extend(_violations_in_file(path))
    assert not all_violations, (
        "app.domain.series_ops must reach stages/merge/orchestrator/state "
        "only via module-qualified access (see this test's module docstring "
        "for why), found:\n" + "\n".join(all_violations)
    )


def test_series_ops_package_has_the_four_guarded_modules() -> None:
    """Sanity check the guard isn't silently scanning zero files."""
    names = {p.stem for p in _iter_package_files()}
    assert GUARDED_MODULES <= names
