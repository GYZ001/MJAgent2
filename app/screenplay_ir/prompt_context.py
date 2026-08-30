"""Prefix recovery for truncated provider output, the generation prompt contract, Bible context rendering, and small per-segment/text helpers shared by the compiler phases."""
from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from typing import Any

from app.identity_authority import identity_resolution_is_authoritative, model_identity_authority_prompt_rule
from app.schemas import Bible
from app.source_excerpt import align_source_excerpt
from app.spoken_contract import content_char_count

from .constants import IR_VERSION
from .models_core import IRBeat, IRScene
from .models_event import IREvent, IRMetadata


def recover_complete_screenplay_ir_prefix(raw: str) -> dict[str, Any] | None:
    """Decode complete top-level IR members from a length-truncated object.

    ``JSONDecoder.raw_decode`` is used member by member, so string contents and
    nested structures still follow the JSON grammar. If the scenes array itself
    is truncated, only fully decoded scene objects are retained; the incomplete
    scene is discarded and the missing source tail must be authored separately.
    """
    text = str(raw or "").lstrip()
    if not text.startswith("{"):
        return None
    decoder = json.JSONDecoder()
    index = 1
    recovered: dict[str, Any] = {}

    def skip_space(position: int) -> int:
        while position < len(text) and text[position].isspace():
            position += 1
        return position

    while True:
        index = skip_space(index)
        if index >= len(text) or text[index] == "}":
            break
        try:
            key, index = decoder.raw_decode(text, index)
        except json.JSONDecodeError:
            break
        if not isinstance(key, str):
            break
        index = skip_space(index)
        if index >= len(text) or text[index] != ":":
            break
        index = skip_space(index + 1)
        try:
            value, index = decoder.raw_decode(text, index)
        except json.JSONDecodeError:
            if key != "scenes" or index >= len(text) or text[index] != "[":
                break
            scene_index = skip_space(index + 1)
            complete_scenes: list[dict[str, Any]] = []
            while scene_index < len(text):
                try:
                    scene, scene_end = decoder.raw_decode(text, scene_index)
                except json.JSONDecodeError:
                    break
                if (
                    not isinstance(scene, dict)
                    or not isinstance(scene.get("units"), list)
                ):
                    break
                complete_scenes.append(scene)
                scene_index = skip_space(scene_end)
                if (
                    scene_index >= len(text)
                    or text[scene_index] != ","
                ):
                    break
                scene_index = skip_space(scene_index + 1)
            if complete_scenes:
                recovered["scenes"] = complete_scenes
                recovered["_scene_prefix_truncated"] = True
            break
        recovered[key] = value
        index = skip_space(index)
        if index >= len(text) or text[index] != ",":
            break
        index += 1

    scenes = recovered.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        return None
    if not all(
        isinstance(scene, dict)
        and isinstance(scene.get("units"), list)
        for scene in scenes
    ):
        return None
    recovered.pop("events", None)
    scene_prefix_truncated = bool(
        recovered.pop("_scene_prefix_truncated", False)
    )
    recovered["normalization_log"] = [
        *(recovered.get("normalization_log") or []),
        {
            "path": "scenes" if scene_prefix_truncated else "events",
            "from": "truncated_provider_suffix",
            "to": (
                "complete_scene_prefix_requires_source_tail_continuation"
                if scene_prefix_truncated
                else "compiler_derived_from_complete_scene_units"
            ),
            "reason": (
                "durable_complete_scene_prefix_recovery"
                if scene_prefix_truncated
                else "durable_prefix_recovery"
            ),
        },
    ]
    return recovered


