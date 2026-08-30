"""Guard against the app.worker / app.media_exec package-split monkeypatch trap.

``app/worker.py`` used to be one of two things, both sharing a single module
namespace with ``app/media_exec/*.py``: first it ``exec()``'d the same chunk
source a *second* time into its own ``globals()`` (two unrelated copies of
every fence exception and mutable registry -- see ``app/media_exec/__init__.py``'s
module docstring for the ``except worker.LeaseLost`` failure this caused), then
it became a bare ``sys.modules`` alias of ``app.media_exec`` (one shared
namespace, but ``app.media_exec`` itself was still an ``exec()`` facade over
its five chunk files). Either way, ``monkeypatch.setattr(worker, "name",
value)`` reached every caller, because there was only ever one namespace behind
every access path.

Both are now real modules: ``app.media_exec`` is a package split into
``common``/``enqueue``/``legacy_keyframes``/``run_job``/``concat``, each an
independent module that imports the names it needs (from a sibling chunk or
from the name's true external source, e.g. ``app.db.get_conn``) at its own top
level; ``app/worker.py`` does explicit named re-exports of the package's public
surface. ``monkeypatch.setattr(worker, name, value)`` now only reaches
``app.worker``'s own re-export attribute -- not the independent copy each
``app.media_exec`` submodule bound for itself at import time. There is no
exception, no error, nothing: the patch silently no-ops and the test keeps
passing while validating a code path that was never mocked.

The fix is ``tests/conftest.py``'s ``patch_worker_everywhere(monkeypatch, name,
value)`` -- it walks ``app.worker``, the ``app.media_exec`` package itself, and
every one of its submodules, patching ``name`` wherever it is actually bound,
reproducing the pre-split single-namespace patch semantics. This test scans
every file under ``tests/`` for the bare forms and fails if any turn up outside
``patch_worker_everywhere``'s own implementation (which *is* the one place
allowed to touch ``worker``/``app.media_exec``/its submodules directly -- that
is what "everywhere" means).

Note what this test does *not* flag, on purpose: ``worker.config``,
``worker.subprocess``, ``worker.hiagent``, ``worker.asyncio``,
``worker.media_scheduler`` and ``worker._queue`` (plus the other worker-pool
queues/lists) are shared objects every submodule references by the same
identity -- patching an attribute *on the object itself*
(``monkeypatch.setattr(worker.subprocess, "run", fake)`` or
``monkeypatch.setattr(worker._queue, "put_nowait", fake)``) mutates that one
shared object and is visible to every submodule regardless of how many of them
hold a reference to it, so it was never affected by the package split. Only
patching a *name* re-exported by value from a submodule (a function/class/
constant, or *rebinding* ``_queue``/``_workers``/etc. to a brand new object) is
broken by it. The AST check below distinguishes the two: ``worker.foo`` (an
``Attribute`` node) is never flagged, only bare ``worker`` (a ``Name`` node) or
an ``"app.worker.<single identifier>"`` string.

This also catches the loop-variable form of the same bug: ``for module in (a,
b, worker): monkeypatch.setattr(module, "name", value)`` -- a sibling package
split (``app.video_supervisor``) shipped exactly this pattern and it reached
production: the loop's ``worker``/``video_supervisor`` iteration patched only
the package-level re-export, the submodule's own ``get_conn`` stayed live, a
test got a real on-disk connection instead of the in-memory one, and a
cross-thread ``sqlite3.ProgrammingError`` only surfaced downstream. Only the
direct tuple/list-literal form is covered (see
``_for_loop_worker_setattr_violations`` below for the known residual blind
spot: a loop over a *variable* that was assigned a worker-containing tuple
earlier, rather than a literal at the ``for`` site).

It also descends into string literals that are themselves substantial,
parseable Python source (see ``_embedded_source_trees``). This closes a
blind spot found 2026-08-30 in ``tests/test_process_restart_recovery.py``:
that file exercises process-restart recovery via
``subprocess.run([sys.executable, "-c", source], ...)`` -- a genuinely fresh
interpreter is the only way to test recovery from a hard process exit, so
the "code" being checked lives inside a Python string, not as real syntax in
this file. ``ast.parse`` on the outer file only ever sees that string as a
``Constant`` node -- a hand-rolled ``for target in (worker, media_run_job):
target.x = stub`` written *inside* the string was completely invisible to
every check above, even though it is exactly the same silent-no-op pattern.
Any string constant that parses as 3+ top-level statements is treated as
embedded source and scanned with the same checks (one level deep only: this
repo has no case of a subprocess script that itself spawns a further nested
``-c`` script, so recursing into an embedded tree's *own* string constants
is not implemented -- if that ever appears, extend
``_embedded_source_trees`` to recurse rather than assuming the blind spot is
still closed).
"""
from __future__ import annotations

