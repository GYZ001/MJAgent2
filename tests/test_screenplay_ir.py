from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import db, stages
from app import errors as app_errors
from app import identity_adjudication
from app.evidence import repository as evidence_repository
from app.harness.types import EvidenceArtifact
from app.identity_adjudication import adjudicate_screenplay_ir_identities
from app.identity_authority import normalize_character_resolution
from app.identity_contracts import narrative_identity_resolver
from app.narrative import validate_screenplay_narrative
from app.observability.tracing import bind_trace
from app.production.screenplay_document import (
    document_to_screenplay,
    screenplay_to_document,
)
from app.schemas import (
    ActionAgency,
    AtomicAction,
    Bible,
    Character,
    EpisodeScreenplay,
    Scene,
    World,
)
from app.screenplay_ir import (
    IREvent,
    ScreenplayIRIdentityConflictError,
    ScreenplayGenerationIR,
    _apply_authoritative_ir_identity_resolutions,
    _normalize_duplicate_ir_identity_displays,
    compile_screenplay_ir,
    normalize_screenplay_ir_payload,
    prepare_ir_identity_authorities,
    recover_complete_screenplay_ir_prefix,
    scene_heading_has_multiple_locations,
    screenplay_beat_fields_repeat,
    screenplay_ir_bible_context,
    screenplay_ir_prompt_contract,
)
from app.source_excerpt import (
    index_compact_source_segments,
    index_source_segments,
    structural_front_matter_ids,
)
from app.validators import validate_screenplay


SOURCE = "\n\n".join([
    "谷言独自在咖啡厅等待旧友。他看向门口说：“再等十分钟。”",
    "旧友推门出现，把钥匙递给谷言说：“拿好这把钥匙。”",
    "门外响起更重的敲门声，旧友立刻说：“别开门。”危险已经逼近。",
])
SS001_ARTIFACT_FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "screenplay_scene_shard_ss001_art_bcebe2075a55.json"
)
MERGED_IR_ARTIFACT_FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "screenplay_generation_ir_merged_art_949de359c598.json"
)


def _bible() -> Bible:
    return Bible(
        characters=[
            Character(
                name="谷言",
                role="主角",
                appearance_canonical="二十八岁男性，黑色短发，深灰外套，神情克制",
                personality="冷静谨慎",
                speech_style="短句直接，语气克制",
            ),
            Character(
                name="未出场人物",
                role="其他章节角色",
                appearance_canonical="成年女性，长发，浅色外套",
                personality="沉稳",
                speech_style="语速平稳",
            ),
        ],
        world=World(
            era="现代",
            genre="悬疑",
            visual_style_canonical="高精度3D动漫CG，电影感光影",
        ),
    )


def _ir_payload() -> dict:
    return {
        "format_version": "screenplay-generation-ir.v1.1",
        "episode_no": 1,
        "metadata": {
            "title": "雨夜敲门",
            "logline": "谷言等来旧友并接过一把会引来危险的钥匙。",
            "script_format_note": "场次化台本稿，含场标、动作段与对白段",
            "dramatic_question": "谷言能否接住旧友交来的危险线索？",
            "protagonist_goal": "弄清旧友来意并保住钥匙",
            "obstacle": "旧友来不及解释，追踪者已经来到门外",
            "stakes": "处理错误会让谷言和旧友同时暴露",
            "emotional_curve": "从等待不安到线索出现，最终危险逼近",
            "ending_hook": "门外再次响起更重的敲门声。",
            "source_basis": "保留等待、递钥匙与危险逼近的完整因果链",
            "adaptation_direction": "压缩过渡但完整保留动作与问答",
            "opening": "谷言等待失约的旧友",
            "development": "旧友现身并交出钥匙",
            "conflict": "钥匙刚完成交付，追踪者已经来到门外",
            "climax": "敲门声迫使两人停止交谈并警戒",
            "episode_premise": "谷言必须判断是否接下旧友带来的危险线索",
            "must_keep_ending": "门外敲门，危险逼近",
            "drop_list": [],
            "approved_adaptations": [],
            "forbidden_additions": ["禁止发明门外人物身份"],
        },
        "identities": [
            {
                "key": "g",
                "display_name": "谷言",
                "kind": "bible_character",
                "visual_policy": "canonical",
                "visual_canonical": "二十八岁男性，黑色短发，深灰外套",
                "asset_requirement": "required",
                "voice_canonical": "短句克制",
                "role_type": "named_character",
                "rationale": "本集主角且由角色圣经登记",
            },
            {
                "key": "friend",
                "display_name": "旧友",
                "kind": "source_backed_contextual_character",
                "visual_policy": "contextual",
                "visual_canonical": "成年男性，外套带血迹，神色慌张",
                "asset_requirement": "optional",
                "voice_canonical": "气息急促，压低声音",
                "role_type": "functional_character",
                "rationale": "来源中负责交付钥匙的一次性可见身份",
            },
        ],
        "beats": [
            {
                "key": "b1",
                "who": "谷言",
                "does": "独自在咖啡厅等待旧友",
                "turn": "等待升级为不安",
                "purpose": "建立主角处境",
                "source_segment_ids": ["SRC0001"],
            },
            {
                "key": "b2",
                "who": "旧友",
                "does": "推门出现并递出钥匙",
                "turn": "谷言接下危险线索",
                "purpose": "完成核心交付",
                "source_segment_ids": ["SRC0002"],
            },
            {
                "key": "b3",
                "who": "谷言与旧友",
                "does": "听见敲门声后转向门口",
                "turn": "危险已经逼近",
                "purpose": "完成本集威胁收束",
                "source_segment_ids": ["SRC0003"],
            },
        ],
        "coverage": [
            {
                "source_segment_ids": ["SRC0001"],
                "disposition": "deliver",
                "beat_keys": ["b1"],
            },
            {
                "source_segment_ids": ["SRC0002"],
                "disposition": "deliver",
                "beat_keys": ["b2"],
            },
            {
                "source_segment_ids": ["SRC0003"],
                "disposition": "deliver",
                "beat_keys": ["b3"],
            },
        ],
        "scenes": [
            {
                "key": "sc1",
                "scene_heading": "【场1】夜 / 咖啡厅里侧",
                "story_function": "建立谷言等待旧友的处境与不安",
                "character_keys": ["g"],
                "summary": "谷言独自在咖啡厅等待旧友，等待逐渐变成不安。",
                "conflict": "旧友迟迟未到，谷言不知道是否继续等待",
                "turn": "谷言决定最后再等十分钟",
                "source_basis": "保留SRC0001中的等待处境与决定",
                "entry_state": "谷言独自在座位等待",
                "exit_state": "谷言的不安明显升级并作出决定",
                "context_requirements": ["夜晚咖啡厅", "谷言正在等待旧友"],
                "units": [
                    {
                        "kind": "action",
                        "text": (
                            "谷言独自在咖啡厅等待旧友，不时看向门口，"
                            "始终没有等到第二个人出现。"
                        ),
                        "event_key": "e1",
                    },
                    {
                        "kind": "dialogue",
                        "text": "再等十分钟。",
                        "event_key": "e1",
                        "speaker_key": "g",
                        "function": "decision",
                        "source_text": "再等十分钟。",
                        "chain_key": "wait",
                    },
                ],
            },
            {
                "key": "sc2",
                "scene_heading": "【场2】夜 / 咖啡厅门口",
                "story_function": "让旧友现身并完成钥匙交付",
                "character_keys": ["g", "friend"],
                "summary": "旧友推门出现，把一把钥匙递到谷言手中。",
                "conflict": "旧友拒绝解释，谷言无法判断钥匙意味着什么",
                "turn": "谷言接过钥匙，正式卷入事件",
                "source_basis": "保留SRC0002中的现身与递钥匙动作",
                "entry_state": "谷言仍在等待，手中没有钥匙",
                "exit_state": "谷言手中已经多了一把钥匙",
                "context_requirements": ["门口与座位的空间关系", "旧友刚刚赶到"],
                "units": [
                    {
                        "kind": "action",
                        "text": (
                            "旧友匆忙推门出现并把钥匙递到谷言面前，"
                            "谷言看清钥匙后伸手接住。"
                        ),
                        "event_key": "e2",
                    },
                    {
                        "kind": "dialogue",
                        "text": "拿好这把钥匙。",
                        "event_key": "e2",
                        "speaker_key": "friend",
                        "function": "announcement",
                        "source_text": "拿好这把钥匙。",
                        "chain_key": "key",
                    },
                ],
            },
            {
                "key": "sc3",
                "scene_heading": "【场3】夜 / 咖啡厅座位",
                "story_function": "用敲门声把外部危险推进现场",
                "character_keys": ["g", "friend"],
                "summary": "谷言刚接住钥匙，门外就响起更重的敲门声。",
                "conflict": "两人不知道门外是谁，也不能贸然开门",
                "turn": "两人确认危险已经来到门口",
                "source_basis": "保留SRC0003中的敲门声与危险逼近",
                "entry_state": "谷言拿着钥匙，旧友仍在他面前",
                "exit_state": "两人停止交谈并同时转向门口警戒",
                "context_requirements": ["钥匙仍在谷言手中", "门位于两人视线前方"],
                "units": [
                    {
                        "kind": "action",
                        "text": "门外响起更重的敲门声，谷言与旧友同时转向门口。",
                        "event_key": "e3",
                    },
                    {
                        "kind": "dialogue",
                        "text": "别开门。",
                        "event_key": "e3",
                        "speaker_key": "friend",
                        "function": "warning",
                        "source_text": "别开门。",
                        "chain_key": "key",
                    },
                ],
            },
        ],
        "events": [
            {
                "key": "e1",
                "scene_key": "sc1",
                "source_segment_ids": ["SRC0001"],
                "source_excerpt": "谷言独自在咖啡厅等待旧友。",
                "source_statement": "谷言在咖啡厅等待旧友",
                "adapted_statement": "谷言独自在咖啡厅等待失约的旧友",
                "adaptation_reason": "保留等待处境并压缩环境描述",
                "actor_keys": ["g"],
                "causal_parent_keys": [],
                "precondition_state": "谷言尚未见到旧友",
                "resulting_state": "谷言等待升级为不安",
                "action_intent": "继续等待旧友",
                "completion_condition": "谷言明确决定再等十分钟",
                "action_phases": [{
                    "start_condition": "谷言独坐并看向门口",
                    "end_condition": "谷言说出最后等待时间",
                    "estimated_min_s": 1.0,
                }],
                "observable_claim": "谷言独坐并反复望向门口",
                "perceivable_by": ["g", "audience"],
                "character_goal": "等到旧友",
                "character_stakes": "继续等待可能让自己卷入风险",
                "character_emotion": "不安",
                "character_tactic": "给出最后等待期限",
                "information": ["谷言正在等待失约的旧友"],
            },
            {
                "key": "e2",
                "scene_key": "sc2",
                "source_segment_ids": ["SRC0002"],
                "source_excerpt": "旧友推门出现，把钥匙递给谷言",
                "source_statement": "旧友出现并把钥匙递给谷言",
                "adapted_statement": "旧友把危险线索交到谷言手中",
                "adaptation_reason": "保留递钥匙动作的因果作用",
                "actor_keys": ["friend"],
                "target_keys": ["g"],
                "causal_parent_keys": ["e1"],
                "precondition_state": "谷言仍在等待且手中没有钥匙",
                "resulting_state": "谷言已经拿到旧友交来的钥匙",
                "action_intent": "把钥匙交给谷言",
                "completion_condition": "钥匙稳定落入谷言手中",
                "action_phases": [{
                    "start_condition": "旧友带着钥匙进门",
                    "end_condition": "谷言伸手接住钥匙",
                    "estimated_min_s": 2.0,
                }],
                "observable_claim": "旧友推门并把钥匙递到谷言手中",
                "perceivable_by": ["g", "friend", "audience"],
                "character_goal": "尽快交出线索",
                "character_stakes": "追踪者可能在交付完成前赶到",
                "character_emotion": "惊慌",
                "character_tactic": "跳过解释直接交付",
                "information": ["旧友把一把关键钥匙交给谷言"],
            },
            {
                "key": "e3",
                "scene_key": "sc3",
                "source_segment_ids": ["SRC0003"],
                "source_excerpt": "门外响起更重的敲门声",
                "source_statement": "门外敲门意味着危险逼近",
                "adapted_statement": "敲门声把外部危险推进咖啡厅",
                "adaptation_reason": "保留原文结尾的现场威胁",
                "actor_keys": ["g"],
                "target_keys": ["friend"],
                "causal_parent_keys": ["e2"],
                "precondition_state": "谷言已经拿到钥匙",
                "resulting_state": "谷言与旧友确认危险已到门外",
                "action_intent": "转向门口警戒",
                "completion_condition": "两人停止交谈并面向门口",
                "action_phases": [{
                    "start_condition": "敲门声突然响起",
                    "end_condition": "两人同时转向门口",
                    "estimated_min_s": 1.0,
                }],
                "observable_claim": "敲门声响起，两人同时转向门口",
                "perceivable_by": ["g", "friend", "audience"],
                "character_goal": "判断门外威胁",
                "character_stakes": "开门可能暴露钥匙与两人的位置",
                "character_emotion": "警觉",
                "character_tactic": "停止交谈并保持安静",
                "information": ["危险已经逼近咖啡厅门口"],
            },
        ],
        "audience_priors": [
            {"key": "cold", "description": "不了解人物关系的冷观众"},
            {
                "key": "aware",
                "description": "知道谷言在等人、但不知道钥匙用途的观众",
                "target_stance": "suspected",
                "target_confidence": 0.65,
            },
        ],
        "experience": {
            "director_objective": "让观众理解钥匙交付直接导致危险逼近",
            "satisfaction_criteria": "观众能复述等待、递钥匙、敲门三步因果",
            "required_processing_s": 1.0,
        },
    }


