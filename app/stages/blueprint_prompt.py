"""叙事蓝图分片——分片提示词组装与 provider/结构化操作 ID。"""
from __future__ import annotations

import hashlib
import json
from typing import Any


from app import hiagent
from app.narrative_blueprint import (
    BLUEPRINT_MAX_SOURCE_SEGMENTS_PER_NODE,
    BLUEPRINT_SHARD_POLICY_VERSION,
    BLUEPRINT_VERSION,
    NarrativeBlueprint,
    NarrativeBlueprintShard,
    blueprint_shard_candidate_hash,
    blueprint_shard_provider_schema,
    blueprint_state_subject_issues,
    blueprint_state_subject_ownership_patch_schema,
    render_blueprint_shard_semantic_issue,
)

from .constants import SCREENPLAY_BLUEPRINT_PROMPT_VERSION, SYSTEM_PREFIX


def _blueprint_state_subject_repair_target_keys(
    issues: list[Any],
) -> list[str]:
    return list(dict.fromkeys(
        unit_key
        for issue in issues
        for unit_key in issue.source_unit_keys
    ))


def _blueprint_state_subject_repair_issues(
    candidate: NarrativeBlueprintShard,
    *,
    validation_errors: list[str],
    source_text: str,
) -> list[Any] | None:
    """Select repair-only mode only when typed issues equal all shard errors."""
    issues = blueprint_state_subject_issues(
        NarrativeBlueprint(
            episode_no=candidate.episode_no,
            nodes=candidate.nodes,
        ),
        source_text,
    )
    if (
        not issues
        or any(not issue.source_unit_keys for issue in issues)
        or validation_errors != [
            render_blueprint_shard_semantic_issue(issue)
            for issue in issues
        ]
    ):
        return None
    target_unit_keys = _blueprint_state_subject_repair_target_keys(issues)
    try:
        blueprint_state_subject_ownership_patch_schema(
            candidate,
            target_unit_keys,
            source_text,
        )
    except (TypeError, ValueError):
        return None
    return issues


