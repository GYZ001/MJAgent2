"""Guard against scripts/ silently no-op'ing on a hardcoded, deleted app/ path.

Several guard scripts (``scripts/check_contract_surface.py``,
``scripts/verify.py``) hardcode string literals shaped like
``"app/some/module.py"`` to name the exact file whose content they must
inspect. When that module later gets split into a package (as happened to
``app/stages.py`` -> ``app.stages``, ``app/portraits.py`` -> ``app.portraits``,
``app/screenplay_scene_shards.py`` -> ``app.screenplay_scene_shards``,
``app/validators.py`` -> ``app.validators`` and
``app/production/screenplay_repair.py`` -> ``app.production.screenplay_repair``
on 2026-08-29, docs/coupling_review_2026-08-29.md C5), the literal keeps
pointing at a path that no longer exists.

The failure mode is silent and split by call site, which is what makes it
dangerous:
  * ``check_contract_surface.py``'s ``REQUIRED`` dict raises ``SystemExit``
    for a missing required path -- loud, but only caught by whoever happens
    to run the script.
  * ``check_contract_surface.py``'s ``FORBIDDEN`` dict does
    ``if not path.exists(): continue`` -- the check for that module's legacy
    tokens silently stops running, with no error at all, while everyone
    still believes the contract surface is being enforced.
  * ``check_version_bump_discipline()``'s ``REUSE_GUARD_ANCHORS`` behaves the
    same way (``if not (ROOT / guard_file).exists(): continue``).

This test scans every ``*.py`` file under ``scripts/`` for string literals
whose *entire* value looks like an ``app/....py`` path (not a substring
inside a longer prose sentence -- see ``_APP_PY_PATH_RE``'s full-string
anchors) and asserts each one exists on disk. It is derived purely from
the source tree on every run, not from a maintained list of "known guard
paths" -- any future guard script that hardcodes a stale ``app/*.py``
literal, in any dict/set/tuple shape, trips this the same way.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"

# Whole-string match on purpose: a literal like "app/screenplay_scene_shards.py"
# used as a dict key/set element *is* a functional reference a guard reads off
# disk. A big triple-quoted docstring that merely *mentions* a path among
# other prose words (e.g. "``app/portraits.py`` cite exact row ids...") is a
# single much-longer string constant whose value is not equal to just the
# path, so it never matches this pattern -- intentional: prose isn't a guard.
_APP_PY_PATH_RE = re.compile(r"^app/[\w./-]+\.py$")


def _hardcoded_app_paths(path: Path) -> list[tuple[int, str]]:
    """(lineno, literal) for every whole string literal shaped like app/....py."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if _APP_PY_PATH_RE.match(node.value):
                found.append((node.lineno, node.value))
    return found


def test_scripts_hardcoded_app_paths_exist() -> None:
    script_files = sorted(
        path for path in SCRIPTS_DIR.rglob("*.py") if "__pycache__" not in path.parts
    )
    # Empty scan scope must fail, not silently read as "nothing to check" --
    # a moved/renamed scripts/ directory, or a CI working-directory mixup, is
    # a real way this guard could stop scanning anything and go green for
    # the wrong reason.
    assert script_files, f"no .py files found under {SCRIPTS_DIR} -- scan scope is empty"

    missing: list[str] = []
    for script in script_files:
        for lineno, literal in _hardcoded_app_paths(script):
            if not (ROOT / literal).exists():
                missing.append(
                    f"{script.relative_to(ROOT)}:{lineno}: hardcoded path {literal!r} "
                    "does not exist on disk -- this guard has gone stale, most likely "
                    "because the module it names was split into a package (or "
                    "renamed/deleted). Update it to point at the post-split location; "
                    "prefer a directory pathspec or a prefix/rglob scan over adding "
                    "another single hardcoded file, or the same break will recur the "
                    "next time this module is reshuffled."
                )

    assert missing == [], "\n".join(missing)
