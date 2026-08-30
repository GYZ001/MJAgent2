"""Guard against the app.validators package-split monkeypatch trap.

``app/validators.py`` used to be one file; every call site inside it shared a
single module namespace, so ``monkeypatch.setattr(validators, "name", value)``
(or the string form ``monkeypatch.setattr("app.validators.name", value)``, or
a direct ``validators.name = value`` assignment) reached every caller. It was
split into the ``app.validators`` package (see ``app/validators/__init__.py``,
docs/coupling_review_2026-08-29.md C5); each submodule now holds its own copy
of any name it imported (``from .storyboard_delivery import
_spine_delivery_clauses`` and friends). Patching only the package-level
re-export no longer reaches the submodule that actually calls the name -- and
there is no exception, no error, nothing: the patch silently no-ops and the
test keeps passing while validating a code path that was never mocked.

Two real, pre-split instances of exactly this shape were found in the test
suite when the package was introduced (see git history around this file's
introduction): ``tests/test_video_completion_authority.py`` patched
``validate_storyboard`` / ``validate_storyboard_continuity_contract`` on the
bare ``app.validators`` module object, and
``tests/test_narrative_action_capacity.py`` patched
``prefer_default_shot_durations`` / ``validate_storyboard_screenplay_scene_
alignment`` the same way. Both happened to keep working after the split only
because their real call sites (``app/video_supervisor.py``,
``app/domain/video_ops.py``) resolve the name through a deferred
``from app.validators import ...`` done at call time, which re-reads the
package's current attribute -- but that is incidental, not guaranteed, and a
call site that instead imports a name from a *sibling submodule* at module
top level (as ``app/validators/screenplay_validate.py`` does for
``_spine_delivery_clauses`` from ``storyboard_delivery.py``) would silently
miss a package-level-only patch. Both files were converted to use
``patch_validators_everywhere`` so the guard below has no legitimate
exceptions to carry.

The fix is ``tests/conftest.py``'s ``patch_validators_everywhere(monkeypatch,
name, value)`` -- it walks every ``app.validators`` submodule and patches
``name`` wherever it is actually bound, reproducing the pre-split
single-namespace patch semantics. This test scans every file under
``tests/`` for the bare forms and fails if any turn up outside
``patch_validators_everywhere``'s own implementation (which *is* the one
place allowed to touch the package/submodules directly -- that is what
"everywhere" means).

Note what this test does *not* flag, on purpose: an attribute access on a
shared singleton module object reached via the validators package (e.g.
``validators.config`` / ``validators.textmatch``, both plain ``from app
import config, textmatch`` re-exports) is never affected by the package
split -- patching an attribute *on that shared object itself*
(``monkeypatch.setattr(validators.config, "SOME_FLAG", value)``) mutates the
one object every submodule holds a reference to. Only patching a *name*
re-exported by the package (a function/constant copied by value into each
importer's namespace) is broken by the split. The AST check below
distinguishes the two: ``validators.foo`` (an ``Attribute`` node) is never
flagged, only bare ``validators`` (a ``Name`` node) or an
``"app.validators.<single identifier>"`` string.
"""
from __future__ import annotations

import ast
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
CONFTEST_PATH = TESTS_DIR / "conftest.py"
HELPER_NAME = "patch_validators_everywhere"

# Bare local names that test files use for ``app.validators`` / ``app import
# validators``. The guard has to know every alias in use, the same way
# ``_is_bare_stages_name`` only had to know one (``stages``) because every
# stages test consistently imports it that way; validators tests use two
# different aliases in the wild (``validators`` and ``validators_module``).
BARE_NAME_ALIASES = {"validators", "validators_module"}


