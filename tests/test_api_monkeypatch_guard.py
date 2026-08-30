"""Guard against the app.api / app.domain package-split monkeypatch trap.

``app/api.py`` + ``app/domain/*.py`` used to share a single ``exec()``'d
namespace (``app/api.py`` first ``exec()``'d the same ``domain/*.py`` chunk
source a *second* time into its own ``globals()``, later collapsed to a bare
``sys.modules`` alias of ``app.domain``, which was itself still an ``exec()``
facade covering all seven chunks): every call site inside any of the seven
chunks -- ``common``/``projects``/``bible_ops``/``screenplay_ops``/
``storyboard_ops``/``review_wall``/``video_ops`` -- shared one module
namespace, so ``monkeypatch.setattr(api, "name", value)`` (or the string form
``monkeypatch.setattr("app.api.name", value)`` / ``"app.domain.name"`` /
``"app.domain.<chunk>.name"``, or a direct ``api.name = value`` assignment)
reached every caller. ``app.domain`` is now a real package (see
``app/domain/__init__.py``); each of the seven chunk submodules holds its own
copy of any name it imported (``from app.domain.common import router`` and
friends). Patching only the ``app.api`` / ``app.domain`` package-level
re-export no longer reaches the chunk submodule that actually calls the name
-- and there is no exception, no error, nothing: the patch silently no-ops
and the test keeps passing while validating a code path that was never
mocked.

The fix is ``tests/conftest.py``'s ``patch_api_everywhere(monkeypatch, name,
value)`` -- it walks ``app.api``, the ``app.domain`` package, and every one of
its seven chunk submodules, patching ``name`` wherever it is actually bound,
reproducing the pre-split single-namespace patch semantics. This test scans
every file under ``tests/`` for the bare forms and fails if any turn up
outside ``patch_api_everywhere``'s own implementation (which *is* the one
place allowed to touch ``api``/``domain``/the chunk submodules directly --
that is what "everywhere" means).

Some of the seven chunks (``bible_ops`` as of this writing; see
``SPLIT_DOMAIN_CHUNKS`` below, computed from the live package rather than
hand-listed) have themselves been split a second time into a real
sub-package of concern-based files instead of staying one flat module --
same trap one level deeper: ``from app.domain import bible_ops;
monkeypatch.setattr(bible_ops, "name", value)`` only reaches
``bible_ops/__init__.py``'s own re-export, not the specific sub-file (e.g.
``bible_ops/precheck.py``) the real call site resolves the name through.
This test flags that bare form too, for exactly the chunks that are
currently split this way -- ``patch_api_everywhere`` already recurses into a
chunk's own submodules (see its docstring), so the fix is the same helper
call, just also required one level deeper now.

Note what this test does *not* flag, on purpose: ``api.worker`` /
``api.task_registry`` are shared singleton modules (``app.worker`` /
``app.task_registry``) reached via ``from app import worker, task_registry``
in every chunk that uses them -- patching an attribute *on the module object
itself* (``monkeypatch.setattr(api.task_registry, "record", fake)``) mutates
that one shared object and is visible to every chunk regardless of how many
of them hold a reference to it, so it was never affected by the package
split. Only patching a *name* re-exported by the package (a
function/class/constant copied by value into each importer's namespace) is
broken by the split. The AST check below distinguishes the two: ``api.foo``
(an ``Attribute`` node) is never flagged, only bare ``api``/``domain`` (a
``Name`` node) or an ``"app.api.<single identifier>"`` /
``"app.domain.<single identifier>"`` / ``"app.domain.<chunk>.<single
identifier>"`` string.
"""
from __future__ import annotations

import ast
from pathlib import Path

import app.domain as _domain