def _compile() -> EpisodeScreenplay:
    ir = ScreenplayGenerationIR.model_validate(_ir_payload())
    return compile_screenplay_ir(
        ir,
        episode={
            "id": "ep-ir-1",
            "episode_no": 1,
            "title": "雨夜敲门",
            "authorized_source_chapters": {"chapter-1": SOURCE},
        },
        source_text=SOURCE,
        bible=_bible(),
    )


def test_compact_ir_compiles_to_existing_screenplay_contract() -> None:
    screenplay = _compile()

    assert set(type(screenplay).model_fields) == set(EpisodeScreenplay.model_fields)
    assert screenplay.narrative_plan is not None
    assert [item.event_id for item in screenplay.events] == ["E1", "E2", "E3"]
    assert [item.event_id for item in screenplay.narrative_plan.events] == [
        "E1", "E2", "E3",
    ]
    assert [item.scene_id for item in screenplay.narrative_plan.scene_contracts] == [
        "SC01", "SC02", "SC03",
    ]
    assert [item.source_segment_id for item in screenplay.source_coverage] == [
        item.segment_id for item in index_source_segments(SOURCE)
    ]
    assert "谷言：再等十分钟。" in screenplay.full_script_text
    assert screenplay.key_lines == [
        "谷言：再等十分钟。",
        "旧友：拿好这把钥匙。",
        "旧友：别开门。",
    ]


def test_event_onscreen_only_identity_is_included_in_compiler_registry() -> None:
    payload = _ir_payload()
    payload["identities"].append({
        "key": "observer",
        "display_name": "门边观察者",
        "kind": "source_backed_contextual_character",
        "visual_policy": "contextual",
        "visual_canonical": "站在门边、外观保持本场连续的观察者",
        "asset_requirement": "optional",
        "role_type": "functional_character",
        "rationale": "来源事件明确要求该身份在画面中",
    })
    payload["events"][0]["onscreen_entity_keys"] = ["observer"]

    screenplay = compile_screenplay_ir(
        ScreenplayGenerationIR.model_validate(payload),
        episode={
            "id": "ep-ir-onscreen-only",
            "episode_no": 1,
            "authorized_source_chapters": {"chapter-1": SOURCE},
        },
        source_text=SOURCE,
        bible=_bible(),
    )

    contract = next(
        item for item in screenplay.narrative_plan.identity_contracts
        if item.display_name == "门边观察者"
    )
    assert contract.visual_policy == "contextual"
    assert contract.visual_canonical


def test_compiled_ir_passes_narrative_and_screenplay_gates() -> None:
    screenplay = _compile()

    narrative_errors = validate_screenplay_narrative(
        screenplay,
        require=True,
        expected_scope_id="ep-ir-1",
        authorized_source_chapters={"chapter-1": SOURCE},
    )
    screenplay_errors = validate_screenplay(
        screenplay,
        _bible(),
        expected_beats=3,
        episode_no=1,
        source_text=SOURCE,
        require_dialogue_chains=True,
        validate_narrative=False,
        require_source_coverage=True,
    )

    assert narrative_errors == []
    assert screenplay_errors == []


def test_coverage_is_expanded_from_beat_source_ownership() -> None:
    payload = _ir_payload()
    payload["coverage"] = []
    screenplay = compile_screenplay_ir(
        ScreenplayGenerationIR.model_validate(payload),
        episode={
            "id": "ep-ir-coverage",
            "episode_no": 1,
            "authorized_source_chapters": {"chapter-1": SOURCE},
        },
        source_text=SOURCE,
        bible=_bible(),
    )

    assert [
        (item.source_segment_id, item.disposition, item.beat_ids)
        for item in screenplay.source_coverage
    ] == [
        ("SRC0001", "deliver", ["S01"]),
        ("SRC0002", "deliver", ["S02"]),
        ("SRC0003", "deliver", ["S03"]),
    ]


def test_real_provider_ir_shape_drift_is_normalized_locally() -> None:
    payload = _ir_payload()
    payload["coverage"] = [{
        "segment_ids": ["SRC0001"],
        "coverage_type": "context",
        "context_note": "作为人物与环境背景保留",
    }]
    payload["events"][0]["information"] = "谷言正在等待失约的旧友"
    payload["events"][0].pop("source_excerpt")
    payload["audience_priors"][0]["familiarity_assumptions"] = [
        "知道故事发生在现代城市",
    ]

    ir = ScreenplayGenerationIR.model_validate(payload)

    assert ir.coverage[0].source_segment_ids == ["SRC0001"]
    assert ir.coverage[0].disposition == "context"
    assert ir.coverage[0].reason == "作为人物与环境背景保留"
    assert ir.events[0].information == ["谷言正在等待失约的旧友"]
    assert ir.events[0].source_excerpt == ""
    assert ir.audience_priors[0].familiarity_assumptions == [{
        "description": "知道故事发生在现代城市",
    }]


def test_open_audience_stance_is_audited_and_normalized() -> None:
    payload = _ir_payload()
    payload["audience_priors"][1]["target_stance"] = "neutral"

    normalized, changes = normalize_screenplay_ir_payload(payload)
    ir = ScreenplayGenerationIR.model_validate(normalized)

    assert ir.audience_priors[1].target_stance == "suspected"
    assert any(
        item["path"] == "audience_priors[1].target_stance"
        and item["from"] == "neutral"
        and item["to"] == "suspected"
        for item in changes
    )


def test_v15_unit_relations_do_not_turn_a_mentioned_absent_identity_into_actor() -> None:
    payload = _ir_payload()
    payload["format_version"] = "screenplay-generation-ir.v1.5"
    relation_rows = (
        (("g",), (), ("g",)),
        ((), (), ("g",)),
        (("friend",), ("g",), ("g", "friend")),
        ((), (), ("g", "friend")),
        (("g", "friend"), (), ("g", "friend")),
        ((), (), ("g", "friend")),
    )
    units = [
        unit
        for scene in payload["scenes"]
        for unit in scene["units"]
    ]
    for index, (unit, (actors, targets, onscreen)) in enumerate(zip(
        units, relation_rows, strict=True,
    )):
        unit["source_segment_ids"] = [f"SRC{index // 2 + 1:04d}"]
        unit["actor_keys"] = list(actors)
        unit["target_keys"] = list(targets)
        unit["onscreen_entity_keys"] = list(onscreen)

    screenplay = compile_screenplay_ir(
        ScreenplayGenerationIR.model_validate(payload),
        episode={
            "id": "ep-ir-v15-relations",
            "episode_no": 1,
            "authorized_source_chapters": {"chapter-1": SOURCE},
        },
        source_text=SOURCE,
        bible=_bible(),
    )

    first_action = screenplay.narrative_plan.atomic_actions[0]
    first_event = screenplay.narrative_plan.events[0]
    assert first_action.actor_ids == ["谷言"]
    assert "旧友" not in first_action.actor_ids
    assert first_event.onscreen_entity_ids == ["谷言"]


def test_v2_ss001_title_action_preserves_empty_identity_relations() -> None:
    replay = json.loads(SS001_ARTIFACT_FIXTURE.read_text(encoding="utf-8"))
    payload = _ir_payload()
    payload["format_version"] = "screenplay-generation-ir.v2"
    units = [
        unit
        for scene in payload["scenes"]
        for unit in scene["units"]
    ]
    title = replay["units"]["title"]
    units[0].update({
        "unit_key": title["unit_key"],
        "text": title["text"],
        "event_key": "ss001-title-event",
    })
    relation_rows = (
        ((), (), (), []),
        (("g",), (), ("g",), []),
        (("friend",), ("g",), ("g", "friend"), []),
        (("friend",), (), ("g", "friend"), []),
        ((), (), (), []),
        (
            ("friend",),
            (),
            ("g",),
            [{
                "participant_key": "friend",
                "observable_claim": "旧友的画外警告清晰可听。",
                "audible": True,
            }],
        ),
    )
    for index, (unit, (actors, targets, onscreen, deliveries)) in enumerate(
        zip(units, relation_rows, strict=True)
    ):
        unit["source_segment_ids"] = [f"SRC{index // 2 + 1:04d}"]
        unit["actor_keys"] = list(actors)
        unit["target_keys"] = list(targets)
        unit["onscreen_entity_keys"] = list(onscreen)
        unit["participant_deliveries"] = deliveries
        unit["narrative_layer"] = "story"
        unit["event_priority"] = "causal"
        unit["render_policy"] = "standalone"
    for event in payload["events"]:
        event["participant_deliveries"] = []
        event["narrative_layer"] = "story"
        event["event_priority"] = "causal"
        event["render_policy"] = "standalone"

    candidate = ScreenplayGenerationIR.model_validate(payload)
    screenplay = compile_screenplay_ir(
        candidate,
        episode={
            "id": replay["episode_id"],
            "episode_no": replay["episode_no"],
            "authorized_source_chapters": {"chapter-1": SOURCE},
        },
        source_text=SOURCE,
        bible=_bible(),
    )

    title_event = screenplay.narrative_plan.events[0]
    title_action = screenplay.narrative_plan.atomic_actions[0]
    assert replay["artifact_id"] == "art_bcebe2075a55"
    assert replay["artifact_content_hash"] == (
        "19c41c704b3524969a0169c66da1e7a829aa2eaba023245ba9eb983fe23fc2f8"
    )
    assert title_event.onscreen_entity_ids == []
    assert title_action.actor_ids == []
    assert title_action.target_ids == []
    assert not any(
        identity.identity_id.startswith("context:")
        for identity in screenplay.narrative_plan.identity_contracts
    )
    assert title_action.action_agency.identity_bearing is False
    assert title_action.action_agency.source_segment_ids == ["SRC0001"]

    offscreen_action = screenplay.narrative_plan.atomic_actions[-1]
    offscreen_event = screenplay.narrative_plan.events[-1]
    assert offscreen_action.actor_ids
    assert offscreen_event.onscreen_entity_ids == ["谷言"]
    assert offscreen_action.participant_deliveries[0].audible is True


