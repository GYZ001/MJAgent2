"""Prompt and token-budget construction for the scene-shard semantic review
and repair calls: review/repair prompts, response-format wrapping, chunking
of large drafts, and their token budgets.

Moved verbatim from ``app/screenplay_scene_shards.py`` (see
``app/screenplay_scene_shards/__init__.py`` for the full re-export map).
"""
from __future__ import annotations

import json
from app import hiagent
from app.db import get_setting
from copy import deepcopy
from typing import Any

from .common import (
    _hash,
    _setting_int,
)
from .constants import (
    SCREENPLAY_SCENE_JSON_ONLY_SYSTEM_PROMPT,
    SCREENPLAY_SCENE_SEMANTIC_FINDING_CODES,
    SCREENPLAY_SCENE_SEMANTIC_REVIEW_CONTEXT_RESERVE_TOKENS,
    SCREENPLAY_SCENE_SEMANTIC_REVIEW_OUTPUT_RESERVE_PERCENT,
)
from .identity_registry import ScreenplaySceneShardError
from .models import (
    ScreenplaySceneInputContract,
    ScreenplaySceneShardCreativeIR,
    ScreenplaySceneShardSemanticFinding,
    ScreenplaySceneShardSemanticReview,
)
from .provider_schema import _scene_shard_strict_provider_schema
from .review_consensus import (
    _screenplay_scene_semantic_token_estimate,
    screenplay_scene_semantic_repair_required_tokens,
    screenplay_scene_semantic_review_required_tokens,
)
from .scene_prompt import _scene_shard_semantic_authority_payload


