from __future__ import annotations

import pytest

from app import portraits, stages
from app.character_policy import is_collective_role, is_functional_extra
from app.compiler import CompileError, compile_prompt, keyframe_visual_contract
from app.identity_contracts import (
    IdentityContractError,
    canonicalize_storyboard_operational_identities,
    identity_ids_in_authority_text,
    narrative_identity_resolver,
    storyboard_visual_identity_relation,
)
from app.schemas import (
    Bible,
    Character,
    CharacterContinuityState,
    Dialogue,
    EpisodeScreenplay,
    IdentityContractEvidence,
    NarrativeContinuityPlan,
    NarrativeIdentityContract,
    NarrativeProposition,
    PropContinuityState,
    RequiredOnScreenText,
    ScriptScene,
    Shot,
    Storyboard,
    SourceEvidence,
    SourceSpan,
    VoiceCanonical,
    World,
    StoryboardOutlineShot,
)


def _bible() -> Bible:
    return Bible(
        characters=[
            Character(
                name="主角",
                role="protagonist",
                appearance_canonical="黑发青年，深灰长衣，左眉有一道细痕",
            ),
        ],
        world=World(visual_style_canonical="cinematic animation"),
    )


def _evidence(reason: str) -> IdentityContractEvidence:
    return IdentityContractEvidence(
        proposition_ids=["P1"],
        rationale=reason,
    )


def _screenplay() -> EpisodeScreenplay:
    contracts = [
        NarrativeIdentityContract(
            identity_id="newcomer-7",
            display_name="阿烬",
            kind="new recurring dramatic person",
            visual_policy="canonical",
            visual_canonical="银灰短发，暗红披肩，右手戴黑色手套",
            asset_requirement="required",
            voice_ids=["阿烬"],
            evidence=_evidence("语义证据表明该人物会持续回归，需要稳定身份资产"),
        ),
        NarrativeIdentityContract(
            identity_id="transient-node",
            display_name="云吞七号",
            kind="single-scene embodied function",
            visual_policy="contextual",
            visual_canonical="浅褐短袍，素面木簪，面容普通且不抢主体",
            asset_requirement="forbidden",
            evidence=_evidence("只在当前场次执行一次性可见行为，无跨镜资产价值"),
        ),
        NarrativeIdentityContract(
            identity_id="assembly-x",
            display_name="静默议会",
            kind="dramatic collective",
            visual_policy="collective",
            visual_canonical="多个外观各异的灰袍成员，以半环形队列低声交流",
            asset_requirement="optional",
            evidence=_evidence("叙事主体是多个个体组成的集体，不能压缩为单人身份"),
        ),
        NarrativeIdentityContract(
            identity_id="voice-x",
            display_name="井下回声",
            kind="diegetic offscreen speaker",
            visual_policy="offscreen_only",
            visual_canonical="",
            asset_requirement="forbidden",
            voice_ids=["井下回声"],
            evidence=_evidence("来源只授权声音存在，没有任何可见实体证据"),
        ),
    ]
    return EpisodeScreenplay(
        episode_no=1,
        narrative_plan=NarrativeContinuityPlan(
            scope_id="episode-1",
            source_evidence=[
                SourceEvidence(
                    source_evidence_id="S1",
                    source_span=SourceSpan(chapter_id="c1", start=0, end=4),
                    verbatim_excerpt="evidence",
                ),
            ],
            propositions=[
                NarrativeProposition(
                    proposition_id="P1",
                    semantic_identity_key="source-declared-identities",
                    canonical_statement="The declared identities participate in this episode.",
                    narrative_domain="source_canon",
                    entity_ids=[item.identity_id for item in contracts],
                    direct_source_evidence_ids=["S1"],
                ),
            ],
            identity_contracts=contracts,
        ),
        voice_bible=[
            VoiceCanonical(
                speaker_id="阿烬",
                voice_canonical="low, calm voice",
            ),
            VoiceCanonical(
                speaker_id="井下回声",
                voice_canonical="distant resonant voice",
                role_type="offscreen_speaker",
            ),
        ],
    )


