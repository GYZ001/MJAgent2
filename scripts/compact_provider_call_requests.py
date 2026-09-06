"""把历史 provider_calls.request_json 里原样落库的 base64 参考图换成摘要占位（2026-09-06）。

B 库实测 video_create 3,853 行平均 1.6 MB、合计 6 GB，是 7.8 GB 库体积与 2 GB WAL 的主因。
运行时新行已由 ``app.observability.provider_call_payload`` 压缩；本脚本按同一函数回填历史行，
所以回填后与新行可用同一形态比较（seedance 重启漂移核对）。

用法（在 B 上，业务空闲时）：
    /root/MJAgent2/.venv/bin/python scripts/compact_provider_call_requests.py --dry-run
    /root/MJAgent2/.venv/bin/python scripts/compact_provider_call_requests.py
    /root/MJAgent2/.venv/bin/python scripts/compact_provider_call_requests.py --vacuum   # 回收磁盘，独占库几分钟

每批 20 行提交，busy_timeout 30 秒，跑在生产上也只是短事务；结束做一次 wal_checkpoint(TRUNCATE)。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import DB_PATH  # noqa: E402
from app.observability.provider_call_payload import compact_exact_request  # noqa: E402

MIN_CHARS = 200_000
BATCH = 20


def _compact_rows(conn: sqlite3.Connection, *, dry_run: bool) -> tuple[int, int]:
    rows = conn.execute(
        "SELECT id, request_json FROM provider_calls WHERE length(request_json) > ? ORDER BY id", (MIN_CHARS,)
    ).fetchall()
    changed = saved_chars = 0
    for offset in range(0, len(rows), BATCH):
        for call_id, raw in rows[offset:offset + BATCH]:
            try:
                compacted = json.dumps(compact_exact_request(json.loads(raw)), ensure_ascii=False, separators=(",", ":"))
            except (TypeError, ValueError):
                continue
            if len(compacted) >= len(raw):
                continue
            changed += 1
            saved_chars += len(raw) - len(compacted)
            if not dry_run:
                conn.execute("UPDATE provider_calls SET request_json=? WHERE id=?", (compacted, call_id))
        if not dry_run:
            conn.commit()
    return changed, saved_chars


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--vacuum", action="store_true", help="回填后 VACUUM 回收磁盘（独占库，业务空闲时用）")
    parser.add_argument("--db", default=str(DB_PATH))
    args = parser.parse_args()
    conn = sqlite3.connect(args.db, timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    started = time.time()
    changed, saved_chars = _compact_rows(conn, dry_run=args.dry_run)
    print(f"{'预览' if args.dry_run else '已回填'} {changed} 行，省下 {saved_chars / 1e6:.0f} MB，用时 {time.time() - started:.0f}s")
    if not args.dry_run:
        print("wal_checkpoint(TRUNCATE):", conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone())
        if args.vacuum:
            started = time.time()
            conn.execute("VACUUM")
            print(f"VACUUM 完成，用时 {time.time() - started:.0f}s")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
