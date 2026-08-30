"""Guard against the app.domain.projects package-split monkeypatch trap.

``app/domain/projects.py`` (1,999 lines) used to be one file; every call site
inside it shared a single module namespace, so ``monkeypatch.setattr(projects,
"name", value)`` (or the string form ``monkeypatch.setattr(
"app.domain.projects.name", value)``, or a direct ``projects.name = value``
assignment) reached every caller regardless of local alias. It was split into
the ``app.domain.projects`` package (see ``app/domain/projects/__init__.py``):
each of its 10 submodules (``constants``/``sql_helpers``/``evidence``/
``create``/``listing``/``bible_attachments``/``detail``/``episode_renumber``/
``episode_delete``/``lifecycle``) now holds its own copy of any name it
imported (``from app.db import get_conn`` and friends). Patching only the
package-level re-export no longer reaches the submodule that actually calls
the name -- and there is no exception, no error, nothing: the patch silently
no-ops and the test keeps passing while validating a code path that was
never mocked.

The fix is ``tests/conftest.py``'s ``patch_projects_everywhere(monkeypatch,
name, value)`` -- it walks every ``app.domain.projects`` submodule and
patches ``name`` wherever it is actually bound, reproducing the pre-split
single-namespace patch semantics. This test scans every file under ``tests/``
for the bare forms and fails if any turn up outside
``patch_projects_everywhere``'s own implementation (which *is* the one place
allowed to touch the package/submodules directly -- that is what
"everywhere" means).

Unlike ``tests/test_stages_monkeypatch_guard.py`` (which only recognizes the
literal name ``stages``), this guard resolves *import aliases*: this test
suite imports ``app.domain.projects`` under at least three different local
names -- ``projects`` (``from app.domain import projects``), ``projects_mod``
(``import app.domain.projects as projects_mod``), and ``projects_api``
(``from app.domain import projects as projects_api``) -- all seen in this
repo's existing tests (``test_workspace_payload_views.py``,
``test_account_deletion.py``/``test_quota.py``, ``test_core_regressions.py``
respectively). ``tests/test_api_monkeypatch_guard.py``'s generic
``SPLIT_DOMAIN_CHUNKS`` detection (which now also covers this package, since
it is computed from the live package's ``__path__``) only recognizes a local
name that is exactly ``"projects"`` -- it does not resolve renamed aliases --
so this dedicated guard closes that gap for this package specifically.

Note what this test does *not* flag, on purpose: ``projects.config`` /
``projects.worker`` / ``projects.task_registry`` are shared singleton modules
reached via ``from app import config, worker, task_registry`` in the
submodules that use them -- patching an attribute *on the module object
itself* (``monkeypatch.setattr(projects.worker, "delete_project_episodes",
fake)``) mutates that one shared object and is visible to every submodule
regardless of how many of them hold a reference to it, so it was never
affected by the package split. Only patching a *name* re-exported by the
package (a function/class/constant copied by value into each importer's
namespace) is broken by the split. The AST check below distinguishes the
two: ``projects.foo`` (an ``Attribute`` node) is never flagged, only a bare
alias (a ``Name`` node) or an ``"app.domain.projects.<single identifier>"``
string.
"""
from __future__ import annotations

import ast
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
CONFTEST_PATH = TESTS_DIR / "conftest.py"
HELPER_NAME = "patch_projects_everywhere"


