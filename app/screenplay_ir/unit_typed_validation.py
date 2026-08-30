"""Compiler phase: computes assigned source indices, builds the identity alias index, and validates typed scene units."""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from app import textmatch
from app.identity_authority import identity_resolution_is_authoritative
from app.source_excerpt import align_source_excerpt

from .constants import ScreenplayIRFidelityError
from .identity_authorities import ATTRIBUTED_TEXT_PROVENANCE_KINDS
from .models_core import IRScene, IRSceneUnit
from .models_event import ScreenplayGenerationIR


def _ir_compute_assigned_source_indices(
    value: ScreenplayGenerationIR,
    flat_units: list[tuple[IRScene, IRSceneUnit]],
    segments_list: list[Any],
    source_text: str,
    strict_unit_ownership: bool,
) -> list[int]:
    def segment_index_for_offset(offset: int) -> int | None:
        return next(
            (
                index
                for index, segment in enumerate(segments_list)
                if segment.start_offset <= offset < segment.end_offset
            ),
            None,
        )

    assigned_indices: list[int] = []
    if strict_unit_ownership:
        source_index = {
            segment.segment_id: index
            for index, segment in enumerate(segments_list)
        }
        assigned_indices = [
            source_index[unit.source_segment_ids[0]]
            for _scene, unit in flat_units
        ]
    else:
        anchored_indices: list[int | None] = []
        for _scene, unit in flat_units:
            candidate = (
                unit.source_text
                if unit.kind == "dialogue" and unit.source_text.strip()
                else unit.text
            )
            exact_offset = source_text.find(candidate) if candidate else -1
            aligned = (
                align_source_excerpt(
                    candidate,
                    source_text,
                    min_match_chars=4,
                )
                if exact_offset < 0 and candidate
                else None
            )
            offset = (
                exact_offset
                if exact_offset >= 0
                else aligned.start_offset if aligned is not None else -1
            )
            anchored_indices.append(
                segment_index_for_offset(offset)
                if offset >= 0 else None
            )

        previous_index = 0
        for unit_index, (_scene, unit) in enumerate(flat_units):
            anchored = anchored_indices[unit_index]
            if anchored is not None:
                selected_index = max(previous_index, anchored)
            else:
                next_anchor = next(
                    (
                        candidate
                        for candidate in anchored_indices[unit_index + 1:]
                        if (
                            candidate is not None
                            and candidate >= previous_index
                        )
                    ),
                    len(segments_list) - 1,
                )
                candidates = range(
                    previous_index,
                    max(previous_index, next_anchor) + 1,
                )
                selected_index = max(
                    candidates,
                    key=lambda index: (
                        max(
                            textmatch.longest_run_ratio(
                                unit.text,
                                segments_list[index].text,
                            ),
                            textmatch.bigram_coverage(
                                unit.text,
                                segments_list[index].text,
                            ),
                        ),
                        -abs(index - previous_index),
                    ),
                )
            assigned_indices.append(selected_index)
            previous_index = selected_index
    return assigned_indices


def _ir_build_identity_alias_index(
    value: ScreenplayGenerationIR,
    episode: dict[str, Any],
) -> tuple[
    dict[str, set[str]],
    dict[str, str],
    dict[str, str],
    dict[tuple[str, int], str],
    "defaultdict[tuple[str, str], list[str]]",
    "defaultdict[tuple[str, str], list[str]]",
    "defaultdict[tuple[str, str], list[str]]",
]:
    identity_aliases: dict[str, set[str]] = {
        identity.key: {
            token
            for token in (
                identity.key,
                identity.display_name,
            )
            if token
        }
        for identity in value.identities
    }
    for resolution in episode.get("character_resolutions") or []:
        if (
            not isinstance(resolution, dict)
            or not identity_resolution_is_authoritative(resolution)
        ):
            continue
        aliases = {
            str(resolution.get(field) or "").strip()
            for field in (
                "source_label",
                "canonical_name",
                "functional_identity_key",
            )
            if str(resolution.get(field) or "").strip()
        }
        matching_keys = [
            key
            for key, tokens in identity_aliases.items()
            if aliases.intersection(tokens)
        ]
        if len(matching_keys) == 1:
            identity_aliases[matching_keys[0]].update(aliases)
    identity_display = {
        identity.key: identity.display_name
        for identity in value.identities
    }
    normalized_event_keys: dict[tuple[str, int], str] = {}
    explicit_actor_keys_by_event: defaultdict[tuple[str, str], list[str]] = (
        defaultdict(list)
    )
    explicit_target_keys_by_event: defaultdict[tuple[str, str], list[str]] = (
        defaultdict(list)
    )
    onscreen_keys_by_event: defaultdict[tuple[str, str], list[str]] = (
        defaultdict(list)
    )
    event_scene_owners: dict[str, str] = {}
    return (
        identity_aliases,
        identity_display,
        event_scene_owners,
        normalized_event_keys,
        explicit_actor_keys_by_event,
        explicit_target_keys_by_event,
        onscreen_keys_by_event,
    )


