"""Hot-backup ``data/manju.db`` via the SQLite online backup API, verify, compress, prune.

Why not ``cp``: the live db runs in WAL mode (``app/db.py`` sets
``PRAGMA journal_mode=WAL``), so committed data can be split across
``manju.db`` and the ``manju.db-wal`` side file. A plain file copy taken
mid-write can land between those two and produce a torn, unopenable
snapshot. This script instead opens the source **read-only**
(``file:...?mode=ro``) and uses ``sqlite3.Connection.backup()``, which is
SQLite's own online/hot-backup API: it walks the source page-by-page inside
a read transaction, is safe to run concurrently with the live backend's
readers and writers, and retries automatically (``sleep=`` param) if a
writer's checkpoint transiently collides with a step.

Pipeline for one run:
  1. Preflight: check free disk space on the backup filesystem.
  2. Backup source -> ``<backup_dir>/.tmp/manju-<ts>.db.partial`` (retried
     up to ``--retries`` times on transient sqlite3.OperationalError).
  3. Convert the partial copy to rollback-journal mode (``PRAGMA
     journal_mode=DELETE``) so the artifact is a single self-contained file
     with no ``-wal``/``-shm`` siblings.
  4. Verify the partial copy with a **fresh, independent** read-only
     connection: ``PRAGMA integrity_check``, ``PRAGMA foreign_key_check``,
     and presence of the app's core tables.
  5. On verify failure: move the partial into ``<backup_dir>/quarantine/``
     with a ``.failed`` suffix and STOP -- the previous good backup and the
     ``manju-latest.db.gz`` pointer are left untouched. This is the
     "verification failure must not clobber a good backup" guarantee.
  6. On verify success: gzip-compress into ``<backup_dir>/manju-<ts>.db.gz``
     (written via a temp name + atomic ``os.replace``), then atomically
     repoint the ``manju-latest.db.gz`` symlink at it.
  7. Prune old backups per the retention policy (see ``prune_backups``) and
     old quarantine files (see ``prune_quarantine``).

Everything is logged to ``logs/backup_manju_db.log`` (rotating by size) so
failures leave a visible trail instead of failing silently under cron.

Usage:
    py scripts/backup_manju_db.py                  # run one backup
    py scripts/backup_manju_db.py --prune-only      # only apply retention (for testing)
    py scripts/backup_manju_db.py --no-compress     # skip gzip (debugging)

Restore:
    gunzip -k /var/backups/mjagent2/db/manju-<ts>.db.gz
    sqlite3 /var/backups/mjagent2/db/manju-<ts>.db "PRAGMA integrity_check;"
    # then copy the .db file over data/manju.db with the backend stopped.
"""
from __future__ import annotations

import argparse
import gzip
import logging
import logging.handlers
import os
import re
import shutil
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "data" / "manju.db"
DEFAULT_BACKUP_DIR = Path("/var/backups/mjagent2/db")
DEFAULT_LOG = ROOT / "logs" / "backup_manju_db.log"

# Tables that must exist in a healthy manju.db (see app/db.py CREATE TABLE
# statements). Not exhaustive -- just enough to catch "opened an empty or
# wrong file" without hardcoding every table (schema evolves independently
# of this script).
CORE_TABLES = ("projects", "episodes", "shots", "shot_versions", "provider_calls", "users")

TS_RE = re.compile(r"^manju-(\d{8}-\d{6})\.db\.gz$")
TS_FMT = "%Y%m%d-%H%M%S"

DAILY_RETENTION_DAYS = 7
WEEKLY_RETENTION_DAYS = 35  # 7 daily + 4 more weekly buckets = 35 days total
FAILED_RETENTION_DAYS = 3

# Backup must not be allowed to fill the disk: require the source db size
# times this factor to be free before starting (temp uncompressed copy +
# gzip output coexist briefly).
FREE_SPACE_FACTOR = 1.3

log = logging.getLogger("backup_manju_db")