def _shot(**overrides) -> Shot:
    payload = {
        "shot_no": 1,
        "duration_s": 5,
        "shot_size": "中景",
        "camera_move": "固定",
        "scene_setting": "地下圆厅",
        "characters": ["云吞七号", "静默议会"],
        "characters_visible": ["云吞七号", "静默议会"],
        "audio_cast": ["井下回声"],
        "action_desc": "云吞七号推开木门，静默议会成员同时转身看向入口。",
        "first_frame_desc": "木门尚未打开，静默议会以半环队列站在厅内。",
        "last_frame_desc": "云吞七号已推开木门，众成员的视线集中到入口。",
        "state_in": "木门关闭，厅内众人背对入口。",
        "primary_action": "云吞七号推门，厅内群体转身。",
        "state_out": "木门打开，厅内群体面向入口。",
        "dialogues": [
            Dialogue(
                speaker="井下回声",
                line="你终于来了。",
                delivery="offscreen_voice",
            ),
        ],
    }
    payload.update(overrides)
    return Shot(**payload)


def test_contract_rationale_uniquely_resolves_contextual_role_alias() -> None:
    screenplay = _screenplay()
    husband = NarrativeIdentityContract(
        identity_id="contextual-husband",
        display_name="路人甲",
        kind="single-scene relationship role",
        visual_policy="contextual",
        visual_canonical="普通中年男性，便装，神情愤怒",
        asset_requirement="optional",
        voice_ids=["路人甲"],
        evidence=_evidence("王芬的丈夫，仅在本场闪回中出现"),
    )
    screenplay.narrative_plan.identity_contracts.append(husband)
    screenplay.narrative_plan.propositions[0].entity_ids.append(
        husband.identity_id
    )
    board = Storyboard(
        episode_no=1,
        shots=[
            _shot(
                characters=["王芬丈夫"],
                characters_visible=["王芬丈夫"],
                audio_cast=["王芬丈夫"],
            )
        ],
    )

    changes = canonicalize_storyboard_operational_identities(
        board,
        _bible(),
        screenplay,
    )

    assert changes
    assert board.shots[0].characters == ["路人甲"]
    assert board.shots[0].characters_visible == ["路人甲"]
    assert board.shots[0].audio_cast == ["路人甲"]


def test_resolver_handles_named_transient_collective_and_offscreen_by_policy() -> None:
    screenplay = _screenplay()
    resolver = narrative_identity_resolver(_bible(), screenplay)

    named = resolver.resolve("newcomer-7", usage="visual")
    transient = resolver.resolve("云吞七号", usage="visual")
    collective = resolver.resolve("静默议会", usage="visual")
    offscreen = resolver.resolve("井下回声", usage="voice")

    assert named.display_name == "阿烬" and named.requires_asset
    assert not transient.allows_asset
    assert collective.is_collective and not collective.requires_asset
    assert offscreen.visual_policy == "offscreen_only"
    with pytest.raises(IdentityContractError, match="只允许画外"):
        resolver.resolve("井下回声", usage="visual")


def test_keyframe_contract_uses_text_verification_for_contextual_identity() -> None:
    contract = keyframe_visual_contract(
        _shot(characters=["云吞七号"], characters_visible=["云吞七号"]),
        _bible(),
        screenplay=_screenplay(),
    )

    verification = contract["identity_verification"]["云吞七号"]
    assert verification["mode"] == "text_contract"
    assert verification["visual_policy"] == "contextual"
    assert verification["visual_canonical"] == (
        "浅褐短袍，素面木簪，面容普通且不抢主体"
    )


