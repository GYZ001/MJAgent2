"""分镜台核心校验入口。

validate_storyboard 是 docs/PROMPT_SPEC.md §C 里 V1/V2/V7~V11 等分镜台业务
校验的主判据函数；storyboard_pack_dialogue_errors / validate_storyboard_pack_dialogue
是分镜台 2.0.0 台词闸门（唯一从 F1-F6 那批幸存的闸门，判据已放宽）。
"""
from __future__ import annotations

from typing import Any

from app import config
from app.character_policy import (
    is_allowed_storyboard_character,
    is_functional_extra,
    typed_functional_identity_names,
)
from app.continuity import (
    action_capacity_errors,
    count_sequential_action_beats,
    dialogue_focus_subject,
    dialogue_two_shot_required,
    implicit_speech_without_dialogue_errors,
    narrative_action_capacity_profile,
    shot_id_space_errors,
    speech_capacity_errors,
    spoken_chars_from_shot,
    spoken_contract_coherence_errors,
    state_chain_errors,
    sync_shot_continuity_fields,
)
from app.renderability import (
    ACTION_DESC_HARD_MIN,
    ACTION_DESC_TARGET_MAX,
    ACTION_DESC_TARGET_MIN,
    DURATION_REVIEW_RISK_TAG,
    HUMAN_DURATION_REVIEW_TAG,
    PREFERRED_SHOT_DURATION_S,
    duration_gt5_errors,
    shot_count_budget_errors,
    shot_duration_should_prefer_five,
)
from app.scene_contract import (
    scene_name_of,
    scene_time_of,
)
from app.schemas import (
    Bible,
    CAMERA_MOVES,
    CONTINUITY_MODES,
    EpisodeScreenplay,
    NarrativeContinuityPlan,
    SHOT_SIZES,
    Shot,
    Storyboard,
    TRANSITIONS,
    is_system_environment_entity_id,
)
from app.spoken_contract import spoken_speakers

from .primitives import (
    SAME_SCENE_CONTINUITY_MODES,
    SCENE_CUT_TRANSITIONS,
    SOURCE_EXCERPT_MIN_CHARS,
    _named_character_is_explicitly_offscreen,
    _scene_time_changed,
    _shot_capacity_budget_total,
    _too_similar,
    adjacent_spoken_repeat_errors,
    normalize_action_desc,
)
from .scene_match import (
    _storyboard_scene_contiguity_key,
    validate_storyboard_scenes,
)