def test_atomic_action_missing_agency_derives_from_owned_relations() -> None:
    unattributed = AtomicAction.model_validate({
        "action_id": "A-environment",
        "actor_ids": [],
        "target_ids": [],
        "semantic_intent": "环境状态发生变化",
        "completion_condition": "变化已可见",
    })
    attributed = AtomicAction.model_validate({
        "action_id": "A-character",
        "actor_ids": ["character-1"],
        "target_ids": ["character-2"],
        "semantic_intent": "人物改变目标状态",
        "completion_condition": "目标状态已改变",
    })

    assert unattributed.action_agency.kind == "unattributed"
    assert unattributed.action_agency.identity_bearing is False
    assert attributed.action_agency.kind == "character"
    assert attributed.action_agency.identity_bearing is True


def test_character_action_agency_requires_identity_bearing_relation() -> None:
    with pytest.raises(
        ValueError,
        match="character.*identity_bearing",
    ):
        ActionAgency(
            kind="character",
            identity_bearing=False,
            source_segment_ids=["SRC0056"],
        )


def test_production_merged_ir_missing_agency_round_trip_is_relation_owned() -> None:
    fixture = json.loads(MERGED_IR_ARTIFACT_FIXTURE.read_text(encoding="utf-8"))
    raw_units = [
        unit
        for scene in fixture["content"]["scenes"]
        for unit in scene["units"]
    ]

    assert fixture["artifact_id"] == "art_949de359c598"
    assert fixture["artifact_type"] == "screenplay_generation_ir_merged"
    assert fixture["artifact_status"] == "validated"
    assert fixture["content_hash"] == (
        "0c375efd1d89780b67a6480d6e7b3c27db6930735313592821430ff9816dc152"
    )
    assert len(raw_units) == 86
    assert all("action_agency" not in unit for unit in raw_units)

    restored = ScreenplayGenerationIR.model_validate(fixture["content"])
    serialized = restored.model_dump(mode="json")
    restored_units = [
        unit
        for scene in restored.scenes
        for unit in scene.units
    ]
    round_trip_units = [
        unit
        for scene in serialized["scenes"]
        for unit in scene["units"]
    ]
    unattributed = [
        unit
        for unit in restored_units
        if not unit.actor_keys and not unit.target_keys and not unit.speaker_key
    ]
    attributed = [
        unit
        for unit in restored_units
        if unit.actor_keys or unit.target_keys or unit.speaker_key
    ]

    assert len(unattributed) == 12
    assert all(unit.action_agency.kind == "unattributed" for unit in unattributed)
    assert all(unit.action_agency.identity_bearing is False for unit in unattributed)
    assert all(
        unit.action_agency.source_segment_ids == unit.source_segment_ids
        for unit in unattributed
    )
    assert all(not unit.actor_keys and not unit.target_keys for unit in unattributed)
    assert len(attributed) == 74
    assert all(unit.action_agency.identity_bearing is True for unit in attributed)
    assert [
        (
            unit.actor_keys,
            unit.target_keys,
            unit.speaker_key,
            unit.source_segment_ids,
        )
        for unit in restored_units
    ] == [
        (
            unit["actor_keys"],
            unit["target_keys"],
            unit.get("speaker_key"),
            unit["source_segment_ids"],
        )
        for unit in raw_units
    ]
    assert all("action_agency" in unit for unit in round_trip_units)
    assert ScreenplayGenerationIR.model_validate(serialized) == restored


def test_compiler_derives_removed_model_fields_without_downstream_drift() -> None:
    payload = _ir_payload()
    payload["beats"] = []
    payload["audience_priors"] = []
    for event in payload["events"]:
        for field in (
            "source_excerpt", "source_statement", "information",
            "perceivable_by", "character_goal", "character_stakes",
            "salience", "irreversibility", "readability_s",
            "precondition_state", "resulting_state", "action_intent",
            "completion_condition", "action_phases", "observable_claim",
            "causal_parent_keys", "character_tactic",
        ):
            event.pop(field, None)
    for scene in payload["scenes"]:
        scene["character_keys"] = []
        scene["source_basis"] = ""
        scene["entry_state"] = ""
        scene["exit_state"] = ""
        scene["context_requirements"] = []

    screenplay = compile_screenplay_ir(
        ScreenplayGenerationIR.model_validate(payload),
        episode={
            "id": "ep-ir-derived",
            "episode_no": 1,
            "authorized_source_chapters": {"chapter-1": SOURCE},
        },
        source_text=SOURCE,
        bible=_bible(),
    )

    assert len(screenplay.plot_spine.spine_beats) == 3
    assert len(screenplay.source_coverage) == 3
    assert len(screenplay.narrative_plan.audience_priors) == 2
    assert all(scene.source_basis for scene in screenplay.scene_outline)
    assert all(scene.entry_state and scene.exit_state for scene in screenplay.scene_outline)
    assert all(
        action.temporal_phases
        for action in screenplay.narrative_plan.atomic_actions
    )
    assert validate_screenplay_narrative(
        screenplay,
        require=True,
        source_text=SOURCE,
        expected_scope_id="ep-ir-derived",
        authorized_source_chapters={"chapter-1": SOURCE},
    ) == []


def test_v12_compiler_uses_compact_source_ids_with_exact_event_evidence() -> None:
    payload = _ir_payload()
    payload["format_version"] = "screenplay-generation-ir.v1.2"
    for beat in payload["beats"]:
        beat["source_segment_ids"] = ["SRC0001"]
    for event in payload["events"]:
        event["source_segment_ids"] = ["SRC0001"]
    payload["coverage"] = [{
        "source_segment_ids": ["SRC0001"],
        "disposition": "deliver",
        "beat_keys": ["b1", "b2", "b3"],
    }]

    screenplay = compile_screenplay_ir(
        ScreenplayGenerationIR.model_validate(payload),
        episode={
            "id": "ep-ir-v12",
            "episode_no": 1,
            "authorized_source_chapters": {"chapter-1": SOURCE},
        },
        source_text=SOURCE,
        bible=_bible(),
    )

    assert screenplay.source_text_range == "screenplay-generation-ir.v1.2"
    assert len(screenplay.source_coverage) == 1
    assert len(screenplay.narrative_plan.source_evidence) == 3
    assert all(
        evidence.verbatim_excerpt in SOURCE
        for evidence in screenplay.narrative_plan.source_evidence
    )


def test_v12_compiler_derives_events_from_authored_scene_units() -> None:
    payload = _ir_payload()
    payload["format_version"] = "screenplay-generation-ir.v1.2"
    payload["events"] = []
    payload["beats"] = []
    payload["coverage"] = []

    screenplay = compile_screenplay_ir(
        ScreenplayGenerationIR.model_validate(payload),
        episode={
            "id": "ep-ir-v12-units",
            "episode_no": 1,
            "authorized_source_chapters": {"chapter-1": SOURCE},
        },
        source_text=SOURCE,
        bible=_bible(),
    )

    assert len(screenplay.events) == 3
    assert len(screenplay.plot_spine.spine_beats) == 3
    assert len(screenplay.narrative_plan.source_evidence) == 3
    assert all(
        event.source_span == "SRC0001"
        for event in screenplay.events
    )


def _v13_payload() -> dict:
    payload = _ir_payload()
    payload["format_version"] = "screenplay-generation-ir.v1.3"
    payload["events"] = []
    payload["beats"] = []
    payload["coverage"] = []
    for source_index, scene in enumerate(payload["scenes"], start=1):
        for unit in scene["units"]:
            unit["source_segment_ids"] = [f"SRC{source_index:04d}"]
    return payload


def test_v13_compiler_accepts_unique_source_scene_owners() -> None:
    payload = _v13_payload()
    payload["source_scene_owners"] = {
        "SRC0001": "sc1",
        "SRC0002": "sc2",
        "SRC0003": "sc3",
    }

    screenplay = compile_screenplay_ir(
        ScreenplayGenerationIR.model_validate(payload),
        episode={"id": "ep-ir-v13-owner", "episode_no": 1},
        source_text=SOURCE,
        bible=_bible(),
    )

    assert screenplay.source_text_range == "screenplay-generation-ir.v1.3"


def test_v13_compiler_rejects_source_owned_by_another_scene() -> None:
    payload = _v13_payload()
    payload["source_scene_owners"] = {
        "SRC0001": "sc1",
        "SRC0002": "sc1",
        "SRC0003": "sc3",
    }

    with pytest.raises(ValueError, match="IR 来源唯一归属冲突.*SRC0002"):
        compile_screenplay_ir(
            ScreenplayGenerationIR.model_validate(payload),
            episode={"id": "ep-ir-v13-owner-conflict", "episode_no": 1},
            source_text=SOURCE,
            bible=_bible(),
        )


def test_v13_requires_every_fine_source_segment_in_authored_units() -> None:
    payload = _v13_payload()
    payload["scenes"][0]["scene_heading"] = "【场1-1】夜 / 咖啡厅里侧"
    screenplay = compile_screenplay_ir(
        ScreenplayGenerationIR.model_validate(payload),
        episode={
            "id": "ep-ir-v13",
            "episode_no": 1,
            "authorized_source_chapters": {"chapter-1": SOURCE},
        },
        source_text=SOURCE,
        bible=_bible(),
    )

    assert screenplay.source_text_range == "screenplay-generation-ir.v1.3"
    assert [
        item.source_segment_id for item in screenplay.source_coverage
    ] == ["SRC0001", "SRC0002", "SRC0003"]
    assert all(
        item.disposition in {"deliver", "merge"}
        for item in screenplay.source_coverage
    )
    assert screenplay.scene_outline[0].scene_heading.startswith("【场1】")
    assert screenplay.full_script_text.count(
        "谷言独自在咖啡厅等待旧友，不时看向门口，始终没有等到第二个人出现。"
    ) == 1
    assert all(
        not screenplay_beat_fields_repeat(beat.does, beat.turn)
        for beat in screenplay.plot_spine.spine_beats
    )


