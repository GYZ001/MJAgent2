"""一次性清理：把 shot_versions.image_inputs 里历史遗留的 base64 url 剥掉，
避免每次 GET /episodes/{id} 都重新加载并解析数百 MB 的 JSON blob。"""
import json
import sqlite3
from app.db import DB_PATH

STRIP_KEYS = ("url", "path")  # base64 数据 URL 与本地绝对路径都不需要留在 DB 里


def strip_ref(ref):
    if not isinstance(ref, dict):
        return ref
    return {k: v for k, v in ref.items() if k not in STRIP_KEYS}


def clean_image_inputs(raw):
    if not raw:
        return raw, False
    try:
        meta = json.loads(raw)
    except (TypeError, ValueError):
        return raw, False
    changed = False
    refs = meta.get("reference_images")
    if isinstance(refs, list):
        new_refs = [strip_ref(r) for r in refs]
        if any(k in r for r in refs if isinstance(r, dict) for k in STRIP_KEYS):
            changed = True
        meta["reference_images"] = new_refs
    for log in meta.get("reference_failure_logs") or []:
        if not isinstance(log, dict):
            continue
        nested = log.get("reference_images")
        if isinstance(nested, list):
            new_nested = [strip_ref(r) for r in nested]
            if any(k in r for r in nested if isinstance(r, dict) for k in STRIP_KEYS):
                changed = True
            log["reference_images"] = new_nested
    if not changed:
        return raw, False
    return json.dumps(meta, ensure_ascii=False), True


def main():
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT id, image_inputs FROM shot_versions").fetchall()
    total = len(rows)
    cleaned = 0
    saved_bytes = 0
    for vid, raw in rows:
        if not raw:
            continue
        before = len(raw)
        new_raw, changed = clean_image_inputs(raw)
        if not changed:
            continue
        after = len(new_raw)
        saved_bytes += before - after
        conn.execute("UPDATE shot_versions SET image_inputs=? WHERE id=?", (new_raw, vid))
        cleaned += 1
    conn.commit()
    conn.close()
    print(f"scanned={total} cleaned={cleaned} saved_bytes={saved_bytes} ({saved_bytes/1024/1024:.1f} MB)")


if __name__ == "__main__":
    main()