def _setup_logging(log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.setLevel(logging.INFO)
    log.addHandler(handler)
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(stream)


def _fmt_bytes(n: int) -> str:
    v = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if v < 1024 or unit == "GB":
            return f"{v:.1f}{unit}"
        v /= 1024
    return f"{v:.1f}GB"


def _preflight_space(db_path: Path, backup_dir: Path) -> None:
    needed = int(db_path.stat().st_size * FREE_SPACE_FACTOR)
    usage = shutil.disk_usage(backup_dir)
    if usage.free < needed:
        raise RuntimeError(
            f"insufficient free space on {backup_dir}: need ~{_fmt_bytes(needed)}, "
            f"have {_fmt_bytes(usage.free)}"
        )
    log.info(
        "preflight ok: db=%s free_space=%s needed=%s",
        _fmt_bytes(db_path.stat().st_size), _fmt_bytes(usage.free), _fmt_bytes(needed),
    )


def _hot_backup(db_path: Path, dest_path: Path, retries: int, sleep_s: float) -> None:
    """Copy db_path -> dest_path via the SQLite online backup API.

    Source is opened read-only so this never blocks or is blocked by the
    live backend's writers. Retries the whole attempt on transient
    sqlite3.OperationalError (belt-and-suspenders on top of backup()'s own
    internal BUSY/LOCKED retry loop, which already sleeps `sleep_s` between
    step attempts).
    """
    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        dest_path.unlink(missing_ok=True)
        source = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
        try:
            source.execute("PRAGMA query_only=ON")
            target = sqlite3.connect(str(dest_path), timeout=30)
            try:
                source.backup(target, sleep=sleep_s)
                # Fold the copy back to rollback-journal mode so the backup
                # artifact is a single portable file (no -wal/-shm needed).
                target.execute("PRAGMA journal_mode=DELETE")
                target.commit()
            finally:
                target.close()
            return
        except sqlite3.OperationalError as exc:
            last_exc = exc
            log.warning("backup attempt %d/%d failed: %s", attempt, retries, exc)
            time.sleep(sleep_s * (2 ** (attempt - 1)))
        finally:
            source.close()
    assert last_exc is not None
    raise last_exc


def verify_backup(db_path: Path) -> dict:
    """Open db_path with an independent read-only connection and check it.

    Independent from whatever connection wrote it -- a connection that
    reads back its own uncommitted buffers isn't proof the file on disk is
    actually valid.
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
    try:
        integrity = [row[0] for row in conn.execute("PRAGMA integrity_check").fetchall()]
        fk_errors = conn.execute("PRAGMA foreign_key_check").fetchall()
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        missing = [t for t in CORE_TABLES if t not in tables]
        counts = {}
        for t in CORE_TABLES:
            if t in tables:
                counts[t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        ok = integrity == ["ok"] and not fk_errors and not missing
        return {
            "ok": ok,
            "integrity_check": integrity,
            "foreign_key_check": [list(r) for r in fk_errors],
            "missing_tables": missing,
            "table_count": len(tables),
            "core_table_counts": counts,
        }
    finally:
        conn.close()


def _quarantine(partial: Path, quarantine_dir: Path, ts: str, reason: str) -> Path:
    quarantine_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(quarantine_dir, 0o700)
    dest = quarantine_dir / f"manju-{ts}.db.failed"
    shutil.move(str(partial), str(dest))
    os.chmod(dest, 0o600)
    reason_path = quarantine_dir / f"manju-{ts}.reason.txt"
    reason_path.write_text(reason, encoding="utf-8")
    os.chmod(reason_path, 0o600)
    return dest


def _compress(src: Path, backup_dir: Path, ts: str) -> Path:
    final = backup_dir / f"manju-{ts}.db.gz"
    tmp = backup_dir / f"manju-{ts}.db.gz.tmp"
    with open(src, "rb") as f_in, gzip.open(tmp, "wb", compresslevel=6) as f_out:
        shutil.copyfileobj(f_in, f_out, length=16 * 1024 * 1024)
    os.chmod(tmp, 0o600)
    os.replace(tmp, final)  # atomic on same filesystem
    return final


def _update_latest_symlink(backup_dir: Path, target: Path) -> None:
    link = backup_dir / "manju-latest.db.gz"
    tmp_link = backup_dir / "manju-latest.db.gz.tmp"
    tmp_link.unlink(missing_ok=True)
    tmp_link.symlink_to(target.name)
    os.replace(tmp_link, link)  # atomic rename, only touched after verify passed


def prune_backups(backup_dir: Path, now: datetime | None = None) -> list[Path]:
    """Apply the retention policy: 7 daily + 1/week for the next 4 weeks.

    - age <= 7 days: keep every backup (daily granularity).
    - 7 < age <= 35 days: keep the oldest backup seen per ISO (year, week)
      bucket (deterministic, independent of run order / missed days).
    - age > 35 days: delete.

    Never touches the file the ``manju-latest.db.gz`` symlink points to
    (it is always inside the 7-day window by construction, but the guard
    is explicit here rather than relied upon).
    """
    now = now or datetime.now()
    candidates = []
    for f in backup_dir.glob("manju-*.db.gz"):
        if f.is_symlink():
            continue
        m = TS_RE.match(f.name)
        if not m:
            continue
        ts = datetime.strptime(m.group(1), TS_FMT)
        candidates.append((ts, f))
    candidates.sort(key=lambda pair: pair[0])  # oldest first

    latest_link = backup_dir / "manju-latest.db.gz"
    latest_target = latest_link.resolve() if latest_link.is_symlink() else None

    daily_cutoff = now - timedelta(days=DAILY_RETENTION_DAYS)
    weekly_cutoff = now - timedelta(days=WEEKLY_RETENTION_DAYS)
    weekly_seen: set[tuple[int, int]] = set()
    removed: list[Path] = []
    for ts, f in candidates:
        keep = False
        if ts >= daily_cutoff:
            keep = True
        elif ts >= weekly_cutoff:
            key = ts.isocalendar()[:2]
            if key not in weekly_seen:
                weekly_seen.add(key)
                keep = True
        if latest_target is not None and f.resolve() == latest_target:
            keep = True
        if not keep:
            log.info("prune: removing %s (age=%dd)", f.name, (now - ts).days)
            f.unlink(missing_ok=True)
            removed.append(f)
    return removed


def prune_quarantine(quarantine_dir: Path, now: datetime | None = None) -> list[Path]:
    if not quarantine_dir.exists():
        return []
    now = now or datetime.now()
    cutoff = now - timedelta(days=FAILED_RETENTION_DAYS)
    removed = []
    for f in quarantine_dir.iterdir():
        if not f.is_file():
            continue
        mtime = datetime.fromtimestamp(f.stat().st_mtime)
        if mtime < cutoff:
            log.info("prune quarantine: removing %s (age=%dd)", f.name, (now - mtime).days)
            f.unlink(missing_ok=True)
            removed.append(f)
    return removed


def run_backup(
    db_path: Path,
    backup_dir: Path,
    *,
    compress: bool = True,
    retries: int = 3,
    sleep_s: float = 0.5,
) -> int:
    if not db_path.exists():
        log.error("source db not found: %s", db_path)
        return 2

    tmp_dir = backup_dir / ".tmp"
    quarantine_dir = backup_dir / "quarantine"
    backup_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(backup_dir, 0o700)
    os.chmod(tmp_dir, 0o700)

    ts = datetime.now().strftime(TS_FMT)
    partial = tmp_dir / f"manju-{ts}.db.partial"
    started = time.monotonic()

    try:
        _preflight_space(db_path, backup_dir)
    except Exception as exc:
        log.error("backup aborted, preflight failed: %s", exc)
        return 3

    try:
        _hot_backup(db_path, partial, retries=retries, sleep_s=sleep_s)
    except Exception as exc:
        log.error("backup FAILED during hot-copy: %s", exc)
        partial.unlink(missing_ok=True)
        return 4

    report = verify_backup(partial)
    if not report["ok"]:
        dest = _quarantine(partial, quarantine_dir, ts, reason=repr(report))
        log.error(
            "backup FAILED verification, quarantined at %s (previous good backup and "
            "manju-latest.db.gz left untouched): %s",
            dest, report,
        )
        return 5

    raw_size = partial.stat().st_size
    if compress:
        final = _compress(partial, backup_dir, ts)
        partial.unlink(missing_ok=True)
    else:
        final = backup_dir / f"manju-{ts}.db"
        os.chmod(partial, 0o600)
        os.replace(partial, final)

    if compress:
        _update_latest_symlink(backup_dir, final)

    duration = time.monotonic() - started
    final_size = final.stat().st_size
    log.info(
        "backup OK: %s raw=%s final=%s ratio=%.2fx duration=%.1fs integrity=%s "
        "tables=%d core_counts=%s",
        final.name, _fmt_bytes(raw_size), _fmt_bytes(final_size),
        (raw_size / final_size) if final_size else 0.0, duration,
        report["integrity_check"], report["table_count"], report["core_table_counts"],
    )

    removed = prune_backups(backup_dir)
    removed_q = prune_quarantine(quarantine_dir)
    if removed or removed_q:
        log.info(
            "retention: removed %d old backup(s), %d old quarantine file(s)",
            len(removed), len(removed_q),
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help=f"source sqlite db (default {DEFAULT_DB})")
    parser.add_argument("--backup-dir", type=Path, default=DEFAULT_BACKUP_DIR, help=f"backup dest dir (default {DEFAULT_BACKUP_DIR})")
    parser.add_argument("--log", type=Path, default=DEFAULT_LOG, help=f"log file (default {DEFAULT_LOG})")
    parser.add_argument("--no-compress", action="store_true", help="skip gzip compression (debugging only)")
    parser.add_argument("--retries", type=int, default=3, help="hot-copy attempts on transient sqlite errors")
    parser.add_argument("--prune-only", action="store_true", help="skip the backup step, only apply retention (for testing retention logic)")
    args = parser.parse_args(argv)

    _setup_logging(args.log)

    if args.prune_only:
        removed = prune_backups(args.backup_dir)
        removed_q = prune_quarantine(args.backup_dir / "quarantine")
        log.info("prune-only: removed %d backup(s), %d quarantine file(s)", len(removed), len(removed_q))
        return 0

    try:
        return run_backup(args.db, args.backup_dir, compress=not args.no_compress, retries=args.retries)
    except Exception:
        log.exception("backup crashed with an unhandled exception")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