def _helper_exempt_span(tree: ast.Module) -> tuple[int, int]:
    """Line range of patch_validators_everywhere's own body in conftest.py.

    This is the sole legitimate place a bare ``setattr(validators, name,
    value)`` / ``validators.<x> = ...`` may exist -- it *is* the
    everywhere-walk. Scoping the exemption to this function's own
    lineno..end_lineno (not the whole file) means any *other* helper later
    added to conftest.py is still checked by this guard.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == HELPER_NAME:
            assert node.end_lineno is not None
            return node.lineno, node.end_lineno
    raise AssertionError(
        f"{HELPER_NAME}() not found in {CONFTEST_PATH} -- this guard's exemption "
        "span cannot be computed. Did the helper get renamed or removed? Update "
        "HELPER_NAME here to match, don't just skip the scan."
    )


def _is_bare_validators_name(node: ast.expr) -> bool:
    return isinstance(node, ast.Name) and node.id in BARE_NAME_ALIASES


def _is_bare_validators_attr_string(node: ast.expr) -> bool:
    """True for ``"app.validators.<single identifier>"``.

    Not ``"app.validators"`` itself (patching the module object as a whole is
    a different, rarer operation) and not a deeper dotted path such as
    ``"app.validators.config.SOME_FLAG"`` -- that patches a real shared
    module object's attribute (see module docstring), which the package
    split never broke.
    """
    if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
        return False
    parts = node.value.split(".")
    return (
        len(parts) == 3
        and parts[0] == "app"
        and parts[1] == "validators"
        and parts[2].isidentifier()
    )


def _violations_in_file(path: Path) -> list[str]:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))

    exempt_start, exempt_end = (-1, -1)
    if path == CONFTEST_PATH:
        exempt_start, exempt_end = _helper_exempt_span(tree)

    violations: list[str] = []
    for node in ast.walk(tree):
        lineno = getattr(node, "lineno", None)
        if lineno is not None and exempt_start <= lineno <= exempt_end:
            continue

        if isinstance(node, ast.Call):
            func = node.func
            is_setattr_call = (
                isinstance(func, ast.Attribute) and func.attr == "setattr"
            ) or (isinstance(func, ast.Name) and func.id == "setattr")
            is_patch_object_call = (
                isinstance(func, ast.Attribute) and func.attr == "object"
            )
            is_patch_call = (
                isinstance(func, ast.Attribute) and func.attr == "patch"
            ) or (isinstance(func, ast.Name) and func.id == "patch")

            if (is_setattr_call or is_patch_object_call) and node.args:
                target = node.args[0]
                if _is_bare_validators_name(target):
                    violations.append(
                        f"{path}:{node.lineno}: bare validators-package attribute "
                        "patch (monkeypatch.setattr(validators, ...) / "
                        "patch.object(validators, ...)) only reaches "
                        "app.validators's own re-export -- app/validators is a "
                        "real package now and every submodule holds its own copy "
                        "of any imported name, so this silently patches nothing "
                        "the real call site sees. Use "
                        "tests.conftest.patch_validators_everywhere(monkeypatch, "
                        "name, value) instead."
                    )
            # monkeypatch.setattr("app.validators.name", value) resolves the
            # dotted string to (module, attr) internally -- same package-only
            # reach as the object form above. mock.patch("app.validators.name")
            # (bare `patch(...)` or `mock.patch(...)`) takes the same string
            # shape, so both call forms are checked against the same target.
            if (is_setattr_call or is_patch_call) and node.args:
                target = node.args[0]
                if _is_bare_validators_attr_string(target):
                    violations.append(
                        f"{path}:{node.lineno}: string-form patch on "
                        f"{ast.literal_eval(target)!r} only reaches "
                        "app.validators's own re-export, not the submodule that "
                        "actually binds the name -- same silent no-op as the "
                        "object form. Use "
                        "tests.conftest.patch_validators_everywhere(monkeypatch, "
                        "name, value) instead."
                    )

        if isinstance(node, ast.Assign):
            for assign_target in node.targets:
                if (
                    isinstance(assign_target, ast.Attribute)
                    and isinstance(assign_target.value, ast.Name)
                    and assign_target.value.id in BARE_NAME_ALIASES
                ):
                    violations.append(
                        f"{path}:{node.lineno}: direct assignment "
                        f"{assign_target.value.id}.{assign_target.attr} = ... only "
                        "rebinds the package attribute, not the submodule-owned "
                        "copy of the name -- same silent no-op. Use "
                        "tests.conftest.patch_validators_everywhere(monkeypatch, "
                        "name, value) instead."
                    )

    return violations


def test_no_bare_app_validators_package_monkeypatch() -> None:
    test_files = sorted(TESTS_DIR.glob("*.py"))
    # Empty scan scope must fail, not silently read as "nothing to report" --
    # a moved/renamed tests/ directory, or a CI working-directory mixup, is a
    # real way this guard could stop scanning anything and go green for the
    # wrong reason.
    assert test_files, f"no .py files found under {TESTS_DIR} -- scan scope is empty"
    assert CONFTEST_PATH in test_files, "expected tests/conftest.py in scan scope"

    violations: list[str] = []
    for path in test_files:
        violations.extend(_violations_in_file(path))

    assert violations == [], "\n".join(violations)