@pytest.mark.parametrize(
    "action_desc, required_text, prop",
    [
        pytest.param(
            "卷首墨字逐笔显现：第一章 阿烬。",
            RequiredOnScreenText(
                surface="卷首",
                exact_text="第一章 阿烬",
                strategy="deterministic_insert",
            ),
            None,
            id="title_with_registered_name",
        ),
        pytest.param(
            "木牌上的道具文字写着“阿烬”。",
            RequiredOnScreenText(
                surface="木牌",
                exact_text="阿烬",
                strategy="embedded_prop",
            ),
            PropContinuityState(
                canonical_name="刻字木牌",
                visibility="required",
                text_state="阿烬",
                required=True,
            ),
            id="prop_text_with_registered_name",
        ),
        pytest.param(
            "风掠过名为阿烬的山谷，云层向远处散开。",
            None,
            None,
            id="environment_surface_with_registered_name",
        ),
    ],
)
def test_visual_relation_does_not_infer_identity_from_text_surfaces(
    action_desc,
    required_text,
    prop,
) -> None:
    screenplay = _screenplay()
    shot = _shot(
        characters=["云吞七号"],
        characters_visible=["云吞七号"],
        visible_entity_ids=["transient-node"],
        action_desc=action_desc,
        first_frame_desc="卷轴、木牌或山谷按当前镜头结构建立。",
        last_frame_desc="画面保持当前非人物文字或环境状态。",
        required_text=required_text,
        continuity_state_out={
            "props": {"prop-1": prop}
            if prop is not None else {},
        },
    )
    before = (
        shot.action_desc,
        shot.first_frame_desc,
        shot.last_frame_desc,
    )
    relation = storyboard_visual_identity_relation(
        shot,
        ["transient-node"],
        _bible(),
        screenplay,
    )

    assert relation["unexpected_identity_ids"] == []
    assert (
        shot.action_desc,
        shot.first_frame_desc,
        shot.last_frame_desc,
    ) == before


def test_visual_relation_uses_unique_typed_appearance_without_mutation() -> None:
    screenplay = _screenplay()
    shot = _shot(
        characters=["云吞七号"],
        characters_visible=["云吞七号"],
        visible_entity_ids=["transient-node"],
        action_desc="银灰短发、暗红披肩的身影站在云吞七号身旁。",
        first_frame_desc="云吞七号独自站在门前。",
        last_frame_desc="云吞七号独自完成动作。",
    )
    before = shot.action_desc
    relation = storyboard_visual_identity_relation(
        shot,
        ["transient-node"],
        _bible(),
        screenplay,
    )

    assert relation["unexpected_identity_ids"] == []
    assert shot.action_desc == before


def test_visual_relation_does_not_infer_collective_from_action_prose() -> None:
    screenplay = _screenplay()
    shot = _shot(
        characters=["云吞七号"],
        characters_visible=["云吞七号"],
        visible_entity_ids=["transient-node"],
        action_desc="云吞七号独自关门，门外的灰袍队列已经离开。",
        first_frame_desc="多个外观各异的灰袍成员离开后，厅内只剩云吞七号。",
        last_frame_desc="云吞七号独自站在关闭的木门前。",
    )

    relation = storyboard_visual_identity_relation(
        shot,
        ["transient-node"],
        _bible(),
        screenplay,
    )

    assert relation["unexpected_identity_ids"] == []


def test_visual_relation_allows_previous_tail_identity_only_at_boundary() -> None:
    screenplay = _screenplay()
    shot = _shot(
        characters=["云吞七号"],
        characters_visible=["云吞七号"],
        visible_entity_ids=["transient-node"],
        continuity_mode="same_scene_cut",
        continuity_from_prev=True,
        action_desc="云吞七号独自关上木门。",
        first_frame_desc="阿烬站在门外，云吞七号正准备关门。",
        last_frame_desc="云吞七号独自站在关闭的木门前。",
    )

    boundary_only = storyboard_visual_identity_relation(
        shot,
        ["transient-node"],
        _bible(),
        screenplay,
    )
    assert boundary_only["unexpected_identity_ids"] == []

    shot.last_frame_desc = "木门关闭后，阿烬仍与云吞七号同处画面。"
    persisted = storyboard_visual_identity_relation(
        shot,
        ["transient-node"],
        _bible(),
        screenplay,
    )
    assert persisted["unexpected_identity_ids"] == []


