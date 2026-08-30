"""单集删除后把剩余分集重新编号为连续序列：数据库行、磁盘目录、路径引用一起搬。"""
from __future__ import annotations

import json
from pathlib import Path

from app import config
from app.db import new_id


def _json_with_episode_number(value: str | None, episode_no: int) -> str | None:
    """Keep mutable screenplay projections aligned with their episode row."""
    if not value:
        return value
    try:
        payload = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return value
    if not isinstance(payload, dict):
        return value
    payload["episode_no"] = episode_no
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _compact_asset_episode_ranges(
    conn,
    *,
    table: str,
    name_column: str,
    project_id: str,
    surviving_numbers: list[int],
) -> dict[str, int]:
    """Project an asset's inclusive episode range onto a compacted sequence."""
    from bisect import bisect_left, bisect_right

    rows = conn.execute(
        f"SELECT id,{name_column} AS asset_name,ep_start,ep_end "
        f"FROM {table} WHERE project_id=? ORDER BY ep_start,id",
        (project_id,),
    ).fetchall()
    mapped: list[dict] = []
    deleted_ids: set[str] = set()
    for row in rows:
        old_start = int(row["ep_start"])
        old_end = int(row["ep_end"]) if row["ep_end"] is not None else None
        if old_start <= 0:
            new_start = old_start
            new_end = old_end
            if old_end is not None and old_end > 0:
                new_end = bisect_right(surviving_numbers, old_end)
        else:
            left = bisect_left(surviving_numbers, old_start)
            if left >= len(surviving_numbers) or (
                old_end is not None and surviving_numbers[left] > old_end
            ):
                deleted_ids.add(row["id"])
                continue
            new_start = left + 1
            new_end = (
                bisect_right(surviving_numbers, old_end)
                if old_end is not None
                else None
            )
        if new_end is not None and new_start > new_end:
            deleted_ids.add(row["id"])
            continue
        mapped.append({
            "id": row["id"],
            "asset_name": row["asset_name"],
            "old_start": old_start,
            "old_end": old_end,
            "new_start": new_start,
            "new_end": new_end,
        })

    # Legacy overlapping ranges can collapse onto the same unique start after a
    # gap is removed. The latest old range is the one that governed the first
    # surviving episode, so retain it deterministically.
    by_key: dict[tuple[str, int], list[dict]] = {}
    for item in mapped:
        by_key.setdefault((item["asset_name"], item["new_start"]), []).append(item)
    for candidates in by_key.values():
        if len(candidates) <= 1:
            continue
        keep = max(candidates, key=lambda item: (item["old_start"], item["id"]))
        deleted_ids.update(item["id"] for item in candidates if item is not keep)
    mapped = [item for item in mapped if item["id"] not in deleted_ids]

    if deleted_ids:
        marks = ",".join("?" for _ in deleted_ids)
        conn.execute(f"DELETE FROM {table} WHERE id IN ({marks})", sorted(deleted_ids))

    updates = [
        item for item in mapped
        if item["old_start"] != item["new_start"] or item["old_end"] != item["new_end"]
    ]
    temporary_base = max([*surviving_numbers, 0]) + 1_000_000
    for index, item in enumerate(updates, start=1):
        conn.execute(
            f"UPDATE {table} SET ep_start=? WHERE id=?",
            (temporary_base + index, item["id"]),
        )
    for item in updates:
        conn.execute(
            f"UPDATE {table} SET ep_start=?,ep_end=? WHERE id=?",
            (item["new_start"], item["new_end"], item["id"]),
        )
    return {"updated": len(updates), "deleted": len(deleted_ids)}


