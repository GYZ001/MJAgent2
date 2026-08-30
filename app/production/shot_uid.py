"""为既有 shots 补齐稳定 shot_uid（幂等）。"""
from __future__ import annotations

from app.db import get_conn, new_id


def backfill_shot_uids(conn=None) -> int:
    db = conn or get_conn()
    try:
        rows = db.execute(
            "SELECT id FROM shots WHERE shot_uid IS NULL OR shot_uid=''"
        ).fetchall()
    except Exception:  # noqa: BLE001 — 列不存在
        return 0
    updated = 0
    for row in rows:
        db.execute(
            "UPDATE shots SET shot_uid=? WHERE id=?",
            (new_id("shotuid"), row["id"]),
        )
        updated += 1
    if updated:
        db.commit()
    return updated


# app.db.init_db() no longer imports this module directly (P0-3 dependency
# inversion, docs/coupling_review_2026-08-29.md 第2步) — it looks this up by
# name through app.db_schema instead.
from app.db_schema import register_table as _register_table  # noqa: E402

_register_table("shot_uid_backfill", backfill_shot_uids)