def test_v13_unit_resulting_state_drives_distinct_spine_turn() -> None:
    payload = _v13_payload()
    payload["scenes"][0]["units"][-1]["resulting_state"] = (
        "谷言结束无期限等待，明确只再等十分钟"
    )

    screenplay = compile_screenplay_ir(
        ScreenplayGenerationIR.model_validate(payload),
        episode={"id": "ep-ir-v13-turn", "episode_no": 1},
        source_text=SOURCE,
        bible=_bible(),
    )

    first = screenplay.plot_spine.spine_beats[0]
    assert first.turn == "谷言结束无期限等待，明确只再等十分钟"
    assert not screenplay_beat_fields_repeat(first.does, first.turn)


def test_v13_aggregates_explicit_actors_across_units_before_context_fallback() -> None:
    payload = _v13_payload()
    scene = payload["scenes"][1]
    scene["character_keys"] = []
    scene["units"][0]["text"] = "二人从座位两侧同时起身。"
    scene["units"].insert(1, {
        "kind": "action",
        "text": "谷言伸手接住旧友递来的钥匙。",
        "event_key": "e2",
        "source_segment_ids": ["SRC0002"],
    })
    candidate = ScreenplayGenerationIR.model_validate(payload)

    screenplay = compile_screenplay_ir(
        candidate,
        episode={"id": "ep-ir-v13-event-actors", "episode_no": 1},
        source_text=SOURCE,
        bible=_bible(),
    )

    assert not any(
        identity.key.startswith("context_actor_")
        for identity in candidate.identities
    )
    assert set(screenplay.scene_outline[1].characters) == {"谷言", "旧友"}


def test_v14_compiler_context_actor_gets_structural_authority() -> None:
    payload = _v13_payload()
    payload["format_version"] = "screenplay-generation-ir.v1.4"
    friend_resolution = normalize_character_resolution({
        "source_label": "旧友",
        "canonical_name": "旧友",
        "resolution": "functional_identity",
        "identity_group": "episode:old-friend",
    })
    payload["identities"][0]["authority_id"] = "bible:谷言"
    payload["identities"][1]["authority_id"] = friend_resolution["authority_id"]
    scene = payload["scenes"][1]
    scene["character_keys"] = []
    scene["units"] = [{
        "kind": "action",
        "text": "门口有人把钥匙放到桌面上。",
        "event_key": "context-event",
        "source_segment_ids": ["SRC0002"],
    }]
    candidate = ScreenplayGenerationIR.model_validate(payload)

    compile_screenplay_ir(
        candidate,
        episode={
            "id": "ep-ir-v14-context",
            "episode_no": 1,
            "character_resolutions": [friend_resolution],
        },
        source_text=SOURCE,
        bible=_bible(),
    )

    context_actors = [
        identity
        for identity in candidate.identities
        if identity.key.startswith("context_actor_")
    ]
    assert len(context_actors) == 1
    assert context_actors[0].authority_id.startswith("context:")


def test_v13_rejects_event_key_reused_across_scenes() -> None:
    payload = _v13_payload()
    payload["scenes"][1]["units"][0]["event_key"] = "e1"

    with pytest.raises(ValueError, match="event_key 必须在本集唯一"):
        compile_screenplay_ir(
            ScreenplayGenerationIR.model_validate(payload),
            episode={"id": "ep-ir-v13-duplicate-event", "episode_no": 1},
            source_text=SOURCE,
            bible=_bible(),
        )


def test_compiler_rejects_repeated_spine_action_and_turn() -> None:
    payload = _ir_payload()
    payload["beats"][0]["turn"] = payload["beats"][0]["does"]

    with pytest.raises(
        ValueError,
        match="主线节拍 does 与 turn 语义重复",
    ):
        compile_screenplay_ir(
            ScreenplayGenerationIR.model_validate(payload),
            episode={"id": "ep-ir-repeat-gate", "episode_no": 1},
            source_text=SOURCE,
            bible=_bible(),
        )


def test_screenplay_gate_rejects_repeated_spine_action_and_turn() -> None:
    screenplay = _compile()
    screenplay.plot_spine.spine_beats[0].turn = (
        screenplay.plot_spine.spine_beats[0].does
    )

    errors = validate_screenplay(
        screenplay,
        _bible(),
        expected_beats=3,
        episode_no=1,
        source_text=SOURCE,
        require_dialogue_chains=True,
        validate_narrative=False,
    )

    assert any(
        "[SPINE_ACTION_TURN_DUPLICATE]" in error
        for error in errors
    )


def test_v13_rejects_source_segment_hidden_as_context() -> None:
    payload = _v13_payload()
    for unit in payload["scenes"][1]["units"]:
        unit["source_segment_ids"] = ["SRC0001"]
    payload["coverage"] = [{
        "source_segment_ids": ["SRC0002"],
        "disposition": "context",
        "reason": "未进入正文",
    }]

    with pytest.raises(ValueError, match="漏掉细粒度来源段.*SRC0002"):
        compile_screenplay_ir(
            ScreenplayGenerationIR.model_validate(payload),
            episode={"id": "ep-ir-v13-missing", "episode_no": 1},
            source_text=SOURCE,
            bible=_bible(),
        )


def test_v13_rebinds_dialogue_to_unique_source_in_same_scene() -> None:
    payload = _v13_payload()
    payload["scenes"][0]["units"][1]["source_segment_ids"] = ["SRC0002"]
    audit: list[dict] = []

    compile_screenplay_ir(
        ScreenplayGenerationIR.model_validate(payload),
        episode={"id": "ep-ir-v13-dialogue", "episode_no": 1},
        source_text=SOURCE,
        bible=_bible(),
        audit=audit,
    )

    assert any(
        item.get("operation") == "rebind_dialogue_exact_source"
        and item.get("from") == ["SRC0002"]
        and item.get("to") == ["SRC0001"]
        for item in audit
    )


def test_v13_rebinds_dialogue_to_globally_unique_source() -> None:
    payload = _v13_payload()
    unit = payload["scenes"][0]["units"][1]
    unit["text"] = "拿好这把钥匙。"
    unit["source_text"] = "拿好这把钥匙。"
    unit["source_segment_ids"] = ["SRC0001"]
    audit: list[dict] = []

    compile_screenplay_ir(
        ScreenplayGenerationIR.model_validate(payload),
        episode={"id": "ep-ir-v13-global-dialogue", "episode_no": 1},
        source_text=SOURCE,
        bible=_bible(),
        audit=audit,
    )

    assert any(
        item.get("operation") == "rebind_dialogue_exact_source"
        and item.get("from") == ["SRC0001"]
        and item.get("to") == ["SRC0002"]
        for item in audit
    )


def test_v13_rejects_dialogue_without_unique_source_evidence() -> None:
    payload = _v13_payload()
    unit = payload["scenes"][0]["units"][1]
    unit["source_text"] = "授权原文里不存在的对白"

    with pytest.raises(
        ValueError,
        match="对白 source_text 不属于声明的来源段",
    ):
        compile_screenplay_ir(
            ScreenplayGenerationIR.model_validate(payload),
            episode={"id": "ep-ir-v13-fake-dialogue", "episode_no": 1},
            source_text=SOURCE,
            bible=_bible(),
        )


def test_v13_rejects_many_source_ids_attached_to_one_unit() -> None:
    source = "\n\n".join(f"来源段{index}。" for index in range(17))
    payload = _v13_payload()
    payload["scenes"] = [payload["scenes"][0]]
    payload["scenes"][0]["units"] = [{
        "kind": "action",
        "text": "十三段剧情被错误压成一句动作。",
        "event_key": "e1",
        "source_segment_ids": [
            f"SRC{index:04d}" for index in range(1, 18)
        ],
    }]

    with pytest.raises(ValueError, match="单个 unit 合并来源段过多"):
        compile_screenplay_ir(
            ScreenplayGenerationIR.model_validate(payload),
            episode={"id": "ep-ir-v13-overloaded", "episode_no": 1},
            source_text=source,
            bible=_bible(),
        )


def test_v13_rejects_globally_overcompressed_units() -> None:
    source = "\n\n".join(
        f"来源段{index}。" + "连续发生的关键剧情动作与人物反应。" * 15
        for index in range(4)
    )
    payload = _v13_payload()
    payload["scenes"] = [payload["scenes"][0]]
    payload["scenes"][0]["units"] = [
        {
            "kind": "action",
            "text": "人物继续行动。",
            "event_key": f"e{index}",
            "source_segment_ids": [f"SRC{index:04d}"],
        }
        for index in range(1, 5)
    ]

    with pytest.raises(ValueError, match="正文过度压缩"):
        compile_screenplay_ir(
            ScreenplayGenerationIR.model_validate(payload),
            episode={"id": "ep-ir-v13-compressed", "episode_no": 1},
            source_text=source,
            bible=_bible(),
        )


def test_fidelity_plan_selection_keeps_low_density_internal_windows() -> None:
    context = {
        "missing_source_ids": [],
        "windows_requiring_expansion": [{
            "source_segments": [
                {"source_segment_id": "SRC0002", "text": "第二段"},
                {"source_segment_id": "SRC0003", "text": "第三段"},
                {"source_segment_id": "SRC0004", "text": "第四段"},
                {"source_segment_id": "SRC0005", "text": "第五段"},
            ],
        }],
    }
    plans = [
        SimpleNamespace(key="bp-sc001", source_segment_ids=["SRC0001", "SRC0002"]),
        SimpleNamespace(key="bp-sc002", source_segment_ids=["SRC0003"]),
        SimpleNamespace(key="bp-sc003", source_segment_ids=["SRC0004", "SRC0005"]),
    ]

    remaining, internal, selected, repair_source_ids = (
        stages._select_fidelity_blueprint_plans(
            context,
            plans,
            candidate_scene_count=3,
        )
    )

    assert remaining == []
    assert [plan.key for plan in internal] == [
        "bp-sc001", "bp-sc002", "bp-sc003",
    ]
    assert selected == []
    assert repair_source_ids == {
        "SRC0002", "SRC0003", "SRC0004", "SRC0005",
    }


def test_fidelity_patch_routes_unit_to_its_source_owner_scene() -> None:
    candidate = ScreenplayGenerationIR.model_validate(_v13_payload())
    candidate.source_scene_owners = {
        "SRC0001": "sc1",
        "SRC0002": "sc2",
        "SRC0003": "sc3",
    }
    unit = candidate.scenes[1].units[0].model_copy(deep=True)
    original_counts = {
        scene.key: len(scene.units) for scene in candidate.scenes
    }
    patch = stages._IRFidelityPatch.model_validate({
        "insertions": [{
            "scene_key": "sc1",
            "units": [unit.model_dump(mode="json")],
        }],
    })

    inserted = stages._merge_ir_fidelity_patch(
        candidate,
        patch,
        SOURCE,
        round_no=1,
    )

    assert inserted == 1
    assert len(candidate.scenes[0].units) == original_counts["sc1"]
    assert len(candidate.scenes[1].units) == original_counts["sc2"] + 1


def test_fidelity_patch_rejects_unit_with_multiple_source_owners() -> None:
    candidate = ScreenplayGenerationIR.model_validate(_v13_payload())
    candidate.source_scene_owners = {
        "SRC0001": "sc1",
        "SRC0002": "sc2",
        "SRC0003": "sc3",
    }
    unit = candidate.scenes[0].units[0].model_copy(deep=True)
    unit.source_segment_ids = ["SRC0001", "SRC0002"]
    unit.action_agency.source_segment_ids = ["SRC0001", "SRC0002"]
    patch = stages._IRFidelityPatch.model_validate({
        "insertions": [{
            "scene_key": "sc1",
            "units": [unit.model_dump(mode="json")],
        }],
    })

    with pytest.raises(ValueError, match="跨越多个 source owner"):
        stages._merge_ir_fidelity_patch(
            candidate,
            patch,
            SOURCE,
            round_no=1,
        )


