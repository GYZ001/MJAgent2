from __future__ import annotations

import pytest

from app.character_policy import is_collective_role, is_functional_extra
from app.compiler import CompileError, compile_prompt, keyframe_visual_contract
from app.identity_contracts import IdentityContractError, narrative_identity_resolver
from app.schemas import (
    Bible,
    Character,
    Dialogue,
    EpisodeScreenplay,
    IdentityContractEvidence,
    NarrativeContinuityPlan,
    NarrativeIdentityContract,
    NarrativeProposition,
    Shot,
    SourceEvidence,
    SourceSpan,
    VoiceCanonical,
    World,
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


def test_narrative_compiler_fails_closed_for_undeclared_legacy_whitelist_role() -> None:
    screenplay = _screenplay()
    shot = _shot(
        characters=["医生"],
        characters_visible=["医生"],
        audio_cast=[],
        dialogues=[],
        action_desc="医生走到门前伸手推开木门。",
    )

    assert is_functional_extra("医生")
    with pytest.raises(CompileError, match="未在 Bible 或 narrative identity contract 中声明"):
        compile_prompt(shot, _bible(), screenplay=screenplay)


def test_offscreen_contract_cannot_be_misused_as_visible_keyframe_subject() -> None:
    screenplay = _screenplay()
    shot = _shot(
        characters=["井下回声"],
        characters_visible=["井下回声"],
        audio_cast=["井下回声"],
    )

    with pytest.raises(CompileError, match="只允许画外"):
        keyframe_visual_contract(shot, _bible(), screenplay=screenplay)