def _replace_episode_path_prefixes(
    conn,
    *,
    project_id: str,
    number_changes: list[tuple[int, int]],
) -> int:
    """Rewrite operational file references after episode directories move."""
    path_columns = {
        "artifacts": ("file_path",),
        "delivery_packages": ("package_path", "manifest_json", "quality_report_json"),
        "evaluations": ("evidence_json",),
        "reference_assets": ("path", "dependency_manifest_json"),
        "shot_scenes": ("image_path",),
        "shot_versions": (
            "video_path",
            "last_frame_url",
            "technical_validation_json",
            "image_inputs",
        ),
    }
    available_tables = {
        row["name"] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    changed = 0
    for table, columns in path_columns.items():
        if table not in available_tables:
            continue
        available_columns = {
            row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        for old_no, new_no in number_changes:
            old_prefix = str(config.PROJECTS_DIR / project_id / "episodes" / str(old_no))
            new_prefix = str(config.PROJECTS_DIR / project_id / "episodes" / str(new_no))
            for column in columns:
                if column not in available_columns:
                    continue
                cursor = conn.execute(
                    f"UPDATE {table} SET {column}=REPLACE({column}, ?, ?) "
                    f"WHERE {column} LIKE ?",
                    (old_prefix, new_prefix, f"%{old_prefix}%"),
                )
                changed += max(0, cursor.rowcount)
    return changed


def _compact_project_episode_numbers(conn, project_id: str) -> dict[str, object]:
    """Renumber surviving episodes densely while preserving stable episode IDs."""
    episodes = conn.execute(
        "SELECT id,episode_no,screenplay_json,storyboard_outline_json "
        "FROM episodes WHERE project_id=? ORDER BY episode_no,id",
        (project_id,),
    ).fetchall()
    surviving_numbers = [int(row["episode_no"]) for row in episodes]
    changes = [
        (row, new_no)
        for new_no, row in enumerate(episodes, start=1)
        if int(row["episode_no"]) != new_no
    ]
    if not changes:
        return {
            "renumbered": 0,
            "directories_moved": 0,
            "path_references_updated": 0,
            "character_ranges": {"updated": 0, "deleted": 0},
            "scene_ranges": {"updated": 0, "deleted": 0},
        }

    episode_root = config.PROJECTS_DIR / project_id / "episodes"
    number_changes = [(int(row["episode_no"]), new_no) for row, new_no in changes]
    source_directories = {episode_root / str(old_no) for old_no, _ in number_changes}
    for _, new_no in number_changes:
        destination = episode_root / str(new_no)
        if destination.exists() and destination not in source_directories:
            raise RuntimeError(f"分集目录重编号目标已存在：{destination}")

    directory_moves: list[tuple[Path, Path, Path]] = []
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        if episode_root.exists():
            for old_no, new_no in number_changes:
                source = episode_root / str(old_no)
                if not source.exists():
                    continue
                temporary = episode_root / f".{new_id('renumber')}-{old_no}"
                source.rename(temporary)
                directory_moves.append((source, temporary, episode_root / str(new_no)))

        temporary_base = max(surviving_numbers) + len(episodes) + 1_000_000
        for index, (row, _) in enumerate(changes, start=1):
            conn.execute(
                "UPDATE episodes SET episode_no=? WHERE id=?",
                (temporary_base + index, row["id"]),
            )
        for row, new_no in changes:
            conn.execute(
                "UPDATE episodes SET episode_no=? WHERE id=?",
                (new_no, row["id"]),
            )
            # Published screenplay/storyboard JSON is an immutable projection
            # of content-addressed artifacts.  Episode numbering is display
            # metadata keyed by the stable episode id; renumbering must not
            # silently rewrite certified narrative content.
            draft = conn.execute(
                "SELECT content_json FROM screenplay_drafts WHERE episode_id=?",
                (row["id"],),
            ).fetchone()
            if draft:
                conn.execute(
                    "UPDATE screenplay_drafts SET content_json=? WHERE episode_id=?",
                    (_json_with_episode_number(draft["content_json"], new_no), row["id"]),
                )

        character_ranges = _compact_asset_episode_ranges(
            conn,
            table="character_portraits",
            name_column="character_name",
            project_id=project_id,
            surviving_numbers=surviving_numbers,
        )
        scene_ranges = _compact_asset_episode_ranges(
            conn,
            table="scene_references",
            name_column="scene_name",
            project_id=project_id,
            surviving_numbers=surviving_numbers,
        )
        path_references_updated = _replace_episode_path_prefixes(
            conn,
            project_id=project_id,
            number_changes=number_changes,
        )
        for _, temporary, destination in directory_moves:
            temporary.rename(destination)
        conn.commit()
    except Exception:
        conn.rollback()
        for source, temporary, destination in reversed(directory_moves):
            try:
                if destination.exists():
                    destination.rename(source)
                elif temporary.exists():
                    temporary.rename(source)
            except OSError:
                # Preserve the original exception. Any stranded hidden directory
                # is intentionally not deleted so its media remains recoverable.
                pass
        raise

    return {
        "renumbered": len(changes),
        "directories_moved": len(directory_moves),
        "path_references_updated": path_references_updated,
        "character_ranges": character_ranges,
        "scene_ranges": scene_ranges,
    }