def test_visual_relation_does_not_treat_scene_owner_as_visible_cast() -> None:
    screenplay = _screenplay()
    shot = _shot(
        scene_name="阿烬家卧室",
        scene_setting="深夜，阿烬家卧室",
        characters=["云吞七号"],
        characters_visible=["云吞七号"],
        visible_entity_ids=["transient-node"],
        action_desc="云吞七号独自起身。",
        first_frame_desc="云吞七号躺在阿烬家卧室的床上。",
        last_frame_desc="云吞七号独自站在床边。",
    )

    location_only = storyboard_visual_identity_relation(
        shot,
        ["transient-node"],
        _bible(),
        screenplay,
    )
    assert location_only["unexpected_identity_ids"] == []

    shot.last_frame_desc = "阿烬走进卧室，与云吞七号同处画面。"
    visible_owner = storyboard_visual_identity_relation(
        shot,
        ["transient-node"],
        _bible(),
        screenplay,
    )
    assert visible_owner["unexpected_identity_ids"] == []


def test_visual_relation_uses_structured_character_state_as_visibility_evidence() -> None:
    screenplay = _screenplay()
    shot = _shot(
        characters=["云吞七号"],
        characters_visible=["云吞七号"],
        visible_entity_ids=["transient-node"],
        action_desc="当前人物关系由结构化 continuity state 声明。",
        first_frame_desc="镜头从当前状态开始。",
        last_frame_desc="镜头停在当前状态。",
        continuity_state_out={
            "characters": {
                "newcomer-7": CharacterContinuityState(
                    visible_in_frame=True,
                ),
            },
        },
    )

    relation = storyboard_visual_identity_relation(
        shot,
        ["transient-node"],
        _bible(),
        screenplay,
    )

    assert relation["unexpected_identity_ids"] == ["newcomer-7"]


def test_authority_text_relation_includes_bible_only_identity() -> None:
    screenplay = _screenplay()

    identity_ids = identity_ids_in_authority_text(
        screenplay,
        "主角走到门边看向阿烬，并说「静默议会已经离开」。",
        bible=_bible(),
        strip_dialogue=True,
    )

    assert identity_ids == {"主角", "newcomer-7"}


def test_allowed_longer_display_name_masks_shorter_identity_prefix() -> None:
    screenplay = _screenplay()
    screenplay.narrative_plan.identity_contracts.extend([
        NarrativeIdentityContract(
            identity_id="guard-a",
            display_name="守门人",
            kind="scene participant",
            visual_policy="contextual",
            visual_canonical="青色长衣，黑色腰带，木质腰牌",
            asset_requirement="optional",
            evidence=_evidence("来源分别定义两个同场但不同的守门身份"),
        ),
        NarrativeIdentityContract(
            identity_id="guard-b",
            display_name="另一个守门人",
            kind="scene participant",
            visual_policy="contextual",
            visual_canonical="青色长衣，黑色腰带，木质腰牌",
            asset_requirement="optional",
            evidence=_evidence("来源分别定义两个同场但不同的守门身份"),
        ),
    ])
    shot = _shot(
        characters=["另一个守门人"],
        characters_visible=["另一个守门人"],
        visible_entity_ids=["guard-b"],
        action_desc="另一个守门人独自完成本镜动作。",
        first_frame_desc="另一个守门人站在门前。",
        last_frame_desc="另一个守门人仍留在原位。",
    )

    relation = storyboard_visual_identity_relation(
        shot,
        ["guard-b"],
        _bible(),
        screenplay,
    )

    assert relation["unexpected_identity_ids"] == []


def test_operational_identity_projection_synchronizes_visible_ids() -> None:
    shot = _shot(
        characters=["阿烬"],
        characters_visible=["newcomer-7"],
        visible_entity_ids=["transient-node"],
        action_desc="阿烬独自推开木门。",
        first_frame_desc="阿烬站在关闭的木门前。",
        last_frame_desc="阿烬站在打开的木门旁。",
    )
    board = Storyboard(episode_no=1, shots=[shot])

    changes = canonicalize_storyboard_operational_identities(
        board,
        _bible(),
        _screenplay(),
    )

    assert board.shots[0].characters_visible == ["阿烬"]
    assert board.shots[0].visible_entity_ids == ["newcomer-7"]
    assert any(change["field"] == "visible_entity_ids" for change in changes)