def _ir_validate_typed_scene_units(
    flat_units: list[tuple[IRScene, IRSceneUnit]],
    format_version: str,
    typed_visual_unit_contract: bool,
    identity_aliases: dict[str, set[str]],
    normalized_event_keys: dict[tuple[str, int], str],
    event_scene_owners: dict[str, str],
    explicit_actor_keys_by_event: "defaultdict[tuple[str, str], list[str]]",
    explicit_target_keys_by_event: "defaultdict[tuple[str, str], list[str]]",
    onscreen_keys_by_event: "defaultdict[tuple[str, str], list[str]]",
) -> None:
    for unit_index, (scene, unit) in enumerate(flat_units, start=1):
        event_key = unit.event_key.strip() or f"derived-event-{unit_index}"
        unit.event_key = event_key
        previous_scene_key = event_scene_owners.setdefault(
            event_key,
            scene.key,
        )
        if previous_scene_key != scene.key:
            raise ValueError(
                "IR scenes.units event_key 必须在本集唯一；"
                f"{event_key} 同时出现在 {previous_scene_key} 与 {scene.key}"
            )
        normalized_event_keys[(scene.key, unit_index)] = event_key
        if (
            typed_visual_unit_contract
            and "onscreen_entity_keys" not in unit.model_fields_set
        ):
            raise ScreenplayIRFidelityError(
                f"IR {format_version} {scene.key}.{event_key} 缺少显式 "
                "onscreen_entity_keys，禁止从姓名词面推断在场关系"
            )
        if (
            typed_visual_unit_contract
            and unit.kind == "action"
            and "actor_keys" not in unit.model_fields_set
        ):
            raise ScreenplayIRFidelityError(
                f"IR {format_version} {scene.key}.{event_key} 动作单元缺少显式 "
                "actor_keys，禁止从动作文本猜测执行者"
            )
        if typed_visual_unit_contract and (
            "state_subject_key" not in unit.model_fields_set
            or "environment_only" not in unit.model_fields_set
        ):
            raise ScreenplayIRFidelityError(
                f"IR {format_version} {scene.key}.{event_key} 缺少显式 "
                "state_subject_key/environment_only "
                "状态归属合同，"
                "旧 IR 必须重建"
            )
        if typed_visual_unit_contract:
            subject_key = unit.state_subject_key.strip()
            subject_keys = list(dict.fromkeys(unit.state_subject_keys))
            if unit.kind == "dialogue":
                if unit.environment_only:
                    raise ScreenplayIRFidelityError(
                        f"IR {format_version} {scene.key}.{event_key} 对白单元"
                        "不得声明 environment_only"
                    )
                if (
                    not unit.speaker_key
                    or subject_keys != [unit.speaker_key]
                    or subject_key != unit.speaker_key
                ):
                    raise ScreenplayIRFidelityError(
                        f"IR {format_version} {scene.key}.{event_key} 对白单元"
                        "state_subject_key 必须等于唯一 speaker_key"
                    )
            elif unit.environment_only:
                if subject_keys or unit.actor_keys:
                    raise ScreenplayIRFidelityError(
                        f"IR {format_version} {scene.key}.{event_key} 纯环境单元"
                        "不得同时声明人物 state subject/actor"
                    )
            elif (
                unit.text_provenance.kind
                in ATTRIBUTED_TEXT_PROVENANCE_KINDS
                and unit.text_provenance.content_owner_keys
                and not subject_keys
            ):
                # 刻字、告示、道具上的字由 content_owner_keys 归属，不是任何
                # 在场人物"当下的状态"：木牌上的「杂」属于宗门。编译器在生成
                # text_provenance 时本来就刻意把这三类的 identity_keys 清空，
                # 说明设计上它们不承载人物关系；同一个编译器再要求它们必须有
                # 人物 state subject 就是自相矛盾，生产上 EP2 每轮都卡在
                # bp-sc005:SRC0020:008（那个「杂」字）。
                # 归属仍然是强制的：没有 content_owner_keys 就不走这条豁免。
                pass
            elif (
                not subject_keys
                or any(key not in unit.actor_keys for key in subject_keys)
                or (
                    len(subject_keys) == 1
                    and subject_key != subject_keys[0]
                )
                or (len(subject_keys) > 1 and subject_key)
            ):
                raise ScreenplayIRFidelityError(
                    f"IR {format_version} {scene.key}.{event_key} 动作单元"
                    "必须由 exact-unit typed actor 承载 single/joint "
                    "state_subject_keys，不得从 visible/roster 猜测"
                )
        # Name occurrences remain a compatibility fallback for untyped IR.
        # Current contracts carry actor/target/on-screen relations as
        # frozen identity keys and never infer them from story words.
        visual_text = (
            f"{unit.text}\n{unit.resulting_state}"
            if unit.kind == "action" and not typed_visual_unit_contract
            else ""
        )
        mentioned = [
            key
            for key, aliases in identity_aliases.items()
            if any(alias and alias in visual_text for alias in aliases)
        ]
        explicit = list(dict.fromkeys([
            *(
                [unit.speaker_key]
                if unit.speaker_key
                else []
            ),
            *(
                unit.actor_keys
                if unit.kind == "action" and typed_visual_unit_contract
                else mentioned if unit.kind == "action" else []
            ),
        ]))
        targets = list(dict.fromkeys(unit.target_keys))
        onscreen = list(dict.fromkeys(
            unit.onscreen_entity_keys
            if typed_visual_unit_contract
            else unit.onscreen_entity_keys or explicit
        ))
        if typed_visual_unit_contract:
            relation_keys = {*explicit, *targets}
            delivery_keys: set[str] = set()
            delivery_errors: list[str] = []
            for delivery in unit.participant_deliveries:
                participant_key = delivery.participant_key.strip()
                if participant_key in delivery_keys:
                    delivery_errors.append(
                        f"{participant_key} 存在重复参与者交付合同"
                    )
                    continue
                delivery_keys.add(participant_key)
                if participant_key not in relation_keys:
                    delivery_errors.append(
                        f"{participant_key} 不是本 unit 的 actor/target/speaker"
                    )
                if participant_key in onscreen:
                    delivery_errors.append(
                        f"{participant_key} 已入画，不得声明为画外参与者交付"
                    )
                if not delivery.observable_claim.strip():
                    delivery_errors.append(
                        f"{participant_key} 缺少可感知 evidence claim"
                    )
                if not delivery.is_perceivable:
                    delivery_errors.append(
                        f"{participant_key} 未声明可听、可见影响或可见反应"
                    )
            missing_deliveries = (
                relation_keys - set(onscreen) - delivery_keys
            )
            if missing_deliveries:
                delivery_errors.append(
                    "未入画参与者缺少结构化交付合同："
                    f"{sorted(missing_deliveries)}"
                )
            if delivery_errors:
                raise ScreenplayIRFidelityError(
                    f"IR v1.5 {scene.key}.{event_key} 动作参与者交付失败："
                    + "；".join(delivery_errors)
                )
        event_actors = explicit_actor_keys_by_event[(scene.key, event_key)]
        event_actors.extend(
            key for key in explicit
            if key not in event_actors
        )
        event_targets = explicit_target_keys_by_event[(scene.key, event_key)]
        event_targets.extend(
            key for key in targets
            if key not in event_targets
        )
        event_onscreen = onscreen_keys_by_event[(scene.key, event_key)]
        event_onscreen.extend(
            key for key in onscreen
            if key not in event_onscreen
        )