def test_truncated_ir_recovers_only_complete_top_level_members() -> None:
    payload = _ir_payload()
    payload["format_version"] = "screenplay-generation-ir.v1.2"
    raw = __import__("json").dumps(payload, ensure_ascii=False)
    events_at = raw.index('"events"')
    truncated = raw[:events_at] + '"events":[{"key":"cut'

    recovered = recover_complete_screenplay_ir_prefix(truncated)

    assert recovered is not None
    assert len(recovered["scenes"]) == 3
    assert "events" not in recovered
    assert recovered["normalization_log"][-1]["reason"] == (
        "durable_prefix_recovery"
    )


def test_truncated_ir_recovers_only_fully_decoded_scene_prefix() -> None:
    payload = _v13_payload()
    raw = __import__("json").dumps(payload, ensure_ascii=False)
    last_scene_at = raw.index('"key": "sc3"')
    truncated = raw[:last_scene_at + 30]

    recovered = recover_complete_screenplay_ir_prefix(truncated)

    assert recovered is not None
    assert [scene["key"] for scene in recovered["scenes"]] == ["sc1", "sc2"]
    assert recovered["normalization_log"][-1]["reason"] == (
        "durable_complete_scene_prefix_recovery"
    )


def test_compiler_repairs_dangling_unit_context_and_time_budget() -> None:
    payload = _ir_payload()
    payload["coverage"] = []
    payload["beats"][2]["source_segment_ids"] = []
    payload["events"][2]["source_segment_ids"] = []
    payload["scenes"][0]["units"][0]["event_key"] = "ev-missing"
    payload["scenes"][0]["units"][0]["text"] = (
        "镜头扫过咖啡厅，最终停在等待中的谷言身上。"
    )
    payload["scenes"][0]["source_basis"] = "短"
    payload["experience"]["required_processing_s"] = 400

    screenplay = compile_screenplay_ir(
        ScreenplayGenerationIR.model_validate(payload),
        episode={
            "id": "ep-ir-repair",
            "episode_no": 1,
            "authorized_source_chapters": {"chapter-1": SOURCE},
        },
        source_text=SOURCE,
        bible=_bible(),
    )

    coverage = {
        item.source_segment_id: item for item in screenplay.source_coverage
    }
    assert coverage["SRC0003"].disposition == "context"
    assert any(
        "SRC0003" in item
        for scene in screenplay.scene_outline
        for item in scene.context_requirements
    )
    assert "镜头扫过咖啡厅" in screenplay.full_script_text
    assert len(screenplay.scene_outline[0].source_basis) >= 8
    assert (
        screenplay.narrative_plan.readability_windows[-1]
        .scheduled_processing_s
        == 10
    )


def test_compiler_splits_long_dialogue_without_rewriting_source_evidence() -> None:
    payload = _ir_payload()
    long_line = (
        "我已经在这里等了很久，但你一直没有出现，"
        "现在请把发生的事情从头到尾说清楚，不要再隐瞒。"
    )
    payload["scenes"][0]["units"][1]["text"] = long_line

    screenplay = compile_screenplay_ir(
        ScreenplayGenerationIR.model_validate(payload),
        episode={
            "id": "ep-ir-dialogue",
            "episode_no": 1,
            "authorized_source_chapters": {"chapter-1": SOURCE},
        },
        source_text=SOURCE,
        bible=_bible(),
    )
    turns = screenplay.dialogue_chains[0].turns

    assert len(turns) > 1
    assert "".join(turn.line for turn in turns) == long_line
    assert all(
        len("".join(char for char in turn.line if char.isalnum())) <= 36
        for turn in turns
    )
    assert len({turn.source_text for turn in turns}) == 1


def test_compiler_demotes_response_at_start_of_capacity_continuation() -> None:
    payload = _ir_payload()
    template = payload["scenes"][0]["units"][1]
    payload["scenes"][0]["units"] = [
        payload["scenes"][0]["units"][0],
        *[
            {
                **template,
                "text": f"第{index}句回应。",
                "function": "response",
            }
            for index in range(14)
        ],
    ]

    screenplay = compile_screenplay_ir(
        ScreenplayGenerationIR.model_validate(payload),
        episode={
            "id": "ep-ir-chain-capacity",
            "episode_no": 1,
            "authorized_source_chapters": {"chapter-1": SOURCE},
        },
        source_text=SOURCE,
        bible=_bible(),
    )

    waiting_chains = [
        chain
        for chain in screenplay.dialogue_chains
        if chain.topic.startswith("建立谷言等待旧友")
    ]
    assert len(waiting_chains) == 2
    assert waiting_chains[1].topic.endswith("（续）")
    assert waiting_chains[1].turns[0].function == "statement"


def test_compiled_ir_identity_contract_is_storyboard_resolvable() -> None:
    screenplay = _compile()
    resolver = narrative_identity_resolver(_bible(), screenplay)

    assert resolver.resolve("谷言", usage="visual").display_name == "谷言"
    assert resolver.resolve("旧友", usage="visual").display_name == "旧友"
    assert resolver.resolve("旧友", usage="voice").display_name == "旧友"


def test_compiled_ir_storyboard_outline_never_calls_full_outline_model(
    monkeypatch,
) -> None:
    screenplay = _compile()
    bible = _bible()
    bible.scenes = [
        Scene(name="咖啡厅里侧", scene_canonical="夜晚咖啡厅里侧座位区"),
        Scene(name="咖啡厅门口", scene_canonical="夜晚咖啡厅门口"),
        Scene(name="咖啡厅座位", scene_canonical="夜晚咖啡厅座位区"),
    ]

    async def forbidden_model_call(*_args, **_kwargs):
        raise AssertionError("narrative outline must compile locally")

    monkeypatch.setattr(
        stages,
        "_run_with_agent_loop",
        forbidden_model_call,
    )
    outline = asyncio.run(stages.generate_storyboard_outline(
        {
            "id": "ep-ir-1",
            "episode_no": 1,
            "title": "雨夜敲门",
            "target_duration_s": 50,
            "screenplay_artifact_id": "art-screenplay-ir-1",
        },
        SOURCE,
        bible,
        prev_ending="",
        screenplay=screenplay,
    ))

    assert {
        event_id
        for shot in outline.shots
        for event_id in shot.event_ids
    } == {
        event.event_id
        for event in screenplay.narrative_plan.events
    }
    assert len(outline.scene_contexts) == len(screenplay.scene_outline)
    assert outline.evidence_artifact_id


def test_duplicate_identity_uses_owned_source_functional_resolution() -> None:
    payload = _v13_payload()
    payload["identities"][1]["display_name"] = "谷言"
    ir = ScreenplayGenerationIR.model_validate(payload)
    ir.events = [IREvent(
        key="contaminated",
        scene_key="sc1",
        source_segment_ids=["SRC0001"],
        adapted_statement="派生 actor 不得扩散直接说话人的来源范围",
        actor_keys=["friend"],
    )]
    audit: list[dict] = []

    changes = _normalize_duplicate_ir_identity_displays(
        ir,
        episode={
            "character_resolutions": [
                {
                    "source_label": "咖啡厅",
                    "canonical_name": "路人丁",
                    "resolution": "functional_extra",
                },
                {
                    "source_label": "旧友",
                    "canonical_name": "路人6",
                    "resolution": "functional_extra",
                },
            ],
        },
        source_text=SOURCE,
        bible=_bible(),
        audit=audit,
    )

    assert ir.identities[0].display_name == "谷言"
    assert ir.identities[1].display_name == "路人6"
    assert changes == audit
    assert changes[0]["source_label"] == "旧友"


def test_ir_identity_display_is_bound_to_preflight_resolution() -> None:
    payload = _v13_payload()
    payload["identities"][1].update({
        "display_name": "路人甲",
        "source_names": ["旧友"],
        "rationale": "原文中的旧友，按预检应使用稳定身份",
    })
    ir = ScreenplayGenerationIR.model_validate(payload)
    audit: list[dict] = []

    changes = _apply_authoritative_ir_identity_resolutions(
        ir,
        episode={"character_resolutions": [{
            "source_label": "旧友",
            "canonical_name": "未出场人物",
            "resolution": "future_identity",
        }]},
        bible=_bible(),
        audit=audit,
    )

    assert ir.identities[1].display_name == "未出场人物"
    assert ir.identities[1].role_type == "named_character"
    assert ir.identities[1].visual_policy == "canonical"
    assert changes == audit


def test_ir_source_names_override_short_terms_inside_rationale() -> None:
    payload = _v13_payload()
    payload["identities"][1].update({
        "display_name": "八岁少年",
        "source_names": ["虎头虎脑的少年"],
        "role_type": "functional_character",
        "rationale": "少年说自己被会飞的女人抓来",
    })
    ir = ScreenplayGenerationIR.model_validate(payload)

    _apply_authoritative_ir_identity_resolutions(
        ir,
        episode={"character_resolutions": [
            {
                "source_label": "虎头虎脑的少年",
                "canonical_name": "八岁少年",
                "resolution": "functional_identity",
            },
            {
                "source_label": "少年",
                "canonical_name": "主角",
                "resolution": "future_identity",
            },
            {
                "source_label": "女",
                "canonical_name": "女主",
                "resolution": "future_identity",
            },
        ]},
        bible=_bible(),
        audit=[],
    )

    assert ir.identities[1].display_name == "八岁少年"


def test_ir_identity_resolution_defers_conflicting_exact_authorities_to_ai() -> None:
    payload = _v13_payload()
    payload["identities"][1].update({
        "key": "malian",
        "display_name": "马脸青年",
        "source_names": ["马脸师兄", "杂役处的师兄"],
        "role_type": "functional_character",
        "rationale": "杂役处管理者，原文称谓马脸师兄",
    })
    for scene in payload["scenes"]:
        scene["character_keys"] = [
            "malian" if key == "friend" else key
            for key in scene["character_keys"]
        ]
        for unit in scene["units"]:
            if unit.get("speaker_key") == "friend":
                unit["speaker_key"] = "malian"
    ir = ScreenplayGenerationIR.model_validate(payload)

    with pytest.raises(ScreenplayIRIdentityConflictError) as exc_info:
        _apply_authoritative_ir_identity_resolutions(
            ir,
            episode={"character_resolutions": [
                {
                    "source_label": "马脸青年",
                    "canonical_name": "马脸青年",
                    "resolution": "functional_identity",
                    "identity_group": "existing:马脸青年",
                },
                {
                    "source_label": "马脸师兄",
                    "canonical_name": "马脸青年",
                    "resolution": "functional_identity",
                    "identity_group": "existing:马脸青年",
                },
                {
                    "source_label": "杂役处的师兄",
                    "canonical_name": "杂役处的师兄",
                    "resolution": "functional_identity",
                    "identity_group": "current-1:F1",
                },
            ]},
            bible=_bible(),
            audit=[],
        )

    assert exc_info.value.issues[0]["reason"] == "multiple_exact_authorities"


