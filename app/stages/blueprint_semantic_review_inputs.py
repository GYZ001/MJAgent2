"""每轮评审的输入快照（投影 + 提示词 + 指纹）。

从 ``blueprint_semantic_review_reviewer.py`` 再抽一层：那个文件拆出来时是
504 行，超过 500 行的新文件上限。**新文件本来就该达标——基线是给存量欠账用
的，不是给新债用的**（CLAUDE.md「装不下时先想怎么拆，不要先想加基线」），
所以这里不发基线，直接再拆一刀。

放成独立叶子模块而不是并进 ``_round.py``：``_run_blueprint_reviewer`` 把
``_BlueprintReviewRoundInputs`` 当参数收，而 ``_round.py`` 又 import reviewer
——并进去会造成循环。consensus/编排器同样只需要这个类型，指向叶子最省事。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from app.narrative_blueprint import (
    NarrativeBlueprint,
    blueprint_semantic_review_schema,
)
from app.source_excerpt import render_indexed_source
from app.source_facts import source_facts

from .blueprint_semantic_review_projection import _blueprint_semantic_review_projection
from .blueprint_semantic_review_prompt import _blueprint_semantic_review_prompt
from .screenplay_source import _render_screenplay_source


@dataclass
class _BlueprintReviewRoundInputs:
    """一轮语义复审的只读快照——两份独立审稿人调用共享同一份。"""

    review_round: int
    targeted_review: bool
    current_blueprint_hash: str
    projected_node_keys: list[str]
    projected_source: str
    node_reference_contract: dict[str, Any]
    source_reference_contract: dict[str, Any]
    review_schema: dict[str, Any]
    prompt: str


def _blueprint_semantic_review_round_inputs(
    blueprint: NarrativeBlueprint,
    source_text: str,
    review_round: int,
    targeted_review: bool,
) -> _BlueprintReviewRoundInputs:
    current_blueprint_hash = hashlib.sha256(
        json.dumps(
            blueprint.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    projected_blueprint, projected_source, projected_node_keys = (
        _blueprint_semantic_review_projection(blueprint, source_text)
        if targeted_review
        else (
            blueprint.model_dump(mode="json"),
            _render_screenplay_source(render_indexed_source(source_text)),
            [node.key for node in blueprint.nodes],
        )
    )
    node_reference_contract = {
        "contract_version": "blueprint-semantic-node-reference.v1",
        "canonical_nodes": [
            {
                "ordinal": ordinal,
                "identity": node_key,
            }
            for ordinal, node_key in enumerate(
                projected_node_keys,
                start=1,
            )
        ],
    }
    projected_source_ids = list(dict.fromkeys(
        source_id
        for node in blueprint.nodes
        if node.key in set(projected_node_keys)
        for source_id in node.source_segment_ids
    ))
    projected_source_facts = [
        fact
        for fact in source_facts(source_text)
        if fact.source_segment_id in set(projected_source_ids)
    ]
    projected_source_unit_keys = [
        fact.source_unit_key for fact in projected_source_facts
    ]
    source_reference_contract = {
        "contract_version": "blueprint-semantic-source-reference.v1",
        "canonical_source_segment_ids": projected_source_ids,
        "canonical_source_unit_keys": projected_source_unit_keys,
        "structured_source_units": [
            fact.model_dump(mode="json")
            for fact in projected_source_facts
        ],
    }
    review_schema = blueprint_semantic_review_schema(
        projected_node_keys,
        projected_source_ids,
        projected_source_unit_keys,
    )
    prompt = _blueprint_semantic_review_prompt(
        node_reference_contract=node_reference_contract,
        source_reference_contract=source_reference_contract,
        projected_blueprint=projected_blueprint,
        projected_source=projected_source,
        review_schema=review_schema,
    )
    return _BlueprintReviewRoundInputs(
        review_round=review_round,
        targeted_review=targeted_review,
        current_blueprint_hash=current_blueprint_hash,
        projected_node_keys=projected_node_keys,
        projected_source=projected_source,
        node_reference_contract=node_reference_contract,
        source_reference_contract=source_reference_contract,
        review_schema=review_schema,
        prompt=prompt,
    )
