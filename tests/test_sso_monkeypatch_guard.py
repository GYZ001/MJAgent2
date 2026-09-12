"""Guard against the app.sso package-split monkeypatch trap.

``app/sso`` (EP-02 第一阶段 OIDC 单点登录内核，2026-09-11 新增) is a real
package from day one -- ``schema.py``/``store.py``/``profiles.py``/
``oidc_verify.py``/``oidc.py``/``provision.py``/``api.py``/``admin_api.py``
each hold their own module namespace, and ``__init__.py`` deliberately
imports none of them (see its docstring: importing ``app.sso.api``/``app.sso.
admin_api`` (L5) at package level would leak onto ``app.sso``'s own L2 layer
declaration). Every production call site is written to reach it via
module-qualified access (``from app.sso import store as sso_store`` then
``sso_store.get_idp(...)``), which is *not* the ``from .x import y``
name-copy trap -- an attribute lookup on the same module object at call time
sees a patch applied directly to that module regardless of how many local
aliases point at it. But the mandate to add ``tests/patch_targets.py::
patch_sso_everywhere`` and this guard applies unconditionally, the same as
every other package split in this repo (15 precedents before this one,
``tests/test_provisioning_monkeypatch_guard.py`` from EP-03 is the closest
template and this file mirrors its structure almost exactly).

The fix is ``tests/patch_targets.py``'s ``patch_sso_everywhere(monkeypatch,
name, value)`` -- it walks ``app.sso.schema``/``.store``/``.profiles``/
``.oidc_verify``/``.oidc``/``.provision``/``.api``/``.admin_api`` and patches
``name`` wherever it is actually bound. This test scans every file under
``tests/`` for bare-module patch attempts on those eight submodules and fails
if any turn up outside ``patch_sso_everywhere``'s own implementation.

Deliberate scope narrowing (same reasoning as the orgs/provisioning guards):
``store``/``oidc``/``provision`` are common-ish words with a non-trivial
chance of legitimately referring to something else in a large test suite (a
local variable named ``store``, an unrelated ``oidc`` fixture, etc.) --
failing closed on an unresolvable bare name would manufacture false
positives unrelated to this package. This guard therefore only flags a bare
name when the file's own import statements resolve it to
``app.sso.<submodule>`` specifically (see ``_sso_aliases``); an unresolvable
bare name is not scanned. The dotted-string form (``"app.sso.store.<attr>"``)
has no such ambiguity, so that check stays fail-closed regardless of imports.

Known blind spots (same residual gaps as the other package-split guards this
one is copied from): the loop-variable form (``for m in (a, b, store):
monkeypatch.setattr(m, "name", value)``) is not traced here; and a patch call
spelled inside a string literal handed to ``subprocess.run([sys.executable,
"-c", "..."])`` is invisible to any AST scan of the enclosing file.
"""
from __future__ import annotations

import ast
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
CONFTEST_PATH = TESTS_DIR / "conftest.py"
PATCH_TARGETS_PATH = TESTS_DIR / "patch_targets.py"
HELPER_NAME = "patch_sso_everywhere"

SUBMODULES = ("schema", "store", "profiles", "oidc_verify", "oidc", "provision", "api", "admin_api")
FULL_NAMES = {f"app.sso.{sub}" for sub in SUBMODULES}


def _helper_exempt_span(tree: ast.Module) -> tuple[int, int]:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == HELPER_NAME:
            assert node.end_lineno is not None
            return node.lineno, node.end_lineno
    raise AssertionError(
        f"{HELPER_NAME}() not found in {PATCH_TARGETS_PATH} -- this guard's exemption "
        "span cannot be computed. Did the helper get renamed or removed? Update "
        "HELPER_NAME here to match, don't just skip the scan."
    )


def _sso_aliases(tree: ast.Module) -> dict[str, str]:
    """Local names in this file that resolve to ``app.sso.<submodule>``."""
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in FULL_NAMES and alias.asname:
                    aliases[alias.asname] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.module == "app.sso":
            for alias in node.names:
                if alias.name in SUBMODULES:
                    aliases[alias.asname or alias.name] = f"app.sso.{alias.name}"
    return aliases


def _is_bare_sso_attr_string(node: ast.expr) -> bool:
    if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
        return False
    parts = node.value.split(".")
    return (
        len(parts) == 4
        and parts[0] == "app"
        and parts[1] == "sso"
        and parts[2] in SUBMODULES
        and parts[3].isidentifier()
    )


