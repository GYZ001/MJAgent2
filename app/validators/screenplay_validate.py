"""剧本台脊柱交付校验（validate_screenplay_spine_delivery）与剧本整体校验
validate_screenplay——docs/PROMPT_SPEC.md §C 里 V 系列判据的剧本侧主入口。
"""
from __future__ import annotations

import math
import re

from app import textmatch
from app.character_policy import (
    is_functional_extra,
    typed_functional_identity_names,
)
from app.continuity import adaptation_hook_errors
from app.renderability import (
    SCENE_OUTLINE_MIN,
    SCENE_STORY_FUNCTION_MIN_CHARS,
)
from app.schemas import (
    Bible,
    DELIVERY_OWNERS,
    EpisodeScreenplay,
    PlotSpineBeat,
)

from .dialogue_chains import (
    _DIALOGUE_RESPONSE_FUNCTIONS,
    validate_dialogue_chains,
)
from .ending_hook import _claim_clearly_absent
from .screenplay_ledger import (
    validate_plot_spine,
    validate_screenplay_source_coverage,
)
from .screenplay_text import (
    KEY_CONTENT_MAX_REPORT,
    KEY_LINE_BIGRAM_COVERAGE,
    KEY_LINE_PRESENT_RATIO,
    MIN_KEY_LINES,
    MIN_KEY_PLOT_POINTS,
    SCRIPT_DIALOGUE_LINE_RE,
    SCRIPT_SCENE_HEADING_RE,
    _bigram_coverage,
    _condense,
    _iter_script_sound_matches,
    _longest_run_ratio,
    _matching_text_indices,
    _script_dialogue_turns,
    _speaker_name,
    _strip_speaker,
    _structured_key_line_functions,
    key_line_order_errors,
    screenplay_speaker_names,
)
from .storyboard_delivery import (
    _spine_delivery_clauses,
    _spine_receptive_claim,
)

def validate_screenplay_spine_delivery(
    script: EpisodeScreenplay,
    *,
    action_text: str,
) -> list[str]:
    """Require every must-keep spine beat to be performed in the screenplay body."""
    spine = script.plot_spine
    if not spine or not spine.spine_beats:
        return []
    dialogue_turns = _script_dialogue_turns(script.full_script_text or "")
    missing: list[str] = []
    all_spoken = "".join(spoken for _scene_no, _speaker, spoken in dialogue_turns)
    full_delivery_text = action_text + "\n" + all_spoken

    def _beat_is_substantially_delivered(beat: PlotSpineBeat) -> bool:
        """Use broad semantic evidence for long beat summaries.

        ``beat.does`` is authored by the same model and can be phrased more
        narrowly or more explicitly than the screenplay body.  The gate should
        catch real omissions, not require near-verbatim repetition of the
        planning sentence.  We therefore combine subject presence, whole-beat
        lexical coverage, and source-coverage linkage before falling back to
        per-clause checks.
        """
        parts = [
            beat.who or "",
            beat.does or "",
            beat.turn or "",
            beat.purpose or "",
        ]
        claim = "。".join(part for part in parts if str(part).strip())
        if not claim.strip():
            return False
        # Whole-beat coverage tolerates paraphrase better than requiring every
        # comma-separated clause to independently pass.
        if (
            _longest_run_ratio(claim, full_delivery_text) >= 0.22
            or _bigram_coverage(claim, full_delivery_text) >= 0.18
        ):
            return True
        if beat.source_segment_ids:
            linked = [
                decision for decision in (script.source_coverage or [])
                if str(
                    (decision.get("source_segment_id") if isinstance(decision, dict) else decision.source_segment_id)
                    or ""
                ) in set(beat.source_segment_ids)
                and (
                    (beat.beat_id or "") in (
                        (decision.get("beat_ids", []) if isinstance(decision, dict) else decision.beat_ids)
                        or []
                    )
                    or (
                        (decision.get("disposition") if isinstance(decision, dict) else decision.disposition)
                        in {"deliver", "merge"}
                    )
                )
            ]
            if linked:
                who = (beat.who or "").strip()
                who_hit = not who or who in full_delivery_text
                weak_hits = [
                    text
                    for text in (beat.does, beat.turn, beat.purpose)
                    if str(text or "").strip()
                    and (
                        _longest_run_ratio(str(text), full_delivery_text) >= 0.12
                        or _bigram_coverage(str(text), full_delivery_text) >= 0.08
                    )
                ]
                if who_hit and weak_hits:
                    return True
        return False

    for beat in spine.spine_beats:
        if not beat.must_keep:
            continue
        if _beat_is_substantially_delivered(beat):
            continue
        visible_clauses, spoken_clauses, receptive_clauses = (
            ([], [], [])
            if beat.key_line_ids or beat.information_ids
            else _spine_delivery_clauses(beat.does or "")
        )
        visible_missing = [
            clause for clause in visible_clauses
            if _claim_clearly_absent(clause, action_text)
        ]
        speaker = (beat.who or "").strip()
        spoken_by_owner = "".join(
            spoken for _scene_no, actual_speaker, spoken in dialogue_turns
            if (
                not speaker
                or speaker == actual_speaker
                or speaker in actual_speaker
                or actual_speaker in speaker
            )
        )
        spoken_missing = [
            clause for clause in spoken_clauses
            if (
                _claim_clearly_absent(clause, spoken_by_owner)
                and _claim_clearly_absent(
                    clause,
                    action_text + "\n" + all_spoken,
                )
            )
        ]
        receptive_missing = [
            clause for clause in receptive_clauses
            if _claim_clearly_absent(
                _spine_receptive_claim(clause),
                action_text + all_spoken,
            )
        ]
        if visible_missing or spoken_missing or receptive_missing:
            missing.append(
                f"{beat.beat_id}/{speaker}:{beat.does}"
            )
    if not missing:
        return []
    shown = "；".join(missing[:KEY_CONTENT_MAX_REPORT])
    extra = (
        f"（另有 {len(missing) - KEY_CONTENT_MAX_REPORT} 条从略）"
        if len(missing) > KEY_CONTENT_MAX_REPORT else ""
    )
    return [
        f"full_script_text 未交付 {len(missing)} 条 must_keep 主线节拍：{shown}{extra}；"
        "必须在对应场次的动作段或角色对白中完整演出，不能只写在 plot_spine/scene_outline 摘要里"
    ]


