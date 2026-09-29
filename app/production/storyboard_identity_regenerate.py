"""复用整集既有规划与邻段，仅重新生成指定片段的身份/发声候选。"""
import json

from app import textmatch
from app.production.storyboard_dialogue_extract import extract_dialogue_targets
from app.production.storyboard_identity_scope import bind_quote_identities
from app.production.storyboard_dialogue_ledger import _AiKeptLine, required_dialogue_for_segments
from app.project_settings import enhance_music_bed_enabled, resolve_aspect_ratio, resolve_narrator_voice_character
from app.production.storyboard_pack import (
    _AiBeatSheetDraft, _AiStoryboardSegmentDraft, _generate_all_segment_prompts,
    _load_indexed_source_segments, _manifest_speaker_names, _paratext_segment_indexes,
)
from app.production.storyboard_scene_binding import _enrich_asset_manifest_canonical_visuals


def refreshed_required_dialogue(stored: dict, quotes: list) -> list[dict]:
    """旧台词清单以原文位置和原话重新绑定证据，不继承已被证伪的旧说话人。"""
    previous = stored.get("required_dialogue") or [dict(line, text=line.get("line")) for line in stored.get("dialogue") or []]
    kept = []
    for item in previous:
        text = textmatch.condense(str(item.get("text") or ""))
        matches = [q for q in quotes if q.source_segment_index == item.get("source_segment_index") and textmatch.condense(q.text) == text]
        if len(matches) > 1 and item.get("source_start", -1) >= 0:
            matches = [q for q in matches if q.start_offset == item["source_start"]]
        if len(matches) == 1:
            kept.append(_AiKeptLine(quote_id=matches[0].quote_id, segment_no=stored["segment_no"]))
        elif text and stored.get("required_dialogue"):
            raise ValueError(f"台词『{item.get('text', '')[:20]}』没有唯一原文位置，请先核对本片段原文引用")
    return required_dialogue_for_segments(kept, quotes).get(stored["segment_no"], [])


def _restored_plan_items(conn, episode_id: str) -> dict[str, list[dict]]:
    """从最近一次整集生成落库的 ``storyboard_pack_adaptation`` 产物里取回
    ``wardrobe_plan_full``/``prop_entrances_full``（见 ``storyboard_beat_
    causality.assemble_adaptation_summary`` 文档）——``shots.shot_contract_json``
    只存逐段结果，不存整集规划阶段的原始提名，这两个字段只能从那份产物找回。
    取『该集这个类型 version 最高的一条』且必须 ``validated``（与
    ``app.domain.video_ops.storyboard_adaptation.current_storyboard_adaptation``
    同一判据，但那个函数在 L5，本文件是 L4，不能反向依赖，这里用显式
    ``conn`` 直接查同一张表，不写第二套判据）；没有留档、非 validated、或
    是改造前生成的老留档（没有这两个字段）时两项都降级为空列表——与
    ``_AiBeatSheetDraft`` 两个字段本来的默认值一致，不是新的失败模式。"""
    row = conn.execute(
        "SELECT status, content_json FROM artifacts WHERE type='storyboard_pack_adaptation' "
        "AND scope_type='episode' AND scope_id=? ORDER BY version DESC LIMIT 1", (episode_id,),
    ).fetchone()
    if row is None or row["status"] != "validated":
        return {"wardrobe_plan": [], "prop_entrances": []}
    content = json.loads(row["content_json"] or "{}")
    return {
        "wardrobe_plan": content.get("wardrobe_plan_full") or [],
        "prop_entrances": content.get("prop_entrances_full") or [],
    }


def _existing_plan(stored: list[dict], conn, episode_id: str) -> _AiBeatSheetDraft:
    """重建单段重生成要用的 ``_AiBeatSheetDraft``：节拍表/分段规划从逐段落库
    的 ``storyboard_pack_segment`` 拼回（真源），全集服装表/道具入场计划从
    ``_restored_plan_items`` 找回（见该函数文档）。"""
    beats = {}
    plans = []
    for segment in stored:
        for beat in segment.get("beats") or []:
            if beat.get("beat_id"):
                beats[beat["beat_id"]] = beat
        plans.append({key: segment.get(key) for key in ("segment_no", "synopsis", "source_segment_indexes", "beat_ids", "source_unit_ranges", "palette") if segment.get(key) is not None})
    restored = _restored_plan_items(conn, episode_id)
    return _AiBeatSheetDraft.model_validate({
        "beat_sheet": list(beats.values()), "segments": plans,
        "wardrobe_plan": restored["wardrobe_plan"], "prop_entrances": restored["prop_entrances"],
    })


async def regenerate_identity_candidate(conn, *, episode: dict, shot_id: str, payload: dict, bible, revision_notes: str = "") -> dict:
    """模型只产出候选，成功前不触碰旧分镜、已采用版本和媒体文件。``revision_notes``
    （人工复核写下的修订意见）只透传给目标段自己的模型任务——不写进 ``reuse``
    里其余段落的既有草稿，见 ``storyboard_revision_notes`` 模块文档「为什么
    裸字符串就够」。"""
    rows = conn.execute("SELECT id, shot_contract_json FROM shots WHERE episode_id=? ORDER BY shot_no", (episode["id"],)).fetchall()
    stored = [(json.loads(row["shot_contract_json"] or "{}").get("storyboard_pack_segment") or {}) for row in rows]
    if not all(stored):
        raise ValueError("本集包含旧式分镜，请使用对应的分镜修订入口")
    target = next(s for row, s in zip(rows, stored) if row["id"] == shot_id)
    source = _load_indexed_source_segments(conn, episode)
    quotes = extract_dialogue_targets(source, _paratext_segment_indexes(payload), speaker_names=_manifest_speaker_names(payload))
    bind_quote_identities(quotes, payload)
    required = {s["segment_no"]: s.get("required_dialogue") or [] for s in stored}
    required[target["segment_no"]] = refreshed_required_dialogue(target, quotes)
    reuse = {s["segment_no"]:_AiStoryboardSegmentDraft.model_validate(dict(s, beats=s.get("montage_beats") or [])) for s in stored if s is not target}
    _enrich_asset_manifest_canonical_visuals(conn, payload, bible=bible, project_id=episode["project_id"])
    drafts = await _generate_all_segment_prompts(
        episode_id=episode["id"], episode_no=episode["episode_no"], beat_draft=_existing_plan(stored, conn, episode["id"]),
        segments=source, payload=payload, target_video_model=episode.get("target_video_model") or "hiagent",
        bible=bible, required_dialogue_by_segment_no=required, conn=conn, project_id=episode["project_id"], aspect_ratio=resolve_aspect_ratio(conn, episode["project_id"]),
        enhance_music_bed=enhance_music_bed_enabled(conn, episode["project_id"]),
        narrator_voice_character=resolve_narrator_voice_character(conn, episode["project_id"]), reuse_segments=reuse,
        revision_notes=revision_notes,
    )
    result = dict(target, **drafts[target["segment_no"]].model_dump(mode="json"))
    result["beats"] = target.get("beats") or []
    result["required_dialogue"] = required[target["segment_no"]]
    return result
