"""叙事蓝图——_repair_narrative_blueprint 修复回路。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
from typing import Any


from app import hiagent
from app.db import get_setting
from app.errors import ContentGenerationError
from app.harness import model_gateway
from app.narrative_blueprint import (
    BLUEPRINT_VERSION,
    NarrativeBlueprint,
    NarrativeBlueprintPatch,
    apply_narrative_blueprint_patch,
    blueprint_patch_schema,
    normalize_blueprint_agency_continuity,
    normalize_blueprint_requirement_state_keys,
    normalize_blueprint_state_subject_perception,
    validate_narrative_blueprint,
    validate_narrative_blueprint_patch_projection,
)
from app.source_excerpt import (
    index_source_segments,
)
from app.source_facts import (
    source_facts,
)

from .blueprint_budget import _BlueprintGenerationBudget
from .blueprint_prompt import _blueprint_structured_operation_id
from .constants import SCREENPLAY_BLUEPRINT_PROMPT_VERSION, SYSTEM_PREFIX
from .ir_snapshot import _current_blueprint_authority_snapshot, _narrative_blueprint_content_hash


async def _repair_narrative_blueprint(
    blueprint: NarrativeBlueprint,
    *,
    episode: dict[str, Any],
    source_text: str,
    additional_errors: list[str] | None = None,
    generation_budget: _BlueprintGenerationBudget | None = None,
) -> NarrativeBlueprint:
    from app.evidence import repository as evidence_repository
    from app.harness.types import EvidenceArtifact
    from app.observability.tracing import current_trace

    parent_artifact_ids: list[str] = []
    pending_external_errors = list(additional_errors or [])
    undelivered_patch_errors: list[str] = []

    def normalize_and_validate() -> list[str]:
        normalize_blueprint_state_subject_perception(blueprint)
        normalize_blueprint_requirement_state_keys(blueprint)
        return validate_narrative_blueprint(blueprint, source_text)

    for round_no in range(1, 7):
        normalize_blueprint_agency_continuity(blueprint)
        errors = normalize_and_validate() + pending_external_errors
        if not errors:
            trace = current_trace()
            evidence_repository.create_artifact(
                EvidenceArtifact(
                    type="screenplay_narrative_blueprint",
                    scope_type="episode",
                    scope_id=str(episode.get("id") or ""),
                    status="validated",
                    trust_level="T1",
                    content=blueprint.model_dump(mode="json"),
                    parent_artifact_ids=parent_artifact_ids,
                    contract_version=BLUEPRINT_VERSION,
                    prompt_version=SCREENPLAY_BLUEPRINT_PROMPT_VERSION,
                    model_snapshot=_current_blueprint_authority_snapshot(
                        source_text,
                        generation_mode="semantic_repair",
                        generation_budget=generation_budget,
                    ),
                ),
                step_run_id=trace.step_run_id,
            )
            return blueprint
        error_text = "\n".join(errors)
        mentioned_keys = {
            node.key
            for node in blueprint.nodes
            if node.key and node.key in error_text
        }
        selected_indexes = {
            neighbor
            for index, node in enumerate(blueprint.nodes)
            if node.key in mentioned_keys
            for neighbor in range(
                max(0, index - 2),
                min(len(blueprint.nodes), index + 3),
            )
        }
        mentioned_source_ids = set(re.findall(
            r"\bSRC\d+\b",
            "\n".join(errors),
        ))
        source_segments = index_source_segments(source_text)
        source_order = {
            segment.segment_id: index
            for index, segment in enumerate(source_segments)
        }
        for source_id in mentioned_source_ids:
            target_position = source_order.get(source_id)
            if target_position is None:
                continue
            nearest_index = min(
                range(len(blueprint.nodes)),
                key=lambda index: min(
                    (
                        abs(
                            source_order[owned_source_id]
                            - target_position
                        )
                        for owned_source_id
                        in blueprint.nodes[index].source_segment_ids
                        if owned_source_id in source_order
                    ),
                    default=len(source_segments),
                ),
            )
            selected_indexes.update(range(
                max(0, nearest_index - 2),
                min(len(blueprint.nodes), nearest_index + 3),
            ))
        open_flashback_depth = 0
        for node in blueprint.nodes:
            if node.time_relation == "flashback_enter":
                open_flashback_depth += 1
            elif node.time_relation == "flashback_exit":
                open_flashback_depth = max(0, open_flashback_depth - 1)
        if not selected_indexes and open_flashback_depth and blueprint.nodes:
            flashback_enter_indexes = [
                index
                for index, node in enumerate(blueprint.nodes)
                if node.time_relation == "flashback_enter"
            ]
            if flashback_enter_indexes:
                enter_index = flashback_enter_indexes[-1]
                selected_indexes.update(range(
                    max(0, enter_index - 1),
                    min(len(blueprint.nodes), enter_index + 2),
                ))
            selected_indexes.update(range(
                max(0, len(blueprint.nodes) - 3),
                len(blueprint.nodes),
            ))
        if not selected_indexes:
            raise ValueError(
                "蓝图错误无法映射到可局部替换的时间线节点："
                + "；".join(errors[:10])
            )
        selected_nodes = [
            blueprint.nodes[index].model_dump(mode="json")
            for index in sorted(selected_indexes)
        ]
        selected_node_keys = [
            str(node["key"]) for node in selected_nodes
        ]
        selected_source_ids = {
            str(source_id)
            for node in selected_nodes
            for source_id in node["source_segment_ids"]
        }
        selected_source_facts = [
            fact.model_dump(mode="json")
            for fact in source_facts(source_text)
            if fact.source_segment_id in selected_source_ids
        ]
        projection_contract = {
            node.key: node.source_semantics().projection_policy
            for node in blueprint.nodes
            if node.key in set(selected_node_keys)
        }
        patch_schema = blueprint_patch_schema(
            blueprint,
            selected_node_keys,
        )
        node_index = [
            {
                "key": node.key,
                "summary": node.summary,
                "time": node.time_label,
                "location": node.location_label,
            }
            for node in blueprint.nodes
        ]
        repair_prompt = (
            "只局部修复叙事蓝图的硬门禁问题，禁止重写整份蓝图。"
            "replacements 中只输出需要修改的完整 node，并使用 node 字段。"
            "每个 replacement 必须与原节点一对一，完整保持 canonical key、"
            "source_segment_ids 的集合与顺序，以及"
            "narrative_layer/event_priority/render_policy 语义三元。"
            "禁止拆分、合并、新增、删除或重排 timeline node；"
            "delete_node_keys 必须为空。"
            "允许修正时间关系、转场、状态事实引用、决定、行为自主性和"
            " released_constraints_for、participant_evidence。每个 replacement node "
            "必须显式保留或修正"
            "除上述 canonical authority 字段外的创作与分场字段。"
            "每个 replacement 必须保持修复前 projection_policy；audit_only "
            "节点与来源只能保留在来源审计，不得改成 story 或放入 scene。"
            "paratext/audit_only replacement 必须把 participants、"
            "participant_evidence、state_subject_assignments、"
            "environment_source_unit_keys、"
            "source_unit_deliveries、state_requirements、state_changes、"
            "released_constraints_for 全部保持为空列表，decision=null，"
            "exit_state=空字符串；标题卡可见文字只写入 summary/"
            "action_logic/opening_image，不得伪造 written_text delivery。"
            "不得修改未列出的节点。原文没有的同谋、"
            "关系、满房、行程或人物动机默认禁止；若为修复原文自身的明确逻辑矛盾"
            "确有必要，必须设 adaptation_kind=logic_bridge，并用 bridge_rationale"
            "说明为何不改变核心事件与结果。已有住宿、车辆、关系等持久事实必须"
            "继续有效，禁止用锁门、系统错误、被占用等新理由让它失效；若剧情需要"
            "临时空间，应在更早的相关节点建立属于其他人物的独立资源，再说明角色"
            "为何临时使用该资源。被迫决定必须建立"
            " constraint_fact_key；只有后续节点用 supersedes_fact_keys 终止"
            "该约束事实后，才能写 released_constraints_for。快感或停止反抗"
            "不能解除威胁。若 setup_missing 涉及原文明确写出的既有关系或人物，"
            "禁止删除、弱化该来源事实；必须在当前节点或更早节点增加可见/可听的"
            "身份与关系建立内容，同节点先建立再引用也有效。\n\n"
            "若硬门禁是 source_delivery 或 voice_identity 问题，只能在保持完整"
            "node、source_segment_ids 顺序和来源语义的前提下，修正"
            "source_unit_deliveries 与 participant_evidence：仅story/picture节点的"
            " projection=quoted "
            "单元必须先根据来源语义明确选择 spoken_dialogue、offscreen_voice、"
            "written_text、sound_effect 或 unspoken_reference。只有声音交付才允许"
            "恰有一个 usage=voice，并用 source_unit_keys 精确引用且与 performer_key "
            "一致；引号只是句法边界，不得自动视为对白。story/picture的"
            " projection=action 正文以及节点的"
            "summary/action_logic 即使写有‘旁白’或‘介绍’，也不得被提升为 dialogue "
            "或伪造 voice evidence；不得删除、拆分、合并或重排节点来规避。\n\n"
            "每个story/picture节点中 projection=action 的 prose source unit 必须三选一："
            "人物思考、反应、发问或动作使用一条 usage=state_subject "
            "participant_evidence，并用 source_unit_keys 精确绑定该 unit；"
            "结构标点切分后仍不可拆的共同动作使用一条 mode=joint 的"
            "state_subject_assignments，identity_keys列出全部共同主体；"
            "真正无人物状态所有者的环境变化才把该 unit 写入 "
            "environment_source_unit_keys。visible 、场次 roster、文本姓名和"
            "content_owner 均不能推断状态主体。repair 只允许补此证据，"
            "不得改 timeline/node/source ownership/audit_only 语义。"
            "paratext/audit_only的quoted/action unit均不适用这两条规则。\n\n"
            "硬门禁：\n"
            + "\n".join(f"- {error}" for error in errors)
            + "\n\n相关节点及前后文：\n"
            + json.dumps(
                selected_nodes,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n\n门禁提到的缺失来源原文：\n"
            + json.dumps(
                [
                    {
                        "source_segment_id": segment.segment_id,
                        "text": segment.text,
                    }
                    for segment in source_segments
                    if segment.segment_id in mentioned_source_ids
                ],
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n\n相关来源的机器结构化 units（唯一 voice 判定权威）：\n"
            + json.dumps(
                selected_source_facts,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n\n全篇节点索引（仅用于引用）：\n"
            + json.dumps(
                node_index,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n\n输出 Schema：\n"
            + json.dumps(
                patch_schema,
                ensure_ascii=False,
            )
        )
        trace = current_trace()
        raw_artifact_ids: list[str] = []

        def record_patch_attempt(attempt: dict[str, Any]) -> None:
            raw_artifact = evidence_repository.create_artifact(
                EvidenceArtifact(
                    type="screenplay_narrative_blueprint_patch_raw",
                    scope_type="episode",
                    scope_id=str(episode.get("id") or ""),
                    status="candidate",
                    trust_level="T0",
                    content={
                        "raw_output": str(attempt.get("raw_response") or ""),
                        "round": round_no,
                        "outcome": str(attempt.get("outcome") or ""),
                        "format_attempt": int(attempt.get("format_attempt") or 0),
                        "semantic_attempt": int(
                            attempt.get("semantic_attempt") or 0
                        ),
                    },
                    contract_version=BLUEPRINT_VERSION,
                    prompt_version=SCREENPLAY_BLUEPRINT_PROMPT_VERSION,
                ),
                step_run_id=trace.step_run_id,
            )
            raw_artifact_ids.append(str(raw_artifact["id"]))

        repair_input_hash = hashlib.sha256(
            json.dumps(
                {
                    "blueprint_hash": _narrative_blueprint_content_hash(blueprint),
                    "errors": errors,
                    "selected_node_keys": [
                        node.get("key") for node in selected_nodes
                    ],
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        requested_max_tokens = 16384
        patch_messages = [
            {"role": "system", "content": SYSTEM_PREFIX},
            {"role": "user", "content": repair_prompt},
        ]
        operation_id, effective_max_tokens = (
            _blueprint_structured_operation_id(
                operation_kind="patch",
                episode_id=str(episode.get("id") or ""),
                semantic_input_hash=repair_input_hash,
                ordinal=str(round_no),
                messages=patch_messages,
                output_schema=patch_schema,
                requested_max_tokens=requested_max_tokens,
                temperature=0.1,
            )
        )
        reservation_id: int | None = None
        remaining_seconds: float | None = None
        legacy_retry_call_id: int | None = None
        if generation_budget is not None:
            legacy_retry_call_id = generation_budget.explicit_retry_call_id(
                "screenplay_blueprint_patch"
            )
            reservation_id = generation_budget.claim(
                max_tokens=effective_max_tokens,
                requested_max_tokens=requested_max_tokens,
                operation_id=operation_id,
            )
            remaining_seconds = generation_budget.remaining_seconds()
        structured_call = model_gateway.chat_structured(
            patch_messages,
            model_type=NarrativeBlueprintPatch,
            validate=lambda value: (
                validate_narrative_blueprint_patch_projection(
                    value,
                    blueprint,
                )
            ),
            operation_id=operation_id,
            temperature=0.1,
            max_tokens=requested_max_tokens,
            # A Blueprint patch participates in the same global call/token
            # budget as its leaves.  An implicit format retry would be a
            # second paid request without a second reservation.
            format_retry_limit=(
                0
                if generation_budget is not None
                else int(get_setting("screenplay_format_retry_limit") or 1)
            ),
            semantic_retry_limit=0,
            call_meta={
                "stage": "剧本蓝图局部语义修复",
                "stage_key": "screenplay_blueprint_patch",
                "call_role": "stage_repair",
                "call_role_label": "蓝图局部语义修复",
                "repair_round": round_no,
                "supersedes_provider_call_id": legacy_retry_call_id,
                "episode_id": str(episode.get("id") or ""),
                "production_grant_id": (
                    generation_budget.retry_grant_id
                    if generation_budget is not None else ""
                ),
                "contract_version": BLUEPRINT_VERSION,
                "expected_json": True,
                "reuse_successful_operation": True,
                "require_cached_successful_operation": bool(
                    generation_budget is not None
                    and operation_id
                    in generation_budget._durable_successful_operations
                ),
                "disable_reasoning_fallback": True,
                "disable_provider_retries": True,
                "disable_provider_candidate_fallback": True,
            },
            repair_context=json.dumps(
                {
                    "replaceable_node_keys": selected_node_keys,
                    "projection_contract": projection_contract,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            output_schema=patch_schema,
            on_attempt=record_patch_attempt,
            usage_callback=(
                None
                if reservation_id is None
                else lambda usage_event: generation_budget.record_usage(
                    reservation_id,
                    usage_event,
                )
            ),
        )
        try:
            patch = (
                await structured_call
                if remaining_seconds is None
                else await asyncio.wait_for(
                    structured_call,
                    timeout=max(0.001, remaining_seconds),
                )
            )
        except hiagent.ProviderError as exc:
            if reservation_id is not None:
                generation_budget.settle(
                    reservation_id,
                    unreported_outcome=(
                        "not_sent"
                        if exc.delivery_state == "not_sent"
                        and exc.replay_safe
                        else "unknown"
                    ),
                )
            raise
        except model_gateway.StructuredFormatError as exc:
            if reservation_id is not None:
                generation_budget.settle(reservation_id)
            # A patch that decoded but failed the schema is an answer the
            # provider actually authored: it keeps the strict one-call rule and
            # must never be re-rolled until it happens to pass.  A response that
            # never decoded into a JSON object at all -- in production the keys
            # degenerated into runs of tabs and spaces -- carries no repair to
            # preserve and is simply a round that was never delivered.  This
            # loop already owns a bounded budget of rounds, each with its own
            # reservation and operation id, so spending the next one is the
            # in-contract answer; aborting the episode on round 1 threw the
            # remaining budgeted rounds away.
            if not getattr(exc, "unparseable", False):
                raise
            undelivered_patch_errors.append(str(exc))
            continue
        except BaseException:
            if reservation_id is not None:
                generation_budget.settle(reservation_id)
            raise
        else:
            if reservation_id is not None:
                generation_budget.settle(reservation_id)
        changed = apply_narrative_blueprint_patch(
            blueprint,
            patch,
            allow_source_expansion=False,
            source_text=source_text,
        )
        if not changed:
            raise ValueError("蓝图局部修复没有替换任何节点")
        pending_external_errors = []
        normalized_artifact = evidence_repository.create_artifact(
            EvidenceArtifact(
                type="screenplay_narrative_blueprint_patch",
                scope_type="episode",
                scope_id=str(episode.get("id") or ""),
                status="validated",
                trust_level="T1",
                content=patch.model_dump(mode="json"),
                parent_artifact_ids=raw_artifact_ids[-1:],
                contract_version=BLUEPRINT_VERSION,
                prompt_version=SCREENPLAY_BLUEPRINT_PROMPT_VERSION,
                model_snapshot={"replaced_nodes": changed},
            ),
            step_run_id=trace.step_run_id,
        )
        parent_artifact_ids = [normalized_artifact["id"]]
        evidence_repository.create_artifact(
            EvidenceArtifact(
                type="screenplay_narrative_blueprint",
                scope_type="episode",
                scope_id=str(episode.get("id") or ""),
                status="candidate",
                trust_level="T1",
                content=blueprint.model_dump(mode="json"),
                parent_artifact_ids=parent_artifact_ids,
                contract_version=BLUEPRINT_VERSION,
                prompt_version=SCREENPLAY_BLUEPRINT_PROMPT_VERSION,
                model_snapshot={
                    "semantic_patch_round": round_no,
                    "remaining_issue_count": len(normalize_and_validate()),
                },
            ),
            step_run_id=trace.step_run_id,
        )

    normalize_blueprint_agency_continuity(blueprint)
    errors = normalize_and_validate()
    if errors:
        raise ContentGenerationError(
            "蓝图局部语义修复六轮后仍未通过："
            + "；".join(errors[:10])
            + (
                f"；其中 {len(undelivered_patch_errors)} 轮未收到可解析的修复响应，"
                f"最近一次：{undelivered_patch_errors[-1]}"
                if undelivered_patch_errors
                else ""
            )
        )
    trace = current_trace()
    evidence_repository.create_artifact(
        EvidenceArtifact(
            type="screenplay_narrative_blueprint",
            scope_type="episode",
            scope_id=str(episode.get("id") or ""),
            status="validated",
            trust_level="T1",
            content=blueprint.model_dump(mode="json"),
            parent_artifact_ids=parent_artifact_ids,
            contract_version=BLUEPRINT_VERSION,
            prompt_version=SCREENPLAY_BLUEPRINT_PROMPT_VERSION,
            model_snapshot=_current_blueprint_authority_snapshot(
                source_text,
                generation_mode="semantic_repair",
                generation_budget=generation_budget,
            ),
        ),
        step_run_id=trace.step_run_id,
    )
    return blueprint
