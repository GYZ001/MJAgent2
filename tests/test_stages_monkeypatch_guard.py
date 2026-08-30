"""Guard against the app.stages package-split monkeypatch trap.

``app/stages.py`` used to be one file; every call site inside it shared a
single module namespace, so ``monkeypatch.setattr(stages, "name", value)``
(or the string form ``monkeypatch.setattr("app.stages.name", value)``, or a
direct ``stages.name = value`` assignment) reached every caller. It was
split into the ``app.stages`` package (see ``app/stages/__init__.py``); each
of the 33 submodules now holds its own copy of any name it imported (``from
.common import get_conn`` and friends). Patching only the package-level
re-export no longer reaches the submodule that actually calls the name --
and there is no exception, no error, nothing: the patch silently no-ops and
the test keeps passing while validating a code path that was never mocked.

Two real, previously-passing instances of exactly this were found and fixed
(see git history around this file's introduction):
``tests/test_run_3fb9353fed4b_voice_review.py`` patched ``get_conn``,
``get_setting`` and ``_repair_narrative_blueprint`` on the bare package
object -- all three are bound in multiple submodules (including the one
actually calling them), so none of the three fakes were ever hit; the real
``app.db`` connection and settings lookup ran underneath the test the whole
time. ``tests/test_scene_routes.py`` used the string form on
``generate_scene_bible``.

The fix is ``tests/conftest.py``'s ``patch_stages_everywhere(monkeypatch,
name, value)`` -- it walks every ``app.stages`` submodule and patches
``name`` wherever it is actually bound, reproducing the pre-split
single-namespace patch semantics. This test scans every file under
``tests/`` for the bare forms and fails if any turn up outside
``patch_stages_everywhere``'s own implementation (which *is* the one place
allowed to touch the package/submodules directly -- that is what
"everywhere" means).

Note what this test does *not* flag, on purpose: ``stages.model_gateway``
and ``stages.time`` are shared singleton module objects (``app.harness.
model_gateway`` and stdlib ``time``) reached via ``from ... import
model_gateway`` / ``import time`` in every submodule that uses them --
patching an attribute *on the module object itself*
(``monkeypatch.setattr(stages.model_gateway, "chat", fake)``) mutates that
one shared object and is visible to every submodule regardless of how many
of them hold a reference to it, so it was never affected by the package
split. Only patching a *name* re-exported by the package (a function/class/
constant copied by value into each importer's namespace) is broken by the
split. The AST check below distinguishes the two: ``stages.foo`` (an
``Attribute`` node) is never flagged, only bare ``stages`` (a ``Name`` node)
or an ``"app.stages.<single identifier>"`` string.
"""
from __future__ import annotations

import ast
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
CONFTEST_PATH = TESTS_DIR / "conftest.py"
HELPER_NAME = "patch_stages_everywhere"


def _helper_exempt_span(tree: ast.Module) -> tuple[int, int]:
    """Line range of patch_stages_everywhere's own body in conftest.py.

    This is the sole legitimate place a bare ``setattr(stages, name, value)``
    / ``stages.<x> = ...`` may exist -- it *is* the everywhere-walk. Scoping
    the exemption to this function's own lineno..end_lineno (not the whole
    file) means any *other* helper later added to conftest.py is still
    checked by this guard.
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


def _is_bare_stages_name(node: ast.expr) -> bool:
    return isinstance(node, ast.Name) and node.id == "stages"


def _is_bare_stages_attr_string(node: ast.expr) -> bool:
    """True for ``"app.stages.<single identifier>"``.

    Not ``"app.stages"`` itself (patching the module object as a whole is a
    different, rarer operation) and not a deeper dotted path such as
    ``"app.stages.model_gateway.chat"`` -- that patches a real shared module
    object's attribute (see module docstring), which the package split never
    broke.
    """
    if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
        return False
    parts = node.value.split(".")
    return (
        len(parts) == 3
        and parts[0] == "app"
        and parts[1] == "stages"
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
                if _is_bare_stages_name(target):
                    violations.append(
                        f"{path}:{node.lineno}: bare stages-package attribute "
                        "patch (monkeypatch.setattr(stages, ...) / "
                        "patch.object(stages, ...)) only reaches app.stages's "
                        "own re-export -- app/stages is a real package now and "
                        "every submodule holds its own copy of any imported "
                        "name, so this silently patches nothing the real call "
                        "site sees. Use "
                        "tests.conftest.patch_stages_everywhere(monkeypatch, "
                        "name, value) instead."
                    )
            # monkeypatch.setattr("app.stages.name", value) resolves the
            # dotted string to (module, attr) internally -- same package-only
            # reach as the object form above. mock.patch("app.stages.name")
            # (bare `patch(...)` or `mock.patch(...)`) takes the same string
            # shape, so both call forms are checked against the same target.
            if (is_setattr_call or is_patch_call) and node.args:
                target = node.args[0]
                if _is_bare_stages_attr_string(target):
                    violations.append(
                        f"{path}:{node.lineno}: string-form patch on "
                        f"{ast.literal_eval(target)!r} only reaches "
                        "app.stages's own re-export, not the submodule that "
                        "actually binds the name -- same silent no-op as the "
                        "object form. Use "
                        "tests.conftest.patch_stages_everywhere(monkeypatch, "
                        "name, value) instead."
                    )

        if isinstance(node, ast.Assign):
            for assign_target in node.targets:
                if (
                    isinstance(assign_target, ast.Attribute)
                    and isinstance(assign_target.value, ast.Name)
                    and assign_target.value.id == "stages"
                ):
                    violations.append(
                        f"{path}:{node.lineno}: direct assignment "
                        f"stages.{assign_target.attr} = ... only rebinds the "
                        "package attribute, not the submodule-owned copy of "
                        "the name -- same silent no-op. Use "
                        "tests.conftest.patch_stages_everywhere(monkeypatch, "
                        "name, value) instead."
                    )

    return violations


def test_no_bare_app_stages_package_monkeypatch() -> None:
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
