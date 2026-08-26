"""参考图集持久化：从 shot_versions.image_inputs 拆出独立资产表，视频重抽复用 reference_set_id。"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from app.db import get_conn, new_id, now


def _fingerprint(shot_id: str, refs: list[dict]) -> str:
    from app.multiview import gallery_fingerprint_material
    material = gallery_fingerprint_material(refs)
    raw = json.dumps({"shot_id": shot_id, "refs": material}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def upsert_reference_set_from_meta(
    *,
    shot_id: str,
    version_id: str | None,
    meta: dict[str, Any],
    conn=None,
    static_ready: bool | None = None,
    continuity_ready: bool | None = None,
    group_gate_passed: bool | None = None,
) -> str | None:
    """把 image_inputs 里的参考图画廊写入 reference_sets / reference_assets。

    返回 reference_set_id；若无可持久化参考图则返回 None。
    原 JSON 仍由调用方保留写入，作兼容读取。
    """
    from app.rejected_media import discard_file, reference_dict_is_rejected
    refs = []
    for ref in list(meta.get("reference_images") or []):
        if reference_dict_is_rejected(ref):
            discard_file(ref.get("path") or ref.get("image_path"))
            continue
        refs.append(ref)
    from app.video_modes import dedupe_reference_dicts

    refs = dedupe_reference_dicts(refs)
    meta["reference_images"] = refs
    if not refs:
        existing = meta.get("reference_set_id")
        return existing
    db = conn or get_conn()
    fp = meta.get("reference_gallery_fingerprint") or _fingerprint(shot_id, refs)
    revision = int(meta.get("reference_gallery_revision") or int(now()))
    static_flag = int(bool(
        static_ready if static_ready is not None else meta.get("reference_static_ready")
    ))
    continuity_flag = int(bool(
        continuity_ready if continuity_ready is not None else meta.get("continuity_anchor_ready")
    ))
    gate_flag = int(bool(
        group_gate_passed if group_gate_passed is not None else meta.get("reference_group_gate_passed")
    ))
    input_fp = meta.get("video_input_fingerprint") or fp
    # 已有同 fingerprint 则复用
    row = db.execute(
        "SELECT id FROM reference_sets WHERE shot_id=? AND fingerprint=? ORDER BY revision DESC LIMIT 1",
        (shot_id, fp),
    ).fetchone()
    if row:
        set_id = row["id"]
        if version_id:
            db.execute(
                "UPDATE reference_sets SET source_version_id=COALESCE(source_version_id, ?) WHERE id=?",
                (version_id, set_id),
            )
        try:
            db.execute(
                """UPDATE reference_sets SET static_ready=?, continuity_ready=?, group_gate_passed=?,
                          input_fingerprint=?, updated_at=? WHERE id=?""",
                (static_flag, continuity_flag, gate_flag, input_fp, now(), set_id),
            )
        except Exception:  # noqa: BLE001 旧库缺列
            pass
        meta["reference_set_id"] = set_id
        meta["reference_gallery_fingerprint"] = fp
        return set_id

    set_id = new_id("refset")
    try:
        db.execute(
            """INSERT INTO reference_sets(
                   id, shot_id, source_version_id, revision, fingerprint, status,
                   static_ready, continuity_ready, group_gate_passed, input_fingerprint,
                   created_at, updated_at
               ) VALUES(?,?,?,?,?,'ready',?,?,?,?,?,?)""",
            (set_id, shot_id, version_id, revision, fp,
             static_flag, continuity_flag, gate_flag, input_fp, now(), now()),
        )
    except Exception:  # noqa: BLE001 旧库缺列时回退
        db.execute(
            """INSERT INTO reference_sets(
                   id, shot_id, source_version_id, revision, fingerprint, status,
                   created_at, updated_at
               ) VALUES(?,?,?,?,?,'ready',?,?)""",
            (set_id, shot_id, version_id, revision, fp, now(), now()),
        )
    slot_state = meta.get("reference_slots") or {}
    for i, ref in enumerate(refs):
        asset_id = ref.get("id") or new_id("refasset")
        path = ref.get("path") or ref.get("image_path")
        slot_key = None
        attempt_no = 1
        gen_status = None
        qa_status = None
        for sk, sv in slot_state.items():
            if isinstance(sv, dict) and sv.get("path") == path:
                slot_key = sk
                gen_status = sv.get("status")
                qa_status = "passed" if sv.get("status") == "passed" else sv.get("status")
                break
        try:
            db.execute(
                """INSERT INTO reference_assets(
                       id, reference_set_id, asset_type, source, path, sort_order,
                       quality_score, consistency_score, selected, deleted, qa_json,
                       slot_key, attempt_no, generation_status, qa_status, input_fingerprint,
                       entity_type, entity_name, library_revision_id, library_view_id, view_role,
                       purposes_json, required, dependency_manifest_json, created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    asset_id, set_id,
                    ref.get("type") or "generated",
                    ref.get("source") or "pipeline",
                    path,
                    i,
                    ref.get("qualityScore"),
                    ref.get("consistencyScore"),
                    int(bool(ref.get("selectedForSeedance", True))),
                    int(bool(ref.get("deleted"))),
                    json.dumps(ref.get("qa") or {}, ensure_ascii=False) if ref.get("qa") else None,
                    slot_key or ref.get("slot_key"), attempt_no, gen_status, qa_status, input_fp,
                    ref.get("entity_type"), ref.get("entity_name"),
                    ref.get("library_revision_id"), ref.get("library_view_id"), ref.get("view_role"),
                    json.dumps(ref.get("purposes") or [], ensure_ascii=False) if ref.get("purposes") is not None else ref.get("purposes_json"),
                    int(bool(ref.get("required"))),
                    json.dumps(ref.get("dependency_manifest") or {}, ensure_ascii=False) if ref.get("dependency_manifest") else None,
                    now(),
                ),
            )
        except Exception:  # noqa: BLE001
            try:
                db.execute(
                    """INSERT INTO reference_assets(
                           id, reference_set_id, asset_type, source, path, sort_order,
                           quality_score, consistency_score, selected, deleted, qa_json,
                           slot_key, attempt_no, generation_status, qa_status, input_fingerprint,
                           created_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        asset_id, set_id,
                        ref.get("type") or "generated",
                        ref.get("source") or "pipeline",
                        path,
                        i,
                        ref.get("qualityScore"),
                        ref.get("consistencyScore"),
                        int(bool(ref.get("selectedForSeedance", True))),
                        int(bool(ref.get("deleted"))),
                        json.dumps(ref.get("qa") or {}, ensure_ascii=False) if ref.get("qa") else None,
                        slot_key, attempt_no, gen_status, qa_status, input_fp,
                        now(),
                    ),
                )
            except Exception:  # noqa: BLE001
                db.execute(
                    """INSERT INTO reference_assets(
                           id, reference_set_id, asset_type, source, path, sort_order,
                           quality_score, consistency_score, selected, deleted, qa_json, created_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        asset_id, set_id,
                        ref.get("type") or "generated",
                        ref.get("source") or "pipeline",
                        path,
                        i,
                        ref.get("qualityScore"),
                        ref.get("consistencyScore"),
                        int(bool(ref.get("selectedForSeedance", True))),
                        int(bool(ref.get("deleted"))),
                        json.dumps(ref.get("qa") or {}, ensure_ascii=False) if ref.get("qa") else None,
                        now(),
                    ),
                )
    # 冻结 manifest
    try:
        manifest = meta.get("reference_manifest")
        if manifest:
            db.execute(
                "UPDATE reference_sets SET dependency_manifest_json=?, frozen=? WHERE id=?",
                (json.dumps(manifest, ensure_ascii=False), int(bool(meta.get("reference_manifest_frozen"))), set_id),
            )
    except Exception:  # noqa: BLE001
        pass
    meta["reference_set_id"] = set_id
    meta["reference_gallery_fingerprint"] = fp
    meta["reference_gallery_revision"] = revision
    if conn is None:
        db.commit()
    return set_id