def validate_storyboard(
    board: Storyboard,
    bible: Bible,
    target_duration_s: int,
    *,
    narrative_authority: bool = False,
    narrative_plan: NarrativeContinuityPlan | None = None,
    screenplay: EpisodeScreenplay | None = None,
) -> list[str]:
    errors: list[str] = []
    shots = board.shots
    if not shots:
        return [
            "shots 为空；请按完整剧本至少生成一个 "
            f"{config.VIDEO_DURATION_MIN_S}~{config.VIDEO_DURATION_MAX_S} 秒镜头"
        ]

    if all(shot.storyboard_pack_segment is not None for shot in shots):
        # 分镜台 2.0.0（app.production.storyboard_pack）的段行不满足本函数其余
        # 500 行假设的「一个 Shot 行 = 一个连续镜头」前提：shot_size/camera_move
        # 枚举、场景连续性、台词按 shot.characters 逐句核验等，对「一行 = 15 秒
        # 段、段内 3-4 镜写在 prompt_text 文本里」的行都无意义。退役声明与
        # app.continuity.dialogue_framing_errors 是同一决策
        # （docs/STORYBOARD_PROMPT_IR_DESIGN.md），这里同样整体短路，只保留对
        # 这类行仍然成立的两条结构检查，不把旧的单镜假设静默套用在新行上。
        pack_errors: list[str] = []
        for i, shot in enumerate(shots):
            if shot.duration_s != 15:
                pack_errors.append(
                    f"shots[{i}](shot_no={shot.shot_no}).duration_s=「{shot.duration_s}」"
                    "不是分镜台 2.0.0 冻结的段时长 15s"
                )
        expected_nos = list(range(1, len(shots) + 1))
        actual_nos = [s.shot_no for s in shots]
        if actual_nos != expected_nos:
            pack_errors.append(f"shot_no 必须为连续递增 1..{len(shots)}，当前为 {actual_nos}")
        return pack_errors

    # 先将模糊/旧式输入归一成规范 scene_name，后续连续性只比较
    # 场景图身份，不再把时间文案混进场景图外键。
    errors.extend(validate_storyboard_scenes(board, bible))

    bible_names = {c.name for c in bible.characters}
    declared_functional_names = typed_functional_identity_names(screenplay)
    narrative_character_ids: set[str] = set()
    narrative_actions: dict[str, Any] = {}
    narrative_action_relations: dict[str, tuple[list[str], list[str]]] = {}
    identity_resolver = None
    if narrative_authority and narrative_plan is not None:
        narrative_actions = {
            action.action_id: action for action in narrative_plan.atomic_actions
        }
        narrative_character_ids.update(
            character_id
            for action in narrative_plan.atomic_actions
            for character_id in [*action.actor_ids, *action.target_ids]
            if character_id
        )
        narrative_character_ids.update(
            entity_id
            for proposition in narrative_plan.propositions
            for entity_id in proposition.entity_ids
            if entity_id and not is_system_environment_entity_id(entity_id)
        )
        narrative_character_ids.update(
            fact.subject_id
            for fact in narrative_plan.state_facts
            if (
                fact.subject_id
                and not is_system_environment_entity_id(fact.subject_id)
            )
        )
        narrative_character_ids.update(
            state.character_id
            for state in narrative_plan.character_states
            if state.character_id
        )
        narrative_character_ids.update(
            belief.character_id
            for belief in narrative_plan.character_beliefs
            if belief.character_id
        )
        narrative_character_ids.update(
            scene.point_of_view_character_id
            for scene in narrative_plan.scene_contracts
            if scene.point_of_view_character_id
        )
        # Bible identities remain valid presentation aliases; every additional
        # identity must come from the authority graph rather than a canned role
        # vocabulary.
        narrative_character_ids.update(bible_names)
        from app.identity_contracts import (
            IdentityContractError,
            narrative_identity_resolver,
        )

        if screenplay is None or screenplay.narrative_plan is not narrative_plan:
            errors.append(
                "[NARRATIVE_IDENTITY_AUTHORITY_MISSING] narrative 分镜校验必须提供"
                "同一已发布 EpisodeScreenplay，禁止只用孤立 graph 推断身份政策"
            )
        else:
            try:
                identity_resolver = narrative_identity_resolver(bible, screenplay)
                from app.identity_contracts import storyboard_action_relation_ids

                action_event_owner = {
                    action_id: event.event_id
                    for event in narrative_plan.events
                    for action_id in event.action_ids
                }
                narrative_action_relations = {
                    action_id: storyboard_action_relation_ids(
                        screenplay,
                        action_event_owner.get(action_id, ""),
                        action,
                    )
                    for action_id, action in narrative_actions.items()
                }
            except IdentityContractError as exc:
                errors.append(f"[NARRATIVE_IDENTITY_CONTRACT_INVALID] {exc}")

    beat_unit = config.VIDEO_DURATION_MIN_S
    if target_duration_s % beat_unit != 0:
        errors.append(
            f"目标时长 {target_duration_s}s 不是 {beat_unit}s 的整数倍；"
            f"节拍单元按 {beat_unit}s 换算，目标时长不设上限")
    # 镜头数量和整集总时长不设产品上限；完整覆盖剧本是唯一收束条件。

    scene_last_seen: dict[str, int] = {}
    for i, shot in enumerate(shots):
        # The historical cleanup below recognizes one Chinese template token.
        # It is safe only for legacy, contract-less boards.  A narrative board
        # carries AI-authored atomic-action identity and must never be rewritten
        # by vocabulary heuristics before graph validation.
        if not narrative_authority:
            shot.action_desc = normalize_action_desc(shot.action_desc)
        tag = f"shots[{i}](shot_no={shot.shot_no})"
        # V2 时长合法取值
        if shot.duration_s not in config.ALLOWED_DURATIONS:
            errors.append(
                f"{tag}.duration_s={shot.duration_s}，必须由模型按本镜动作与口播判断为 "
                f"{config.VIDEO_DURATION_MIN_S}~{config.VIDEO_DURATION_MAX_S}s 的整数")
        spoken_for_dur = spoken_chars_from_shot(shot)
        if narrative_authority:
            action_beats_for_dur, action_min_s, _contract_errors = (
                narrative_action_capacity_profile(shot, narrative_plan)
            )
            narrative_viewing_min_s = max(
                action_min_s,
                _shot_capacity_budget_total(shot),
            )
        else:
            action_beats_for_dur = count_sequential_action_beats(
                (shot.primary_action or shot.action_desc or "").strip()
            )
            action_min_s = 0.0
        if HUMAN_DURATION_REVIEW_TAG not in (shot.risk_tags or []):
            if not narrative_authority or narrative_viewing_min_s <= PREFERRED_SHOT_DURATION_S:
                errors.extend(duration_gt5_errors(
                    shot_no=shot.shot_no,
                    duration_s=shot.duration_s,
                    spoken_chars=spoken_for_dur,
                    action_beats=action_beats_for_dur,
                ))
        if (
            int(shot.duration_s or 0) > PREFERRED_SHOT_DURATION_S
            and not shot_duration_should_prefer_five(
                spoken_chars=spoken_for_dur, action_beats=action_beats_for_dur
            )
            and HUMAN_DURATION_REVIEW_TAG not in (shot.risk_tags or [])
            and DURATION_REVIEW_RISK_TAG not in (shot.risk_tags or [])
        ):
            tags = list(shot.risk_tags or [])
            tags.append(DURATION_REVIEW_RISK_TAG)
            shot.risk_tags = tags
        # V8 画面清晰度：单镜只演一个连贯主动作（Renderability：硬下限约 18，目标 25~55）。
        if len(shot.action_desc) < ACTION_DESC_HARD_MIN:
            errors.append(
                f"{tag}.action_desc 仅 {len(shot.action_desc)} 字，低于硬下限 {ACTION_DESC_HARD_MIN} 字；"
                f"请用 {ACTION_DESC_TARGET_MIN}~{ACTION_DESC_TARGET_MAX} 字写清这一个大形体主动作（谁做了什么），"
                "禁止堆微表情/手指/衣褶细节")
        elif len(shot.action_desc) > ACTION_DESC_TARGET_MAX + 40:
            errors.append(
                f"{tag}.action_desc 共 {len(shot.action_desc)} 字，过长易塞入超纲细节；"
                f"请压缩到约 {ACTION_DESC_TARGET_MAX} 字以内，只保留单主动作")
        source_len = len((shot.source_excerpt or "").strip())
        if source_len < SOURCE_EXCERPT_MIN_CHARS:
            errors.append(
                f"{tag}.source_excerpt 仅 {source_len} 字；每个分镜必须带对应小说原文摘录，"
                f"请从本集原文中逐字摘录至少 {SOURCE_EXCERPT_MIN_CHARS} 字作为上游改编证据与审核追溯，不得送入 Seedance")
        # 首尾帧：必须填写且明显不同（否则生成的首图/尾图一模一样、视频没有动作）
        ff = (shot.first_frame_desc or "").strip()
        lf = (shot.last_frame_desc or "").strip()
        if len(ff) < 10:
            errors.append(f"{tag}.first_frame_desc 太短或缺失；请写本镜【开始】的静止画面（动作发生前，25~50字）")
        if len(lf) < 10:
            errors.append(f"{tag}.last_frame_desc 太短或缺失；请写本镜【结束】的静止画面（动作完成后，25~50字）")
        if not narrative_authority and ff and lf and _too_similar(ff, lf):
            errors.append(
                f"{tag} 首帧与尾帧画面描述几乎相同；二者必须明显不同（动作前 vs 动作后，体现姿态/表情/手部/道具的可见变化），"
                "否则首图尾图会一模一样、视频没有动作")
        errors.extend(action_capacity_errors(
            shot,
            narrative_authority=narrative_authority,
            narrative_plan=narrative_plan,
        ))
        # 口播容量只在 speech_capacity_errors 里实现一次；此处曾重复计算同一规则，
        # 导致同一根因在确认门输出两条不同文案（VAL-422 根因 R5）。
        errors.extend(speech_capacity_errors(shot))
        # 同一镜头只能有一套有效口播：dialogues 与 audio_timeline 分叉即 blocker。
        errors.extend(spoken_contract_coherence_errors(shot))
        errors.extend(implicit_speech_without_dialogue_errors(shot))
        # 产品合同：禁止一切旁白/内心OS；信息由真实台词或画面动作承载。
        narration_len = len((shot.narration or "").strip())
        if narration_len > 0:
            errors.append(
                f"{tag}.narration 非空（{narration_len} 字）；禁止旁白/内心OS，请删空 narration，"
                "改用 dialogues 真实台词或 action_desc 画面动作")
        errors.extend(shot_id_space_errors(shot))
        # V4 角色合法性
        if not shot.characters and not narrative_authority:
            errors.append(
                f"{tag}.characters 为空；每个视频段至少包含 1 个画面角色，"
                "可以是角色圣经成员或功能性路人"
            )
        for name in shot.characters:
            if narrative_authority:
                try:
                    if identity_resolver is None:
                        raise IdentityContractError("身份解析器不可用")
                    identity_resolver.resolve(name, usage="visual")
                except IdentityContractError as exc:
                    errors.append(
                        f"[NARRATIVE_CHARACTER_REF_MISSING] {tag}.characters 含「{name}」：{exc}"
                    )
            elif not narrative_authority and not is_allowed_storyboard_character(
                name,
                bible_names,
                declared_functional_names=declared_functional_names,
            ):
                errors.append(
                    f"{tag}.characters 含「{name}」，既不在角色圣经中，也不是允许的功能性路人标签；"
                    f"圣经角色为：{'/'.join(sorted(bible_names))}。功能身份必须由持久化人物决议"
                    "或 voice_bible 明确声明，不能根据称谓推断"
                )
        # characters 不是唯一的角色来源。Prompt 会从 characters_visible、
        # audio_cast/audio_timeline 和 reference_roles 继续取名，所以必须在同一门禁
        # 中检查，防止旧合同绕过 characters 校验后在编译阶段爆炸。
        # 空的 characters_visible 会在渲染时合法回退到 characters，
        # 这里只校验“显式扩展合同”，避免同一 legacy 错误重复报两次。
        task_actor_ids = {
            actor_id
            for action_id in [
                *([shot.primary_action_id] if shot.primary_action_id else []),
                *(shot.supporting_action_ids or []),
            ]
            for actor_id in narrative_action_relations.get(
                action_id,
                (
                    list(narrative_actions[action_id].actor_ids)
                    if action_id in narrative_actions else [],
                    [],
                ),
            )[0]
        }
        task_target_ids = {
            target_id
            for action_id in [
                *([shot.primary_action_id] if shot.primary_action_id else []),
                *(shot.supporting_action_ids or []),
            ]
            for target_id in narrative_action_relations.get(
                action_id,
                (
                    [],
                    list(narrative_actions[action_id].target_ids)
                    if action_id in narrative_actions else [],
                ),
            )[1]
        }
        spoken_identity_names = set(spoken_speakers(shot))
        delivered_actor_ids = {
            *shot.characters,
            *(shot.characters_visible or []),
            *(shot.visible_entity_ids or []),
            *spoken_identity_names,
            *(shot.offscreen_action_actor_ids or []),
        }
        delivered_target_ids = {
            *shot.characters,
            *(shot.characters_visible or []),
            *(shot.visible_entity_ids or []),
            *spoken_identity_names,
            *(shot.offscreen_action_target_ids or []),
        }
        missing_task_actors = task_actor_ids - delivered_actor_ids
        if narrative_authority and missing_task_actors:
            errors.append(
                f"[NARRATIVE_ACTION_ACTOR_UNDELIVERED] {tag} 执行者 "
                f"{sorted(missing_task_actors)} 既未可见/可听，也未通过 "
                "offscreen_action_actor_ids 显式声明画外交付"
            )
        invalid_offscreen_actors = set(shot.offscreen_action_actor_ids or []) - task_actor_ids
        if narrative_authority and invalid_offscreen_actors:
            errors.append(
                f"[NARRATIVE_OFFSCREEN_ACTOR_INVALID] {tag}.offscreen_action_actor_ids "
                f"含非本镜绑定动作执行者 {sorted(invalid_offscreen_actors)}"
            )
        missing_task_targets = task_target_ids - delivered_target_ids
        if narrative_authority and missing_task_targets:
            errors.append(
                f"[NARRATIVE_ACTION_TARGET_UNDELIVERED] {tag} 作用对象 "
                f"{sorted(missing_task_targets)} 既未可见/可听，也未通过 "
                "offscreen_action_target_ids 显式声明画外交付"
            )
        invalid_offscreen_targets = (
            set(shot.offscreen_action_target_ids or []) - task_target_ids
        )
        if narrative_authority and invalid_offscreen_targets:
            errors.append(
                f"[NARRATIVE_OFFSCREEN_TARGET_INVALID] {tag}.offscreen_action_target_ids "
                f"含非本镜绑定动作作用对象 {sorted(invalid_offscreen_targets)}"
            )

        for entity_id in shot.visible_entity_ids or []:
            if narrative_authority and entity_id not in narrative_character_ids:
                errors.append(
                    f"[NARRATIVE_ENTITY_REF_MISSING] {tag}.visible_entity_ids 含"
                    f"权威图未定义的实体「{entity_id}」"
                )

        declared_visible = list(shot.characters_visible or [])
        for name in declared_visible:
            if narrative_authority:
                try:
                    if identity_resolver is None:
                        raise IdentityContractError("身份解析器不可用")
                    identity_resolver.resolve(name, usage="visual")
                except IdentityContractError as exc:
                    errors.append(
                        f"[NARRATIVE_CHARACTER_REF_MISSING] {tag}.characters_visible 含「{name}」：{exc}"
                    )
            elif not narrative_authority and not is_allowed_storyboard_character(
                name,
                bible_names,
                declared_functional_names=declared_functional_names,
            ):
                errors.append(
                    f"{tag}.characters_visible 含「{name}」，既不在角色圣经中，"
                    "也不是允许的功能性路人或群体标签；请同步镜头角色合同"
                )
            elif name not in shot.characters:
                errors.append(
                    f"{tag}.characters_visible 含「{name}」，但 characters 中没有该角色；"
                    "可见名单必须是镜头角色名单的子集"
                )
        for name in spoken_speakers(shot):
            if narrative_authority:
                try:
                    if identity_resolver is None:
                        raise IdentityContractError("身份解析器不可用")
                    identity_resolver.resolve(name, usage="voice")
                except IdentityContractError as exc:
                    errors.append(
                        f"[NARRATIVE_SPEAKER_REF_MISSING] {tag}.声轨角色「{name}」：{exc}"
                    )
            elif not narrative_authority and not is_allowed_storyboard_character(
                name,
                bible_names,
                declared_functional_names=declared_functional_names,
            ):
                errors.append(
                    f"{tag}.声轨角色「{name}」既不在角色圣经中，"
                    "也不是允许的功能性路人或群体标签"
                )
        for role in shot.reference_roles or []:
            prefix, separator, name = str(role or "").partition(":")
            if separator and prefix in {"character_identity", "collective_group"}:
                if narrative_authority:
                    try:
                        if identity_resolver is None:
                            raise IdentityContractError("身份解析器不可用")
                        identity = identity_resolver.resolve(name, usage="visual")
                        if (prefix == "collective_group") != identity.is_collective:
                            raise IdentityContractError(
                                f"reference role={prefix} 与 visual_policy={identity.visual_policy} 不一致"
                            )
                    except IdentityContractError as exc:
                        errors.append(
                            f"[NARRATIVE_REFERENCE_ROLE_MISSING] {tag}.reference_roles 引用「{name}」：{exc}"
                        )
                elif not narrative_authority and not is_allowed_storyboard_character(
                    name,
                    bible_names,
                    declared_functional_names=declared_functional_names,
                ):
                    errors.append(
                        f"{tag}.reference_roles 残留非法角色「{name}」；"
                        "请重建角色参考合同"
                    )
        named_mentions = [name for name in shot.characters if name in shot.action_desc]
        if shot.characters and not named_mentions and not narrative_authority:
            errors.append(
                f"{tag}.action_desc 未出现本镜头角色名；必须用 characters 中的准确姓名"
                "（角色圣经成员或功能性路人标签）写人物动作，不要只写他/她/纸张/镜头/场景")
        visual_text = "".join(
            (shot.action_desc or "", shot.first_frame_desc or "", shot.last_frame_desc or "")
        )
        if not narrative_authority:
            focus_subject = dialogue_focus_subject(shot)
            if focus_subject and not dialogue_two_shot_required(shot):
                for other_name in sorted(bible_names - {focus_subject}):
                    if other_name not in visual_text:
                        continue
                    if _named_character_is_explicitly_offscreen(other_name, visual_text):
                        continue
                    errors.append(
                        f"{tag} 是「{focus_subject}」的单人对白近景，但 action_desc/首尾帧仍把"
                        f"「{other_name}」写进可见画面；请把听者明确留在画外，下一话轮再切反打"
                    )
        for name in (
            item for item in shot.characters
            if (
                not narrative_authority
                and (
                    is_functional_extra(item)
                    or item in declared_functional_names
                )
            )
        ):
            if name not in visual_text:
                errors.append(
                    f"{tag}.characters 中的功能性路人「{name}」未在 action_desc/首尾帧中明确入画；"
                    "路人可以不进角色圣经，但必须看得见其位置、动作或开口过程"
                )
        visible_speakers = set(shot.characters)
        audio_cast = set(getattr(shot, "audio_cast", []) or [])
        for j, d in enumerate(shot.dialogues):
            delivery = getattr(d, "delivery", "spoken_dialogue") or "spoken_dialogue"
            if delivery == "offscreen_voice" or d.speaker in audio_cast:
                continue
            if d.speaker not in visible_speakers:
                errors.append(
                    f"{tag}.dialogues[{j}].speaker=「{d.speaker}」不在该镜头 characters 中；"
                    "画面开口台词必须由 characters 中的可见角色说出，画外音请设 delivery=offscreen_voice 或加入 audio_cast")
        # V5：可变时长视频段只允许一个连续动作，禁止回到低信息空动作。
        if len(shot.action_desc) < 10:
            errors.append(f"{tag}.action_desc 长度 {len(shot.action_desc)} 字，要求至少 10 字")
        # 枚举值
        if shot.shot_size not in SHOT_SIZES:
            errors.append(f"{tag}.shot_size=「{shot.shot_size}」不在 {sorted(SHOT_SIZES)}")
        if shot.camera_move not in CAMERA_MOVES:
            errors.append(f"{tag}.camera_move=「{shot.camera_move}」不在 {sorted(CAMERA_MOVES)}")
        if shot.transition not in TRANSITIONS:
            errors.append(f"{tag}.transition=「{shot.transition}」不在 {sorted(TRANSITIONS)}")
        # Authority outlines distinguish later revisits with scene_id; legacy
        # boards without IDs continue to use the normalized location name.
        scene = scene_name_of(shot)
        scene_key = _storyboard_scene_contiguity_key(
            shot,
            narrative_authority=narrative_authority,
        )
        if scene_key in scene_last_seen and scene_last_seen[scene_key] != i - 1:
            errors.append(f"场景「{scene}」在 shots[{scene_last_seen[scene_key]}] 与 shots[{i}] 间被其他场景打断，同场景镜头必须连续排列")
        scene_last_seen[scene_key] = i
        # V6+ 连贯性：continuity_mode 表达剪辑语义；所有同场景模式都使用上一镜真实视频尾帧。
        # 始终 sync：无 prev 时会降级 action_continuation，与 derive_continuity_mode / 入队门禁一致。
        prev_for_mode = shots[i - 1] if i > 0 else None
        mode = (
            (shot.continuity_mode or "").strip()
            if narrative_authority
            else sync_shot_continuity_fields(shot, prev_for_mode)
        )
        if mode not in CONTINUITY_MODES:
            errors.append(f"{tag}.continuity_mode=「{mode}」不在 {sorted(CONTINUITY_MODES)}")
        if i == 0:
            if mode == "action_continuation":
                errors.append(f"{tag}.continuity_mode=action_continuation，但第一个镜头没有上一镜可承接")
            if shot.continuity_from_prev:
                errors.append(f"{tag}.continuity_from_prev=true，但第一个镜头没有上一镜可承接")
        elif mode in CONTINUITY_MODES:
            prev = shots[i - 1]
            prev_scene = scene_name_of(prev)
            time_changed = _scene_time_changed(scene_time_of(prev), scene_time_of(shot))
            same_scene = scene == prev_scene and not time_changed
            shared_chars = set(prev.characters) & set(shot.characters)
            if same_scene and mode == "scene_change":
                errors.append(f"{tag}.continuity_mode=scene_change 但 scene_name/scene_time 与上一镜相同")
            if not same_scene and mode != "scene_change":
                errors.append(
                    f"{tag}.continuity_mode={mode} 但 scene_name 或 scene_time 已变化；"
                    "跨时间/地点必须使用 scene_change")
            if mode == "action_continuation":
                if shot.transition != "硬切":
                    errors.append(f"{tag}.transition=「{shot.transition}」，action_continuation 必须使用「硬切」")
                if not narrative_authority and not shared_chars:
                    errors.append(
                        f"{tag}.continuity_mode=action_continuation 但与上一镜没有共同角色；"
                        "动作连续必须由同一权威身份承接")
                if not shot.continuity_from_prev:
                    errors.append(
                        f"{tag}.continuity_from_prev=false 但 continuity_mode=action_continuation；"
                        "同场景镜头必须使用上一镜采用视频的真实尾帧作为唯一首帧输入")
            elif mode in SAME_SCENE_CONTINUITY_MODES:
                if not same_scene:
                    errors.append(
                        f"{tag}.continuity_mode={mode} 但 scene_name 或 scene_time 已变化；"
                        "同场景切换模式必须沿用同一场景与时间")
                if shot.transition != "硬切":
                    errors.append(f"{tag}.transition=「{shot.transition}」，{mode} 必须使用「硬切」")
                if not shot.continuity_from_prev:
                    errors.append(
                        f"{tag}.continuity_from_prev=false 但 continuity_mode={mode}；"
                        "同场景镜头必须使用上一镜采用视频的真实尾帧作为唯一首帧输入")
            elif mode == "scene_change":
                if shot.continuity_from_prev:
                    errors.append(
                        f"{tag}.continuity_from_prev=true 但 continuity_mode=scene_change；"
                        "换场不得使用上一镜尾帧连续参考")
                if shot.transition == "硬切":
                    errors.append(
                        f"{tag}.transition=硬切 但 continuity_mode=scene_change（「{prev_scene}」→「{scene}」）；"
                        f"跨时间/地点请用 {sorted(SCENE_CUT_TRANSITIONS)} 之一，并写清承接")
                elif shot.transition not in SCENE_CUT_TRANSITIONS:
                    errors.append(
                        f"{tag}.transition=「{shot.transition}」不适合换场；"
                        f"换场请用 {sorted(SCENE_CUT_TRANSITIONS)} 之一")
    # V7 shot_no 连续
    expected = list(range(1, len(shots) + 1))
    actual = [s.shot_no for s in shots]
    if actual != expected:
        errors.append(f"shot_no 必须为连续递增 1..{len(shots)}，当前为 {actual}")

    errors.extend(adjacent_spoken_repeat_errors(board))
    errors.extend(state_chain_errors(
        board,
        narrative_authority=narrative_authority,
    ))
    errors.extend(shot_count_budget_errors(len(shots), context="分镜"))

    return errors