def test_ir_identity_conflict_has_generation_error_classification() -> None:
    payload = _v13_payload()
    payload["identities"][1].update({
        "display_name": "杂役管理者",
        "source_names": ["甲师兄", "乙师兄"],
        "role_type": "functional_character",
    })
    ir = ScreenplayGenerationIR.model_validate(payload)

    with pytest.raises(ScreenplayIRIdentityConflictError):
        _apply_authoritative_ir_identity_resolutions(
            ir,
            episode={"character_resolutions": [
                {
                    "source_label": "甲师兄",
                    "canonical_name": "甲",
                    "resolution": "functional_identity",
                    "identity_group": "group-a",
                },
                {
                    "source_label": "乙师兄",
                    "canonical_name": "乙",
                    "resolution": "functional_identity",
                    "identity_group": "group-b",
                },
            ]},
            bible=_bible(),
            audit=[],
        )

    assert app_errors.classify(
        ScreenplayIRIdentityConflictError("ambiguous"),
    ) == ("generation", "GEN")


def test_identity_adjudication_skips_ai_when_exact_authorities_are_complete(
    monkeypatch,
) -> None:
    payload = _v13_payload()
    payload["format_version"] = "screenplay-generation-ir.v1.4"
    friend_resolution = normalize_character_resolution({
        "source_label": "旧友",
        "canonical_name": "旧友",
        "resolution": "functional_identity",
        "identity_group": "episode:old-friend",
    })
    payload["identities"][0]["authority_id"] = "bible:谷言"
    payload["identities"][1]["authority_id"] = friend_resolution["authority_id"]
    episode = {
        "id": "ep-ir-exact",
        "episode_no": 1,
        "authorized_source_chapters": {"chapter-1": SOURCE},
        "character_resolutions": [friend_resolution],
    }
    candidate = ScreenplayGenerationIR.model_validate(payload)

    async def forbidden_chat(*_args, **_kwargs):
        raise AssertionError("精确 authority_id 完整时不应调用 AI")

    monkeypatch.setattr(identity_adjudication.model_gateway, "chat", forbidden_chat)
    resolved = asyncio.run(adjudicate_screenplay_ir_identities(
        candidate,
        episode=episode,
        source_text=SOURCE,
        bible=_bible(),
    ))
    screenplay = compile_screenplay_ir(
        resolved,
        episode=episode,
        source_text=SOURCE,
        bible=_bible(),
    )

    assert screenplay.id == "ep-ir-exact"
    assert resolved.identities[1].authority_id == friend_resolution["authority_id"]


def test_identity_adjudication_normalizes_backend_owned_narrator_without_ai(
    monkeypatch,
) -> None:
    payload = _v13_payload()
    payload["format_version"] = "screenplay-generation-ir.v1.4"
    friend_resolution = normalize_character_resolution({
        "source_label": "旧友",
        "canonical_name": "旧友",
        "resolution": "functional_identity",
        "identity_group": "episode:old-friend",
    })
    payload["identities"][0]["authority_id"] = "bible:谷言"
    payload["identities"][1]["authority_id"] = friend_resolution["authority_id"]
    payload["identities"].append({
        **payload["identities"][1],
        "key": "narrator",
        "display_name": "旁白",
        "source_names": [],
        "kind": "narrator",
        "visual_policy": "offscreen_only",
        "visual_canonical": "",
        "asset_requirement": "forbidden",
        "role_type": "narrator",
        "authority_id": "functional:narrator",
    })
    payload["scenes"][0]["character_keys"].append("narrator")
    episode = {
        "id": "ep-ir-narrator-authority",
        "episode_no": 1,
        "character_resolutions": [friend_resolution],
    }

    async def forbidden_chat(*_args, **_kwargs):
        raise AssertionError("后端拥有的纯旁白 authority 不应调用 AI")

    monkeypatch.setattr(identity_adjudication.model_gateway, "chat", forbidden_chat)
    resolved = asyncio.run(adjudicate_screenplay_ir_identities(
        ScreenplayGenerationIR.model_validate(payload),
        episode=episode,
        source_text=SOURCE,
        bible=_bible(),
        persist_new_resolutions=False,
    ))

    narrator = next(item for item in resolved.identities if item.key == "narrator")
    assert narrator.authority_id == "narrator:narrator"
    assert narrator.role_type == "narrator"
    assert any(
        item.get("path") == "identities.narrator"
        and item.get("operation") == "bind_backend_owned_identity_authority"
        and item.get("from", {}).get("authority_id") == "functional:narrator"
        and item.get("to", {}).get("authority_id") == "narrator:narrator"
        for item in resolved.normalization_log
    )


def test_identity_adjudication_does_not_expose_unverified_authority_to_model() -> None:
    payload = _v13_payload()
    payload["format_version"] = "screenplay-generation-ir.v1.4"
    payload["identities"][0]["authority_id"] = "bible:谷言"
    payload["identities"][1]["authority_id"] = "model-proposed:friend"
    candidate = ScreenplayGenerationIR.model_validate(payload)
    episode = {
        "id": "ep-ir-unverified-authority",
        "episode_no": 1,
        "character_resolutions": [],
    }
    _changes, issues = prepare_ir_identity_authorities(
        candidate,
        episode=episode,
        bible=_bible(),
        audit=[],
    )

    model_payload = identity_adjudication._adjudication_payload(
        candidate,
        episode=episode,
        source_text=SOURCE,
        bible=_bible(),
        issues=issues,
    )
    unresolved_identity = next(
        item for item in model_payload["identities"]
        if item["key"] == "friend"
    )

    assert unresolved_identity["authority_id"] == ""
    assert model_payload["issues"] == [{
        "identity_key": "friend",
        "reason": "unknown_explicit_authority",
    }]
    assert "model-proposed:friend" not in json.dumps(
        model_payload,
        ensure_ascii=False,
    )


def test_screenplay_ir_prompt_delegates_pure_narrator_authority_to_backend() -> None:
    contract = screenplay_ir_prompt_contract()

    assert "authority_id 只允许逐字引用人物谱或身份预解析中已有 ID" in contract
    assert "没有精确已登记 authority 的身份必须留空" in contract
    assert "role_type=narrator 的纯旁白也必须留空" in contract
    assert "由后端根据 identity.key 确定性生成" in contract


def test_identity_adjudication_prunes_unreferenced_identity_without_ai(
    monkeypatch,
) -> None:
    payload = _v13_payload()
    payload["format_version"] = "screenplay-generation-ir.v1.4"
    payload["identities"][0]["authority_id"] = "bible:谷言"
    friend_resolution = normalize_character_resolution({
        "source_label": "旧友",
        "canonical_name": "旧友",
        "resolution": "functional_identity",
        "identity_group": "episode:old-friend",
    })
    payload["identities"][1]["authority_id"] = friend_resolution["authority_id"]
    payload["identities"].append({
        **payload["identities"][1],
        "key": "unused_extra",
        "display_name": "未引用路人",
        "source_names": [],
        "authority_id": "",
    })

    async def forbidden_chat(*_args, **_kwargs):
        raise AssertionError("完全未被引用的身份应结构性删除，不应交给 AI 猜测")

    monkeypatch.setattr(identity_adjudication.model_gateway, "chat", forbidden_chat)
    candidate = ScreenplayGenerationIR.model_validate(payload)
    resolved = asyncio.run(adjudicate_screenplay_ir_identities(
        candidate,
        episode={
            "id": "ep-ir-orphan",
            "episode_no": 1,
            "character_resolutions": [friend_resolution],
        },
        source_text=SOURCE,
        bible=_bible(),
        persist_new_resolutions=False,
    ))

    assert "unused_extra" not in {item.key for item in resolved.identities}
    assert any(
        item.get("operation") == "remove_unreferenced_identity"
        for item in resolved.normalization_log
    )


def test_identity_adjudication_calls_ai_once_for_conflicting_exact_authorities(
    monkeypatch,
) -> None:
    payload = _v13_payload()
    payload["format_version"] = "screenplay-generation-ir.v1.4"
    payload["identities"][0]["authority_id"] = "bible:谷言"
    payload["identities"][1].update({
        "authority_id": "",
        "source_names": ["旧友", "来人"],
    })
    old_friend = normalize_character_resolution({
        "source_label": "旧友",
        "canonical_name": "旧友",
        "resolution": "functional_identity",
        "identity_group": "episode:old-friend",
    })
    visitor = normalize_character_resolution({
        "source_label": "来人",
        "canonical_name": "来人",
        "resolution": "functional_identity",
        "identity_group": "episode:visitor",
    })
    episode = {
        "id": "ep-ir-conflict",
        "episode_no": 1,
        "authorized_source_chapters": {"chapter-1": SOURCE},
        "character_resolutions": [old_friend, visitor],
    }
    calls = []

    async def fake_chat(messages, **kwargs):
        calls.append((messages, kwargs))
        return json.dumps({"decisions": [{
            "identity_key": "friend",
            "status": "bind",
            "authority_id": old_friend["authority_id"],
            "canonical_name": "",
            "evidence_source_ids": ["SRC0002"],
            "rationale": "SRC0002 明确写明旧友推门出现并递钥匙",
        }]}, ensure_ascii=False)

    monkeypatch.setattr(identity_adjudication.model_gateway, "chat", fake_chat)
    candidate = ScreenplayGenerationIR.model_validate(payload)
    resolved = asyncio.run(adjudicate_screenplay_ir_identities(
        candidate,
        episode=episode,
        source_text=SOURCE,
        bible=_bible(),
        persist_new_resolutions=False,
    ))
    screenplay = compile_screenplay_ir(
        resolved,
        episode=episode,
        source_text=SOURCE,
        bible=_bible(),
    )

    assert screenplay.id == "ep-ir-conflict"
    assert resolved.identities[1].authority_id == old_friend["authority_id"]
    assert len(calls) == 1
    assert calls[0][1]["call_meta"]["stage"] == (
        "screenplay_ir_identity_adjudication"
    )
    assert calls[0][1]["call_meta"]["reuse_successful_operation"] is True


def test_identity_adjudication_creates_source_backed_functional_authority(
    monkeypatch,
) -> None:
    payload = _v13_payload()
    payload["format_version"] = "screenplay-generation-ir.v1.4"
    payload["identities"][0]["authority_id"] = "bible:谷言"
    payload["identities"][1]["authority_id"] = ""
    episode = {
        "id": "ep-ir-new-functional",
        "episode_no": 1,
        "authorized_source_chapters": {"chapter-1": SOURCE},
        "character_resolutions": [],
    }

    async def fake_chat(*_args, **_kwargs):
        return json.dumps({"decisions": [{
            "identity_key": "friend",
            "status": "new_functional",
            "authority_id": "",
            "canonical_name": "旧友",
            "evidence_source_ids": ["SRC0002"],
            "rationale": "原文明确写出旧友出现、递钥匙并开口",
        }]}, ensure_ascii=False)

    monkeypatch.setattr(identity_adjudication.model_gateway, "chat", fake_chat)
    candidate = ScreenplayGenerationIR.model_validate(payload)
    resolved = asyncio.run(adjudicate_screenplay_ir_identities(
        candidate,
        episode=episode,
        source_text=SOURCE,
        bible=_bible(),
        persist_new_resolutions=False,
    ))

    assert resolved.identities[1].authority_id.startswith("functional:")
    assert episode["character_resolutions"][0]["authority_id"] == (
        resolved.identities[1].authority_id
    )
    assert episode["character_resolutions"][0]["evidence_source_ids"] == [
        "SRC0002"
    ]


