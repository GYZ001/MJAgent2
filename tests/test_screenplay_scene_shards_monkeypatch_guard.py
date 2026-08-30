"""Guard against the app.screenplay_scene_shards package-split monkeypatch trap.

``app/screenplay_scene_shards.py`` used to be one file; every call site
inside it shared a single module namespace, so
``monkeypatch.setattr(screenplay_scene_shards, "name", value)`` (or the
string form ``monkeypatch.setattr("app.screenplay_scene_shards.name",
value)``, or a direct ``screenplay_scene_shards.name = value`` assignment)
reached every caller. It was split into the ``app.screenplay_scene_shards``
package (see ``app/screenplay_scene_shards/__init__.py``); each of the 20
submodules now holds its own copy of any name it imported. Patching only
the package-level re-export no longer reaches the submodule that actually
calls the name -- and there is no exception, no error, nothing: the patch
silently no-ops and the test keeps passing while validating a code path
that was never mocked. This is the same trap that
``tests/test_stages_monkeypatch_guard.py`` and
``tests/test_validators_monkeypatch_guard.py`` guard against for their own
package splits.

The fix is ``tests/conftest.py``'s
``patch_screenplay_scene_shards_everywhere(monkeypatch, name, value)`` -- it
walks every ``app.screenplay_scene_shards`` submodule and patches ``name``
wherever it is actually bound, reproducing the pre-split single-namespace
patch semantics. ``tests/test_screenplay_scene_shards.py`` already routes
all 16 of its call-site patches through this helper (imported there as
``_patch_scene_shards``). This test scans every file under ``tests/`` for
the bare forms and fails if any turn up outside
``patch_screenplay_scene_shards_everywhere``'s own implementation (which
*is* the one place allowed to touch the package/submodules directly --
that is what "everywhere" means).

Note what this test does *not* flag, on purpose: an attribute access on a
shared singleton module object reached via the screenplay_scene_shards
package (e.g. ``screenplay_scene_shards.model_gateway`` /
``"app.screenplay_scene_shards.model_gateway.chat_structured"``, both a
plain ``from app.harness import model_gateway`` re-export) is never
affected by the package split -- patching an attribute *on that shared
object itself* mutates the one object every submodule holds a reference
to. Only patching a *name* re-exported by the package (a function/constant
copied by value into each importer's namespace) is broken by the split.
The AST check below distinguishes the two: ``screenplay_scene_shards.foo``
(an ``Attribute`` node) is never flagged, only a bare
``screenplay_scene_shards``/``scene_shards_module`` (a ``Name`` node) or an
``"app.screenplay_scene_shards.<single identifier>"`` string (exactly three
dotted parts, not the four-part ``...model_gateway.chat_structured`` form
used throughout ``tests/test_screenplay_scene_shards.py``).

This also catches the loop-variable form of the same bug: ``for module in
(a, b, screenplay_scene_shards): monkeypatch.setattr(module, name, value)``.
A purely Call-shaped scan only ever sees the loop variable (``module``) as
the first argument to ``setattr``, never the literal name
``screenplay_scene_shards``, so the bare-package trap slips straight past
it on the iteration where the loop variable happens to be the package
itself. A repo-wide sample audit (2026-08-30) found 14 real instances of
this general loop shape across ``tests/*.py`` targeting other modules
(``app.evidence.repository``, ``app.orchestration.engine``, single-file
modules like ``app.system_api``/``app.monitoring``/``app.video_plan``,
etc.) -- all of them already name a specific submodule or an unsplit flat
file, never the bare ``app.screenplay_scene_shards``/other split-package
object, so none were live hazards. This branch exists so a *future*
instance naming this package specifically does not become an invisible
one.
"""
from __future__ import annotations

import ast
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
CONFTEST_PATH = TESTS_DIR / "conftest.py"
HELPER_NAME = "patch_screenplay_scene_shards_everywhere"

# Bare local names that test files use for ``app.screenplay_scene_shards`` /
# ``from app import screenplay_scene_shards``. The guard has to know every
# alias in use, the same way ``patch_validators_everywhere``'s guard tracks
# both ``validators`` and ``validators_module`` -- this package is imported
# both bare (``screenplay_scene_shards``) and aliased
# (``as scene_shards_module`` in tests/test_screenplay_scene_shards.py).
BARE_NAME_ALIASES = {"screenplay_scene_shards", "scene_shards_module"}