def test_unknown_visible_token_is_not_guessed_and_is_blocked() -> None:
    shot = _shot(
        characters=["阿烬"],
        characters_visible=["opaque-model-token"],
        visible_entity_ids=["transient-node"],
        action_desc="阿烬独自推开木门。",
        first_frame_desc="阿烬站在关闭的木门前。",
        last_frame_desc="阿烬站在打开的木门旁。",
    )
    board = Storyboard(episode_no=1, shots=[shot])
    screenplay = _screenplay()

    canonicalize_storyboard_operational_identities(
        board,
        _bible(),
        screenplay,
    )
    relation = storyboard_visual_identity_relation(
        board.shots[0],
        ["newcomer-7", "transient-node"],
        _bible(),
        screenplay,
    )

    assert board.shots[0].characters_visible == ["opaque-model-token"]
    assert board.shots[0].visible_entity_ids == ["transient-node"]
    assert relation["unresolved_visible_tokens"] == ["opaque-model-token"]
    assert relation["identity_binding_mismatches"]


def test_visual_relation_rejects_swapped_name_id_bindings() -> None:
    screenplay = _screenplay()
    shot = _shot(
        characters=["阿烬", "云吞七号"],
        characters_visible=["阿烬", "云吞七号"],
        visible_entity_ids=["transient-node", "newcomer-7"],
        action_desc="阿烬与云吞七号并肩推开木门。",
        first_frame_desc="阿烬与云吞七号站在关闭的木门前。",
        last_frame_desc="阿烬与云吞七号站在打开的木门旁。",
    )
    allowed_ids = ["newcomer-7", "transient-node"]

    relation = storyboard_visual_identity_relation(
        shot,
        allowed_ids,
        _bible(),
        screenplay,
    )
    issues = stages._storyboard_shot_visual_identity_issues(
        shot,
        StoryboardOutlineShot(
            shot_no=1,
            visible_entity_ids=allowed_ids,
        ),
        _bible(),
        screenplay,
        episode_id="episode-1",
    )

    assert relation["unexpected_identity_ids"] == []
    assert len(relation["identity_binding_mismatches"]) == 2
    assert [issue.code for issue in issues] == [
        "SHOT_VISIBLE_IDENTITY_NOT_GROUNDED",
    ]
    assert issues[0].evidence["identity_binding_mismatches"]


def test_bible_identity_overrides_redundant_model_identity_contract() -> None:
    screenplay = _screenplay()
    screenplay.narrative_plan.identity_contracts.append(
        NarrativeIdentityContract(
            identity_id="character-lead",
            display_name="主角",
            kind="model-declared lead",
            visual_policy="canonical",
            visual_canonical="与人物谱冲突的银发白衣外观",
            asset_requirement="required",
            voice_ids=["主角"],
            evidence=_evidence("模型重复声明了人物谱已有角色"),
        )
    )
    screenplay.voice_bible.append(
        VoiceCanonical(
            speaker_id="主角",
            voice_canonical="young lead voice",
        )
    )

    resolver = narrative_identity_resolver(_bible(), screenplay)
    resolved = resolver.resolve("character-lead", usage="visual")

    assert resolved.asset_name == "主角"
    assert resolved.visual_policy == "canonical"
    assert resolved.visual_canonical == "黑发青年，深灰长衣，左眉有一道细痕"
    assert resolved.asset_requirement == "required"
    assert resolver.resolve("主角", usage="voice").identity_id == "character-lead"