import ast
import textwrap
import warnings
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
CONFTEST_PATH = TESTS_DIR / "conftest.py"
HELPER_NAME = "patch_worker_everywhere"


def _helper_exempt_span(tree: ast.Module) -> tuple[int, int]:
    """Line range of patch_worker_everywhere's own body in conftest.py.

    This is the sole legitimate place a bare ``setattr(worker, name, value)``
    / ``worker.<x> = ...`` may exist -- it *is* the everywhere-walk. Scoping
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


def _is_bare_worker_name(node: ast.expr) -> bool:
    return isinstance(node, ast.Name) and node.id == "worker"


def _worker_aliases(tree: ast.Module) -> set[str]:
    """Local names in this file that refer to the ``app.worker`` module object.

    Almost always just ``{"worker"}`` (every test does ``from app import
    worker`` or ``import app.worker as worker``), but resolving real import
    aliases here means the loop-variable check below isn't fooled by e.g.
    ``from app import worker as media_worker``.
    """
    aliases = {"worker"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "app.worker":
                    aliases.add(alias.asname or "worker")
        if isinstance(node, ast.ImportFrom) and node.module == "app":
            for alias in node.names:
                if alias.name == "worker":
                    aliases.add(alias.asname or "worker")
    return aliases


def _for_loop_worker_setattr_violations(
    tree: ast.Module, location: str
) -> list[tuple[int, str]]:
    """Catch the loop-variable form of the same bug: ``for module in (a, b,
    worker): monkeypatch.setattr(module, "name", value)`` *or* ``for module
    in (a, b, worker): module.name = value``.

    This is the same silent no-op as the literal ``monkeypatch.setattr(worker,
    ...)`` / ``worker.name = ...`` forms the rest of this file catches --
    ``module`` walks through ``worker`` on one iteration and patches it
    exactly as if it had been written literally -- but the AST shape is
    different: the target is the loop variable (an ``ast.Name`` whose id is
    *not* ``"worker"``), not ``worker`` itself, so the direct checks above
    can't see it. A real instance of the ``setattr(module, ...)`` spelling
    (with a different package that went through the same exec()-facade
    removal) reached production: ``for module in (..., video_supervisor,
    ...): monkeypatch.setattr(module, "get_conn", ...)`` left
    video_supervisor's own submodule copy of ``get_conn`` unpatched, the test
    got a real on-disk connection instead of the in-memory one, and a
    cross-thread ``sqlite3.ProgrammingError`` only surfaced because something
    downstream happened to use ``asyncio.to_thread``. The ``module.name =
    value`` spelling was a second, independent instance of this same blind
    spot found 2026-08-30 in the *original* (pre-fix) version of
    ``tests/test_process_restart_recovery.py``: ``for target in (worker,
    media_run_job): target._assert_review_dependency_fence_async = no_fence``
    -- direct attribute assignment through the loop variable, not a
    ``setattr()`` call, so the original version of this function (which only
    inspected ``ast.Call`` nodes) walked right past it despite already
    covering the literal ``worker.x = ...`` form above. Only the direct
    tuple/list-literal form (``for x in (a, worker, b):``) is checked here --
    ``for x in some_var:`` where ``some_var`` was assigned a tuple containing
    ``worker`` earlier is a known residual blind spot (no occurrences found
    in this repo as of the 2026-08-29 media_exec package split; grep ``for
    [a-zA-Z_]+ in \\(`` tests/*.py by hand if you add one).
    """
    aliases = _worker_aliases(tree)
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
        matched_names = [n for n in elts_names if n in aliases]
        for sub in ast.walk(ast.Module(body=node.body, type_ignores=[])):
            is_setattr_call = False
            if isinstance(sub, ast.Call):
                func = sub.func
                is_setattr = (
                    isinstance(func, ast.Attribute) and func.attr == "setattr"
                ) or (isinstance(func, ast.Name) and func.id == "setattr")
                is_setattr_call = (
                    is_setattr
                    and bool(sub.args)
                    and isinstance(sub.args[0], ast.Name)
                    and sub.args[0].id == loopvar
                )
            is_attr_assign = False
            if isinstance(sub, ast.Assign):
                is_attr_assign = any(
                    isinstance(t, ast.Attribute)
                    and isinstance(t.value, ast.Name)
                    and t.value.id == loopvar
                    for t in sub.targets
                )
            if not (is_setattr_call or is_attr_assign):
                continue
            spelling = (
                f"monkeypatch.setattr({loopvar}, ...)"
                if is_setattr_call
                else f"{loopvar}.<attr> = ..."
            )
            violations.append((
                node.lineno,
                f"{location}:{node.lineno}: for-loop over a tuple/list "
                f"containing {matched_names!r} calls {spelling} on the loop "
                "variable -- on the worker iteration this is the exact "
                "same silent no-op as a literal "
                "monkeypatch.setattr(worker, ...) / worker.<attr> = ... "
                "(see module docstring), just spelled through a loop "
                "variable instead of the bare name. Pull worker out of the "
                "tuple and use tests.conftest.patch_worker_everywhere("
                "monkeypatch, name, value) for it separately.",
            ))
            break
    return violations


def _is_bare_worker_attr_string(node: ast.expr) -> bool:
    """True for ``"app.worker.<single identifier>"``.

    Not ``"app.worker"`` itself (patching the module object as a whole is a
    different, rarer operation) and not a deeper dotted path such as
    ``"app.worker.config.PROJECTS_DIR"`` -- that patches a real shared module
    object's attribute (see module docstring), which the package split never
    broke.
    """
    if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
        return False
    parts = node.value.split(".")
    return (
        len(parts) == 3
        and parts[0] == "app"
        and parts[1] == "worker"
        and parts[2].isidentifier()
    )


def _violations_in_tree(
    tree: ast.Module,
    location: str,
    exempt_start: int = -1,
    exempt_end: int = -1,
) -> list[str]:
    """Core AST scan, shared by the top-level file scan and the embedded
    subprocess-source scan (``_embedded_source_trees`` below). ``location``
    is used verbatim as the ``path:lineno`` prefix in reported violations --
    for the real file it is just the file path; for a tree parsed out of a
    string literal it also names the outer line the string starts on, since
    the embedded tree's own line numbers restart at 1 and would otherwise
    point at the wrong place in the real file.
    """
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
                if _is_bare_worker_name(target):
                    violations.append(
                        f"{location}:{node.lineno}: bare worker-module attribute "
                        "patch (monkeypatch.setattr(worker, ...) / "
                        "patch.object(worker, ...)) only reaches app.worker's "
                        "own re-export -- app.media_exec is a real package now "
                        "and every submodule holds its own copy of any "
                        "imported name, so this silently patches nothing the "
                        "real call site sees. Use "
                        "tests.conftest.patch_worker_everywhere(monkeypatch, "
                        "name, value) instead."
                    )
            # monkeypatch.setattr("app.worker.name", value) resolves the
            # dotted string to (module, attr) internally -- same package-only
            # reach as the object form above. mock.patch("app.worker.name")
            # (bare `patch(...)` or `mock.patch(...)`) takes the same string
            # shape, so both call forms are checked against the same target.
            if (is_setattr_call or is_patch_call) and node.args:
                target = node.args[0]
                if _is_bare_worker_attr_string(target):
                    violations.append(
                        f"{location}:{node.lineno}: string-form patch on "
                        f"{ast.literal_eval(target)!r} only reaches "
                        "app.worker's own re-export, not the app.media_exec "
                        "submodule that actually binds the name -- same "
                        "silent no-op as the object form. Use "
                        "tests.conftest.patch_worker_everywhere(monkeypatch, "
                        "name, value) instead."
                    )

        if isinstance(node, ast.Assign):
            for assign_target in node.targets:
                if (
                    isinstance(assign_target, ast.Attribute)
                    and isinstance(assign_target.value, ast.Name)
                    and assign_target.value.id == "worker"
                ):
                    violations.append(
                        f"{location}:{node.lineno}: direct assignment "
                        f"worker.{assign_target.attr} = ... only rebinds the "
                        "app.worker re-export attribute, not the "
                        "app.media_exec submodule-owned copy of the name -- "
                        "same silent no-op. Use "
                        "tests.conftest.patch_worker_everywhere(monkeypatch, "
                        "name, value) instead."
                    )

    for loop_violation_line, loop_violation in _for_loop_worker_setattr_violations(
        tree, location
    ):
        if exempt_start <= loop_violation_line <= exempt_end:
            continue
        violations.append(loop_violation)

    return violations


def _embedded_source_trees(tree: ast.Module) -> list[tuple[int, ast.Module]]:
    """Find string literals in ``tree`` that are themselves substantial,
    parseable Python source -- the
    ``subprocess.run([sys.executable, "-c", textwrap.dedent(source)], ...)``
    pattern ``tests/test_process_restart_recovery.py`` uses to exercise
    process-restart recovery in a genuinely fresh interpreter (a real crash
    can only be simulated by actually exiting a process, so that test can't
    use the monkeypatch fixture or run in-process like every other test
    here). Code inside a string literal is invisible to ``ast.parse`` on the
    outer file -- it is just an ``ast.Constant`` string node -- so a
    hardcoded-module-list monkeypatch trap written *inside* one of these
    strings used to be a blind spot no check above could see (found
    2026-08-30). Any string constant that parses as 3+ top-level statements
    (filters out short strings, e.g. SQL or prompt fixtures, that
    coincidentally parse as a single expression) is treated as embedded
    source and returned for scanning with the exact same checks as real
    files. One level deep only -- see module docstring.
    """
    found: list[tuple[int, ast.Module]] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        try:
            # This is a speculative probe -- most string constants in the
            # suite are not Python source (docstrings, SQL, regexes, prompt
            # fixtures) and are expected to fail here. The tokenizer can
            # still emit a real SyntaxWarning (e.g. "invalid escape
            # sequence") as a side effect *before* it gives up and raises
            # SyntaxError -- found 2026-08-30 via a regex literal elsewhere
            # in tests/ containing ``\[``/``\{``. That warning is genuine
            # (the regex file's own raw string is fine either way) but
            # attributing it to whatever test happens to call this function
            # is misleading noise, not a real finding, so it's suppressed
            # rather than left to leak into the suite's warnings summary.
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                inner = ast.parse(textwrap.dedent(node.value))
        except SyntaxError:
            continue
        if len(inner.body) < 3:
            continue
        found.append((node.lineno, inner))
    return found


def _violations_in_file(path: Path) -> list[str]:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))

    exempt_start, exempt_end = (-1, -1)
    if path == CONFTEST_PATH:
        exempt_start, exempt_end = _helper_exempt_span(tree)

    violations = _violations_in_tree(tree, str(path), exempt_start, exempt_end)

    for outer_lineno, inner_tree in _embedded_source_trees(tree):
        violations.extend(
            _violations_in_tree(
                inner_tree,
                f"{path} (embedded source at line {outer_lineno})",
            )
        )

    return violations


def test_no_bare_app_worker_package_monkeypatch() -> None:
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