def screenplay_ir_prompt_contract() -> str:
    """Compact output contract included in the generation prompt."""
    return """{
  "format_version":"__IR_VERSION__",
  "episode_no":1,
  "metadata":{
    "title":"", "logline":"", "script_format_note":"场次化台本稿",
    "dramatic_question":"", "protagonist_goal":"", "obstacle":"", "stakes":"",
    "emotional_curve":"", "ending_hook":"", "source_basis":"",
    "adaptation_direction":"", "opening":"", "development":"",
    "conflict":"", "climax":"", "episode_premise":"",
    "must_keep_ending":"", "drop_list":[],
    "approved_adaptations":[], "forbidden_additions":[]
  },
  "identities":[{
    "key":"person_a", "authority_id":"__IDENTITY_AUTHORITY_CONTRACT__",
    "display_name":"人物谱准确姓名或功能身份",
    "source_names":["该身份在本集原文中的逐字称谓"],
    "kind":"当前来源定义的开放身份语义",
    "visual_policy":"canonical|contextual|collective|offscreen_only",
    "visual_canonical":"可见身份的中性识别锚点",
    "asset_requirement":"required|optional|forbidden",
    "voice_canonical":"声音描述", "role_type":"named_character|functional_character|narrator",
    "rationale":"身份与视觉/声音策略的来源理由"
  }],
  "coverage":[],
  "scenes":[{
    "key":"sc1", "scene_heading":"【场1】日 / 地点", "story_function":"",
    "summary":"", "conflict":"", "turn":"",
    "units":[
      {"kind":"action","text":"可拍动作","event_key":"ev1",
       "narrative_layer":"story",
       "event_priority":"causal",
       "render_policy":"standalone",
       "actor_keys":["person_a"],"target_keys":[],
       "onscreen_entity_keys":["person_a"],
       "action_agency":{"kind":"character","identity_bearing":true,
         "source_segment_ids":["SRC0001"]},
       "participant_deliveries":[],
       "resulting_state":"该动作完成后新成立的局势，禁止复述 text",
       "source_segment_ids":["SRC0001"]},
      {"kind":"dialogue","text":"改编台词","event_key":"ev1",
       "narrative_layer":"story",
       "event_priority":"supporting",
       "render_policy":"merge_adjacent",
       "actor_keys":[],"target_keys":[],
       "onscreen_entity_keys":["person_a"],
       "action_agency":{"kind":"character_voice","identity_bearing":true,
         "source_segment_ids":["SRC0001"]},
       "participant_deliveries":[],
       "resulting_state":"该话轮交付后人物/信息/决策发生的变化，禁止复述 text",
       "source_segment_ids":["SRC0001"],
       "speaker_key":"person_a","function":"statement",
       "source_text":"原文逐字话语","chain_key":"dc1"}
    ]
  }],
  "experience":{
    "director_objective":"", "satisfaction_criteria":"",
    "required_processing_s":1.0, "forbidden_misconceptions":[]
  }
}

System contract: do not create an identity, speaker, actor, target, or visible
character for prose-only environment/establishing events. Leave their typed
identity relations empty. The deterministic compiler alone may assign the
reserved environment:<episode-scope> narrative subject; it is never a person,
voice, scene character, or asset identity.""".replace(
        "__IDENTITY_AUTHORITY_CONTRACT__",
        model_identity_authority_prompt_rule(),
    ).replace("__IR_VERSION__", IR_VERSION)