TESTS_DIR = Path(__file__).resolve().parent
CONFTEST_PATH = TESTS_DIR / "conftest.py"
HELPER_NAME = "patch_api_everywhere"
BARE_NAMES = {"api", "domain"}
DOMAIN_CHUNKS = {
    "common", "projects", "bible_ops", "screenplay_ops",
    "storyboard_ops", "review_wall", "video_ops",
}
# Some domain chunks (``bible_ops`` today; more may follow the same path --
# see app/FILE_CONVENTIONS.toml's line_count baseline entries for which ones
# still need it) have themselves been split further into a real sub-package
# of concern-based files instead of staying one flat module (P0-1 follow-up).
# For those specific chunks, a *bare* patch of the chunk name itself --
# ``from app.domain import bible_ops; monkeypatch.setattr(bible_ops, "name",
# value)`` -- has exactly the same silent-no-op failure mode as a bare
# ``api``/``domain`` patch: it only reaches ``app.domain.bible_ops``'s own
# re-export (``bible_ops/__init__.py``), not the specific sub-file (e.g.
# ``bible_ops/precheck.py``) that actually resolves the name at the real call
# site. Chunks that are still one flat module (``common``, ``projects``,
# ``screenplay_ops``, ``storyboard_ops``, ``review_wall``, ``video_ops`` as of
# this writing) do not have this problem -- there is only one namespace for
# the whole chunk, so a bare patch on the chunk name reaches every call site
# inside it, same as before any split. Computed from the live package (not
# hand-maintained) so this guard automatically starts covering a chunk the
# moment it is split, with no separate edit required.
SPLIT_DOMAIN_CHUNKS = {
    chunk for chunk in DOMAIN_CHUNKS
    if hasattr(getattr(_domain, chunk), "__path__")
}


