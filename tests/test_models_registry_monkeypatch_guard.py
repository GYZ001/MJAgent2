"""Guard against the app.models_registry package-split monkeypatch trap.

``app/models_registry`` (EP-05 第一阶段模型库落表 + 凭据加密，2026-09-10 新增)
is a real package from day one -- ``crypto.py``/``keyprovider.py``/``store.py``/
``migration.py`` each hold their own module namespace. Every production call
site is written to reach it via module-qualified access (``from
app.models_registry import store as models_registry_store`` then
``models_registry_store.get_credential(...)``), which is *not* the ``from .x
import y`` name-copy trap -- an attribute lookup on the same module object at
call time sees a patch applied directly to that module regardless of how many
local aliases point at it. But three of the four highest-risk call sites this
package feeds (``app/hiagent.py``, ``app/video_providers.py``,
``app/system_api.py``, plus the pre-existing singular compat facade
``app/model_registry.py``) were exactly the ones the EP-05 dispatch flagged as
"拆包后 monkeypatch 静默失效" 的高危点 -- so the mandate to add
``tests/conftest.py::patch_models_registry_everywhere`` and this guard applies
unconditionally, the same as every other package split in this repo (13
precedents, ``tests/test_quota_monkeypatch_guard.py`` is the closest -- four
flat sibling modules, not an ``exec()`` facade, same shape as this package).

The fix is ``tests/conftest.py``'s ``patch_models_registry_everywhere(
monkeypatch, name, value)`` -- it walks ``app.models_registry.crypto``/
``.keyprovider``/``.store``/``.migration`` and patches ``name`` wherever it is
actually bound. This test scans every file under ``tests/`` for bare-module
patch attempts on those four submodules and fails if any turn up outside
``patch_models_registry_everywhere``'s own implementation.

Deliberate scope narrowing vs. ``test_quota_monkeypatch_guard.py`` /
``test_stages_monkeypatch_guard.py``: those two fail closed on an
unresolvable bare identifier (e.g. bare ``stages`` with no visible import is
still flagged, because "cannot prove it is something else" must not read as
safe). ``crypto``/``keyprovider``/``store``/``migration`` are common English
words with a much higher chance of legitimately referring to something else
in a 271-file test suite (a local variable literally named ``store``, an
unrelated ``keyprovider`` fixture, etc.) -- failing closed on those would
manufacture false positives across files that have nothing to do with this
package. This guard therefore only flags a bare name when the file's own
import statements resolve it to ``app.models_registry.<submodule>``
specifically (see ``_registry_aliases``); an unresolvable bare ``store`` is
not scanned. The dotted-string form (``"app.models_registry.store.<attr>"``)
has no such ambiguity -- it always names this package outright -- so that
check stays fail-closed regardless of imports, matching the other guards.

Known blind spots (same residual gaps as the other package-split guards this
one is copied from, not re-derived here): the loop-variable form (``for m in
(a, b, store): monkeypatch.setattr(m, "name", value)``) is not traced by this
file (the four names are generic enough that a literal-tuple scan would add
noise disproportionate to the risk -- there is no current in-repo instance of
this shape for ``app.models_registry``); and a patch call spelled inside a
string literal handed to ``subprocess.run([sys.executable, "-c", "..."])`` is
invisible to any AST scan of the enclosing file.
"""
from __future__ import annotations

import ast
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
CONFTEST_PATH = TESTS_DIR / "conftest.py"
PATCH_TARGETS_PATH = TESTS_DIR / "patch_targets.py"  # helper 定义搬到这里，见 tests/patch_targets.py 模块文档
HELPER_NAME = "patch_models_registry_everywhere"

SUBMODULES = (
    "crypto", "keyprovider", "store", "migration",
    "bindings", "health", "routing", "ratelimit", "purposes", "binding_migration",
    "video_confirmation",
)
FULL_NAMES = {f"app.models_registry.{sub}" for sub in SUBMODULES}