def _scene_shard_semantic_review_prompt(
    *,
    draft: ScreenplaySceneShardCreativeIR,
    scene_input_contracts: list[ScreenplaySceneInputContract],
    identity_registry: list[dict[str, Any]],
    unit_keys: list[str] | None = None,
    review_schema: dict[str, Any] | None = None,
) -> str:
    authority_slots, identity_labels = (
        _scene_shard_semantic_authority_payload(
            scene_input_contracts=scene_input_contracts,
            identity_registry=identity_registry,
        )
    )
    projected_unit_keys = (
        list(draft.slots) if unit_keys is None else list(unit_keys)
    )
    projected_authority_slots = {
        unit_key: authority_slots[unit_key]
        for unit_key in projected_unit_keys
        if unit_key in authority_slots
    }
    projected_draft = draft.model_copy(update={
        "slots": {
            unit_key: draft.slots[unit_key]
            for unit_key in projected_unit_keys
        },
    })
    effective_review_schema = (
        review_schema
        if review_schema is not None
        else _scene_shard_semantic_review_schema(projected_unit_keys)
    )
    return (
        # 措辞刻意避开「审查/违规/定罪」这类合规裁决语义：实测本阶段 93 次调用里
        # 有 19 次被供应商以「该问题不符合安全合规要求」拒答，而携带同样故事内容的
        # 场次写作阶段零拒绝——触发的是这段提示词的裁决口吻，不是素材本身。
        # 字段名与枚举值属于合同，一律不动；只把自然语言改回创作校对的说法。
        "你是剧本场次分片的独立一致性校对员。必须逐 slot 穷举核对，不得抽样、"
        "提前停止或只报告部分不一致。逐 slot 对照原始 source_text 与"
        "程序冻结的 exact-unit state_subject/actor/speaker，核对 creative text、"
        "performance、resulting_state 是否把主体 A 改写成主体 B，或加入来源中"
        "不存在/相反的人物行为与反应。不能从姓名词面、visible、scene roster 猜主体；"
        "environment_only 也不能承载人物思考、发问、反应或动作。同一 (unit_key,"
        " code) 的全部适用类型必须放入同一个 violation_kinds 数组，只能从 "
        "wrong_subject、unsupported_action、source_contradiction、"
        "cross_slot_duplication、environment_personification 中选择，不得重复 "
        "kind 或为同一 pair 输出重复 finding；message 必须覆盖数组中的全部 kinds。"
        "每个 finding 都必须显式输出 related_unit_keys。若包含 "
        "cross_slot_duplication，related_unit_keys 必须恰好包含当前核对 payload "
        "内另一个且非自身的 unit_key；它是跨 slot 证据的唯一 typed 引用。"
        "不包含 cross_slot_duplication 时 related_unit_keys 必须为 []。不得引用"
        "当前 payload 外的 slot，跨 chunk 内容不能作为本 finding 的证据。"
        "finding 只能归因到 creative fields 与该 slot 自身 source_fact/冻结权威"
        "发生明确冲突的 slot，不能借用其他 slot 的 source_fact 来判定当前 slot。"
        "跨 slot 重复时，标记最早超出自身来源、或没有自身来源承载该内容的 slot；不得标记后来"
        "正确承载其自身 source_fact 的 slot。若较早 slot 正确、后来 slot 无来源"
        "重复，则标记后来的重复 slot。每个 (unit_key, code) 最多一个 finding，"
        "但必须报告全部明确不一致。不得建议改结构、主体、时间线、source ownership "
        "或 audit。"
        '无问题时只输出合法 JSON 对象 {"findings":[]}。不得输出 Markdown、解释或'
        "任何对象外文本。\n冻结 slot 权威：\n"
        + json.dumps(
            projected_authority_slots,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n冻结身份最小映射：\n"
        + json.dumps(identity_labels, ensure_ascii=False, separators=(",", ":"))
        + "\n待审 creative fields：\n"
        + projected_draft.model_dump_json()
        + "\n完整输出 JSON Schema：\n"
        + json.dumps(
            effective_review_schema,
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )


def _scene_shard_semantic_review_schema(
    unit_keys: list[str],
) -> dict[str, Any]:
    """Bind semantic-review references and cardinality to one exact chunk."""
    known_unit_keys = list(dict.fromkeys(unit_keys))
    if not known_unit_keys:
        raise ValueError("semantic review schema requires at least one unit_key")
    schema = ScreenplaySceneShardSemanticReview.model_json_schema()
    finding_schema = schema["$defs"][
        "ScreenplaySceneShardSemanticFinding"
    ]
    finding_properties = finding_schema["properties"]
    finding_properties["unit_key"]["enum"] = known_unit_keys
    related_schema = finding_properties["related_unit_keys"]
    related_schema["items"]["enum"] = known_unit_keys
    related_schema["maxItems"] = 1
    findings_schema = schema["properties"]["findings"]
    findings_schema["maxItems"] = (
        len(known_unit_keys) * len(SCREENPLAY_SCENE_SEMANTIC_FINDING_CODES)
    )
    return schema


def _scene_shard_semantic_review_response_format(
    review_schema: dict[str, Any],
) -> dict[str, Any]:
    return _scene_shard_strict_response_format(
        name="screenplay_scene_semantic_review",
        local_schema=review_schema,
    )


def _scene_shard_strict_response_format(
    *,
    name: str,
    local_schema: dict[str, Any],
) -> dict[str, Any]:
    provider_schema = _scene_shard_strict_provider_schema(local_schema)
    return {
        "type": "json_schema",
        "json_schema": {
            "name": name,
            "strict": True,
            "schema": provider_schema,
        },
    }


def _scene_shard_semantic_review_budget(
    *,
    unit_keys: list[str],
    review_prompt: str,
) -> dict[str, int | str]:
    provider = hiagent.active_provider("text")
    model = hiagent.active_model("text", provider)
    limits = hiagent.active_model_token_limits(
        provider,
        model,
        get_setting,
    )
    messages = [
        {
            "role": "system",
            "content": SCREENPLAY_SCENE_JSON_ONLY_SYSTEM_PROMPT,
        },
        {"role": "user", "content": review_prompt},
    ]
    input_chars = len(json.dumps(
        messages,
        ensure_ascii=False,
        separators=(",", ":"),
    ))
    input_estimate = _screenplay_scene_semantic_token_estimate(input_chars)
    output_reserve_percent = _setting_int(
        "screenplay_scene_semantic_review_output_reserve_percent",
        SCREENPLAY_SCENE_SEMANTIC_REVIEW_OUTPUT_RESERVE_PERCENT,
        minimum=0,
        maximum=200,
    )
    required = screenplay_scene_semantic_review_required_tokens(
        unit_keys,
        output_reserve_percent=output_reserve_percent,
    )
    context_ceiling = max(
        0,
        int(limits["context_window_tokens"])
        - input_estimate
        - SCREENPLAY_SCENE_SEMANTIC_REVIEW_CONTEXT_RESERVE_TOKENS,
    )
    ceiling = min(
        int(limits["max_output_tokens"]),
        context_ceiling,
    )
    return {
        "provider": provider,
        "model": model,
        "unit_count": len(unit_keys),
        "output_reserve_percent": output_reserve_percent,
        "input_estimate": input_estimate,
        "required": required,
        "ceiling": ceiling,
        "context_window": int(limits["context_window_tokens"]),
        "max_output": int(limits["max_output_tokens"]),
    }


def _scene_shard_semantic_review_chunks(
    *,
    draft: ScreenplaySceneShardCreativeIR,
    scene_input_contracts: list[ScreenplaySceneInputContract],
    identity_registry: list[dict[str, Any]],
    shard_id: str,
) -> list[dict[str, Any]]:
    unit_keys = list(draft.slots)

    def candidate(chunk_unit_keys: list[str]) -> dict[str, Any]:
        review_schema = _scene_shard_semantic_review_schema(
            chunk_unit_keys
        )
        review_prompt = _scene_shard_semantic_review_prompt(
            draft=draft,
            scene_input_contracts=scene_input_contracts,
            identity_registry=identity_registry,
            unit_keys=chunk_unit_keys,
            review_schema=review_schema,
        )
        budget = _scene_shard_semantic_review_budget(
            unit_keys=chunk_unit_keys,
            review_prompt=review_prompt,
        )
        return {
            "unit_keys": chunk_unit_keys,
            "review_prompt": review_prompt,
            "review_schema": review_schema,
            "budget": budget,
            "chunk_hash": _hash({
                "unit_keys": chunk_unit_keys,
                "review_prompt": review_prompt,
            }),
        }

    def fits(value: dict[str, Any]) -> bool:
        budget = value["budget"]
        return int(budget["required"]) <= int(budget["ceiling"])

    def raise_single_unit_budget_error(value: dict[str, Any]) -> None:
        budget = value["budget"]
        raise ScreenplaySceneShardError(
            shard_id,
            [
                "语义审查输出预算不足，provider 调用已阻断："
                f"unit_key={value['unit_keys'][0]}，"
                f"unit_count={budget['unit_count']}，"
                f"required={budget['required']}，"
                f"ceiling={budget['ceiling']}，"
                f"provider={budget['provider']}，"
                f"model={budget['model']}"
            ],
        )

    chunks: list[dict[str, Any]] = []
    start = 0
    while start < len(unit_keys):
        remaining = candidate(unit_keys[start:])
        if fits(remaining):
            chunks.append(remaining)
            break

        single = candidate(unit_keys[start:start + 1])
        if not fits(single):
            raise_single_unit_budget_error(single)

        best = single
        low = 2
        high = len(unit_keys) - start - 1
        while low <= high:
            size = (low + high) // 2
            projected = candidate(unit_keys[start:start + size])
            if fits(projected):
                best = projected
                low = size + 1
            else:
                high = size - 1
        chunks.append(best)
        start += len(best["unit_keys"])
    return chunks


def _scene_shard_reviewer_findings_payload(
    consensus_findings: list[ScreenplaySceneShardSemanticFinding],
    reviews: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    consensus_kinds = {
        (finding.unit_key, finding.code): set(finding.violation_kinds)
        for finding in consensus_findings
    }
    reviewer_findings: list[dict[str, Any]] = []
    for review_payload in reviews:
        review = ScreenplaySceneShardSemanticReview.model_validate(
            review_payload
        )
        for finding in review.findings:
            shared_kinds = consensus_kinds.get(
                (finding.unit_key, finding.code),
                set(),
            )
            if shared_kinds.intersection(finding.violation_kinds):
                reviewer_findings.append(
                    finding.model_dump(mode="json")
                )
    return reviewer_findings


def _scene_shard_semantic_repair_prompt(
    *,
    findings_payload: list[dict[str, Any]],
    reviewer_findings_payload: list[dict[str, Any]],
    frozen_slots: dict[str, dict[str, Any]],
    draft_json: str,
    creative_schema: dict[str, Any],
    identity_labels: dict[str, dict[str, Any]] | None = None,
) -> str:
    del identity_labels
    return (
        "只修复下列 consensus finding 对应 slot 的 creative fields：text、"
        "performance、resulting_state、function、required_text、prop_text、"
        "on_screen_text。必须忠于 source_text 与 exact state_subject/actor/speaker；"
        "不得输出未标记 slot，不得输出或改变任何结构、身份、timeline、"
        "source ownership 或 audit 字段。"
        "修复 environment_personification 时：该 slot 在冻结 slots 中 "
        "environment_only=true，必须删除 text/performance/resulting_state 里所有"
        "人物思考、发问、反应、动作、情绪以及环境拟人化表达，改写为只描写环境、"
        "空间、氛围与无主体客观现象的中性描述，不得保留任何人物主体或让环境替人物"
        "行动。只返回当前标记 slot 的 subset creative "
        "root。\nfindings：\n"
        + json.dumps(
            findings_payload,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\nreviewer_findings：\n"
        + json.dumps(
            reviewer_findings_payload,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n冻结 slots：\n"
        + json.dumps(frozen_slots, ensure_ascii=False, separators=(",", ":"))
        + "\n当前 flagged creative slots：\n"
        + draft_json
        + "\nsubset creative 输出 JSON Schema：\n"
        + json.dumps(
            creative_schema,
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )


def _scene_shard_semantic_repair_subset_schema(
    flagged_unit_keys: list[str],
    *,
    full_creative_schema: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Project one exact repair subset from the plan-bound creative schema."""
    schema = deepcopy(
        full_creative_schema
        if full_creative_schema is not None
        else ScreenplaySceneShardCreativeIR.model_json_schema()
    )
    source_slots = schema.get("properties", {}).get("slots", {})
    source_slot_schemas = source_slots.get("properties")
    if full_creative_schema is not None:
        if not isinstance(source_slot_schemas, dict):
            raise ValueError(
                "semantic repair full creative schema has no bound slots"
            )
        missing_unit_keys = [
            unit_key
            for unit_key in flagged_unit_keys
            if unit_key not in source_slot_schemas
        ]
        if missing_unit_keys:
            raise ValueError(
                "semantic repair subset references unknown schema slots: "
                + ",".join(missing_unit_keys)
            )
        slot_schemas = {
            unit_key: deepcopy(source_slot_schemas[unit_key])
            for unit_key in flagged_unit_keys
        }
    else:
        # Compatibility for direct unit-level callers that do not own a plan.
        # Production generation always supplies the full plan-bound schema.
        slot_schemas = {
            unit_key: {
                "$ref": "#/$defs/ScreenplaySceneShardCreativeUnit",
            }
            for unit_key in flagged_unit_keys
        }
    schema["properties"]["slots"] = {
        "type": "object",
        "properties": slot_schemas,
        "required": flagged_unit_keys,
        "additionalProperties": False,
        "minProperties": len(flagged_unit_keys),
        "maxProperties": len(flagged_unit_keys),
    }
    return schema


def _scene_shard_semantic_repair_budget(
    *,
    draft_json: str,
    repair_prompt: str,
    unit_count: int,
) -> dict[str, int | str]:
    provider = hiagent.active_provider("text")
    model = hiagent.active_model("text", provider)
    limits = hiagent.active_model_token_limits(
        provider,
        model,
        get_setting,
    )
    messages = [
        {
            "role": "system",
            "content": SCREENPLAY_SCENE_JSON_ONLY_SYSTEM_PROMPT,
        },
        {"role": "user", "content": repair_prompt},
    ]
    input_chars = len(json.dumps(
        messages,
        ensure_ascii=False,
        separators=(",", ":"),
    ))
    input_estimate = _screenplay_scene_semantic_token_estimate(input_chars)
    required = screenplay_scene_semantic_repair_required_tokens(
        draft_json=draft_json,
        repair_prompt=repair_prompt,
    )
    context_ceiling = max(
        0,
        int(limits["context_window_tokens"])
        - input_estimate
        - SCREENPLAY_SCENE_SEMANTIC_REVIEW_CONTEXT_RESERVE_TOKENS,
    )
    ceiling = min(
        int(limits["max_output_tokens"]),
        context_ceiling,
    )
    return {
        "provider": provider,
        "model": model,
        "unit_count": unit_count,
        "input": input_estimate,
        "required": required,
        "ceiling": ceiling,
        "context_window": int(limits["context_window_tokens"]),
        "max_output": int(limits["max_output_tokens"]),
    }
