"""Blank out stale ``provider_calls`` request/response bodies to reclaim space.

``provider_calls`` logs every LLM/video-provider call. The metadata columns
(``ts``/``kind``/``status``/``model``/``latency_ms``/``operation_id``/``run_id``/...)
are cheap and are the durable audit trail -- they stay forever. The
``request_json``/``response_json`` bodies are the expensive part (chat
transcripts, base64 reference images, full provider payloads) and are only
worth keeping while an incident is still being actively debugged. This
script blanks those two columns (sets them to SQL ``NULL``, matching the
column's existing "no payload yet" state used for RUNNING calls) on rows
older than a configurable retention window. Rows are never deleted.

Why NULL and not ``''``: the schema has no ``NOT NULL`` constraint on either
column (see ``app/db.py``'s ``CREATE TABLE provider_calls``), and NULL is
already the sentinel every reader uses for "no body" -- ``... IS NOT NULL``
guards in ``app/hiagent.py``, ``app/db.py``, ``app/video_plan.py``,
``app/screenplay_scene_shards.py``, ``app/media_exec/{run_job,enqueue}.py``,
``app/completion_grant.py``, ``app/system_api.py`` and ``app/observability/api.py``
all already filter or fall back (``row["response_json"] or "{}"``) around a
NULL body. An empty string would slip past every one of those ``IS NOT NULL``
filters and then blow up the first ``json.loads("")`` inside the loop instead
of being cleanly excluded by the SQL predicate -- NULL is the only sentinel
that reproduces today's "no cached body yet" behaviour for old rows.

Exceptions (rows that are never touched, regardless of age):
  * ``status != 'OK'``  -- INTERRUPTED/FAILED/... calls are exactly what an
    incident postmortem re-reads, and measured on 2026-08-26 they are only
    ~9% of total payload bytes, so exempting them costs little.
  * ``kind = 'blueprint_authority_resolution'`` -- not a real provider call;
    ``app/stages.py`` reuses this table as a durable CAS receipt store for
    screenplay-blueprint retry authority. Its own request/response_json
    *is* the state (artifact_id/artifact_hash/receipts_hash), and blanking
    it would make an in-window retry raise BLUEPRINT_RESOLUTION_RECEIPT_INVALID
    instead of degrading gracefully. In practice this is moot under any
    sane --keep-days: the grant that gates re-entry into that code path
    (``app/production/grant.py``, ``GRANT_TTL_S = 6 * 3600``) expires in 6h,
    long before a row would ever become scrub-eligible -- but the exclusion
    costs nothing (near-zero row volume) so it stays as defense in depth.
  * explicit ``--protect-id`` (repeatable) -- pin specific call ids.
  * ids referenced by ``provider_calls...id=NNNN`` in ``app/**/*.py`` code
    comments (auto-discovered every run; see ``_ids_referenced_in_comments``).
    Several modules (``app/production/prep_pack.py``, ``app/portraits.py``)
    cite exact row ids as evidence for specific historical bugs; the source
    tree itself is the freshest signal for "still cited", so this is derived
    from a live grep rather than a maintained list. Disable with
    ``--no-protect-comment-refs`` if that grep ever gets too broad.

Defaults to a dry run. Pass --execute to actually write. VACUUM is a
separate, never-implied-by-default option -- see its --help text.

Usage:
    py scripts/purge_provider_call_payloads.py                      # report only
    py scripts/purge_provider_call_payloads.py --backup             # snapshot first
    py scripts/purge_provider_call_payloads.py --execute            # blank rows
    py scripts/purge_provider_call_payloads.py --execute --vacuum   # + reclaim file space
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "data" / "manju.db"
DEFAULT_KEEP_DAYS = 30.0
DEFAULT_BATCH_SIZE = 500

# Never scrubbed regardless of age -- see module docstring.
_EXEMPT_KINDS: tuple[str, ...] = ("blueprint_authority_resolution",)

_COMMENT_ID_RE = re.compile(r"provider_calls[^\n]{0,80}?id[=:\s]*(\d+(?:/\d+)*)")


def _ids_referenced_in_comments(root: Path = ROOT) -> set[int]:
    """Grep app/**/*.py for ``provider_calls...id=NNNN[/MMMM...]`` mentions.

    These are ids developers cited by hand as evidence while root-causing a
    bug (e.g. ``app/production/prep_pack.py``: "复核（provider_calls id=10582，
    EP1，可复核 request_json）"). Re-derived from the source tree on every
    run instead of a static list, so it can't silently go stale.
    """
    ids: set[int] = set()
    app_dir = root / "app"
    if not app_dir.is_dir():
        return ids
    for path in app_dir.rglob("*.py"):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for match in _COMMENT_ID_RE.finditer(text):
            for piece in match.group(1).split("/"):
                if piece.isdigit():
                    ids.add(int(piece))
    return ids


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _build_where(cutoff_ts: float, protect_ids: set[int]) -> tuple[str, list]:
    clauses = [
        "status='OK'",
        "ts < ?",
        "(request_json IS NOT NULL OR response_json IS NOT NULL)",
    ]
    params: list = [cutoff_ts]
    for kind in _EXEMPT_KINDS:
        clauses.append("kind != ?")
        params.append(kind)
    if protect_ids:
        placeholders = ",".join("?" for _ in protect_ids)
        clauses.append(f"id NOT IN ({placeholders})")
        params.extend(sorted(protect_ids))
    return " AND ".join(clauses), params


def scan(conn: sqlite3.Connection, cutoff_ts: float, protect_ids: set[int]) -> dict:
    where, params = _build_where(cutoff_ts, protect_ids)
    row = conn.execute(
        f"""SELECT COUNT(*) AS rows,
                   SUM(COALESCE(length(request_json),0) + COALESCE(length(response_json),0)) AS bytes
              FROM provider_calls WHERE {where}""",
        params,
    ).fetchone()
    return {"rows": row["rows"] or 0, "bytes": row["bytes"] or 0}


def purge(
    conn: sqlite3.Connection,
    cutoff_ts: float,
    protect_ids: set[int],
    batch_size: int,
) -> dict:
    where, params = _build_where(cutoff_ts, protect_ids)
    select_sql = f"SELECT id FROM provider_calls WHERE {where} ORDER BY id LIMIT ?"
    total_rows = 0
    total_bytes = 0
    while True:
        ids = [r["id"] for r in conn.execute(select_sql, [*params, batch_size]).fetchall()]
        if not ids:
            break
        placeholders = ",".join("?" for _ in ids)
        size_row = conn.execute(
            f"""SELECT SUM(COALESCE(length(request_json),0) + COALESCE(length(response_json),0)) AS bytes
                  FROM provider_calls WHERE id IN ({placeholders})""",
            ids,
        ).fetchone()
        total_bytes += size_row["bytes"] or 0
        conn.execute(
            f"UPDATE provider_calls SET request_json=NULL, response_json=NULL WHERE id IN ({placeholders})",
            ids,
        )
        conn.commit()
        total_rows += len(ids)
    return {"rows": total_rows, "bytes": total_bytes}


def backup_db(db_path: Path, dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = dest_dir / f"{db_path.stem}-before-payload-purge-{stamp}.db"
    src_conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        dest_conn = sqlite3.connect(str(dest))
        try:
            src_conn.backup(dest_conn)
        finally:
            dest_conn.close()
    finally:
        src_conn.close()
    return dest


def run_vacuum(conn: sqlite3.Connection) -> None:
    conn.execute("VACUUM")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help=f"sqlite db path (default {DEFAULT_DB})")
    parser.add_argument(
        "--keep-days", type=float, default=DEFAULT_KEEP_DAYS,
        help=f"rows with ts older than this many days become scrub-eligible (default {DEFAULT_KEEP_DAYS})",
    )
    parser.add_argument(
        "--execute", action="store_true",
        help="actually blank the eligible rows (default is a dry-run report only)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="force dry-run reporting even if --execute is also given (explicit, matches the default)",
    )
    parser.add_argument(
        "--protect-id", type=int, action="append", default=[], metavar="CALL_ID",
        help="never scrub this provider_calls.id, regardless of age (repeatable)",
    )
    parser.add_argument(
        "--no-protect-comment-refs", action="store_true",
        help="disable auto-protection of ids cited by 'provider_calls...id=NNNN' in app/**/*.py comments",
    )
    parser.add_argument(
        "--batch-size", type=int, default=DEFAULT_BATCH_SIZE,
        help=f"rows per UPDATE/commit batch, to bound lock duration on a live db (default {DEFAULT_BATCH_SIZE})",
    )
    parser.add_argument(
        "--backup", action="store_true",
        help="snapshot the db to logs/backups/ via the sqlite online backup API, then exit "
             "(run this before --execute; combine with --execute to backup then purge in one call)",
    )
    parser.add_argument(
        "--backup-dir", type=Path, default=ROOT / "logs" / "backups",
        help="directory for --backup snapshots (default logs/backups/)",
    )
    parser.add_argument(
        "--vacuum", action="store_true",
        help="run VACUUM after purging (or standalone with no --execute purge work to do). "
             "NEVER implied by --execute alone: VACUUM rewrites the whole file under an "
             "exclusive lock and will stall a live backend for as long as that takes. "
             "Blanking a column only makes its pages reusable by future writes -- the .db "
             "file will NOT shrink on disk until VACUUM (or equivalent) runs.",
    )
    args = parser.parse_args(argv)

    if not args.db.exists():
        print(f"db not found: {args.db}", file=sys.stderr)
        return 2

    if args.backup:
        dest = backup_db(args.db, args.backup_dir)
        print(f"backed up {args.db} -> {dest} ({dest.stat().st_size:,} bytes)")
        if not args.execute and not args.vacuum:
            return 0

    dry_run = args.dry_run or not args.execute

    protect_ids = set(args.protect_id)
    if not args.no_protect_comment_refs:
        discovered = _ids_referenced_in_comments()
        if discovered:
            print(f"auto-protected {len(discovered)} id(s) cited in app/**/*.py comments: "
                  f"{sorted(discovered)}")
        protect_ids |= discovered

    cutoff_ts = time.time() - args.keep_days * 86400
    cutoff_label = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(cutoff_ts))

    conn = _connect(args.db)
    try:
        before_size = args.db.stat().st_size
        stats = scan(conn, cutoff_ts, protect_ids)
        print(f"db: {args.db}")
        print(f"keep-days: {args.keep_days}  (cutoff ts={cutoff_ts:.0f}, {cutoff_label})")
        print(f"exempt kinds: {', '.join(_EXEMPT_KINDS) or '(none)'}  |  exempt status: != 'OK'")
        print(f"protected ids: {len(protect_ids)}")
        print(f"{'would blank' if dry_run else 'blanking'} {stats['rows']:,} row(s), "
              f"{stats['bytes']:,} bytes of request_json/response_json payload")

        if dry_run:
            print("dry run: no changes written. Pass --execute to apply.")
        elif stats["rows"] == 0:
            print("nothing to purge.")
        else:
            result = purge(conn, cutoff_ts, protect_ids, args.batch_size)
            print(f"blanked {result['rows']:,} row(s), freed {result['bytes']:,} logical bytes "
                  f"(file will not shrink until VACUUM)")

        # --vacuum is its own explicit trigger (never implied by --execute, and never
        # blocked by a dry run of the blanking step): it can also run standalone to
        # reclaim space freed by an earlier --execute pass.
        if args.vacuum:
            print("running VACUUM (this takes an exclusive lock and may take a while)...")
            run_vacuum(conn)
            print("VACUUM complete.")
        elif dry_run:
            return 0

        after_size = args.db.stat().st_size
        print(f"file size: {before_size:,} -> {after_size:,} bytes "
              f"({'unchanged, as expected without --vacuum' if before_size == after_size else 'shrank'})")
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