def _helper_exempt_span(tree: ast.Module) -> tuple[int, int]:
    """Line range of patch_models_registry_everywhere's own body in conftest.py.

    This is the sole legitimate place a bare ``setattr(store, name, value)``
    / ``store.<x> = ...`` targeting this package may exist -- it *is* the
    everywhere-walk. Scoping the exemption to this function's own
    lineno..end_lineno (not the whole file) means any *other* helper later
    added to conftest.py is still checked by this guard.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == HELPER_NAME:
            assert node.end_lineno is not None
            return node.lineno, node.end_lineno
    raise AssertionError(
        f"{HELPER_NAME}() not found in {PATCH_TARGETS_PATH} -- this guard's exemption "
        "span cannot be computed. Did the helper get renamed or removed? Update "
        "HELPER_NAME here to match, don't just skip the scan."
    )


def _registry_aliases(tree: ast.Module) -> dict[str, str]:
    """Local names in this file that resolve to ``app.models_registry.<sub>``.

    Handles ``import app.models_registry.store as X`` (binds ``X``),
    ``import app.models_registry.store`` (binds ``app``, not ``store`` --
    intentionally not resolved as a bare alias), and ``from app.models_registry
    import store [as X]`` (binds ``store``/``X``). Returns a map of local name
    -> full dotted submodule name, restricted to the four known submodules.
    """
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in FULL_NAMES and alias.asname:
                    aliases[alias.asname] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.module == "app.models_registry":
            for alias in node.names:
                if alias.name in SUBMODULES:
                    aliases[alias.asname or alias.name] = f"app.models_registry.{alias.name}"
    return aliases


def _is_bare_registry_attr_string(node: ast.expr) -> bool:
    """True for ``"app.models_registry.<submodule>.<identifier>"`` (4 parts).

    Always flagged regardless of what this file imports -- the string names
    the package outright, no alias resolution needed or possible.
    """
    if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
        return False
    parts = node.value.split(".")
    return (
        len(parts) == 4
        and parts[0] == "app"
        and parts[1] == "models_registry"
        and parts[2] in SUBMODULES
        and parts[3].isidentifier()
    )


def _violations_in_file(path: Path) -> list[str]:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))

    exempt_start, exempt_end = (-1, -1)
    if path == PATCH_TARGETS_PATH:
        exempt_start, exempt_end = _helper_exempt_span(tree)
    aliases = _registry_aliases(tree)

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
                        f"{path}:{node.lineno}: bare app.models_registry "
                        f"submodule attribute patch (resolves to "
                        f"{aliases[target.id]!r}) only reaches that one "
                        "submodule's own binding -- use "
                        "tests.conftest.patch_models_registry_everywhere("
                        "monkeypatch, name, value) instead."
                    )
            if (is_setattr_call or is_patch_call) and node.args:
                target = node.args[0]
                if _is_bare_registry_attr_string(target):
                    violations.append(
                        f"{path}:{node.lineno}: string-form patch on "
                        f"{ast.literal_eval(target)!r} only reaches that one "
                        "submodule's own binding, not any sibling submodule "
                        "that may hold an independent copy of the same name "
                        "-- use tests.conftest.patch_models_registry_everywhere"
                        "(monkeypatch, name, value) instead."
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
                        "tests.conftest.patch_models_registry_everywhere("
                        "monkeypatch, name, value) instead."
                    )

    return violations


def test_no_bare_app_models_registry_submodule_monkeypatch() -> None:
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


def _scan_source(tmp_path: Path, source: str) -> list[str]:
    path = tmp_path / "sample_test.py"
    path.write_text(source, encoding="utf-8")
    return _violations_in_file(path)


def test_resolved_alias_flags_bare_object_and_assignment_forms(tmp_path: Path) -> None:
    hazard = (
        "from app.models_registry import store\n"
        "monkeypatch.setattr(store, 'get_credential', fake)\n"
        "store.get_credential = fake\n"
    )
    assert len(_scan_source(tmp_path, hazard)) == 2


def test_import_as_alias_is_resolved_too(tmp_path: Path) -> None:
    hazard = "import app.models_registry.store as reg_store\nmonkeypatch.setattr(reg_store, 'get_credential', fake)\n"
    assert len(_scan_source(tmp_path, hazard)) == 1


def test_unresolvable_bare_name_is_not_flagged(tmp_path: Path) -> None:
    """Deliberate scope narrowing (see module docstring): a local variable
    literally named ``store`` with no import tying it to this package is not
    proof of the hazard, and these four words are common enough that treating
    "unresolvable" as "guilty" would flag unrelated code across the suite."""
    unrelated = "store = {}\nmonkeypatch.setattr(store, 'x', fake)\n"
    assert _scan_source(tmp_path, unrelated) == []


def test_string_form_flagged_regardless_of_imports(tmp_path: Path) -> None:
    source = "monkeypatch.setattr('app.models_registry.store.get_credential', fake)\n"
    assert len(_scan_source(tmp_path, source)) == 1


def test_unrelated_submodule_attribute_access_not_flagged(tmp_path: Path) -> None:
    """``store.foo`` read as an assertion (not the first arg to setattr) is
    never a patch attempt and must not be flagged."""
    source = (
        "from app.models_registry import store\n"
        "assert store.get_credential('x') == {}\n"
    )
    assert _scan_source(tmp_path, source) == []
