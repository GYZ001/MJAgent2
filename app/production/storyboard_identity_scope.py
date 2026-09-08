"""以原文范围解析分镜称谓；稳定 ID、局部提及和非排他别名保持分离。"""
from typing import Any


def entry_indexes(entry: dict[str, Any]) -> set[int]:
    values = entry.get("segment_indexes") or [entry.get("segment_index")]
    return {int(v) for v in values if v is not None}


def scoped_identity_candidates(payload: dict, label: str, indexes: list[int]) -> set[str]:
    """局部提及优先；同范围多人同称谓返回全部候选，不按出现顺序抢占。"""
    manifest = payload.get("asset_manifest") or {}
    wanted = set(indexes)
    candidates = {
        str(row["identity_id"]) for row in payload.get("appellation_map") or []
        if row.get("raw_mention") == label and row.get("identity_id")
        and (wanted & entry_indexes(row))
    }
    for extra in manifest.get("functional_extras") or []:
        if extra.get("label") == label and wanted & entry_indexes(extra) and extra.get("visual_entity_id"):
            candidates.add(str(extra["visual_entity_id"]))
    if candidates:
        return candidates
    for character in manifest.get("characters") or []:
        identity = str(character.get("identity_id") or "")
        if not identity:
            continue
        if label == character.get("display_name") or label == identity:
            candidates.add(identity)
        elif label in (character.get("aliases") or []) and wanted & entry_indexes(character):
            # 精确称谓提及记录存在但不属于当前段时，不能再由整集 aliases 扩散回来。
            mentions = [r for r in payload.get("appellation_map") or [] if r.get("raw_mention") == label]
            if not mentions:
                candidates.add(identity)
    return candidates


def scoped_name_map(payload: dict, indexes: list[int] | None = None) -> dict[str, str]:
    """正名唯一映射；不带范围时只收无碰撞名字，带范围时使用原文局部证据。"""
    manifest = payload.get("asset_manifest") or {}
    entries = manifest.get("characters") or []
    labels = {str(n) for c in entries for n in [c.get("display_name"), *(c.get("aliases") or [])] if n}
    labels.update(str(e["label"]) for e in manifest.get("functional_extras") or [] if e.get("label"))
    result = {"旁白": "旁白"}
    for label in sorted(labels):
        if indexes is not None:
            candidates = scoped_identity_candidates(payload, label, indexes)
        else:
            candidates = {str(c["identity_id"]) for c in entries if c.get("identity_id") and label in [c.get("display_name"), *(c.get("aliases") or [])]}
            candidates.update(str(e["visual_entity_id"]) for e in manifest.get("functional_extras") or [] if e.get("label") == label and e.get("visual_entity_id"))
        if len(candidates) == 1:
            result[label] = next(iter(candidates))
    return result


def bind_quote_identities(quotes: list, payload: dict) -> None:
    """在切片之前固化台词的原文身份与听者证据，使用已有 quote_id 和偏移。"""
    for quote in quotes:
        names = scoped_name_map(payload, [quote.source_segment_index])
        quote.speaker_identity_id = names.get(quote.speaker, "")
        quote.excluded_speaker_identity_ids = sorted({names[n] for n in quote.listener_names if n in names})
