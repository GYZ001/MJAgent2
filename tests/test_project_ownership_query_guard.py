"""Guard: every query against ``projects`` that can return more than one row
must carry an explicit ownership filter (structural backstop for account
isolation, not a "remember to check" convention).

Why this exists: account-level isolation (1 account = 1 independent project
space) has exactly one HTTP-boundary gate today
(``app.authz.resolve.require_project_owner_access``, wired onto path params
like ``project_id``/``episode_id``/...). That is a single perimeter -- miss
wiring it onto one new route and the leak is silent (this repo has already
shipped one silent fail-open of an equivalent gate; see
``app/local_session.py``'s ``bind_request_principal`` docstring). This test is
the second, independent layer: it does not care whether any particular route
remembered to call the HTTP gate -- it statically proves that no query
capable of returning *multiple* projects across owner boundaries exists
anywhere in ``app/`` without an inline ``owner_user_id`` filter. If someone
adds a new listing/search endpoint tomorrow and forgets the filter, this
guard turns that mistake into a CI failure the moment the query is written,
not into a support ticket after a customer sees someone else's project.

Detection, derived from the source itself (no maintained list of "known safe
files" beyond the one categorical exemption explained below):

1. Walk every ``app/**/*.py`` file with ``ast``, find every
   ``<expr>.execute(...)`` / ``<expr>.executemany(...)`` / ``<expr>.
   executescript(...)`` call.
2. Statically extract the source text of the first positional argument
   (string literal / f-string / implicit or ``+`` concatenation --
   whatever ``ast.get_source_segment`` can resolve for it). Calls that build
   their SQL through an opaque variable are skipped: not because they're
   assumed safe, but because this guard cannot see their text at all, and an
   exhaustive sweep of the codebase at the time this guard was written found
   zero ``projects``-touching queries built that way (every real call site
   inlines its SQL). If that ever changes, this guard's blind spot grows
   silently -- see ``test_no_opaque_sql_variables_hide_projects_queries``
   below, which independently catches the one shape that would create such a
   blind spot (assigning a ``projects``-mentioning string to a variable and
   passing the variable to ``.execute``).
3. A match against ``FROM projects`` / ``JOIN projects`` (word-bounded,
   case-insensitive) passes if the *same extracted text* satisfies any of:
   a. contains ``owner_user_id`` -- the explicit filter every listing query
      in this codebase now carries (see ``app.domain.projects.list_projects``
      / ``list_deleted_projects``).
   b. anchors on the ``projects`` table's own primary key (``id =`` /
      ``id IN (`` -- ``\\bid\\b`` so this does not accidentally match
      ``owner_user_id=`` or ``episode_id=``). A lookup keyed to one already-
      known id cannot enumerate rows across owners by construction: the
      leak surface for *that specific id* was already closed at the point
      the id entered the system (an HTTP path param validated by
      ``require_project_owner_access``, or a row the caller already held
      and was independently entitled to, e.g. ``ep["project_id"]`` off an
      episode row already fetched through the same gate).
   c. the text carries the explicit ``-- ALL_OWNERS: <reason>`` SQL comment
      marker -- the escape hatch for the handful of call sites that
      *intentionally* return every project regardless of caller (a system-
      admin-only dashboard, a background/startup/maintenance routine with no
      request context -- task-recovery-on-reload, the recycle-bin sweep --
      or the admin/no-principal branch of ``list_projects()``). This has to
      be a conscious, greppable, self-documenting declaration written at the
      call site, not a separately maintained list of "files we decided are
      fine", and NOT an automatic pass just because the call happens to sit
      outside a ``@router``-decorated function -- most business logic in
      this codebase lives in ``app/domain/**`` and is called *by* a route
      handler, not decorated as one itself, and still runs inside the
      original request's Principal context (see
      ``app.auth.principal.get_current_principal``, readable from any
      function, not just route handlers -- ``app.domain.common.
      _project_or_404`` already relies on exactly this). Every use must
      justify itself right there -- see ``_ALL_OWNERS_MARKER_RE`` below for
      the existing uses (system-admin dashboards/branches in
      ``app/domain/projects/listing.py`` and ``app/observability/api.py``, and
      startup-recovery/background-sweep scans with no request context in
      ``app/domain/bible_ops/task_recovery.py``, ``app/planning.py`` and
      ``app/domain/projects/lifecycle.py``).

``app/db.py`` is excluded categorically, not because someone reviewed it and
signed off: schema definition (``SCHEMA``), additive migrations
(``MIGRATIONS``), and integrity repair (``INTEGRITY_SCHEMA`` /
``_drop_obsolete_*`` / ``_migrate_project_ownership_and_drop_team_model``)
are not request-scoped code -- they run at process startup or from a
maintenance CLI, over the whole table, by design (an orphan-row repair that
only looked at *some* projects would be a bug, not a safety feature). The
exemption is self-checking, not a bare assertion: this test also asserts
``app/db.py`` never imports ``get_current_principal`` -- the one signal that
would mean it started doing request-scoped work -- and fails loudly if that
ever changes, instead of staying silently exempt.
"""
from __future__ import annotations