def _helper_exempt_span(tree: ast.Module) -> tuple[int, int]:
    """Line range of patch_screenplay_scene_shards_everywhere's own body.

    This is the sole legitimate place a bare
    ``setattr(screenplay_scene_shards, name, value)`` /
    ``screenplay_scene_shards.<x> = ...`` may exist -- it *is* the
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


def _is_bare_shards_name(node: ast.expr) -> bool:
    return isinstance(node, ast.Name) and node.id in BARE_NAME_ALIASES


def _is_bare_shards_attr_string(node: ast.expr) -> bool:
    """True for ``"app.screenplay_scene_shards.<single identifier>"``.

    Not ``"app.screenplay_scene_shards"`` itself (patching the module
    object as a whole is a different, rarer operation) and not a deeper
    dotted path such as
    ``"app.screenplay_scene_shards.model_gateway.chat_structured"`` --
    that patches a real shared module object's attribute (see module
    docstring), which the package split never broke.
    """
    if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
        return False
    parts = node.value.split(".")
    return (
        len(parts) == 3
        and parts[0] == "app"
        and parts[1] == "screenplay_scene_shards"
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
                if _is_bare_shards_name(target):
                    violations.append(
                        f"{path}:{node.lineno}: bare screenplay_scene_shards-package "
                        "attribute patch (monkeypatch.setattr(screenplay_scene_shards, "
                        "...) / patch.object(screenplay_scene_shards, ...)) only "
                        "reaches app.screenplay_scene_shards's own re-export -- "
                        "app/screenplay_scene_shards is a real package now and every "
                        "submodule holds its own copy of any imported name, so this "
                        "silently patches nothing the real call site sees. Use "
                        "tests.conftest.patch_screenplay_scene_shards_everywhere("
                        "monkeypatch, name, value) instead."
                    )
            # monkeypatch.setattr("app.screenplay_scene_shards.name", value)
            # resolves the dotted string to (module, attr) internally -- same
            # package-only reach as the object form above.
            # mock.patch("app.screenplay_scene_shards.name") (bare `patch(...)`
            # or `mock.patch(...)`) takes the same string shape, so both call
            # forms are checked against the same target.
            if (is_setattr_call or is_patch_call) and node.args:
                target = node.args[0]
                if _is_bare_shards_attr_string(target):
                    violations.append(
                        f"{path}:{node.lineno}: string-form patch on "
                        f"{ast.literal_eval(target)!r} only reaches "
                        "app.screenplay_scene_shards's own re-export, not the "
                        "submodule that actually binds the name -- same silent "
                        "no-op as the object form. Use "
                        "tests.conftest.patch_screenplay_scene_shards_everywhere("
                        "monkeypatch, name, value) instead."
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
                        "tests.conftest.patch_screenplay_scene_shards_everywhere("
                        "monkeypatch, name, value) instead."
                    )

        # Loop-variable form: ``for module in (a, b, screenplay_scene_shards):
        # monkeypatch.setattr(module, name, value)``. The Call-node checks
        # above only see the literal Name passed to setattr -- here that's
        # the loop variable (``module``), not ``screenplay_scene_shards``,
        # so the same bare-package trap slips past a purely Call-shaped scan.
        # Confirmed real instances of this *pattern* exist elsewhere in the
        # suite (e.g. tests/test_video_providers.py, tests/test_media_job_
        # recovery.py) targeting other, unsplit single-file modules where
        # it's harmless; this closes the gap for when the tuple names this
        # package specifically.
        if isinstance(node, ast.For) and isinstance(node.target, ast.Name):
            loop_var = node.target.id
            iterable = node.iter
            if isinstance(iterable, (ast.Tuple, ast.List)) and any(
                isinstance(elt, ast.Name) and elt.id in BARE_NAME_ALIASES
                for elt in iterable.elts
            ):
                for inner in ast.walk(ast.Module(body=node.body, type_ignores=[])):
                    if not isinstance(inner, ast.Call):
                        continue
                    inner_func = inner.func
                    inner_is_setattr = (
                        isinstance(inner_func, ast.Attribute) and inner_func.attr in ("setattr", "object")
                    ) or (isinstance(inner_func, ast.Name) and inner_func.id == "setattr")
                    if not (inner_is_setattr and inner.args):
                        continue
                    inner_target = inner.args[0]
                    if isinstance(inner_target, ast.Name) and inner_target.id == loop_var:
                        violations.append(
                            f"{path}:{inner.lineno}: loop-variable patch "
                            f"(for {loop_var} in (...)) iterates over a tuple that "
                            "includes the bare screenplay_scene_shards package "
                            f"object, then calls setattr({loop_var}, ...) -- on the "
                            "iteration where the loop variable is the package "
                            "itself this is the same silent no-op as a direct bare "
                            "patch. Use tests.conftest."
                            "patch_screenplay_scene_shards_everywhere(monkeypatch, "
                            "name, value) instead."
                        )

    return violations


def test_no_bare_app_screenplay_scene_shards_package_monkeypatch() -> None:
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