def load_reference_set(set_id: str, *, conn=None) -> dict[str, Any] | None:
    db = conn or get_conn()
    row = db.execute("SELECT * FROM reference_sets WHERE id=?", (set_id,)).fetchone()
    if not row:
        return None
    assets = db.execute(
        "SELECT * FROM reference_assets WHERE reference_set_id=? ORDER BY sort_order, created_at",
        (set_id,),
    ).fetchall()
    refs = []
    for a in assets:
        purposes = None
        try:
            purposes = json.loads(a["purposes_json"]) if "purposes_json" in a.keys() and a["purposes_json"] else None
        except Exception:  # noqa: BLE001
            purposes = None
        item = {
            "id": a["id"],
            "type": a["asset_type"],
            "source": a["source"],
            "path": a["path"],
            "qualityScore": a["quality_score"],
            "consistencyScore": a["consistency_score"],
            "selectedForSeedance": bool(a["selected"]),
            "deleted": bool(a["deleted"]),
            "qa": json.loads(a["qa_json"]) if a["qa_json"] else None,
        }
        frozen_beat = (
            item["qa"].get("keyframe_beat")
            if isinstance(item.get("qa"), dict) and isinstance(item["qa"].get("keyframe_beat"), dict)
            else None
        )
        if frozen_beat:
            item["keyframe_index"] = frozen_beat.get("beat_index")
            item["keyframe_total"] = frozen_beat.get("beat_total")
            item["keyframe_time_ratio"] = frozen_beat.get("time_ratio")
            item["keyframe_target_desc"] = frozen_beat.get("target_desc")
        for key, col in (
            ("entity_type", "entity_type"),
            ("entity_name", "entity_name"),
            ("library_revision_id", "library_revision_id"),
            ("library_view_id", "library_view_id"),
            ("view_role", "view_role"),
            ("slot_key", "slot_key"),
        ):
            try:
                if col in a.keys() and a[col] is not None:
                    item[key] = a[col]
            except Exception:  # noqa: BLE001
                pass
        if purposes is not None:
            item["purposes"] = purposes
        try:
            if "required" in a.keys():
                item["required"] = bool(a["required"])
        except Exception:  # noqa: BLE001
            pass
        try:
            if "dependency_manifest_json" in a.keys() and a["dependency_manifest_json"]:
                item["dependency_manifest"] = json.loads(a["dependency_manifest_json"])
        except Exception:  # noqa: BLE001
            pass
        refs.append(item)
    result = {
        "id": row["id"],
        "shot_id": row["shot_id"],
        "revision": row["revision"],
        "fingerprint": row["fingerprint"],
        "status": row["status"],
        "reference_images": refs,
    }
    try:
        if "dependency_manifest_json" in row.keys() and row["dependency_manifest_json"]:
            result["reference_manifest"] = json.loads(row["dependency_manifest_json"])
        if "frozen" in row.keys():
            result["frozen"] = bool(row["frozen"])
    except Exception:  # noqa: BLE001
        pass
    return result