def screenplay_ir_bible_context(
    bible: Bible,
    *,
    source_text: str,
    episode_no: int,
    character_resolutions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return the source-evidenced Bible closure needed by this episode.

    Selection is driven only by current source mentions, persisted identity
    resolutions and scene discovery evidence. It does not use a role/name
    whitelist. Relationship edges touching selected characters remain present
    so the model does not lose the meaning of an interaction.
    """
    source = re.sub(r"\s+", "", source_text or "")
    resolution_tokens = {
        str(item.get(field) or "").strip()
        for item in (character_resolutions or [])
        if (
            isinstance(item, dict)
            and identity_resolution_is_authoritative(item)
        )
        for field in ("source_label", "canonical_name")
        if str(item.get(field) or "").strip()
    }
    selected_names = {
        character.name
        for character in bible.characters
        if (
            character.name in source
            or character.name in resolution_tokens
        )
    }
    if not selected_names:
        # Empty evidence usually means a placeholder/very short source. Keep
        # the existing authority instead of guessing which identity is safe.
        selected_names = {character.name for character in bible.characters}

    characters: list[dict[str, Any]] = []
    for character in bible.characters:
        if character.name not in selected_names:
            continue
        payload = character.model_dump(
            mode="json",
            exclude={"ref_image_path", "portrait_prompt_override"},
        )
        payload["relationships"] = [
            relationship
            for relationship in payload.get("relationships") or []
            if (
                str(relationship.get("to") or "") in selected_names
                or str(relationship.get("to") or "") in source
            )
        ]
        characters.append(payload)

    scenes: list[dict[str, Any]] = []
    for scene in bible.scenes:
        aliases = [str(value or "").strip() for value in scene.aliases or []]
        evidence = [str(value or "").strip() for value in scene.discovery_sources or []]
        selected = bool(
            scene.name in source
            or any(alias and alias in source for alias in aliases)
            or int(scene.first_episode or 0) == int(episode_no)
            or any(
                item
                and (
                    re.sub(r"\s+", "", item) in source
                    or source in re.sub(r"\s+", "", item)
                )
                for item in evidence
            )
        )
        if not selected:
            continue
        scenes.append(scene.model_dump(
            mode="json",
            exclude={"ref_image_path", "scene_prompt_override"},
        ))

    return {
        "characters": characters,
        "world": bible.world.model_dump(mode="json"),
        "scenes": scenes,
    }


def _unique_by_key(values: list[Any], label: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for value in values:
        key = str(getattr(value, "key", "") or "").strip()
        if not key:
            raise ValueError(f"{label} 存在空 key")
        if key in result:
            raise ValueError(f"{label} key 重复：{key}")
        result[key] = value
    return result


def _semantic_key(domain: str, statement: str) -> str:
    normalized = re.sub(r"\s+", "", statement).casefold()
    digest = hashlib.sha256(f"{domain}:{normalized}".encode("utf-8")).hexdigest()[:16]
    return f"{domain}:{digest}"


def _first_sentence(value: str, *, minimum: int = 8) -> str:
    for item in re.findall(r"[^。！？\n]+[。！？]?", value or ""):
        candidate = item.strip()
        if len(re.sub(r"\s+", "", candidate)) >= minimum:
            return candidate
    return (value or "").strip()


def _split_spoken_line(value: str, *, max_chars: int) -> list[str]:
    """Split one authored utterance without rewriting its words."""
    line = str(value or "").strip()
    if not line or content_char_count(line) <= max_chars:
        return [line] if line else []
    clauses = [
        item.strip()
        for item in re.findall(r".*?[，。！？；,.!?;]|.+$", line)
        if item.strip()
    ]
    chunks: list[str] = []
    current = ""
    for clause in clauses:
        if current and content_char_count(current + clause) > max_chars:
            chunks.append(current)
            current = ""
        if content_char_count(clause) <= max_chars:
            current += clause
            continue
        for character in clause:
            if (
                current
                and content_char_count(current + character) > max_chars
            ):
                chunks.append(current)
                current = ""
            current += character
    if current:
        chunks.append(current)
    return [item.strip() for item in chunks if item.strip()]


def _screenplay_action_text(value: str) -> str:
    """Preserve source-authored action prose; schema fields own semantics."""
    return re.sub(r"\s{2,}", " ", str(value or "").strip()).strip(" ，,；;")


def _source_location(
    excerpt: str,
    *,
    source_text: str,
    source_segment_ids: list[str],
    segments: dict[str, Any],
    authorized_source_chapters: dict[str, str],
) -> tuple[str, int, int, str]:
    candidates = [excerpt]
    for segment_id in source_segment_ids:
        if segment_id not in segments:
            continue
        segment_text = segments[segment_id].text
        candidates.extend([
            _first_sentence(segment_text),
            _first_sentence(
                re.sub(r"^\s*【[^】]+】\s*", "", segment_text),
            ),
        ])
    candidates = list(dict.fromkeys(
        str(candidate or "").strip()
        for candidate in candidates
        if str(candidate or "").strip()
    ))
    candidates = [
        *[
            candidate for candidate in candidates
            if len(re.sub(r"\s+", "", candidate)) >= 8
        ],
        *[
            candidate for candidate in candidates
            if len(re.sub(r"\s+", "", candidate)) < 8
        ],
    ]
    for candidate in candidates:
        for chapter_id, chapter_text in authorized_source_chapters.items():
            offset = chapter_text.find(candidate)
            if offset >= 0:
                return chapter_id, offset, offset + len(candidate), candidate
        candidate_chars = len(re.sub(r"\s+", "", candidate))
        aligned = align_source_excerpt(
            candidate,
            source_text,
            min_match_chars=min(8, max(2, candidate_chars)),
        )
        if aligned is not None and not authorized_source_chapters:
            return (
                "source",
                aligned.start_offset,
                aligned.end_offset,
                aligned.excerpt,
            )
    raise ValueError(
        "事件来源摘录无法在授权章节中精确定位："
        + (excerpt[:80] if excerpt else "未提供摘录")
    )


def _dialogue_source_text(value: str, source_text: str) -> str:
    raw = str(value or "").strip()
    if raw and raw in source_text:
        return raw
    aligned = align_source_excerpt(raw, source_text, min_match_chars=2)
    if aligned is not None:
        return aligned.excerpt
    raise ValueError(f"对白 source_text 未在本集原文中找到：{raw[:80] or '空'}")


def _default_metadata(episode: dict[str, Any]) -> IRMetadata:
    ending = str(episode.get("cliffhanger") or "").strip()
    title = str(episode.get("title") or f"第{episode.get('episode_no') or 1}集")
    premise = str(episode.get("synopsis") or title)
    return IRMetadata(
        title=title,
        logline=premise,
        script_format_note="场次化台本稿，含场标、动作段与对白段",
        dramatic_question=f"{title}中的核心目标能否实现？",
        protagonist_goal="推动本集核心事件完成",
        obstacle="人物目标受到当前局势与关系阻碍",
        stakes="失败将使当前矛盾继续升级",
        emotional_curve="从建立处境到冲突升级，最终完成本集状态变化",
        ending_hook=ending,
        source_basis="依据本集授权原文完成改编",
        adaptation_direction="保留完整因果链并转换为可导演台本",
        opening="建立人物处境与本集目标",
        development="事件推进并形成阻力",
        conflict="核心矛盾正面发生",
        climax="关键行动改变局势",
        episode_premise=premise,
        must_keep_ending=ending,
    )


def _segment_ordinal(segment_id: str) -> int:
    return int(re.sub(r"\D", "", segment_id) or 0)


def _nearest_event_for_segment(
    segment_id: str, events: list[IREvent],
) -> IREvent:
    target = _segment_ordinal(segment_id)
    return min(
        events,
        key=lambda event: min(
            (
                abs(target - _segment_ordinal(candidate))
                for candidate in event.source_segment_ids
            ),
            default=10**9,
        ),
    )


def _beats_for_event(event: IREvent, beats: list[IRBeat]) -> list[IRBeat]:
    event_sources = set(event.source_segment_ids)
    overlaps = [
        (
            len(event_sources.intersection(beat.source_segment_ids)),
            beat,
        )
        for beat in beats
    ]
    best_overlap = max((score for score, _beat in overlaps), default=0)
    if best_overlap:
        return [
            beat for score, beat in overlaps if score == best_overlap
        ]
    target = min(
        (_segment_ordinal(item) for item in event.source_segment_ids),
        default=0,
    )
    return [
        min(
            beats,
            key=lambda beat: min(
                (
                    abs(target - _segment_ordinal(candidate))
                    for candidate in beat.source_segment_ids
                ),
                default=10**9,
            ),
        )
    ]


def _retain_source_segment_as_scene_context(
    segment_id: str,
    *,
    reason: str = "",
    events: list[IREvent],
    scene_by_key: dict[str, IRScene],
    segments: dict[str, Any],
    inferred_context_by_scene: defaultdict[str, list[str]],
) -> str:
    event = _nearest_event_for_segment(segment_id, events)
    scene = scene_by_key[event.scene_key]
    excerpt = _first_sentence(segments[segment_id].text, minimum=4)
    context = (
        f"来源段 {segment_id} 作为本场人物、环境或因果上下文保留："
        f"{excerpt[:180]}"
    )
    if context not in inferred_context_by_scene[event.scene_key]:
        inferred_context_by_scene[event.scene_key].append(context)
    return reason.strip() or (
        f"作为「{scene.scene_heading}」的来源上下文保留，"
        "并写入该场 context_requirements"
    )


def _state_fact_ids(position: int, count: int) -> list[str]:
    base = f"F-{position}"
    if count == 1:
        return [base]
    return [f"{base}-{index}" for index in range(1, count + 1)]
