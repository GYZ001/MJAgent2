"""Guard against the app.quota / app.quota_tiers split monkeypatch trap.

``app/quota.py`` used to define ``TierLimits``/``TIER_TABLE``/``VALID_TIERS``/
``_UNLIMITED``/``_UPGRADE_PATH`` itself. It was sitting at 600/600 against its
``app/FILE_CONVENTIONS.toml`` line-count baseline with zero slack, so the
static, behavior-free half of the file (the tier table and its upgrade-path
text -- no ``sqlite3.Connection``, no judgement, no ledger writes) moved to a
new sibling module, ``app/quota_tiers.py``. ``app/quota.py`` now does
``from app.quota_tiers import TIER_TABLE`` (among others) so every existing
``quota.TIER_TABLE`` / ``quota.VALID_TIERS`` call site keeps working unchanged.

That import is the trap: ``app.quota.TIER_TABLE`` and
``app.quota_tiers.TIER_TABLE`` are two *independent* bindings that merely
happen to point at the same dict object right after import. This is not a
package split (no ``exec()`` facade, no ``pkgutil`` subpackage) -- it is two
flat sibling modules -- but the exact same silent-no-op applies to any name
copied across a module boundary by ``from .x import y``:

- ``monkeypatch.setattr(quota, "TIER_TABLE", fake)`` only rebinds
  ``app.quota``'s own copy. ``effective_limits`` is defined *in*
  ``app.quota`` and reads the global name from that module's own namespace,
  so it does see this patch -- but anything reading
  ``app.quota_tiers.TIER_TABLE`` directly (or a future caller importing
  straight from ``app.quota_tiers`` instead of via ``app.quota``) would still
  see the real table.
- ``monkeypatch.setattr(quota_tiers, "TIER_TABLE", fake)`` is the mirror
  failure: it never reaches ``app.quota``'s own copy, so
  ``effective_limits``/``check_module_concurrency`` -- the actual call path
  every production quota gate goes through -- silently keeps using the real
  numbers while the test believes it swapped in a fake tier table. There is
  no exception, no error, nothing: the patch appears to apply and the test
  keeps passing while validating a code path that was never mocked.

The fix is ``tests/conftest.py``'s ``patch_quota_everywhere(monkeypatch, name,
value)`` -- it walks ``app.quota``, ``app.quota_tiers``, ``app.quota_addon``,
and ``app.quota_scope`` and patches ``name`` wherever it is actually bound,
reproducing the pre-split single-namespace patch semantics regardless of
which of the four modules the real call site happens to read from. This test
scans every file under ``tests/`` for the bare forms and fails if any turn up
outside ``patch_quota_everywhere``'s own implementation (which *is* the one
place allowed to touch ``quota``/``quota_tiers`` directly -- that is what
"everywhere" means).

Note what this test does *not* flag, on purpose: ``quota.foo`` /
``quota_tiers.foo`` (an ``Attribute`` access, e.g. reading
``quota.SECONDS_PER_SHOT`` in an assertion) is never flagged -- only a bare
``quota`` / ``quota_tiers`` module reference passed as the *first argument* to
``setattr``/``patch.object``/``patch``, or as the target of a direct
attribute assignment, is a real patch attempt.

Known blind spots (documented, not fully closed here -- same residual gaps as
``tests/test_worker_monkeypatch_guard.py``, which this file's loop-detection
logic is copied from):

- Loop-variable form: ``for m in (a, b, quota): monkeypatch.setattr(m, "name",
  value)`` is caught when ``quota``/``quota_tiers`` appears as a *literal*
  element of the ``for``-loop's tuple/list. A loop over a *variable* that was
  assigned such a tuple earlier (``mods = (a, quota); for m in mods: ...``)
  is not traced and remains a blind spot -- grep ``for [a-zA-Z_]+ in \\(``
  tests/*.py by hand if a new one is added.
- A patch call spelled inside a string literal handed to
  ``subprocess.run([sys.executable, "-c", "..."])`` (spawning a fresh
  interpreter that does its own bare ``monkeypatch.setattr(quota, ...)`` in
  isolation) is invisible to this AST scan, which only parses the enclosing
  ``tests/*.py`` file's own syntax tree, not string payloads inside it.
"""
from __future__ import annotations

import ast
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
CONFTEST_PATH = TESTS_DIR / "conftest.py"
HELPER_NAME = "patch_quota_everywhere"

BARE_NAME_ALIASES = {"quota", "quota_tiers"}