def _blueprint_state_subject_repair_prompt(
    *,
    previous_candidate: dict[str, Any],
    issues: list[Any],
    source_payload: list[dict[str, Any]],
    source_text: str,
) -> str:
    """Render the bounded attempt-2 ownership map contract."""
    candidate = NarrativeBlueprintShard.model_validate(previous_candidate)
    target_unit_keys = _blueprint_state_subject_repair_target_keys(issues)
    patch_schema = blueprint_state_subject_ownership_patch_schema(
        candidate,
        target_unit_keys,
        source_text,
    )
    facts_by_source = {
        str(source.get("source_segment_id") or ""): list(
            source.get("source_facts") or []
        )
        for source in source_payload
    }
    facts_by_key = {
        str(fact.get("source_unit_key") or ""): fact
        for facts in facts_by_source.values()
        for fact in facts
    }
    target_source_context: dict[str, Any] = {}
    current_claims: dict[str, Any] = {}
    allowed_identities: dict[str, list[str]] = {}
    for unit_key in target_unit_keys:
        fact = facts_by_key[unit_key]
        source_id = str(fact.get("source_segment_id") or "")
        source_facts_for_unit = facts_by_source[source_id]
        fact_index = next(
            index
            for index, value in enumerate(source_facts_for_unit)
            if value.get("source_unit_key") == unit_key
        )
        target_source_context[unit_key] = {
            "source_fact": fact,
            "adjacent_units": source_facts_for_unit[
                max(0, fact_index - 1):fact_index
            ] + source_facts_for_unit[
                fact_index + 1:fact_index + 2
            ],
        }
        current_claims[unit_key] = {
            "single": [
                evidence.identity_key
                for node in candidate.nodes
                for evidence in node.participant_evidence
                if (
                    evidence.usage == "state_subject"
                    and unit_key in evidence.source_unit_keys
                )
            ],
            "joint": [
                list(assignment.identity_keys)
                for node in candidate.nodes
                for assignment in node.state_subject_assignments
                if assignment.source_unit_key == unit_key
            ],
            "environment": any(
                unit_key in node.environment_source_unit_keys
                for node in candidate.nodes
            ),
        }
        owner = next(
            node
            for node in candidate.nodes
            if source_id in node.source_segment_ids
        )
        allowed_identities[unit_key] = list(owner.participants)

    compact = lambda value: json.dumps(  # noqa: E731
        value,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return (
        "仅输出state-subject ownership repair JSON，不得输出完整shard。"
        "repairs必须逐项覆盖schema中repairs.required的全部properties，"
        "不得漏项或增加target。single表示唯一人物主体；joint只用于结构上"
        "不可拆的共同动作；environment只用于无人物状态所有者的环境变化。"
        "identity_keys只能使用对应target的allowed_identities；"
        "不要输出source ids，服务端会从source facts派生。\n"
        f"base_candidate_hash={blueprint_shard_candidate_hash(candidate)}\n"
        f"target_source_facts={compact(target_source_context)}\n"
        f"current_claims={compact(current_claims)}\n"
        f"allowed_identities={compact(allowed_identities)}\n"
        f"schema={compact(patch_schema)}"
    )


def _blueprint_shard_prompt(
    *,
    episode_no: int,
    shard_index: int,
    shard_count: int,
    errors: list[str],
    bible_context: dict[str, Any],
    boundary: dict[str, Any],
    source_payload: list[dict[str, Any]],
    previous_candidate: dict[str, Any] | None = None,
) -> str:
    """Render the complete Blueprint contract without prose duplication."""

    rules = (
        "仅处理target_sources；每个SRC必须整体且只归一个节点，严禁把同一SRC按"
        "source unit、动作、对白、时空或容量拆给多个节点。节点只能在SRC边界拆分，"
        "按源顺序且最多"
        f"{BLUEPRINT_MAX_SOURCE_SEGMENTS_PER_NODE}个连续SRC。每节点把所拥有SRC内的"
        "连续动作压缩为一个核心因果进程和一个因果/情绪转折；仅story节点填exit_state。"
        "首分片首节点time_relation=episode_start；其余严格延续boundary_context。"
        "复用有效fact_key、人物位置、时间域和稳定character_key；本分片新key保持唯一。"
        "participants去重后的identity集合必须与participant_evidence中非空"
        "identity_key集合及exact-unit joint assignment identity_keys的并集完全相等；"
        "每个identity至少有一条来源证据，每条证据的source_unit_keys必须非空且"
        "只引用本节点owned SRC的unit。"
        "participant_evidence一律不要输出source_segment_ids：后端会从"
        "source_unit_keys确定性派生，多写只会浪费输出并引入不一致。"
        "usage=visible只写「在本节点画面中出现、但不是任何action unit的"
        "state_subject、也没有voice」的人物；已经写了state_subject或voice的人物"
        "不要再为同一unit补visible，后端会确定性派生其可感知证据。"
        "修复缺证据时必须保留"
        "原文已有角色并补同identity_key的participant_evidence，禁止删除角色、"
        "合并多个身份或改用默认身份。"
        "每节点显式narrative_layer/event_priority/render_policy。故事画面用"
        "story+causal+standalone；旁文本用paratext+connective+exclude_from_spine。"
        "paratext只保留summary/action_logic/opening_image等文字展示；participants、"
        "participant_evidence、state_subject_assignments、"
        "environment_source_unit_keys、source_unit_deliveries、"
        "state_requirements、state_changes、released_constraints_for必须全为[]，"
        "decision必须null，exit_state必须空字符串。"
        "不得按SRC编号、位置、空人物或词表猜分类。仅story/picture节点的quoted单元逐一给"
        "source_unit_deliveries；quoted_span不等于开口。每条spoken_dialogue/"
        "offscreen_voice delivery除performer_key外，还必须在participant_evidence"
        "追加一条独立对象：identity_key与performer_key相同、usage=\"voice\"、"
        "source_unit_keys只含该delivery的source_unit_key。"
        "performer_key不能替代这条typed voice evidence；每个声音"
        "unit必须恰有一条；written_text、sound_effect、unspoken_reference等非声音"
        "delivery在同一source_unit_key上不得有usage=voice。content_owner可以是"
        "文字、物件或概念的归属，不要求列入participants，也绝不等于performer。"
        "仅story/picture的action单元"
        "不写delivery，但必须精确三选一：单主体动作/思考/反应/发问写唯一"
        "usage=state_subject；结构切分后仍不可拆的共同动作写唯一mode=joint的"
        "state_subject_assignments并列出全部identity_keys；纯环境写"
        "environment_source_unit_keys。主体是执行动作或经历状态变化者，不得把"
        "动作目标、被观察者、同场者或unit中出现的所有姓名默认加入主体；"
        "非人物力量或环境状态作用于人物时归environment，不把受影响人物写成joint。"
        "source-fact unit可能是逗号切开的句法片段，必须结合相邻unit判断共享谓语；"
        "visible、roster和content_owner绝非主体默认值。paratext/audit_only无论原文unit是"
        "quoted还是action，都不适用delivery/state-subject规则。"
        "retry必须以previous_candidate为基线，仅修改validation_errors明确"
        "报错的source_unit_key及对应字段。state_subject_ambiguous中，可拆动作"
        "只保留唯一single state_subject；结构切分后仍不可拆的共同动作必须移除"
        "该unit全部single claims，再建立唯一mode=joint且identity_keys列出全部"
        "有来源共同主体、至少2个。未报错unit的single/joint/environment ownership"
        "必须逐项保持不变，禁止把正确single改成单元素joint。"
        "修复SRC重复owner时必须省略失去来源的冗余node，禁止输出"
        "source_segment_ids=[]的无来源node。"
        "若地点变化发生在两个SRC之间，才可在该SRC边界拆节点；若同一SRC内部跨越"
        "多个主要地点，仍必须整段只归一个节点，location_key/location_label只填写"
        "该SRC核心因果进程实际发生的一个主要地点，移动过程写入transition_cue和"
        "action_logic，禁止复合地点、禁止拆SRC。"
        "state_requirements每条的required_fact_key必须指向一个已建立事实："
        "要么是本分片更早节点state_changes里出现过的fact_key，要么是"
        "boundary_context.active_state_facts里已带入的fact_key；"
        "严禁引用任何没有在这两处建立过的fact_key（会触发"
        "BLUEPRINT_SHARD_FACT_UNKNOWN）。若某状态确实在本集之前就已成立、"
        "本分片无对应state_changes来源，则该requirement必须写assumed_prior=true"
        "并留空required_fact_key，绝不可凭空编造fact_key。supersedes_fact_keys"
        "同理只能指向已建立事实。"
        "所有描述字段简洁，不复述原文或Schema。"
    )
    if any(
        item.get("downstream_semantic_conflicts")
        for item in source_payload
    ):
        rules += (
            "本分片的 target_sources 中带 downstream_semantic_conflicts 的 unit，"
            "上一版蓝图给它的归类在下游场次写作里被两位独立校对员一致判定为无法成立："
            "环境 slot 不得写人物内容，而这些 unit 的源文本身就在讲人物，"
            "或者归属主体与源文语义不符。必须为这些 unit 重新给出成立的归属："
            "若源文讲的是某个人物的状态、反应、动作或内在特质，就把它写进该人物的 "
            "state_subject 证据，不要放进 environment_source_unit_keys；"
            "若它确实只是环境、空间或物件的客观现象，才保留为环境。"
            "不得为了绕过冲突而删除、合并或改写这些 unit 的来源归属之外的结构。"
        )
    compact = lambda value: json.dumps(  # noqa: E731 - local canonical renderer
        value,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return (
        f"生成第{episode_no}集Blueprint分片{shard_index}/{shard_count}。{rules}\n"
        f"validation_errors={compact(errors)}\n"
        f"characters={compact(bible_context)}\n"
        f"boundary_context={compact(boundary)}\n"
        f"previous_candidate={compact(previous_candidate)}\n"
        f"target_sources={compact(source_payload)}\n"
        "schema="
        + compact(blueprint_shard_provider_schema(source_payload))
    )


def _blueprint_provider_operation_id(
    *,
    episode_id: str,
    shard_index: int,
    attempt: int,
    split_depth: int,
    source_hash: str,
    boundary_hash: str,
    prompt: str,
    provider: str,
    model: str,
    max_tokens: int,
    effective_max_tokens: int,
    temperature: float,
    provider_semantic_settings: dict[str, Any],
    stall_epoch: int = 0,
) -> str:
    material = {
        "contract_version": BLUEPRINT_VERSION,
        "prompt_version": SCREENPLAY_BLUEPRINT_PROMPT_VERSION,
        "shard_policy_version": BLUEPRINT_SHARD_POLICY_VERSION,
        "episode_id": episode_id,
        "shard_index": shard_index,
        "attempt": attempt,
        "split_depth": split_depth,
        "source_hash": source_hash,
        "boundary_hash": boundary_hash,
        "provider": provider,
        "model": model,
        "temperature": temperature,
        "provider_semantic_settings": provider_semantic_settings,
        "requested_max_tokens": max_tokens,
        "effective_max_tokens": effective_max_tokens,
        "system_prompt_hash": hashlib.sha256(
            SYSTEM_PREFIX.encode("utf-8")
        ).hexdigest(),
        "prompt_hash": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
    }
    if stall_epoch:
        # Only a retry after a stall carries this key, so the operation ids of
        # every ordinary attempt stay exactly what they were.
        material["stall_epoch"] = int(stall_epoch)
    return "blueprint_" + hashlib.sha256(
        json.dumps(
            material,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:32]


def _blueprint_structured_operation_id(
    *,
    operation_kind: str,
    episode_id: str,
    semantic_input_hash: str,
    ordinal: str,
    messages: list[dict[str, str]],
    output_schema: dict[str, Any],
    requested_max_tokens: int,
    temperature: float,
) -> tuple[str, int]:
    """Fingerprint an exact paid structured request, independent of run id.

    A process/run boundary is not a new semantic operation.  Provider/model
    routing and every setting that changes the outbound payload are included,
    so a legitimate configuration change becomes a new operation instead of
    either reusing or being blocked by an old cached response.
    """
    provider, model, effective_max_tokens = hiagent.text_request_token_limits(
        requested_max_tokens=requested_max_tokens,
    )
    material = {
        "operation_kind": operation_kind,
        "episode_id": episode_id,
        "contract_version": BLUEPRINT_VERSION,
        "prompt_version": SCREENPLAY_BLUEPRINT_PROMPT_VERSION,
        "semantic_input_hash": semantic_input_hash,
        "ordinal": ordinal,
        "provider": provider,
        "model": model,
        "requested_max_tokens": requested_max_tokens,
        "effective_max_tokens": effective_max_tokens,
        "temperature": temperature,
        "provider_semantic_settings": (
            hiagent.text_request_semantic_settings(provider)
        ),
        "messages": messages,
        "output_schema": output_schema,
    }
    digest = hashlib.sha256(
        json.dumps(
            material,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return f"screenplay.blueprint.{operation_kind}:{digest}", effective_max_tokens


def _blueprint_format_repair_reservation_operation_id(
    base_operation_id: str,
) -> str:
    return (
        f"{base_operation_id}:reservation:"
        f"{BLUEPRINT_VERSION}:structured-format-repair"
    )