def _helper_exempt_span(tree: ast.Module) -> tuple[int, int]:
    """Line range of patch_projects_everywhere's own body in conftest.py.

    This is the sole legitimate place a bare ``setattr(projects, name,
    value)`` / ``projects.<x> = ...`` may exist -- it *is* the
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


def _projects_aliases(tree: ast.Module) -> set[str]:
    """Local names in this file that refer to the ``app.domain.projects``
    package object.

    Always includes ``"projects"`` (the canonical name), plus any renamed
    alias actually bound by this file -- ``import app.domain.projects as X``
    or ``from app.domain import projects as X``. Resolving real aliases here
    (rather than hand-hardcoding ``projects_mod``/``projects_api``) means a
    future third alias is caught automatically instead of silently slipping
    past this guard the way it slips past ``test_api_monkeypatch_guard.py``'s
    stricter same-name-only check.
    """
    aliases = {"projects"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "app.domain.projects":
                    aliases.add(alias.asname or "projects")
        if isinstance(node, ast.ImportFrom) and node.module == "app.domain":
            for alias in node.names:
                if alias.name == "projects":
                    aliases.add(alias.asname or "projects")
    return aliases


def _for_loop_projects_setattr_violations(
    tree: ast.Module, path: Path, aliases: set[str]
) -> list[tuple[int, str]]:
    """Catch the loop-variable form of the same bug: ``for module in (a, b,
    projects_mod): monkeypatch.setattr(module, "name", value)``.

    Same silent no-op as the literal ``monkeypatch.setattr(projects_mod,
    ...)`` form the rest of this file catches, just spelled through a loop
    variable -- see ``tests/test_worker_monkeypatch_guard.py``'s identical
    check (and its docstring) for the real, shipped incident (``app.
    video_supervisor``) this pattern is modeled on. Only the direct
    tuple/list-literal form is checked here.
    """
    violations: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.For):
            continue
        it = node.iter
        if not isinstance(it, (ast.Tuple, ast.List)):
            continue
        elts_names = [e.id for e in it.elts if isinstance(e, ast.Name)]
        if not any(n in aliases for n in elts_names):
            continue
        target = node.target
        loopvar = target.id if isinstance(target, ast.Name) else None
        if loopvar is None:
            continue
        for sub in ast.walk(ast.Module(body=node.body, type_ignores=[])):
            if not isinstance(sub, ast.Call):
                continue
            func = sub.func
            is_setattr = (
                isinstance(func, ast.Attribute) and func.attr == "setattr"
            ) or (isinstance(func, ast.Name) and func.id == "setattr")
            if (
                is_setattr
                and sub.args
                and isinstance(sub.args[0], ast.Name)
                and sub.args[0].id == loopvar
            ):
                violations.append((
                    node.lineno,
                    f"{path}:{node.lineno}: for-loop over a tuple/list "
                    f"containing {[n for n in elts_names if n in aliases]!r} "
                    f"calls monkeypatch.setattr({loopvar}, ...) on the loop "
                    "variable -- on the projects iteration this is the exact "
                    "same silent no-op as a literal "
                    "monkeypatch.setattr(projects, ...) (see module "
                    "docstring), just spelled through a loop variable "
                    "instead of the bare name. Pull it out of the tuple and "
                    "use tests.conftest.patch_projects_everywhere(monkeypatch, "
                    "name, value) for it separately.",
                ))
                break
    return violations


def _is_bare_projects_attr_string(node: ast.expr) -> bool:
    """True for ``"app.domain.projects.<single identifier>"``.

    Not ``"app.domain.projects"`` itself (patching the module object as a
    whole is a different, rarer operation) and not a deeper dotted path
    through a specific submodule such as
    ``"app.domain.projects.lifecycle.get_conn"`` -- that already targets the
    correct submodule and was never broken by the package split.
    """
    if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
        return False
    parts = node.value.split(".")
    return (
        len(parts) == 4
        and parts[0] == "app"
        and parts[1] == "domain"
        and parts[2] == "projects"
        and parts[3].isidentifier()
    )


def _violations_in_file(path: Path) -> list[str]:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))

    exempt_start, exempt_end = (-1, -1)
    if path == CONFTEST_PATH:
        exempt_start, exempt_end = _helper_exempt_span(tree)

    aliases = _projects_aliases(tree)

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
                if isinstance(target, ast.Name) and target.id in aliases:
                    violations.append(
                        f"{path}:{node.lineno}: bare app.domain.projects "
                        f"attribute patch (monkeypatch.setattr({target.id}, "
                        "...) / patch.object(...)) only reaches the "
                        "package's own re-export -- app.domain.projects is a "
                        "real package now and every submodule holds its own "
                        "copy of any imported name, so this silently patches "
                        "nothing the real call site sees. Use "
                        "tests.conftest.patch_projects_everywhere(monkeypatch, "
                        "name, value) instead."
                    )
            # monkeypatch.setattr("app.domain.projects.name", value) resolves
            # the dotted string to (module, attr) internally -- same
            # package-only reach as the object form above. mock.patch(
            # "app.domain.projects.name") (bare `patch(...)` or
            # `mock.patch(...)`) takes the same string shape, so both call
            # forms are checked against the same target.
            if (is_setattr_call or is_patch_call) and node.args:
                target = node.args[0]
                if _is_bare_projects_attr_string(target):
                    violations.append(
                        f"{path}:{node.lineno}: string-form patch on "
                        f"{ast.literal_eval(target)!r} only reaches "
                        "app.domain.projects's own re-export, not the "
                        "submodule that actually binds the name -- same "
                        "silent no-op as the object form. Use "
                        "tests.conftest.patch_projects_everywhere(monkeypatch, "
                        "name, value) instead."
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
                        "only rebinds the package re-export attribute, not "
                        "the submodule-owned copy of the name -- same "
                        "silent no-op. Use "
                        "tests.conftest.patch_projects_everywhere(monkeypatch, "
                        "name, value) instead."
                    )

    for loop_violation_line, loop_violation in _for_loop_projects_setattr_violations(
        tree, path, aliases
    ):
        if exempt_start <= loop_violation_line <= exempt_end:
            continue
        violations.append(loop_violation)

    return violations


def test_no_bare_app_domain_projects_package_monkeypatch() -> None:
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