def _helper_exempt_span(tree: ast.Module) -> tuple[int, int]:
    """Line range of patch_quota_everywhere's own body in conftest.py.

    This is the sole legitimate place a bare ``setattr(quota, name, value)``
    / ``quota.<x> = ...`` may exist -- it *is* the everywhere-walk. Scoping
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


def _is_bare_quota_name(node: ast.expr) -> bool:
    return isinstance(node, ast.Name) and node.id in BARE_NAME_ALIASES


def _is_bare_quota_attr_string(node: ast.expr) -> bool:
    """True for ``"app.quota.<single identifier>"`` / ``"app.quota_tiers.<id>"``.

    Not ``"app.quota"`` itself (patching the module object as a whole is a
    different, rarer operation) and not a deeper dotted path -- those two
    forms aren't the shape this trap takes.
    """
    if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
        return False
    parts = node.value.split(".")
    return (
        len(parts) == 3
        and parts[0] == "app"
        and parts[1] in BARE_NAME_ALIASES
        and parts[2].isidentifier()
    )


def _quota_aliases(tree: ast.Module) -> set[str]:
    """Local names in this file that refer to ``app.quota`` / ``app.quota_tiers``.

    Almost always exactly ``BARE_NAME_ALIASES`` itself (every test does
    ``from app import quota`` or ``import app.quota_tiers as quota_tiers``),
    but resolving real import aliases here means the loop-variable check
    below isn't fooled by e.g. ``from app import quota as quota_mod``.
    """
    aliases = set(BARE_NAME_ALIASES)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in {"app.quota", "app.quota_tiers"}:
                    aliases.add(alias.asname or alias.name.rsplit(".", 1)[-1])
        if isinstance(node, ast.ImportFrom) and node.module == "app":
            for alias in node.names:
                if alias.name in BARE_NAME_ALIASES:
                    aliases.add(alias.asname or alias.name)
    return aliases


def _for_loop_quota_setattr_violations(
    tree: ast.Module, path: Path
) -> list[tuple[int, str]]:
    """Catch the loop-variable form: ``for m in (a, b, quota): monkeypatch.
    setattr(m, "name", value)`` -- see module docstring's "known blind spots"
    section; this only covers the literal tuple/list case, copied from
    ``tests/test_worker_monkeypatch_guard.py``'s
    ``_for_loop_worker_setattr_violations``.
    """
    aliases = _quota_aliases(tree)
    violations: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.For):
            continue
        it = node.iter
        if not isinstance(it, (ast.Tuple, ast.List)):
            continue
        elts_names = [e.id for e in it.elts if isinstance(e, ast.Name)]
        hit_names = [n for n in elts_names if n in aliases]
        if not hit_names:
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
                    f"containing {hit_names!r} calls monkeypatch.setattr("
                    f"{loopvar}, ...) on the loop variable -- on the "
                    "quota/quota_tiers iteration this is the exact same "
                    "silent no-op as a literal monkeypatch.setattr(quota, "
                    "...) (see module docstring), just spelled through a "
                    "loop variable instead of the bare name. Pull it out of "
                    "the tuple and use tests.conftest.patch_quota_everywhere"
                    "(monkeypatch, name, value) for it separately.",
                ))
                break
    return violations


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
                if _is_bare_quota_name(target):
                    violations.append(
                        f"{path}:{node.lineno}: bare quota/quota_tiers "
                        "module attribute patch (monkeypatch.setattr(quota, "
                        "...) / patch.object(quota_tiers, ...)) only reaches "
                        "that one module's own binding -- app.quota and "
                        "app.quota_tiers hold two independent copies of any "
                        "name imported across that boundary (e.g. "
                        "TIER_TABLE), so this silently patches nothing the "
                        "other module's call sites see. Use "
                        "tests.conftest.patch_quota_everywhere(monkeypatch, "
                        "name, value) instead."
                    )
            if (is_setattr_call or is_patch_call) and node.args:
                target = node.args[0]
                if _is_bare_quota_attr_string(target):
                    violations.append(
                        f"{path}:{node.lineno}: string-form patch on "
                        f"{ast.literal_eval(target)!r} only reaches that one "
                        "module's own binding, not the sibling module that "
                        "may hold an independent copy of the same name -- "
                        "same silent no-op as the object form. Use "
                        "tests.conftest.patch_quota_everywhere(monkeypatch, "
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
                        f"{assign_target.value.id}.{assign_target.attr} = "
                        "... only rebinds that one module's attribute, not "
                        "the sibling module's independent copy of the name "
                        "-- same silent no-op. Use "
                        "tests.conftest.patch_quota_everywhere(monkeypatch, "
                        "name, value) instead."
                    )

    for loop_violation_line, loop_violation in _for_loop_quota_setattr_violations(
        tree, path
    ):
        if exempt_start <= loop_violation_line <= exempt_end:
            continue
        violations.append(loop_violation)

    return violations


def test_no_bare_app_quota_split_monkeypatch() -> None:
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