import ast
import inspect
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "app"

# Categorical exemption: the schema/migration/integrity-repair module itself.
# See module docstring -- this is not "we reviewed it once", it's "this file
# defines the schema and runs outside any request", checked below.
_SCHEMA_MODULE = APP_DIR / "db.py"

_EXECUTE_METHODS = {"execute", "executemany", "executescript"}

_PROJECTS_TABLE_RE = re.compile(r"\b(from|join)\s+projects\b", re.IGNORECASE)
_OWNER_FILTER_RE = re.compile(r"owner_user_id", re.IGNORECASE)
_ID_ANCHOR_RE = re.compile(r"\bid\b\s*(=|in\s*\()", re.IGNORECASE)
# Explicit, self-documenting escape hatch for the handful of call sites that
# *intentionally* return every project regardless of owner (system-admin-only
# dashboards, the caller-is-admin branch of list_projects()). It has to live
# inside the SQL text itself (a SQL "--" comment, not a Python "#" comment --
# ast.get_source_segment() only sees the expression's own span, comments
# above/beside it are not part of that span) so grepping the SQL a reviewer
# actually runs is enough to find every use, with no separate list to keep in
# sync. Every use must justify itself right there -- see the existing uses in
# app/domain/projects/listing.py, app/domain/projects/lifecycle.py,
# app/observability/api.py, app/domain/bible_ops/task_recovery.py and
# app/planning.py.
_ALL_OWNERS_MARKER_RE = re.compile(r"--\s*ALL_OWNERS:")


def _iter_py_files() -> list[Path]:
    return sorted(p for p in APP_DIR.rglob("*.py") if p != _SCHEMA_MODULE)


def _sql_text_of(source: str, call: ast.Call) -> str | None:
    if not call.args:
        return None
    first = call.args[0]
    if not isinstance(first, (ast.Constant, ast.JoinedStr, ast.BinOp)):
        return None
    return ast.get_source_segment(source, first)


def _execute_calls(tree: ast.AST) -> list[ast.Call]:
    calls = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in _EXECUTE_METHODS
        ):
            calls.append(node)
    return calls


def _violations_in_file(path: Path) -> list[str]:
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return [f"{path}: could not parse"]
    rel = path.relative_to(ROOT).as_posix()
    violations = []
    for call in _execute_calls(tree):
        text = _sql_text_of(source, call)
        if not text or not _PROJECTS_TABLE_RE.search(text):
            continue
        if (
            _OWNER_FILTER_RE.search(text)
            or _ID_ANCHOR_RE.search(text)
            or _ALL_OWNERS_MARKER_RE.search(text)
        ):
            continue
        violations.append(
            f"{rel}:{call.lineno}: query touches projects without an "
            f"owner_user_id filter, a projects.id anchor, or an explicit "
            f"-- ALL_OWNERS: <reason> marker -> {text.strip()[:160]}"
        )
    return violations


def test_no_unfiltered_multi_row_projects_queries() -> None:
    violations: list[str] = []
    for path in _iter_py_files():
        violations.extend(_violations_in_file(path))
    assert not violations, (
        "Found projects-table queries that can return rows across account "
        "boundaries without an ownership filter (see this test module's "
        "docstring for the exact rule):\n" + "\n".join(violations)
    )


def test_db_module_never_becomes_request_scoped() -> None:
    """Self-check for the one categorical exemption this guard makes.

    ``app/db.py`` is excluded from the scan above because it is schema/
    migration/integrity-repair infrastructure, not request-scoped business
    logic -- it never reads the current Principal. If that ever stops being
    true, the exemption above is no longer justified and this assertion
    (not a silently-still-passing scan) is what should fail first.
    """
    source = _SCHEMA_MODULE.read_text(encoding="utf-8")
    assert "get_current_principal" not in source, (
        "app/db.py now references get_current_principal -- it has started "
        "doing request-scoped work, so it can no longer be categorically "
        "exempt from test_no_unfiltered_multi_row_projects_queries(). "
        "Remove the exemption in this guard and let the scan cover it."
    )


def test_no_opaque_sql_variables_hide_projects_queries() -> None:
    """Independent check for the guard's one acknowledged blind spot.

    The main scan can only see SQL text that is a literal/f-string/
    concatenation inlined directly into the ``.execute(...)`` call -- it
    cannot resolve ``sql = "...FROM projects..."; conn.execute(sql)``. This
    check closes that blind spot from the other direction: it flags any
    string assignment anywhere in ``app/`` (outside app/db.py, same
    exemption as above) whose value mentions ``FROM projects``/``JOIN
    projects`` and is not inlined directly at the call site, so a future
    opaque-variable pattern gets caught here even though the main scan would
    silently skip it.
    """
    offenders = []
    for path in _iter_py_files():
        source = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError:
            continue
        rel = path.relative_to(ROOT).as_posix()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            value = node.value
            if not isinstance(value, (ast.Constant, ast.JoinedStr, ast.BinOp)):
                continue
            text = ast.get_source_segment(source, value)
            if text and _PROJECTS_TABLE_RE.search(text):
                offenders.append(f"{rel}:{node.lineno}: {text.strip()[:160]}")
    assert not offenders, (
        "SQL text mentioning projects is being built in a variable instead "
        "of inlined at the .execute(...) call site -- test_project_ownership_"
        "query_guard.py's main scan cannot verify these; inline them or "
        "extend the guard to resolve the variable:\n" + "\n".join(offenders)
    )