# ---------- 分镜台 2.0.0 台词闸门（唯一从 F1-F6 那批幸存的闸门，判据已放宽） ----------
#
# docs/STORYBOARD_PROMPT_IR_DESIGN.md「台词闸门」：原 F1-F6「内容不许编」批次
# 是老「事件链 -> 分镜大纲 -> 逐镜」管线的闸门，管线拆了闸门跟着作废，不在
# 分镜台 2.0.0 里复活。只有 F3 的内核（说话人张冠李戴 / 凭空造话）升级留用，
# 且判据已于 2026-08-26 由用户放宽：台词不要求逐字出自原文，允许省略、压缩、
# 改写措辞，只要不偏离本章剧情；只保留两条——
#   1. 说话人必须在场：该角色在这一段原文里有在场证据，不是只出现过名字。
#   2. 每句台词必须有可溯源的原文段落：source_segment_index 指向的原文里，
#      确实是这个人在这个位置说了意思相当的话。比对出处，不比对措辞。
# 「在场证据」用 app.production.storyboard_pack 生成时已经算好的
# resources.characters（来自 asset_manifest 的 segment_indexes 交集，即
# prep_pack 自己对「这个人物在这些段落被提到/在场」的判定），不在这里重新
# 从原文做字符串匹配——避免和映射台的在场判据产生第二套、可能漂移的实现。

