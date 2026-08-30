"""Guard against the app.video_modes package-split monkeypatch trap.

``app/video_modes.py`` (4,081 lines) used to be one file; every call site
inside it shared a single module namespace, so ``monkeypatch.setattr(
video_modes, "name", value)`` (or the string form ``monkeypatch.setattr(
"app.video_modes.name", value)``, or a direct ``video_modes.name = value``
assignment) reached every caller. It was split into the ``app.video_modes``
package (see ``app/video_modes/__init__.py``); each of the 9 submodules now
holds its own copy of any name it imported (``from .keyframe_contract import
_keyframe_contract`` and friends). Patching only the package-level re-export
no longer reaches the submodule that actually calls the name -- and there is
no exception, no error, nothing: the patch silently no-ops and the test keeps
passing while validating a code path that was never mocked.

Several real instances of exactly this shape were found in the test suite
when the package was introduced: ``tests/test_video_modes.py`` (settings
getters like ``min_generated_references``/``max_reference_images``, and
functions like ``character_reference_assets``/``build_reference_assets``/
``_extract_last_frame``), ``tests/test_keyframe_outer_accounting.py``
(``keyframe_candidate_count``/``supporting_keyframe_candidate_count``/
``estimated_keyframe_generation_count``), ``tests/test_video_prompt_ai.py``
(``max_reference_images``), and ``tests/test_worker_reference_gallery.py``
(``estimated_keyframe_generation_count``) all patched directly on the bare
``app.video_modes`` module object. All were converted to use
``patch_video_modes_everywhere`` so this guard has no legitimate exceptions
to carry.

The fix is ``tests/conftest.py``'s ``patch_video_modes_everywhere(
monkeypatch, name, value)`` -- it walks every ``app.video_modes`` submodule
and patches ``name`` wherever it is actually bound, reproducing the
pre-split single-namespace patch semantics. This test scans every file under
``tests/`` for the bare forms and fails if any turn up outside
``patch_video_modes_everywhere``'s own implementation (which *is* the one
place allowed to touch the package/submodules directly -- that is what
"everywhere" means).

Note what this test does *not* flag, on purpose: an attribute access on a
shared singleton module object reached via the video_modes package (e.g.
``video_modes.hiagent`` / ``video_modes.model_gateway``, both plain
``from app import hiagent`` / ``from app.harness import model_gateway``
re-exports) is never affected by the package split -- patching an attribute
*on that shared object itself* (``monkeypatch.setattr(video_modes.hiagent,
"chat", fake)``) mutates the one object every submodule holds a reference to.
Only patching a *name* re-exported by the package (a function/constant
copied by value into each importer's namespace) is broken by the split. The
AST check below distinguishes the two: ``video_modes.foo`` (an
``Attribute`` node) is never flagged, only bare ``video_modes`` (a ``Name``
node) or an ``"app.video_modes.<single identifier>"`` string.
"""
from __future__ import annotations

import ast
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
CONFTEST_PATH = TESTS_DIR / "conftest.py"
HELPER_NAME = "patch_video_modes_everywhere"

# Bare local name the test suite uses for ``app.video_modes`` /
# ``app import video_modes``. Only one alias is in the wild today, but this
# is kept as a set (not a single string) so a future alias can be added
# without changing the check's shape -- see the "supervisor" precedent in
# test_video_supervisor_monkeypatch_guard.py.
BARE_NAME_ALIASES = {"video_modes"}


def _helper_exempt_span(tree: ast.Module) -> tuple[int, int]:
    """Line range of patch_video_modes_everywhere's own body in conftest.py.

    This is the sole legitimate place a bare ``setattr(video_modes, name,
    value)`` / ``video_modes.<x> = ...`` may exist -- it *is* the
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


def _is_bare_video_modes_name(node: ast.expr) -> bool:
    return isinstance(node, ast.Name) and node.id in BARE_NAME_ALIASES


def _is_bare_video_modes_attr_string(node: ast.expr) -> bool:
    """True for ``"app.video_modes.<single identifier>"``.

    Not ``"app.video_modes"`` itself (patching the module object as a whole
    is a different, rarer operation) and not a deeper dotted path such as
    ``"app.video_modes.hiagent.chat"`` -- that patches a real shared module
    object's attribute (see module docstring), which the package split never
    broke.
    """
    if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
        return False
    parts = node.value.split(".")
    return (
        len(parts) == 3
        and parts[0] == "app"
        and parts[1] == "video_modes"
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
                if _is_bare_video_modes_name(target):
                    violations.append(
                        f"{path}:{node.lineno}: bare video_modes-package "
                        "attribute patch (monkeypatch.setattr(video_modes, "
                        "...) / patch.object(video_modes, ...)) only reaches "
                        "app.video_modes's own re-export -- app/video_modes "
                        "is a real package now and every submodule holds its "
                        "own copy of any imported name, so this silently "
                        "patches nothing the real call site sees. Use "
                        "tests.conftest.patch_video_modes_everywhere("
                        "monkeypatch, name, value) instead."
                    )
            # monkeypatch.setattr("app.video_modes.name", value) resolves the
            # dotted string to (module, attr) internally -- same
            # package-only reach as the object form above. mock.patch(
            # "app.video_modes.name") (bare `patch(...)` or `mock.patch(...)`)
            # takes the same string shape, so both call forms are checked
            # against the same target.
            if (is_setattr_call or is_patch_call) and node.args:
                target = node.args[0]
                if _is_bare_video_modes_attr_string(target):
                    violations.append(
                        f"{path}:{node.lineno}: string-form patch on "
                        f"{ast.literal_eval(target)!r} only reaches "
                        "app.video_modes's own re-export, not the submodule "
                        "that actually binds the name -- same silent no-op "
                        "as the object form. Use tests.conftest."
                        "patch_video_modes_everywhere(monkeypatch, name, "
                        "value) instead."
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
                        f"{assign_target.value.id}.{assign_target.attr} = ... "
                        "only rebinds the package attribute, not the "
                        "submodule-owned copy of the name -- same silent "
                        "no-op. Use tests.conftest."
                        "patch_video_modes_everywhere(monkeypatch, name, "
                        "value) instead."
                    )

    return violations


def test_no_bare_app_video_modes_package_monkeypatch() -> None:
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