def _violations_in_file(path: Path) -> list[str]:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))

    exempt_start, exempt_end = (-1, -1)
    if path == PATCH_TARGETS_PATH:
        exempt_start, exempt_end = _helper_exempt_span(tree)
    aliases = _sso_aliases(tree)

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
            is_patch_object_call = isinstance(func, ast.Attribute) and func.attr == "object"
            is_patch_call = (
                isinstance(func, ast.Attribute) and func.attr == "patch"
            ) or (isinstance(func, ast.Name) and func.id == "patch")

            if (is_setattr_call or is_patch_object_call) and node.args:
                target = node.args[0]
                if isinstance(target, ast.Name) and target.id in aliases:
                    violations.append(
                        f"{path}:{node.lineno}: bare app.sso submodule "
                        f"attribute patch (resolves to {aliases[target.id]!r}) "
                        "only reaches that one submodule's own binding -- use "
                        "tests.conftest.patch_sso_everywhere(monkeypatch, "
                        "name, value) instead."
                    )
            if (is_setattr_call or is_patch_call) and node.args:
                target = node.args[0]
                if _is_bare_sso_attr_string(target):
                    violations.append(
                        f"{path}:{node.lineno}: string-form patch on "
                        f"{ast.literal_eval(target)!r} only reaches that one "
                        "submodule's own binding, not any sibling submodule "
                        "that may hold an independent copy of the same name "
                        "-- use tests.conftest.patch_sso_everywhere("
                        "monkeypatch, name, value) instead."
                    )

        if isinstance(node, ast.Assign):
            for assign_target in node.targets:
                if (
                    isinstance(assign_target, ast.Attribute)
                    and isinstance(assign_target.value, ast.Name)
                    and assign_target.value.id in aliases
                ):
                    violations.append(
                        f"{path}:{node.lineno}: direct assignment "
                        f"{assign_target.value.id}.{assign_target.attr} = ... "
                        f"(resolves to {aliases[assign_target.value.id]!r}) "
                        "only rebinds that one submodule's attribute -- use "
                        "tests.conftest.patch_sso_everywhere(monkeypatch, "
                        "name, value) instead."
                    )

    return violations


def test_no_bare_app_sso_submodule_monkeypatch() -> None:
    test_files = sorted(TESTS_DIR.glob("*.py"))
    assert test_files, f"no .py files found under {TESTS_DIR} -- scan scope is empty"
    assert CONFTEST_PATH in test_files, "expected tests/conftest.py in scan scope"

    violations: list[str] = []
    for path in test_files:
        violations.extend(_violations_in_file(path))

    assert violations == [], "\n".join(violations)


def _scan_source(tmp_path: Path, source: str) -> list[str]:
    path = tmp_path / "sample_test.py"
    path.write_text(source, encoding="utf-8")
    return _violations_in_file(path)


def test_resolved_alias_flags_bare_object_and_assignment_forms(tmp_path: Path) -> None:
    hazard = (
        "from app.sso import store\n"
        "monkeypatch.setattr(store, 'get_idp', fake)\n"
        "store.get_idp = fake\n"
    )
    assert len(_scan_source(tmp_path, hazard)) == 2


def test_import_as_alias_is_resolved_too(tmp_path: Path) -> None:
    hazard = "import app.sso.store as sso_store\nmonkeypatch.setattr(sso_store, 'get_idp', fake)\n"
    assert len(_scan_source(tmp_path, hazard)) == 1


def test_unresolvable_bare_name_is_not_flagged(tmp_path: Path) -> None:
    unrelated = "store = {}\nmonkeypatch.setattr(store, 'x', fake)\n"
    assert _scan_source(tmp_path, unrelated) == []


def test_string_form_flagged_regardless_of_imports(tmp_path: Path) -> None:
    source = "monkeypatch.setattr('app.sso.store.get_idp', fake)\n"
    assert len(_scan_source(tmp_path, source)) == 1


def test_unrelated_submodule_attribute_access_not_flagged(tmp_path: Path) -> None:
    source = (
        "from app.sso import store\n"
        "assert store.get_idp('x') == {}\n"
    )
    assert _scan_source(tmp_path, source) == []