def test_identity_adjudication_preserves_two_entities_with_one_source_label(
    monkeypatch,
) -> None:
    payload = _v13_payload()
    payload["format_version"] = "screenplay-generation-ir.v1.4"
    payload["identities"][0]["authority_id"] = "bible:谷言"
    payload["identities"][1].update({
        "key": "friend_a",
        "display_name": "旧友",
        "source_names": ["旧友"],
        "authority_id": "",
    })
    payload["identities"].append({
        **payload["identities"][1],
        "key": "friend_b",
        "display_name": "旧友",
    })
    for scene in payload["scenes"]:
        scene["character_keys"] = [
            "friend_a" if key == "friend" else key
            for key in scene["character_keys"]
        ]
        for unit in scene["units"]:
            if unit.get("speaker_key") == "friend":
                unit["speaker_key"] = "friend_a"
    payload["scenes"][2]["character_keys"].append("friend_b")
    payload["scenes"][2]["units"][1]["speaker_key"] = "friend_b"
    episode = {
        "id": "ep-ir-shared-source-label",
        "episode_no": 1,
        "character_resolutions": [],
    }

    async def fake_chat(*_args, **_kwargs):
        return json.dumps({"decisions": [
            {
                "identity_key": "friend_a",
                "status": "new_functional",
                "canonical_name": "旧友",
                "evidence_source_ids": ["SRC0002"],
                "rationale": "该 identity 在 SRC0002 独立出场并开口",
            },
            {
                "identity_key": "friend_b",
                "status": "new_functional",
                "canonical_name": "旧友",
                "evidence_source_ids": ["SRC0003"],
                "rationale": "该 identity 在 SRC0003 独立开口",
            },
        ]}, ensure_ascii=False)

    monkeypatch.setattr(identity_adjudication.model_gateway, "chat", fake_chat)
    candidate = ScreenplayGenerationIR.model_validate(payload)
    resolved = asyncio.run(adjudicate_screenplay_ir_identities(
        candidate,
        episode=episode,
        source_text=SOURCE,
        bible=_bible(),
        persist_new_resolutions=False,
    ))

    resolved_ids = {
        identity.authority_id
        for identity in resolved.identities
        if identity.key in {"friend_a", "friend_b"}
    }
    assert len(resolved_ids) == 2
    assert len(episode["character_resolutions"]) == 2
    assert {
        item["source_label"] for item in episode["character_resolutions"]
    } == {"旧友"}
    assert all(
        item["source_instance_key"] == item["authority_id"]
        for item in episode["character_resolutions"]
    )
    screenplay = compile_screenplay_ir(
        resolved,
        episode=episode,
        source_text=SOURCE,
        bible=_bible(),
    )
    assert screenplay.id == "ep-ir-shared-source-label"


def test_identity_adjudication_fails_closed_on_insufficient_source_evidence(
    monkeypatch,
) -> None:
    payload = _v13_payload()
    payload["format_version"] = "screenplay-generation-ir.v1.4"
    payload["identities"][0]["authority_id"] = "bible:谷言"
    episode = {
        "id": "ep-ir-insufficient",
        "episode_no": 1,
        "character_resolutions": [],
    }

    async def fake_chat(*_args, **_kwargs):
        return json.dumps({"decisions": [{
            "identity_key": "friend",
            "status": "insufficient_evidence",
            "authority_id": "",
            "canonical_name": "",
            "evidence_source_ids": ["SRC0002"],
            "rationale": "原文没有提供可唯一绑定到既有实体的证据",
        }]}, ensure_ascii=False)

    monkeypatch.setattr(identity_adjudication.model_gateway, "chat", fake_chat)
    candidate = ScreenplayGenerationIR.model_validate(payload)

    with pytest.raises(ScreenplayIRIdentityConflictError):
        asyncio.run(adjudicate_screenplay_ir_identities(
            candidate,
            episode=episode,
            source_text=SOURCE,
            bible=_bible(),
            persist_new_resolutions=False,
        ))


def test_identity_adjudication_rejects_unavailable_source_id(monkeypatch) -> None:
    payload = _v13_payload()
    payload["format_version"] = "screenplay-generation-ir.v1.4"
    payload["identities"][0]["authority_id"] = "bible:谷言"

    async def fake_chat(*_args, **_kwargs):
        return json.dumps({"decisions": [{
            "identity_key": "friend",
            "status": "new_functional",
            "canonical_name": "旧友",
            "evidence_source_ids": ["SRC9999"],
            "rationale": "无效引用",
        }]}, ensure_ascii=False)

    monkeypatch.setattr(identity_adjudication.model_gateway, "chat", fake_chat)
    candidate = ScreenplayGenerationIR.model_validate(payload)

    with pytest.raises(app_errors.ContentGenerationError, match="原文来源段"):
        asyncio.run(adjudicate_screenplay_ir_identities(
            candidate,
            episode={
                "id": "ep-ir-bad-source",
                "episode_no": 1,
                "character_resolutions": [],
            },
            source_text=SOURCE,
            bible=_bible(),
            persist_new_resolutions=False,
        ))


def test_document_identity_adjudication_uses_only_source_backed_typed_reference(
    monkeypatch,
) -> None:
    screenplay = _compile()
    screenplay.scene_outline[0].characters.append("门卫")
    source_text = SOURCE + "\n\n门卫推开外门，示意众人暂时不要离开。"
    identity_key = (
        "document_identity_"
        + hashlib.sha256("门卫".encode("utf-8")).hexdigest()[:12]
    )
    prompts: list[str] = []

    async def fake_chat(messages, **_kwargs):
        prompts.append(messages[0]["content"])
        return json.dumps({"decisions": [{
            "identity_key": identity_key,
            "status": "new_functional",
            "canonical_name": "门卫",
            "evidence_source_ids": ["SRC0004"],
            "rationale": "SRC0004 明确出现门卫并承担开门动作",
        }]}, ensure_ascii=False)

    monkeypatch.setattr(identity_adjudication.model_gateway, "chat", fake_chat)
    friend_resolution = normalize_character_resolution({
        "source_label": "旧友",
        "canonical_name": "旧友",
        "resolution": "functional_identity",
        "identity_group": "episode:old-friend",
    })
    episode = {
        "episode_no": 1,
        "character_resolutions": [friend_resolution],
    }
    resolutions = asyncio.run(
        identity_adjudication.adjudicate_screenplay_document_identities(
            screenplay,
            episode=episode,
            source_text=source_text,
            bible=_bible(),
        )
    )
    assert len(prompts) == 1
    assert "门外再次响起更重的敲门声" not in prompts[0]
    assert "门卫推开外门" in prompts[0]
    guard_resolution = next(
        item for item in resolutions if item["source_label"] == "门卫"
    )
    assert guard_resolution["authority_id"].startswith("functional:")


def test_shared_functional_source_label_requires_ai_before_merging_identities() -> None:
    payload = _v13_payload()
    payload["identities"] = [
        {
            **payload["identities"][0],
            "key": "guard_a",
            "display_name": "绿袍修士甲",
            "source_names": ["两个绿袍修士", "其中一人"],
            "role_type": "functional_character",
        },
        {
            **payload["identities"][1],
            "key": "guard_b",
            "display_name": "绿袍修士乙",
            "source_names": ["两个绿袍修士", "另一人"],
            "role_type": "functional_character",
        },
    ]
    for scene in payload["scenes"]:
        scene["character_keys"] = [
            "guard_a" if key == "g" else "guard_b"
            for key in scene["character_keys"]
        ]
        for unit in scene["units"]:
            if unit.get("speaker_key") == "g":
                unit["speaker_key"] = "guard_a"
            elif unit.get("speaker_key") == "friend":
                unit["speaker_key"] = "guard_b"
    ir = ScreenplayGenerationIR.model_validate(payload)

    with pytest.raises(ScreenplayIRIdentityConflictError) as exc_info:
        _apply_authoritative_ir_identity_resolutions(
            ir,
            episode={"character_resolutions": [{
                "source_label": "两个绿袍修士",
                "canonical_name": "两个绿袍修士",
                "resolution": "functional_identity",
            }]},
            bible=_bible(),
            audit=[],
        )

    assert any(
        issue["reason"] == "shared_inferred_authority"
        for issue in exc_info.value.issues
    )


def test_bible_context_includes_character_named_by_resolution() -> None:
    payload = screenplay_ir_bible_context(
        _bible(),
        source_text="小胖子站在门口。",
        episode_no=1,
        character_resolutions=[{
            "source_label": "小胖子",
            "canonical_name": "未出场人物",
            "resolution": "future_identity",
        }],
    )

    assert [item["name"] for item in payload["characters"]] == ["未出场人物"]


def test_bible_context_uses_source_evidence_not_full_project_dump() -> None:
    payload = screenplay_ir_bible_context(
        _bible(),
        source_text=SOURCE,
        episode_no=1,
    )

    assert [item["name"] for item in payload["characters"]] == ["谷言"]
    assert "未出场人物" not in str(payload)


def test_legacy_full_screenplay_candidate_remains_compatible() -> None:
    legacy = EpisodeScreenplay(
        episode_no=1,
        title="旧格式",
        full_script_text="【场1】日 / 室内\n角色站在门口。",
    )
    envelope = ScreenplayGenerationIR.model_validate(
        legacy.model_dump(mode="json")
    )

    compiled = compile_screenplay_ir(
        envelope,
        episode={"id": "ep-legacy", "episode_no": 1},
        source_text="原文",
        bible=_bible(),
    )

    assert compiled.title == "旧格式"
    assert compiled.id == "ep-legacy"


def test_storyboard_context_consumes_compiled_ids_without_adapter() -> None:
    screenplay = _compile()

    narrative_context = stages._compact_narrative_plan_context(screenplay)
    key_context = stages._storyboard_key_content_block(screenplay)

    assert '"event_id":"E1"' in narrative_context
    assert '"action_id":"A-1"' in narrative_context
    assert '"scene_id":"SC01"' in narrative_context
    assert '"readability_window_id":"RW-1"' in narrative_context
    assert "KL01｜谷言：再等十分钟。" in key_context
    assert "S01｜谷言｜独自在咖啡厅等待旧友" in key_context