def _helper_exempt_span(tree: ast.Module) -> tuple[int, int]:
    """Line range of patch_api_everywhere's own body in conftest.py.

    This is the sole legitimate place a bare ``setattr(api, name, value)`` /
    ``setattr(domain, name, value)`` / ``api.<x> = ...`` may exist -- it *is*
    the everywhere-walk. Scoping the exemption to this function's own
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


def _bare_names_bound_to_app_module(tree: ast.Module) -> set[str]:
    """Which of ``{"api", "domain"}`` this file actually binds to
    ``app.api``/``app.domain`` at the bare-name level.

    ``api`` is not a unique identifier in this codebase -- ``tests/
    test_model_catalog.py`` does ``from app import system_api as api``, a
    completely unrelated module. Flagging every bare ``Name(id="api")`` in
    every file would false-positive on that file's ``monkeypatch.setattr(api,
    ...)`` calls, which patch ``app.system_api`` and have nothing to do with
    the domain package split. Only treat a file's bare ``api``/``domain`` as
    this guard's business if the file actually imports it from ``app.api`` /
    ``app.domain`` (``from app import api`` / ``from app import domain`` with
    no rename, or ``import app.api as api`` / ``import app.domain as
    domain``).
    """
    bound: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "app":
            for alias in node.names:
                local = alias.asname or alias.name
                if alias.name in BARE_NAMES and local == alias.name:
                    bound.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module == "app.domain":
            # `from app.domain import bible_ops` (and any future further-split
            # chunk) -- same bare-name trap one level deeper, see
            # SPLIT_DOMAIN_CHUNKS above.
            for alias in node.names:
                local = alias.asname or alias.name
                if alias.name in SPLIT_DOMAIN_CHUNKS and local == alias.name:
                    bound.add(alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in {"app.api", "app.domain"} and alias.asname:
                    bare = alias.name.rsplit(".", 1)[1]
                    if alias.asname == bare:
                        bound.add(bare)
                elif alias.name.startswith("app.domain."):
                    chunk = alias.name.rsplit(".", 1)[1]
                    if chunk in SPLIT_DOMAIN_CHUNKS:
                        local = alias.asname or chunk
                        if local == chunk:
                            bound.add(chunk)
    return bound


def _is_bare_api_or_domain_name(node: ast.expr, relevant_names: set[str]) -> bool:
    return isinstance(node, ast.Name) and node.id in relevant_names


def _loop_variable_setattr_violations(
    tree: ast.Module, path: Path, exempt_start: int, exempt_end: int
) -> list[str]:
    """``for module in (api, other, ...): monkeypatch.setattr(module, ...)``.

    The AST check above only recognizes the *literal* forms
    ``setattr(api, ...)`` / ``setattr(domain, ...)``. A ``for`` loop that
    iterates a tuple/list containing ``api`` (or ``domain``, or a chunk name)
    and calls ``setattr(<loop variable>, ...)`` inside the loop body reaches
    exactly the same package-level-only attribute for the iteration where the
    loop variable is bound to ``api``/``domain`` -- the AST shape just hides
    it one level deeper. This was a real, shipped bug in this codebase (see
    ``app/video_supervisor``'s package split write-up): a sibling refactor's
    ``for module in (..., video_supervisor, ...): monkeypatch.setattr(module,
    "get_conn", ...)`` silently left ``authority.py``'s own ``get_conn`` copy
    unpatched, so a test got a real on-disk sqlite connection instead of the
    in-memory one and cross-thread-used it, surfacing as a late
    ``sqlite3.ProgrammingError`` nowhere near the actual patch call site.
    Flag the loop instead of trying to prove the specific iteration is safe --
    a false positive here just means splitting one line into
    ``patch_api_everywhere(...)`` plus a shorter loop over the rest of the
    tuple, which every real occurrence found by this check has needed anyway.
    """
    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.For) or not isinstance(node.target, ast.Name):
            continue
        lineno = node.lineno
        if exempt_start <= lineno <= exempt_end:
            continue
        loop_var = node.target.id
        if not isinstance(node.iter, (ast.Tuple, ast.List)):
            continue
        elt_names = {e.id for e in node.iter.elts if isinstance(e, ast.Name)}
        if not (elt_names & (BARE_NAMES | DOMAIN_CHUNKS)):
            continue
        for sub in ast.walk(node):
            sub_lineno = getattr(sub, "lineno", None)
            if sub_lineno is not None and exempt_start <= sub_lineno <= exempt_end:
                continue
            if not isinstance(sub, ast.Call):
                continue
            func = sub.func
            is_setattr_call = (
                isinstance(func, ast.Attribute) and func.attr == "setattr"
            ) or (isinstance(func, ast.Name) and func.id == "setattr")
            if (
                is_setattr_call
                and sub.args
                and isinstance(sub.args[0], ast.Name)
                and sub.args[0].id == loop_var
            ):
                hit = elt_names & (BARE_NAMES | DOMAIN_CHUNKS)
                violations.append(
                    f"{path}:{sub.lineno}: loop-variable-form patch -- "
                    f"`for {loop_var} in (...)` at line {node.lineno} iterates "
                    f"a tuple/list containing {sorted(hit)}, and "
                    f"`setattr({loop_var}, ...)` inside the loop body reaches "
                    "only app.api's / app.domain's own re-export for the "
                    "iteration where the loop variable is api/domain/a chunk "
                    "-- same silent no-op as the literal form. Pull "
                    "api/domain out of the tuple and use "
                    "tests.conftest.patch_api_everywhere(monkeypatch, name, "
                    "value) for it separately."
                )
    return violations


def _is_bare_api_or_domain_attr_string(node: ast.expr) -> bool:
    """True for ``"app.api.<single identifier>"`` or ``"app.domain.<single
    identifier>"`` or ``"app.domain.<chunk>.<single identifier>"``.

    Not ``"app.api"`` / ``"app.domain"`` themselves (patching the module
    object as a whole is a different, rarer operation) and not a deeper
    dotted path through a non-chunk name such as
    ``"app.domain.common.model_gateway.chat"`` -- that patches a real shared
    module object's attribute, which the package split never broke.
    ``"app.domain.<chunk>.<name>"`` (exactly 4 segments, chunk one of the
    seven known submodules) *is* flagged: it patches only that one chunk's
    own copy, missing every other chunk that separately imported the same
    name (e.g. ``_board_from_shot_rows`` is bound in both ``storyboard_ops``
    and ``review_wall`` and ``video_ops``).
    """
    if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
        return False
    parts = node.value.split(".")
    if len(parts) == 3 and parts[0] == "app" and parts[1] in ("api", "domain"):
        return parts[2].isidentifier()
    if (
        len(parts) == 4
        and parts[0] == "app"
        and parts[1] == "domain"
        and parts[2] in DOMAIN_CHUNKS
    ):
        return parts[3].isidentifier()
    return False


def _violations_in_file(path: Path) -> list[str]:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))

    exempt_start, exempt_end = (-1, -1)
    if path == CONFTEST_PATH:
        exempt_start, exempt_end = _helper_exempt_span(tree)

    relevant_names = _bare_names_bound_to_app_module(tree)

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
                if _is_bare_api_or_domain_name(target, relevant_names):
                    bare_name = target.id  # type: ignore[union-attr]
                    if bare_name in SPLIT_DOMAIN_CHUNKS:
                        reach_desc = (
                            f"app.domain.{bare_name}'s own re-export "
                            f"({bare_name}/__init__.py) -- app.domain.{bare_name} "
                            "is itself a real sub-package now and every one of "
                            f"its own sub-files holds its own copy of any "
                            "imported name"
                        )
                    else:
                        reach_desc = (
                            "app.api's / app.domain's own re-export -- "
                            "app.domain is a real package now and every chunk "
                            "submodule holds its own copy of any imported name"
                        )
                    violations.append(
                        f"{path}:{node.lineno}: bare {bare_name}-package "
                        "attribute patch (monkeypatch.setattr("
                        f"{bare_name}, ...) / patch.object({bare_name}, "
                        f"...)) only reaches {reach_desc}, so this silently "
                        "patches nothing the real call site sees. Use "
                        "tests.conftest.patch_api_everywhere(monkeypatch, "
                        "name, value) instead."
                    )
            # monkeypatch.setattr("app.api.name", value) / ("app.domain.name",
            # value) / ("app.domain.<chunk>.name", value) resolves the dotted
            # string to (module, attr) internally -- same package/single-chunk
            # reach as the object form above. mock.patch("app.api.name") (bare
            # `patch(...)` or `mock.patch(...)`) takes the same string shape,
            # so both call forms are checked against the same target.
            if (is_setattr_call or is_patch_call) and node.args:
                target = node.args[0]
                if _is_bare_api_or_domain_attr_string(target):
                    violations.append(
                        f"{path}:{node.lineno}: string-form patch on "
                        f"{ast.literal_eval(target)!r} only reaches the "
                        "named module's own re-export, not the chunk "
                        "submodule that actually binds the name -- same "
                        "silent no-op as the object form. Use "
                        "tests.conftest.patch_api_everywhere(monkeypatch, "
                        "name, value) instead."
                    )

        if isinstance(node, ast.Assign):
            for assign_target in node.targets:
                if (
                    isinstance(assign_target, ast.Attribute)
                    and isinstance(assign_target.value, ast.Name)
                    and assign_target.value.id in relevant_names
                ):
                    bare_name = assign_target.value.id
                    violations.append(
                        f"{path}:{node.lineno}: direct assignment "
                        f"{bare_name}.{assign_target.attr} = ... only rebinds "
                        "the package attribute, not the chunk-submodule-owned "
                        "copy of the name -- same silent no-op. Use "
                        "tests.conftest.patch_api_everywhere(monkeypatch, "
                        "name, value) instead."
                    )

    violations.extend(
        _loop_variable_setattr_violations(tree, path, exempt_start, exempt_end)
    )
    return violations


def test_no_bare_app_api_domain_package_monkeypatch() -> None:
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