def validate_screenplay(script: EpisodeScreenplay, bible: Bible, expected_beats: int,
                        episode_no: int | None = None, source_text: str | None = None,
                        require_dialogue_chains: bool = False,
                        validate_narrative: bool = True,
                        require_source_coverage: bool = False,
                        functional_identity_names: set[str] | None = None,
                        episode: dict | None = None) -> list[str]:
    """纯 QA：只读取候选并返回问题，不补字段、不覆盖投影、不修改输入。"""
    errors: list[str] = []
    narrative_authority = script.narrative_plan is not None
    if narrative_authority and validate_narrative:
        from app.narrative import validate_screenplay_narrative

        errors.extend(validate_screenplay_narrative(
            script,
            require=True,
            source_text=source_text,
            expected_scope_id=str(script.id) if script.id else None,
        ))
    errors.extend(validate_dialogue_chains(
        script, source_text=source_text, required=require_dialogue_chains,
    ))
    if require_source_coverage:
        errors.extend(validate_screenplay_source_coverage(script, source_text))
    if episode_no is not None and script.episode_no != episode_no:
        errors.append(f"episode_no={script.episode_no}，必须等于 {episode_no}")
    if (script.mode or "full_script") != "full_script":
        errors.append(f"mode=「{script.mode}」非法；映射台仅支持 full_script")
    errors.extend(validate_plot_spine(
        script,
        narrative_authority=narrative_authority,
    ))
    if len((script.title or "").strip()) < 2:
        errors.append("title 过短或缺失；请填写本集标题")
    if len((script.logline or "").strip()) < 8:
        errors.append("logline 过短或缺失；请用一句话概括本集核心事件")
    if len((script.script_format_note or "").strip()) < 6:
        errors.append("script_format_note 过短或缺失；请说明正文采用的台本格式")
    scenes = script.scene_outline or []
    if len(scenes) < SCENE_OUTLINE_MIN:
        errors.append(
            f"scene_outline 场次数量为 {len(scenes)}；至少需要 {SCENE_OUTLINE_MIN} 场，"
            "场次数由完整剧情与时空边界决定，不设上限"
        )
    bible_names = {c.name for c in bible.characters}
    typed_functional_names = {
        *set(functional_identity_names or ()),
        *typed_functional_identity_names(script),
    }
    narrative_character_ids = set(bible_names)
    nonvisual_voice_ids: set[str] = set()
    visible_identity_names: dict[str, str] = {}
    expected_visible_by_scene: dict[int, set[str]] = {}
    if script.narrative_plan is not None:
        for identity in script.narrative_plan.identity_contracts:
            narrative_character_ids.update({
                identity.identity_id,
                identity.display_name,
                *identity.voice_ids,
            })
            if identity.visual_policy == "offscreen_only":
                nonvisual_voice_ids.update({
                    identity.identity_id,
                    identity.display_name,
                    *identity.voice_ids,
                })
            else:
                for value in {
                    identity.identity_id,
                    identity.display_name,
                    *identity.voice_ids,
                }:
                    if value:
                        visible_identity_names[value] = identity.display_name
        narrative_character_ids.update(
            actor_id
            for action in script.narrative_plan.atomic_actions
            for actor_id in action.actor_ids
            if actor_id
        )
        narrative_character_ids.update(
            state.character_id
            for state in script.narrative_plan.character_states
            if state.character_id
        )
        narrative_character_ids.update(
            belief.character_id
            for belief in script.narrative_plan.character_beliefs
            if belief.character_id
        )
        narrative_character_ids.update(
            voice.speaker_id for voice in script.voice_bible if voice.speaker_id
        )
        nonvisual_voice_ids.update(
            voice.speaker_id
            for voice in script.voice_bible
            if (
                voice.speaker_id
                and voice.role_type == "narrator"
                and voice.speaker_id not in visible_identity_names
            )
        )

        # The narrative graph is authoritative for whether an environment-only
        # scene actually has a visible participant.  A scene may legitimately
        # establish time/place with no person at all; conversely, a visible
        # graph identity cannot be omitted merely because the prose body has no
        # dialogue.  Only typed identities count here -- natural-language
        # mentions in summaries/actions never manufacture characters.
        event_by_id = {
            event.event_id: event
            for event in script.narrative_plan.events
            if event.event_id
        }
        action_by_id = {
            action.action_id: action
            for action in script.narrative_plan.atomic_actions
            if action.action_id
        }
        character_by_state_id = {
            state.character_state_id: state.character_id
            for state in script.narrative_plan.character_states
            if state.character_state_id and state.character_id
        }
        for fallback_index, contract in enumerate(
            script.narrative_plan.scene_contracts,
            start=1,
        ):
            match = re.search(r"\d+", contract.scene_id or "")
            scene_no = int(match.group(0)) if match else fallback_index
            referenced_ids: set[str] = set()
            for event_id in contract.turn_event_ids:
                event = event_by_id.get(event_id)
                if event is not None:
                    referenced_ids.update(event.onscreen_entity_ids)
                    for action_id in event.action_ids:
                        action = action_by_id.get(action_id)
                        if action is not None:
                            referenced_ids.update(action.actor_ids)
                            referenced_ids.update(action.target_ids)
            referenced_ids.update(
                character_by_state_id[state_id]
                for state_id in (
                    *contract.character_state_in_ids,
                    *contract.character_state_out_ids,
                )
                if state_id in character_by_state_id
            )
            if contract.point_of_view_character_id:
                referenced_ids.add(contract.point_of_view_character_id)
            expected_visible_by_scene[scene_no] = {
                visible_identity_names[value]
                for value in referenced_ids
                if value in visible_identity_names
            }

    spoken_by_scene: dict[int, set[str]] = {}
    # Use the same deterministic projection as publication.  This makes an
    # explicit chain.scene_id authoritative over stale prose placement, maps
    # an empty legacy binding through the documented semantic fallback, and
    # still retains genuinely unowned dialogue parsed from the prose body.
    from app.production.screenplay_document import (
        DialogueSceneBindingError,
        rederive_projections,
        screenplay_to_document,
    )
    try:
        projected_document = rederive_projections(
            screenplay_to_document(script),
        )
    except DialogueSceneBindingError as exc:
        errors.append(f"[DIALOGUE_SCENE_BINDING_INVALID] {exc}")
    else:
        for block in projected_document.scene_blocks:
            spoken_by_scene.setdefault(block.scene_no, set()).update(
                turn.speaker
                for turn in block.dialogue_turns
                if turn.speaker
            )
    for i, scene in enumerate(scenes, start=1):
        heading = (scene.scene_heading or "").strip()
        tag = f"scene_outline 第{i}场" + (f"「{heading}」" if heading else "")
        if scene.scene_no != i:
            errors.append(f"{tag}.scene_no 必须从 1 连续递增；当前为 {scene.scene_no}")
        if len((scene.scene_heading or "").strip()) < 4:
            errors.append(f"{tag}.scene_heading 过短；请写成可读的场次标题")
        if (
            len((scene.story_function or "").strip())
            < SCENE_STORY_FUNCTION_MIN_CHARS
        ):
            errors.append(
                f"[SCENE_STORY_FUNCTION_TOO_SHORT] {tag}.story_function "
                "过短；请说明本场戏剧功能"
            )
        if len((scene.summary or "").strip()) < 16:
            errors.append(f"{tag}.summary 过短；请概括本场具体戏剧内容")
        if len((scene.turn or "").strip()) < 4:
            errors.append(f"{tag}.turn 过短；请说明本场交给下一场的状态变化")
        if len((scene.source_basis or "").strip()) < 8:
                errors.append(
                    f"[SCENE_SOURCE_BASIS_INVALID] {tag}.source_basis "
                    "过短；请保留本场原文依据"
                )
        if script.source_coverage:
            if len((scene.entry_state or "").strip()) < 6:
                errors.append(f"{tag}.entry_state 过短；请写清人物位置、目标和关键道具")
            if len((scene.exit_state or "").strip()) < 6:
                errors.append(f"{tag}.exit_state 过短；请写清交给下一场的状态")
            if not scene.context_requirements:
                errors.append(
                    f"{tag}.context_requirements 为空；必须声明本场先建立的"
                    "时间、地点、空间关系、人物关系或关键道具"
                )
        visible_spoken = {
            speaker
            for speaker in spoken_by_scene.get(i, set())
            if speaker not in nonvisual_voice_ids
        }
        required_visible_names = {
            *expected_visible_by_scene.get(i, set()),
            *(
                visible_identity_names.get(speaker, speaker)
                for speaker in visible_spoken
            ),
        }
        requires_visible_character = (
            not narrative_authority
            or bool(required_visible_names)
        )
        if not scene.characters and requires_visible_character:
            errors.append(f"{tag}.characters 不能为空；请写本场实际参与角色")
        elif required_visible_names:
            missing_visible = sorted(
                required_visible_names - set(scene.characters),
            )
            if missing_visible:
                errors.append(
                    f"{tag}.characters 缺少结构化权威要求的"
                    f"可见参与者：{missing_visible}"
                )
        invalid_nonvisual = [
            name for name in scene.characters if name in nonvisual_voice_ids
        ]
        if invalid_nonvisual:
            errors.append(
                f"{tag}.characters 含仅声音/离屏身份：{invalid_nonvisual}；"
                "旁白或 offscreen_only 声源不得伪装成可见角色"
            )
        unknown = (
            [name for name in scene.characters if name not in narrative_character_ids]
            if narrative_authority
            else [
                name for name in scene.characters
                if (
                    name not in bible_names
                    and name not in typed_functional_names
                    and not is_functional_extra(name)
                )
            ]
        )
        if unknown and (narrative_authority or bible_names):
            contract_name = "叙事权威图" if narrative_authority else "角色圣经"
            errors.append(f"{tag}.characters 含{contract_name}外角色：{unknown}")
    full_text = (script.full_script_text or "").strip()
    spine_n = len((script.plot_spine.spine_beats if script.plot_spine else None) or [])
    if narrative_authority:
        from app.screenplay_ir import IR_MIN_ADAPTED_SOURCE_RATIO

        script_length_source_chars = len(
            textmatch.condense(source_text or "")
        )
        min_script_chars = max(
            160,
            math.ceil(
                script_length_source_chars
                * IR_MIN_ADAPTED_SOURCE_RATIO
            ),
        )
    else:
        script_length_source_chars = 0
        min_script_chars = max(
            160,
            spine_n * 36
            if spine_n
            else max(160, expected_beats * 30),
        )
    hard_min_script_chars = max(160, (min_script_chars * 99 + 99) // 100)
    if len(full_text) < hard_min_script_chars:
        errors.append(
            f"full_script_text 过短；当前仅 {len(full_text)} 字，至少需要 {min_script_chars} 字"
            "（只演主线骨架，勿注水细节）"
        )
    action_text = "\n".join(
        line for line in full_text.splitlines()
        if not SCRIPT_DIALOGUE_LINE_RE.match(line.strip())
        and not SCRIPT_SCENE_HEADING_RE.match(line.strip())
    )
    errors.extend(validate_screenplay_spine_delivery(
        script,
        action_text=action_text,
    ))
    heading_matches = SCRIPT_SCENE_HEADING_RE.findall(full_text)
    if len(heading_matches) < 3:
        errors.append("full_script_text 缺少足够的场次标题；请使用“【场1】...”这类场次化台本格式")
    elif scenes and len(heading_matches) != len(scenes):
        errors.append(f"full_script_text 场次标题数 {len(heading_matches)} 与 scene_outline 场次数 {len(scenes)} 不一致")
    content_lines = [ln for ln in full_text.splitlines() if ln.strip()]
    min_lines = max(6, len(scenes) * 2)
    if len(content_lines) < min_lines:
        errors.append("full_script_text 段落过少；请按场次标题、动作段、对白段分行书写，不要挤成一段梗概")
    dialogue_lines = [
        match.group(0) for match in _iter_script_sound_matches(full_text)
    ]
    if len(dialogue_lines) < 2:
        errors.append("full_script_text 对白行过少；请按“角色名：台词”写出真正可演的对白")
    if bible_names or narrative_authority:
        offbible_speakers = sorted({
            speaker
            for speaker in screenplay_speaker_names(full_text)
            if speaker != "旁白"
            and (
                speaker not in narrative_character_ids
                if narrative_authority
                else (
                    speaker not in bible_names
                    and speaker not in typed_functional_names
                    and not is_functional_extra(speaker)
                )
            )
        })
        if offbible_speakers:
            if narrative_authority:
                errors.append(
                    "full_script_text 含未受叙事权威图/voice_bible 定义的说话人："
                    f"{offbible_speakers}；请根据来源证据与戏剧职责补全身份合同"
                )
            else:
                errors.append(
                    "full_script_text 含未进入人物谱的具名说话人："
                    f"{offbible_speakers}；重要具名角色必须先由人物发现步骤补进人物谱，"
                    "无需定妆的临时角色请改用功能性身份标签"
                )
    if len((script.emotional_curve or "").strip()) < 6:
        errors.append("emotional_curve 过短或缺失；请说明本集情绪推进")
    ending_hook = (script.ending_hook or "").strip()
    if ending_hook and len(ending_hook) < 6:
        errors.append("ending_hook 过短或缺失；请明确本集结尾钩子")
    if len((script.source_basis or "").strip()) < 12:
        errors.append("source_basis 过短或缺失；请概括本集原文依据与关键事件")
    if len((script.dramatic_question or "").strip()) < 6:
        errors.append("dramatic_question 过短或缺失；请用一句话写出本集观众心里追问的戏剧问题")
    if len((script.protagonist_goal or "").strip()) < 4:
        errors.append("protagonist_goal 过短或缺失；请写本集主角看得见、可完成的外在目标")
    if len((script.obstacle or "").strip()) < 4:
        errors.append("obstacle 过短或缺失；请写本集阻力（外部对手/规则 + 内部恐惧/执念）")
    if len((script.stakes or "").strip()) < 4:
        errors.append("stakes 过短或缺失；请写失败代价（输了会失去什么关系/尊严/目标）")
    key_lines = [ln.strip() for ln in (script.key_lines or []) if ln and ln.strip()]
    if len(key_lines) < MIN_KEY_LINES:
        errors.append(
            f"key_lines 仅 {len(key_lines)} 条；请保留至少 {MIN_KEY_LINES} 条推动主线的台词")
    bible_names = {c.name for c in bible.characters}
    if bible_names or narrative_authority:
        non_bible_key_lines = []
        for ln in key_lines:
            speaker = _speaker_name(ln)
            if not speaker:
                continue
            invalid_speaker = (
                speaker not in narrative_character_ids
                if narrative_authority
                else (
                    speaker not in bible_names
                    and speaker not in typed_functional_names
                    and not is_functional_extra(speaker)
                )
            )
            if invalid_speaker:
                non_bible_key_lines.append(ln)
        if non_bible_key_lines:
            shown = "；".join(non_bible_key_lines[:KEY_CONTENT_MAX_REPORT])
            extra = (f"（另有 {len(non_bible_key_lines) - KEY_CONTENT_MAX_REPORT} 条从略）"
                     if len(non_bible_key_lines) > KEY_CONTENT_MAX_REPORT else "")
            if narrative_authority:
                errors.append(
                    f"key_lines 有 {len(non_bible_key_lines)} 条含未受叙事权威图/voice_bible "
                    f"定义的说话人：{shown}{extra}"
                )
            else:
                errors.append(
                    f"key_lines 有 {len(non_bible_key_lines)} 条含非人物谱角色台词：{shown}{extra}"
                    f"；key_lines 只能保留角色圣经角色（{'、'.join(sorted(bible_names))}）的台词，"
                    "功能性角色可作为对白链触发者进入 key_lines；旁白不得进入；"
                    "其他具名角色必须先补进人物谱")
    # 主线台词只能由真正的“角色名：台词”行交付。旧校验拿整个 full_script_text
    # 当 haystack，导致模型把台词抄进动作描述/梗概也能通过，页面看得到 key_lines
    # 清单，角色却从未开口。
    dialogue_turns = _script_dialogue_turns(full_text)
    script_dialogues: list[tuple[str, str]] = [
        (speaker, spoken) for _scene_no, speaker, spoken in dialogue_turns
    ]
    missing_in_dialogue: list[str] = []
    mismatched: list[str] = []
    for ln in key_lines:
        core = _strip_speaker(ln)
        matching_speakers = {
            speaker for speaker, spoken in script_dialogues
            if _longest_run_ratio(core, spoken) >= KEY_LINE_PRESENT_RATIO
            or _bigram_coverage(core, spoken) >= KEY_LINE_BIGRAM_COVERAGE
        }
        if not matching_speakers:
            missing_in_dialogue.append(ln)
            continue
        expected_speaker = _speaker_name(ln)
        if expected_speaker and expected_speaker not in matching_speakers:
            mismatched.append(
                f"{ln}（正文归属为：{'、'.join(sorted(matching_speakers))}）"
            )
    if missing_in_dialogue:
        shown = "；".join(missing_in_dialogue[:KEY_CONTENT_MAX_REPORT])
        errors.append(
            f"key_lines 有 {len(missing_in_dialogue)} 条未真正写进 full_script_text 的角色对白：{shown}"
            "；主线台词必须落在“角色名：台词”对白行，动作描述或梗概中的文字不算交付")
    if mismatched:
        shown = "；".join(mismatched[:KEY_CONTENT_MAX_REPORT])
        extra = (f"（另有 {len(mismatched) - KEY_CONTENT_MAX_REPORT} 条从略）"
                 if len(mismatched) > KEY_CONTENT_MAX_REPORT else "")
        errors.append(
            f"key_lines 有 {len(mismatched)} 条台词的说话人与 full_script_text 不一致：{shown}{extra}"
            "；同一句台词在 key_lines 和 full_script_text 中必须由同一角色说出")
    errors.extend(key_line_order_errors(
        key_lines,
        [spoken for _scene_no, _speaker, spoken in dialogue_turns],
        subject="full_script_text",
    ))
    orphan_responses: list[str] = []
    spoken_turn_texts = [spoken for _scene_no, _speaker, spoken in dialogue_turns]
    key_turn_indices = {
        index
        for key_line in key_lines
        for index in _matching_text_indices(key_line, spoken_turn_texts)
    }
    for line in key_lines:
        structured_functions = _structured_key_line_functions(script, line)
        is_context_dependent = bool(
            structured_functions & _DIALOGUE_RESPONSE_FUNCTIONS
        )
        if not is_context_dependent:
            continue
        candidates = _matching_text_indices(
            line, [spoken for _scene_no, _speaker, spoken in dialogue_turns]
        )
        if not candidates:
            continue
        expected_speaker = _speaker_name(line)
        spoken_core = _condense(_strip_speaker(line))
        exact_speaker_candidates = [
            index for index in candidates
            if (
                (not expected_speaker or dialogue_turns[index][1] == expected_speaker)
                and _condense(dialogue_turns[index][2]) == spoken_core
            )
        ]
        if exact_speaker_candidates:
            candidates = exact_speaker_candidates
        elif expected_speaker:
            same_speaker_candidates = [
                index for index in candidates
                if dialogue_turns[index][1] == expected_speaker
            ]
            if same_speaker_candidates:
                candidates = same_speaker_candidates
        turn_index = candidates[0]
        scene_no, speaker, _spoken = dialogue_turns[turn_index]
        prior_context = [
            dialogue_turns[prior_index]
            for prior_index in range(max(0, turn_index - 2), turn_index)
            if dialogue_turns[prior_index][0] == scene_no
            and dialogue_turns[prior_index][1] != speaker
            and prior_index in key_turn_indices
        ]
        if not prior_context:
            orphan_responses.append(line)
    if orphan_responses:
        shown = "；".join(orphan_responses[:KEY_CONTENT_MAX_REPORT])
        errors.append(
            f"[KEY_LINE_MISSING] 主线对白上下文断裂：{shown}；"
            "该话轮依赖前文，"
            "必须把同一场前两轮内另一角色的触发台词也列入 key_lines，"
            "让下游整组保留，不能让主要角色突然冒出一句回应"
        )
    _ = source_text  # 保留参数兼容；全量原文台词入库已废止
    key_points = [pt.strip() for pt in (script.key_plot_points or []) if pt and pt.strip()]
    if len(key_points) < MIN_KEY_PLOT_POINTS:
        errors.append(
            f"key_plot_points 仅 {len(key_points)} 条；请列出至少 {MIN_KEY_PLOT_POINTS} 条与 spine 对齐的局势变化"
            "，数量由完整剧情决定")
    event_ids: set[str] = set()
    if not script.events:
        errors.append("events 不能为空；必须把完整剧本拆成可追溯的状态变化事件")
    for i, event in enumerate(script.events or []):
        tag = f"events[{i}]"
        event_id = (event.event_id or "").strip()
        if not event_id:
            errors.append(f"{tag}.event_id 不能为空")
        elif event_id in event_ids:
            errors.append(f"{tag}.event_id=「{event_id}」重复；events.event_id 必须唯一")
        else:
            event_ids.add(event_id)
        for field in ("state_in", "visible_change", "state_out"):
            if len((getattr(event, field, "") or "").strip()) < 4:
                errors.append(f"{tag}.{field} 缺失或过短；事件必须写清状态输入、可见变化和状态输出")
    info_ids: set[str] = set()
    if not script.information_ledger:
        errors.append("information_ledger 不能为空；必须为观众需要获得的剧情信息建立中文交付台账")
    ledger = script.information_ledger or []
    for i, item in enumerate(ledger):
        tag = f"information_ledger[{i}]"
        info_id = (item.info_id or "").strip()
        if not info_id:
            errors.append(f"{tag}.info_id 不能为空")
        elif info_id in info_ids:
            errors.append(f"{tag}.info_id=「{info_id}」重复；information_ledger.info_id 必须唯一")
        else:
            info_ids.add(info_id)
        if info_id and not re.fullmatch(r"I\d{1,4}", info_id, flags=re.IGNORECASE):
            errors.append(
                f"{tag}.info_id=「{info_id}」不是稳定内部编号；请使用 I1、I2 这类编号，"
                "不要使用英文 snake_case 剧情描述"
            )
        content = (item.content or "").strip()
        if len(content) < 4 or not re.search(r"[\u3400-\u9fff]", content):
            errors.append(f"{tag}.content 必须用简体中文写清观众获得的具体信息")
        event_id = (item.event_id or "").strip()
        if not event_id or event_id not in event_ids:
            errors.append(f"{tag}.event_id=「{event_id}」未对应 events 中的有效事件")
        if item.delivery_owner and item.delivery_owner not in DELIVERY_OWNERS:
            errors.append(f"信息 {item.info_id} 的 delivery_owner={item.delivery_owner} 不合法")
    if script.plot_spine and script.plot_spine.drop_list:
        drop_hits = []
        for drop in script.plot_spine.drop_list:
            d = (drop or "").strip()
            if len(_condense(d)) < 6:
                continue
            if _bigram_coverage(d, full_text) >= 0.55:
                drop_hits.append(d)
        if drop_hits:
            shown = "；".join(drop_hits[:KEY_CONTENT_MAX_REPORT])
            errors.append(
                f"full_script_text 又写回了 drop_list 中的内容：{shown}；"
                "已声明不拍的支线/气氛戏不得出现在正文"
            )
    errors.extend(adaptation_hook_errors(script, episode))
    return list(dict.fromkeys(errors))