def test_preflight_entry_points_route_through_ownership_helpers() -> None:
    """P0-1 blind spot this guard used to have (found by manual audit, not by
    this scan): app/capabilities/preflight.py's functions are the Command
    Bus's ``spec.preflight`` callables (see ``PREFLIGHT_MAP`` at the bottom of
    that module) -- ``CommandBus.preflight()``/``preflight_async()`` calls
    them *before* any ownership check runs. ``CommandBus._authorize()`` only
    checks "system-admin-only" at the bus level; its own docstring says
    "is this project yours" is deliberately left to the HTTP-boundary
    ``app.authz.resolve.require_project_owner_access`` -- which only inspects
    *path* params. An Agent/MCP tool call embeds project_id/episode_id/
    shot_id in the command's JSON args, not a URL path segment, so that gate
    never runs for them.

    ``test_no_unfiltered_multi_row_projects_queries`` above could not catch
    this: its id-anchor exemption assumes any ``WHERE id=?`` lookup is safe
    because the id "already passed through a gate upstream" (true for HTTP
    path params, or for an id read off an already-fetched row) -- false here,
    since every ``PREFLIGHT_MAP`` function receives its id straight off
    ``args`` with zero upstream gate. This is exactly how
    app/capabilities/preflight.py ended up with real cross-account
    information leaks in ``project_delete`` and every sibling function
    (project name, episode/shot counts, cost estimates -- all readable by any
    logged-in account for any other account's project/episode/shot id).

    Fix pattern: every ``PREFLIGHT_MAP`` function must resolve its primary
    object through one of ``app.domain.common.owned_project_row``/
    ``owned_episode_row``/``owned_shot_row`` (the non-raising siblings of
    ``_project_or_404``/``_episode_or_404`` -- same ownership judgement,
    returns ``None`` instead of raising so preflight construction can fold
    "missing" and "not yours" into the existing not-found ``PreflightResult``
    branch), or delegate to another ``PREFLIGHT_MAP`` function that already
    does (e.g. ``screenplay_repair_draft`` calls ``screenplay_update(args)``).
    This test does not hardcode which function needs which helper -- it
    walks the real ``PREFLIGHT_MAP`` dict (so a newly registered command is
    covered automatically) and requires each function's own source to
    reference an ownership helper or another registered preflight function.
    """
    from app.capabilities import preflight as preflight_module

    helper_names = ("owned_project_row", "owned_episode_row", "owned_shot_row")
    offenders = []
    for command, fn in preflight_module.PREFLIGHT_MAP.items():
        source = inspect.getsource(fn)
        references_helper = any(name in source for name in helper_names)
        delegates = any(
            f"{other.__name__}(args)" in source
            for other in preflight_module.PREFLIGHT_MAP.values()
            if other is not fn
        )
        if not references_helper and not delegates:
            offenders.append(f"{command} ({fn.__name__})")
    assert not offenders, (
        "These Command Bus preflight constructors resolve project_id/"
        "episode_id/shot_id straight off command args against projects/"
        "episodes/shots without going through app.domain.common."
        "owned_project_row/owned_episode_row/owned_shot_row -- see this "
        "test's docstring for why the general query guard above cannot see "
        "this class of bug:\n" + "\n".join(offenders)
    )


def test_delivery_readiness_routes_through_ownership_helper() -> None:
    """Same P0-1 blind spot as the preflight test above, for
    ``app.delivery.delivery_readiness``: it is reachable from
    ``app/capabilities/handlers/delivery.py::check()`` (a Command Bus
    handler -- episode_id from command args, not an HTTP path param) and
    from the ``delivery`` MCP Resource in ``app/mcp/resources.py`` (MCP has
    no session gate at all -- see ``app/main.py``: "MCP 使用 Bearer
    Token，不叠本机会话闸门"). Its own ``SELECT * FROM episodes WHERE id=?``
    used to be a bare existence check with no ownership filter, on a
    function keyed only by table name (``episodes``, not ``projects``) --
    outside even the general scan's ``_PROJECTS_TABLE_RE`` coverage, on top
    of the same id-anchor blind spot the preflight test documents.
    """
    from app import delivery as delivery_module

    source = inspect.getsource(delivery_module.delivery_readiness)
    assert "owned_episode_row" in source, (
        "app.delivery.delivery_readiness no longer resolves its episode_id "
        "through app.domain.common.owned_episode_row -- a raw "
        "'SELECT * FROM episodes WHERE id=?' here is a cross-account "
        "information leak for the Command Bus / MCP callers documented in "
        "this test's docstring, not a false alarm."
    )