def storyboard_pack_dialogue_errors(shot: Shot) -> list[str]:
    """分镜台段行的台词闸门：说话人在场 + 台词可溯源；delivery 感知（2.1.0
    受控画外音，旧行无此键按 spoken_dialogue 处理）；旧架构行返回空列表。"""
    segment = shot.storyboard_pack_segment
    if segment is None:
        return []
    errors: list[str] = []
    known_character_ids = {
        str(c.get("identity_id") or "")
        for c in ((segment.get("resources") or {}).get("characters") or [])
    }
    allowed_segment_indexes = set(segment.get("source_segment_indexes") or [])
    for index, line in enumerate(segment.get("dialogue") or []):
        speaker_id = str(line.get("speaker_identity_id") or "")
        source_index = line.get("source_segment_index")
        delivery = str(line.get("delivery") or "spoken_dialogue")
        if not speaker_id or speaker_id not in known_character_ids:
            tail = (
                f"是画外音，说话人「{speaker_id}」未列入本段 resources.characters（画外发声也需要注明归属角色）"
                if delivery == "offscreen_voice" else
                f"的说话人「{speaker_id}」在本段原文中没有在场证据（不在本段 resources.characters 内）"
            )
            errors.append(f"[STORYBOARD_PACK_DIALOGUE_SPEAKER_ABSENT] shot_no={shot.shot_no} dialogue[{index}] {tail}")
        try:
            source_index_int = int(source_index)
        except (TypeError, ValueError):
            source_index_int = None
        if source_index_int is None or source_index_int not in allowed_segment_indexes:
            errors.append(
                f"[STORYBOARD_PACK_DIALOGUE_NO_SOURCE] shot_no={shot.shot_no} "
                f"dialogue[{index}] 的 source_segment_index={source_index!r} "
                f"不在本段引用的原文段号 {sorted(allowed_segment_indexes)} 内，无法溯源"
            )
    return errors


def validate_storyboard_pack_dialogue(board: Storyboard) -> list[str]:
    errors: list[str] = []
    for shot in board.shots:
        errors.extend(storyboard_pack_dialogue_errors(shot))
    return errors