def test_generation_entry_uses_compact_ir_model_and_bounded_output(
    monkeypatch,
) -> None:
    captured: dict = {}
    compiled = _compile()

    async def fake_blueprint(_episode, _source_text, _bible_context):
        return stages.NarrativeBlueprint.model_validate({
            "episode_no": 1,
            "nodes": [
                {
                    "key": f"n{index}",
                    "source_segment_ids": [f"SRC{index:04d}"],
                    "summary": text,
                    "temporal_domain_key": "present",
                    "time_label": "夜",
                    "time_relation": (
                        "episode_start" if index == 1 else "continuous"
                    ),
                    "location_key": "cafe",
                    "location_label": "咖啡厅",
                    "participants": ["谷言"],
                    "action_logic": text,
                }
                for index, text in enumerate(
                    ["等待", "旧友到达", "危险逼近"],
                    start=1,
                )
            ],
        })

    async def fake_loop(stage, stage_key, prompt, model_cls, _validate, **kwargs):
        captured.update({
            "stage": stage,
            "stage_key": stage_key,
            "prompt": prompt,
            "model_cls": model_cls,
            "max_tokens": kwargs["max_tokens"],
            "prefill": kwargs["prefill"],
        })
        return compiled

    async def fake_review(blueprint, **_kwargs):
        return blueprint

    monkeypatch.setattr(stages, "_run_with_agent_loop", fake_loop)
    monkeypatch.setattr(
        stages,
        "_generate_sharded_narrative_blueprint",
        fake_blueprint,
    )
    monkeypatch.setattr(
        stages,
        "_semantic_review_narrative_blueprint",
        fake_review,
    )
    original_get_setting = stages.get_setting
    monkeypatch.setattr(
        stages,
        "get_setting",
        lambda key: (
            "false" if key == "screenplay_scene_shards_enabled"
            else original_get_setting(key)
        ),
    )
    asyncio.run(stages.generate_screenplay(
        {
            "id": "ep-ir-1",
            "episode_no": 1,
            "title": "雨夜敲门",
            "target_duration_s": 50,
            "hook": "",
            "cliffhanger": "",
            "synopsis": "谷言等来旧友并接过危险线索",
            "authorized_source_chapters": {"chapter-1": SOURCE},
        },
        SOURCE,
        _bible(),
    ))

    assert captured["model_cls"] is ScreenplayGenerationIR
    assert captured["max_tokens"] == stages.SCREENPLAY_IR_MIN_TOKENS == 20480
    assert captured["prefill"]["format_version"] == stages.IR_VERSION
    assert f'"format_version":"{stages.IR_VERSION}"' in captured["prompt"]
    assert '"source_names":["该身份在本集原文中的逐字称谓"]' in captured["prompt"]
    assert '"resulting_state":"该动作完成后新成立的局势' in captured["prompt"]
    assert "所有 SRC 必须至少被一个正文 unit 消费" in captured["prompt"]
    assert "coverage 留空" in captured["prompt"]
    assert "禁止输出 events 或 beats" in captured["prompt"]
    assert "禁止输出 events" in captured["prompt"]
    assert "不要输出 audience_priors" in captured["prompt"]
    assert '"initial_state_fact_ids"' not in captured["prompt"]
    assert "未出场人物" not in captured["prompt"]


def test_baseline_recompiles_durable_ir_without_second_model_call(
    monkeypatch,
) -> None:
    candidate = ScreenplayGenerationIR.model_validate(_ir_payload())
    monkeypatch.setattr(
        stages,
        "_recover_screenplay_ir_candidate",
        lambda _episode_id, **_kwargs: (candidate, "art-ir-recovered"),
    )

    async def forbidden_model_call(*_args, **_kwargs):
        raise AssertionError("durable IR recovery must not call the model")

    monkeypatch.setattr(stages, "_run_with_agent_loop", forbidden_model_call)
    script = asyncio.run(stages.generate_screenplay_baseline(
        {
            "id": "ep-ir-recover",
            "episode_no": 1,
            "title": "雨夜敲门",
            "target_duration_s": 50,
            "authorized_source_chapters": {"chapter-1": SOURCE},
        },
        SOURCE,
        _bible(),
        _prompt="unused because durable IR exists",
    ))

    assert script.id == "ep-ir-recover"
    assert script._source_ir_artifact_id == "art-ir-recovered"
    assert script.narrative_plan is not None


def _participant_delivery_complete_ir_payload(version: str) -> dict:
    payload = _ir_payload()
    payload["format_version"] = version
    for scene in payload["scenes"]:
        for unit in scene["units"]:
            unit["narrative_layer"] = "story"
            unit["event_priority"] = "causal"
            unit["render_policy"] = "standalone"
            unit["participant_deliveries"] = []
    for event in payload["events"]:
        event["narrative_layer"] = "story"
        event["event_priority"] = "causal"
        event["render_policy"] = "standalone"
        event["participant_deliveries"] = []
    return payload


def _persist_recoverable_ir(
    *,
    episode_id: str,
    input_fingerprint: str,
    contract_version: str,
    payload: dict,
) -> tuple[str, str, dict]:
    run_id = evidence_repository.create_run(
        workflow_type="screenplay",
        scope_type="episode",
        scope_id=episode_id,
        input_fingerprint=input_fingerprint,
    )
    step_id = evidence_repository.create_step(run_id, "screenplay.iteration")
    artifact = evidence_repository.create_artifact(
        EvidenceArtifact(
            type="screenplay_generation_ir",
            scope_type="episode",
            scope_id=episode_id,
            status="candidate",
            trust_level="T1",
            content=payload,
            contract_version=contract_version,
            prompt_version=stages.SCREENPLAY_BASELINE_PROMPT_VERSION,
        ),
        step_run_id=step_id,
    )
    return run_id, step_id, artifact


def test_current_ir_serialization_declares_participant_delivery_contract() -> None:
    payload = _participant_delivery_complete_ir_payload(
        "screenplay-generation-ir.v2"
    )

    serialized = ScreenplayGenerationIR.model_validate(payload).model_dump(
        mode="json"
    )

    assert stages.IR_VERSION == "screenplay-generation-ir.v2"
    assert serialized["format_version"] == stages.IR_VERSION
    assert all(
        "participant_deliveries" in unit
        for scene in serialized["scenes"]
        for unit in scene["units"]
    )
    assert all(
        "participant_deliveries" in event
        for event in serialized["events"]
    )


def test_recovery_accepts_legal_current_ir_artifact() -> None:
    episode_id = "ep-ir-contract-v2"
    run_id, step_id, artifact = _persist_recoverable_ir(
        episode_id=episode_id,
        input_fingerprint="ir-contract-v2",
        contract_version="screenplay-generation-ir.v2",
        payload=_participant_delivery_complete_ir_payload(
            "screenplay-generation-ir.v2"
        ),
    )

    with bind_trace(run_id, step_id):
        recovered = stages._recover_screenplay_ir_candidate(episode_id)

    assert recovered is not None
    candidate, artifact_id = recovered
    assert artifact_id == artifact["id"]
    assert candidate.format_version == "screenplay-generation-ir.v2"
    row = db.get_conn().execute(
        "SELECT status,stale_reason FROM artifacts WHERE id=?",
        (artifact["id"],),
    ).fetchone()
    assert tuple(row) == ("candidate", None)


def test_recovery_keeps_structurally_complete_legacy_ir_explicitly_legacy() -> None:
    episode_id = "ep-ir-contract-v1-complete"
    run_id, step_id, artifact = _persist_recoverable_ir(
        episode_id=episode_id,
        input_fingerprint="ir-contract-v1-complete",
        contract_version="screenplay-generation-ir.v1.5",
        payload=_participant_delivery_complete_ir_payload(
            "screenplay-generation-ir.v1.5"
        ),
    )

    with bind_trace(run_id, step_id):
        recovered = stages._recover_screenplay_ir_candidate(episode_id)

    assert recovered is not None
    candidate, artifact_id = recovered
    assert artifact_id == artifact["id"]
    assert candidate.format_version == "screenplay-generation-ir.v1.5"
    assert db.get_conn().execute(
        "SELECT status FROM artifacts WHERE id=?",
        (artifact["id"],),
    ).fetchone()["status"] == "candidate"


def test_recovery_marks_legacy_ir_without_participant_deliveries_stale() -> None:
    episode_id = "ep-ir-contract-v1-incomplete"
    payload = _ir_payload()
    payload["format_version"] = "screenplay-generation-ir.v1.5"
    for scene in payload["scenes"]:
        for unit in scene["units"]:
            unit.pop("participant_deliveries", None)
    for event in payload["events"]:
        event.pop("participant_deliveries", None)
    run_id, step_id, artifact = _persist_recoverable_ir(
        episode_id=episode_id,
        input_fingerprint="ir-contract-v1-incomplete",
        contract_version="screenplay-generation-ir.v1.5",
        payload=payload,
    )

    with bind_trace(run_id, step_id):
        recovered = stages._recover_screenplay_ir_candidate(episode_id)

    assert recovered is None
    row = db.get_conn().execute(
        "SELECT status,stale_reason FROM artifacts WHERE id=?",
        (artifact["id"],),
    ).fetchone()
    assert row["status"] == "stale"
    assert "ARTIFACT_NEEDS_REBUILD" in row["stale_reason"]
    assert "participant_deliveries" in row["stale_reason"]


def test_durable_ir_without_blueprint_hash_uses_strict_runtime_validation() -> None:
    assert stages._screenplay_ir_blueprint_snapshot_matches(
        {},
        "current-blueprint-hash",
    )
    assert stages._screenplay_ir_blueprint_snapshot_matches(
        {"blueprint_hash": "current-blueprint-hash"},
        "current-blueprint-hash",
    )
    assert not stages._screenplay_ir_blueprint_snapshot_matches(
        {"blueprint_hash": "different-blueprint-hash"},
        "current-blueprint-hash",
    )


def test_ir_token_budget_scales_with_source_segments() -> None:
    medium_source = "\n\n".join(
        f"第{index}段。" + "剧情动作" * 20
        for index in range(400)
    )
    very_long_source = "\n\n".join(
        f"第{index}段。" + "剧情动作" * 20
        for index in range(700)
    )

    assert stages.screenplay_ir_token_budget("短章正文") == 20480
    assert stages.screenplay_ir_token_budget(medium_source) == 27392
    assert stages.screenplay_ir_token_budget(very_long_source) == 36864


def test_compact_source_index_packs_short_paragraphs_without_losing_text() -> None:
    source = "\n\n".join(
        f"短段{index}。" for index in range(120)
    )

    legacy = index_source_segments(source)
    compact = index_compact_source_segments(source)

    assert len(legacy) == 120
    assert len(compact) < 5
    assert compact[0].start_offset == 0
    assert compact[-1].end_offset == len(source)
    assert "".join(
        source[item.start_offset:item.end_offset]
        for item in compact
    ).replace("\n", "") == source.replace("\n", "")


def test_structural_front_matter_only_exempts_chapter_heading_and_subtitle() -> None:
    source = "\n\n".join([
        "【第8章】\n第8章",
        "一路风流荡少妇",
        "三个月后，白洁回到家中。",
    ])
    segments = index_source_segments(source)

    assert structural_front_matter_ids(segments) == {
        "SRC0001", "SRC0002",
    }


def test_multi_location_scene_heading_is_detected_structurally() -> None:
    assert scene_heading_has_multiple_locations(
        "【场5】日 / 白洁家、学校办公室"
    )
    assert scene_heading_has_multiple_locations(
        "【场6】夜 / 出租车+白洁家"
    )
    assert not scene_heading_has_multiple_locations(
        "【场7】夜 / 白洁家卧室"
    )


def test_screenplay_document_roundtrip_preserves_body_interleave() -> None:
    screenplay = _compile()
    screenplay.scene_outline[0].previous_scene_exit_state = "上一场等待状态"
    screenplay.scene_outline[0].opening_image = "谷言独坐在门边"
    screenplay.scene_outline[0].agency_contracts = [{
        "actor_id": "谷言",
        "agency_mode": "voluntary",
    }]
    screenplay.full_script_text = screenplay.full_script_text.replace(
        "谷言：再等十分钟。",
        "谷言：再等十分钟。\n谷言说完后把杯子推到桌角。",
    )

    restored = document_to_screenplay(screenplay_to_document(screenplay))

    assert restored.full_script_text == screenplay.full_script_text
    assert (
        restored.scene_outline[0].previous_scene_exit_state
        == "上一场等待状态"
    )
    assert restored.scene_outline[0].opening_image == "谷言独坐在门边"
    assert restored.scene_outline[0].agency_contracts == [{
        "actor_id": "谷言",
        "agency_mode": "voluntary",
    }]