def test_character_resolution_updates_contract_display_name_but_keeps_stable_id() -> None:
    screenplay = _screenplay()

    changes = portraits.apply_screenplay_character_resolutions(screenplay, [{
        "source_label": "云吞七号",
        "canonical_name": "路人甲",
        "resolution": "functional_extra",
    }])

    contract = screenplay.narrative_plan.identity_contracts[1]
    assert changes
    assert contract.identity_id == "transient-node"
    assert contract.display_name == "路人甲"
    resolver = narrative_identity_resolver(_bible(), screenplay)
    assert resolver.resolve("路人甲").identity_id == "transient-node"
    screenplay.scene_outline = [ScriptScene(
        scene_no=1,
        scene_heading="【场1】日 / 地下圆厅",
        story_function="路人甲推门引发议会成员转身",
        characters=["路人甲"],
        summary="路人甲推开木门，厅内成员同时转身看向入口。",
        conflict="来者身份不明，议会保持戒备。",
        turn="木门打开，双方正式照面。",
        source_basis="原文写来者推门进入地下圆厅。",
    )]
    from app.validators import validate_screenplay

    errors = validate_screenplay(
        screenplay,
        _bible(),
        expected_beats=1,
        validate_narrative=False,
    )
    assert not any(
        "叙事权威图外角色" in error and "路人甲" in error
        for error in errors
    )


def test_character_resolution_merges_duplicate_canonical_identity_contracts() -> None:
    screenplay = _screenplay()
    plan = screenplay.narrative_plan
    plan.identity_contracts.extend([
        NarrativeIdentityContract(
            identity_id="主角",
            display_name="主角",
            kind="canonical lead",
            visual_policy="canonical",
            visual_canonical="黑发青年，深灰长衣，左眉有一道细痕",
            asset_requirement="required",
            voice_ids=["主角"],
            evidence=_evidence("角色圣经中的当前身份"),
        ),
        NarrativeIdentityContract(
            identity_id="lead-flashback",
            display_name="年轻主角",
            kind="flashback version",
            visual_policy="contextual",
            visual_canonical="年轻时期的主角",
            asset_requirement="optional",
            evidence=_evidence("同一人物的回忆时期"),
        ),
    ])
    plan.propositions[0].entity_ids.extend(["主角", "lead-flashback"])

    changes = portraits.apply_screenplay_character_resolutions(screenplay, [{
        "source_label": "年轻主角",
        "canonical_name": "主角",
        "resolution": "canonical_identity",
    }])

    matching = [
        contract
        for contract in plan.identity_contracts
        if contract.display_name == "主角"
    ]
    assert len(matching) == 1
    assert matching[0].identity_id == "主角"
    assert "lead-flashback" not in plan.propositions[0].entity_ids
    assert plan.propositions[0].entity_ids.count("主角") == 1
    assert any(
        change.get("kind") == "identity_contract_merge"
        for change in changes
    )


def test_storyboard_operational_identity_projection_uses_contract_and_dialogue() -> None:
    screenplay = _screenplay()
    screenplay.voice_bible.append(VoiceCanonical(
        speaker_id="主角",
        voice_canonical="young lead voice",
    ))
    screenplay.key_lines = ["主角：出发。", "阿烬：请进。"]
    shot = _shot(
        characters=["主角", "云吞七号"],
        characters_visible=["character-main", "transient-node"],
        audio_cast=["character-main"],
        dialogues=[Dialogue(
            speaker="character-main",
            line="出发。",
            delivery="spoken_dialogue",
        )],
    )
    second = _shot(
        shot_no=2,
        characters=["阿烬", "主角"],
        characters_visible=["newcomer-7"],
        audio_cast=["newcomer-7", "character-main"],
        dialogues=[Dialogue(
            speaker="newcomer-7",
            line="请进。",
            delivery="spoken_dialogue",
        )],
    )
    board = Storyboard(episode_no=1, shots=[shot, second])

    changes = canonicalize_storyboard_operational_identities(
        board,
        _bible(),
        screenplay,
    )

    assert changes
    assert board.shots[0].characters_visible == ["主角", "云吞七号"]
    assert board.shots[0].audio_cast == ["主角"]
    assert board.shots[0].dialogues[0].speaker == "主角"
    assert board.shots[1].audio_cast == ["阿烬", "主角"]


def test_storyboard_candidate_projects_operational_identity_before_validation() -> None:
    screenplay = _screenplay()
    screenplay.voice_bible.append(VoiceCanonical(
        speaker_id="主角",
        voice_canonical="young lead voice",
    ))
    screenplay.key_lines = ["主角：出发。"]
    shot = _shot(
        characters=["主角"],
        characters_visible=["character-main"],
        audio_cast=["character-main"],
        dialogues=[Dialogue(
            speaker="character-main",
            line="出发。",
            delivery="spoken_dialogue",
        )],
    )

    board, errors = stages._normalized_candidate_board(
        1,
        [],
        shot,
        _bible(),
        5,
        narrative_authority=True,
        narrative_plan=screenplay.narrative_plan,
        screenplay=screenplay,
    )

    assert errors == []
    assert board.shots[0].characters_visible == ["主角"]
    assert board.shots[0].audio_cast == ["主角"]
    assert board.shots[0].dialogues[0].speaker == "主角"


def test_narrative_compiler_uses_typed_policy_not_role_name_classifiers() -> None:
    screenplay = _screenplay()
    shot = _shot()

    assert not is_functional_extra("云吞七号")
    assert not is_collective_role("静默议会")

    prompt = compile_prompt(
        shot,
        _bible(),
        screenplay=screenplay,
        voice_bible=screenplay.voice_bible,
    )
    contract = keyframe_visual_contract(
        shot, _bible(), screenplay=screenplay,
    )

    assert "浅褐短袍，素面木簪" in prompt
    assert "多个外观各异的灰袍成员" in prompt
    assert "collective_group:静默议会" in prompt
    assert "character_identity:云吞七号" in prompt
    assert "井下回声" in prompt
    assert contract["collective_visible_roles"] == ["静默议会"]
    assert contract["individual_visible_characters"] == ["云吞七号"]


def test_narrative_compiler_fails_closed_for_undeclared_identity() -> None:
    screenplay = _screenplay()
    shot = _shot(
        characters=["医生"],
        characters_visible=["医生"],
        audio_cast=[],
        dialogues=[],
        action_desc="医生走到门前伸手推开木门。",
    )

    assert not is_functional_extra("医生")
    with pytest.raises(CompileError, match="未在 Bible 或 narrative identity contract 中声明"):
        compile_prompt(shot, _bible(), screenplay=screenplay)


def test_narrative_compiler_does_not_treat_ambient_source_as_voice_identity() -> None:
    screenplay = _screenplay()
    shot = _shot(
        audio_cast=["untyped-metallic-impact"],
        dialogues=[],
        audio_timeline=[{
            "start_s": 0.0,
            "end_s": 5.0,
            "type": "ambient_sound",
            "speaker_id": None,
            "text": "A metallic impact echoes through the room.",
            "lip_sync": False,
        }],
    )

    prompt = compile_prompt(
        shot,
        _bible(),
        screenplay=screenplay,
        voice_bible=screenplay.voice_bible,
    )

    assert "A metallic impact echoes through the room." in prompt


def test_narrative_compiler_still_rejects_undeclared_spoken_identity() -> None:
    screenplay = _screenplay()
    shot = _shot(
        audio_cast=["undeclared-speaker"],
        dialogues=[Dialogue(
            speaker="undeclared-speaker",
            line="This line is audibly spoken.",
            delivery="offscreen_voice",
        )],
    )

    with pytest.raises(CompileError, match="声音身份"):
        compile_prompt(
            shot,
            _bible(),
            screenplay=screenplay,
            voice_bible=screenplay.voice_bible,
        )


def test_offscreen_contract_cannot_be_misused_as_visible_keyframe_subject() -> None:
    screenplay = _screenplay()
    shot = _shot(
        characters=["井下回声"],
        characters_visible=["井下回声"],
        audio_cast=["井下回声"],
    )

    with pytest.raises(CompileError, match="只允许画外"):
        keyframe_visual_contract(shot, _bible(), screenplay=screenplay)
