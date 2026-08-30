"""_llm_field_patch (the semantic patch planner entry point) and
_llm_field_patch_once (runs one LLM call for it and validates/normalizes
the operations it returns), kept in the same file: _llm_field_patch calls
_llm_field_patch_once directly and _llm_field_patch_once in turn needs
helpers from this package that do not depend back on either of them, so
splitting them into separate files would create a two-file import cycle
(see llm_patch_prompt.py's docstring for the piece that was carved out
instead). _llm_field_patch_once is kept as one function verbatim (moved,
not rewritten) at ~480 lines, pushing this file's longest-function metric
over the usual 200-line target.

Split out of app/production/screenplay_repair.py.
"""
from __future__ import annotations

import json
import re
from app import config
from app.harness.types import Issue
from app.production.patch import PatchOperation
from app.production.structured_issues import issues_from_validator_messages
from app.renderability import DIALOGUE_CHAIN_TURNS_HARD_MAX
from app.schemas import (
    Bible,
    EpisodeScreenplay,
    NARRATIVE_CONTRACT_VERSION,
)
from typing import Any

from .dialogue_chain_repair import (
    _dialogue_chain_replacement_is_local,
    _normalize_character_decision_basis,
    _normalize_dialogue_source_references,
    _source_references_are_grounded,
)
from .gates import (
    MAX_STRATEGY_ATTEMPTS_PER_ISSUE,
    NARRATIVE_PATCH_PLANNER_MAX_OUTPUT_TOKENS,
)
from .issue_selection import (
    _identity_contract_repair_policy,
    _introduced_issue_messages,
    _issue_acceptance_test,
    _target_issue_signature_still_open,
)
from .llm_patch_prompt import _narrative_patch_prompt_context
from .narrative_graph_normalize import _normalize_screenplay_narrative_graph
from .narrative_patch_ops import (
    _candidate_is_executable,
    _candidate_targets_narrative_graph,
    _expand_single_action_event_closure,
    _find_narrative_node,
    _narrative_collection_for_new_node,
    _narrative_collection_for_node,
    _normalize_patch_operation_payload,
    _normalize_top_level_narrative_parent,
    _resolve_dialogue_chain_turn_target,
    _resolve_narrative_patch_owner,
    _try_document_patch_operation,
)
from .patch_planning import (
    _patch_strategy_key,
    _strategy_was_tried,
)
from .preflight import _preflight_document_candidate


async def _llm_field_patch(
    issue: Issue,
    script: EpisodeScreenplay,
    *,
    source_text: str,
    strategy_history: list[str] | None = None,
    episode: dict[str, Any] | None = None,
) -> list[PatchOperation]:
    """Retry rejected or duplicate semantic candidates with explicit feedback."""
    feedback: list[str] = []
    tried = list(strategy_history or [])
    for planner_attempt in range(1, MAX_STRATEGY_ATTEMPTS_PER_ISSUE + 1):
        operations = await _llm_field_patch_once(
            issue,
            script,
            source_text=source_text,
            planner_attempt=planner_attempt,
            rejection_feedback=feedback,
            episode=episode,
        )
        if not operations:
            feedback.append(
                "上一候选未通过本地结构、Schema 或确定性不变量校验。"
                "replace_field.target.id 必须指向直接拥有 path 字段的节点，"
                "不得指向其祖先；请提交不同候选。"
            )
            continue
        strategy_key = _patch_strategy_key(operations)
        if _strategy_was_tried(tried, strategy_key):
            feedback.append(
                f"策略 {strategy_key} 已尝试过；必须提供不同且仍满足验收测试的局部候选。"
            )
            continue
        return operations
    return []


# 修复提示词过去只带 7 个 key（metadata / scene_blocks / dialogue_chains /
# voice_bible / narrative_plan 两跳闭包 / graph 索引 / scope_note），
# 而 ScreenplayDocument 还有 plot_spine、source_coverage、story_events、
# information_ledger 四个顶层字段**完全不在其中**。
# 于是任何落在这四个字段上的 issue，修复模型都是在**盲写**：它看不到自己要改的
# 内容，只能凭消息文本猜。生产 EP1 的 SPINE_ACTION_TURN_DUPLICATE 正是如此，
# 规划器一次补丁都没产出就记了 exhausted。
#
# 这里不把整份字段塞进提示词（plot_spine 有 300+ 条节拍），
# 而是按 issue 自己指明的下标取一个很小的窗口——确定性、有界、且只在
# 该字段真的是本次 issue 的目标时才带上。
async def _llm_field_patch_once(
    issue: Issue,
    script: EpisodeScreenplay,
    *,
    source_text: str,
    planner_attempt: int = 1,
    rejection_feedback: list[str] | None = None,
    episode: dict[str, Any] | None = None,
) -> list[PatchOperation]:
    """Compare semantic candidates, then return one bounded candidate patch.

    New narrative artifacts never map an issue code to an operation.  The AI
    compares at least two relation-level candidates and the selected candidate
    is CAS-applied to an isolated working artifact before full-graph QA.
    """
    from app.harness import model_gateway
    from app.production.screenplay_document import screenplay_to_document
    from app.schemas import extract_json

    document = screenplay_to_document(script)
    prompt_context, source_excerpt = _narrative_patch_prompt_context(
        document,
        issue,
        source_text,
    )
    prompt = {
        "task": "诊断当前剧本叙事关系缺口，比较至少两个最小候选，再选择一个局部候选",
        "planner_attempt": planner_attempt,
        "prior_rejections": list(rejection_feedback or []),
        "issue": issue.model_dump(mode="json"),
        "acceptance_test": _issue_acceptance_test(issue),
        "screenplay_document": prompt_context,
        "authorized_source_excerpt": source_excerpt,
        "identity_contract_policy": _identity_contract_repair_policy(),
        "operation_contract": {
            "op": "使用当前 PatchOperation 协议；每个候选会由生产执行器在副本上探测可执行性",
            "path": "单个现存字段；结构操作留空",
            "target": {
                "kind": "目标节点在当前文档 schema 中的类型；不得按固定类型清单猜测",
                "collection": "narrative_plan 的 schema 列表字段（包括 identity_contracts）；非叙事节点可省略",
                "id": "replace 时必须是直接拥有 path 字段的节点 ID；create_node 时必须是新节点自身 ID",
                "parent_id": "创建嵌套节点时的现存父节点 ID，可省略",
                "parent_field": "父节点中的列表字段，可省略",
                "to_index": "移动/插入位置，可省略",
            },
            "value": "replace 的新字段值或 create 的完整单节点",
            "dialogue_chain_turns": {
                "count": f"1~{DIALOGUE_CHAIN_TURNS_HARD_MAX} 个连续话轮",
                "speaker": "只能使用 voice_bible 或 identity_contracts 已声明的说话人",
                "line": (
                    f"非空且每轮纯文字不得超过 "
                    f"{config.MAX_SPOKEN_CHARS_PER_SHOT} 字"
                ),
                "function": (
                    "只能是 trigger|announcement|question|response|"
                    "decision|statement"
                ),
                "source_text": (
                    "每轮必填，且必须逐字连续存在于 authorized_source_excerpt"
                ),
            },
        },
        "output_contract": {
            "semantic_gap": "自由语义诊断；无法归类时仍需保留",
            "unclassified_dimensions": [],
            "candidate_plans": [{
                "candidate_id": "CANDIDATE-ID",
                "operations": [],
                "satisfies_gap_test": False,
                "passes_deletion_test": False,
                "passes_marginal_gain_test": False,
                "preserves_invariants": False,
                "expected_narrative_gain": 0.0,
                "destructive_cost": 0.0,
                "rationale": "关系、证据和状态理由",
            }],
            "selected_candidate_id": "CANDIDATE-ID",
            "selection_reason": "为什么是最小充分修改",
        },
        "rules": [
            "candidate_plans 至少两个；问题码只描述失败关系，不得决定操作",
            "选中候选必须逐字满足 acceptance_test；修复相邻语义但未消除当前 issue 的候选无效",
            "选中候选只能含 1~3 个局部操作，不得替换根对象或整个集合",
            "replace_field.path 只写目标节点的直接字段名，target.id 必须是该字段所属节点自身 ID，禁止用祖先节点 ID",
            "create_node 的 target.id 必须等于 value 内新节点的稳定 *_id；嵌套创建时 parent_id 指向直接父节点",
            "允许创建/删除/移动单个叙事节点，但必须证明全图引用、DAG、状态、信念和观众路径可恢复",
            "新增必须通过缺口与边际增益测试；删除必须通过删除测试；所有候选必须保持不变量",
            "不得修改现存节点的身份 ID",
            "create/replace 一旦引入新 identity_id、display_name 或非旁白 voice ID，同一候选必须以局部操作创建或补齐完整 identity_contracts 节点及 voice_ids 连接；否则候选无效",
            "修复可以更正身份合同本身，但不得借修复器绕过已有角色圣经或已发布身份合同的 ID 权威",
            "来源证据必须逐字来自 authorized_source_excerpt",
            (
                "替换 dialogue_chain.turns 时，每个 line 的纯文字不得超过 "
                f"{config.MAX_SPOKEN_CHARS_PER_SHOT} 字，"
                "function 只能是 trigger|announcement|question|response|decision|statement；"
                "禁止输出 narration、voiceover、explanation、apology、closing 等其他值"
            ),
            "改写命题不得直接挂原文证据，角色/观众信念不得补入不可感知证据",
            "修复后仍会运行整图 DAG、状态、信念与观众路径全量复验",
        ],
    }
    prompt_json = json.dumps(prompt, ensure_ascii=False)
    raw = await model_gateway.chat(
        [
            {"role": "system", "content": "你是叙事图局部修复器。只输出 JSON，不按题材或剧情关键词判断。"},
            {"role": "user", "content": prompt_json},
        ],
        temperature=0.1,
        max_tokens=NARRATIVE_PATCH_PLANNER_MAX_OUTPUT_TOKENS,
        call_meta={
            "stage": "screenplay_narrative_patch",
            "stage_key": "narrative_graph_patch",
            "call_role": "semantic_patch_planner",
            "contract_version": NARRATIVE_CONTRACT_VERSION,
            "reuse_successful_operation": True,
            "planner_attempt": planner_attempt,
            "prompt_context_chars": len(prompt_json),
            "requested_max_tokens": (
                NARRATIVE_PATCH_PLANNER_MAX_OUTPUT_TOKENS
            ),
        },
    )
    try:
        plan_data = script.narrative_plan.model_dump(mode="json")
        payload = extract_json(raw, repair_unescaped_inner_quotes=True)
        candidates = list(payload.get("candidate_plans") or [])
        if len(candidates) < 2:
            return []
        selected_id = str(payload.get("selected_candidate_id") or "")
        model_selected = next((
            item for item in candidates
            if isinstance(item, dict) and str(item.get("candidate_id") or "") == selected_id
        ), None)
        ordered_candidates = [
            *([model_selected] if isinstance(model_selected, dict) else []),
            *[
                item for item in candidates
                if isinstance(item, dict) and item is not model_selected
            ],
        ]
        for candidate in ordered_candidates:
            if not (
                bool(candidate.get("satisfies_gap_test"))
                and bool(candidate.get("preserves_invariants"))
            ):
                continue
            if not _candidate_targets_narrative_graph(
                candidate,
                plan_data,
                document=document,
            ):
                preflight = _preflight_document_candidate(
                    candidate,
                    document=document,
                    source_text=source_text,
                    issue=issue,
                    episode=episode,
                )
                if preflight:
                    return preflight
        selected = next((
            item for item in ordered_candidates
            if (
                bool(item.get("satisfies_gap_test"))
                and bool(item.get("preserves_invariants"))
                and _candidate_is_executable(item, document)
            )
        ), None)
        if selected is None:
            return []
        selected_id = str(selected.get("candidate_id") or "")
        used_model_selection = selected is model_selected
        raw_ops = list(selected.get("operations") or [])
        if not 1 <= len(raw_ops) <= 3:
            return []
        normalized_ops: list[dict[str, Any]] = []
        for item in raw_ops:
            if not isinstance(item, dict):
                return []
            normalized_ops.append(_normalize_patch_operation_payload(item))
        operations = [
            PatchOperation.model_validate(item) for item in normalized_ops
        ]
    except Exception:  # noqa: BLE001 - model output is untrusted
        return []
    if any(operation.op in {"create_node", "insert_node"} for operation in operations) and not bool(
        selected.get("satisfies_gap_test")
    ):
        return []
    if any(operation.op == "delete_node" for operation in operations) and not bool(
        selected.get("passes_deletion_test")
    ):
        return []

    operations = _expand_single_action_event_closure(
        operations,
        plan_data,
    )
    if len(operations) > 3:
        return []
    safe: list[PatchOperation] = []
    for operation in operations:
        operation.value = _normalize_character_decision_basis(operation.value)
        operation.value = _normalize_dialogue_source_references(
            operation.value,
            source_text,
        )
        direct_patch = _try_document_patch_operation(
            operation,
            document,
            plan_data,
        )
        is_document_patch = direct_patch is not None
        if direct_patch is not None:
            operation, _ = direct_patch
        target = operation.target or {}
        raw_collection = str(target.get("collection") or "").strip()
        collection = re.split(r"[.\[]+", raw_collection, maxsplit=1)[0]
        node_id = str(target.get("id") or "")
        if not is_document_patch and not collection and node_id:
            collection = (
                _narrative_collection_for_node(plan_data, node_id) or ""
            )
        if (
            not is_document_patch
            and
            not collection
            and operation.op in {"create_node", "insert_node"}
            and node_id
            and isinstance(operation.value, dict)
        ):
            collection = (
                _narrative_collection_for_new_node(
                    plan_data,
                    node_id,
                    operation.value,
                )
                or ""
            )
        if operation.op in {"create_node", "insert_node"} and collection:
            target = _normalize_top_level_narrative_parent(
                target,
                collection=collection,
                plan_data=plan_data,
            )
        nodes = None if is_document_patch else plan_data.get(collection)
        if isinstance(nodes, list) and node_id:
            target = {
                **target,
                "kind": "narrative_node",
                "collection": collection,
                "normalized_from_kind": str(target.get("kind") or ""),
            }

            node = _find_narrative_node(nodes, node_id)
            if operation.op == "replace_field":
                patch_field = re.split(
                    r"[./]+", operation.path.strip("/"),
                )[-1]
                resolved_owner = _resolve_narrative_patch_owner(
                    nodes,
                    node_id=node_id,
                    patch_field=patch_field,
                    issue=issue,
                )
                if resolved_owner is None:
                    continue
                node, resolved_node_id = resolved_owner
                if resolved_node_id != node_id:
                    target = {
                        **target,
                        "id": resolved_node_id,
                        "retargeted_from_id": node_id,
                    }
                    node_id = resolved_node_id
                if patch_field.endswith("_id") and str(node.get(patch_field) or "") == node_id:
                    continue
                if patch_field in {"verbatim_excerpt", "source_text"} and str(
                    operation.value or ""
                ) not in source_text:
                    continue
                operation.path = patch_field
                if patch_field == "target_deltas" and isinstance(
                    operation.value, list,
                ):
                    valid_proposition_ids = {
                        str(item.get("proposition_id") or "")
                        for item in (plan_data.get("propositions") or [])
                        if isinstance(item, dict)
                    }
                    operation.value = [
                        {
                            **delta,
                            "proposition_ids": [
                                proposition_id
                                for proposition_id in (
                                    delta.get("proposition_ids") or []
                                )
                                if proposition_id in valid_proposition_ids
                            ],
                        }
                        if isinstance(delta, dict) else delta
                        for delta in operation.value
                    ]
            elif operation.op in {"delete_node", "move_node"} and node is None:
                continue
            elif operation.op in {"create_node", "insert_node"}:
                if node is not None or not isinstance(operation.value, dict):
                    continue
        elif operation.op == "replace_field":
            from app.production.screenplay_document import resolve_field_patch_target

            if not operation.path or operation.path in {"/", "$", "full_script_text"}:
                continue
            patch_field = re.split(
                r"[./]+", operation.path.strip("/"),
            )[-1]
            target = resolve_field_patch_target(
                document,
                path=patch_field,
                target=target,
            )
            chain_id = str(
                target.get("chain_id") or target.get("id") or "",
            ).strip()
            chain = next(
                (
                    item for item in document.dialogue_chains
                    if (item.chain_id or "").strip() == chain_id
                ),
                None,
            )
            if chain is not None and patch_field == "turns":
                if not _dialogue_chain_replacement_is_local(
                    document,
                    chain_id=chain_id,
                    turns=operation.value,
                    source_text=source_text,
                ):
                    continue
                target = {
                    **target,
                    "kind": "dialogue_chain",
                    "id": chain_id,
                    "chain_id": chain_id,
                }
            else:
                resolved_turn = _resolve_dialogue_chain_turn_target(
                    document,
                    target=target,
                    patch_field=patch_field,
                )
                if resolved_turn is not None:
                    target = {
                        **resolved_turn,
                        "kind": "dialogue_chain_turn",
                    }
            operation.path = patch_field
        if not _source_references_are_grounded(operation.value, source_text):
            continue
        selection_evidence = {
            "semantic_gap": payload.get("semantic_gap"),
            "candidate_ids": [item.get("candidate_id") for item in candidates if isinstance(item, dict)],
            "selected_candidate_id": selected_id,
            "selection_reason": (
                payload.get("selection_reason")
                if used_model_selection
                else (
                    "模型首选候选无法由当前 schema 与生产执行器解释；采用首个满足 "
                    f"gap/invariant 且可隔离执行的备选：{selected.get('rationale') or selected_id}"
                )
            ),
            "unclassified_dimensions": payload.get("unclassified_dimensions") or [],
            "expected_narrative_gain": selected.get("expected_narrative_gain"),
            "destructive_cost": selected.get("destructive_cost"),
        }
        operation.target = {**target, "semantic_selection": selection_evidence}
        safe.append(operation)
    if not safe:
        return []
    try:
        from app.production.patch import apply_patch_operation_to_document

        candidate_document = document
        for operation in safe:
            candidate_document, _ = apply_patch_operation_to_document(
                candidate_document,
                operation,
            )
    except Exception as exc:  # noqa: BLE001 - reject an invalid model-authored candidate
        # Distinguish "target didn't resolve to a real node" from a semantic
        # rejection further below, so the next planner attempt retargets
        # instead of re-proposing the same unsatisfied semantics.
        if rejection_feedback is not None:
            rejection_feedback.append(
                "候选操作的 target 未能定位到真实文档节点（结构性失败，"
                f"不是语义未满足）：{type(exc).__name__}: {exc}",
            )
        return []
    try:
        from app.narrative import validate_screenplay_narrative
        from app.production.screenplay_document import document_to_screenplay
        from app.validators import validate_screenplay

        def targeted_errors(candidate: EpisodeScreenplay) -> list[str]:
            errors = validate_screenplay_narrative(candidate, require=True)
            errors.extend(validate_screenplay(
                candidate,
                Bible(
                    characters=[],
                    world={"visual_style_canonical": ""},
                ),
                expected_beats=max(1, len(candidate.scene_outline or [])),
                episode_no=candidate.episode_no,
                source_text=source_text,
                require_dialogue_chains=True,
                validate_narrative=False,
                episode=episode,
            ))
            return errors

        baseline_errors = targeted_errors(document_to_screenplay(document))
        baseline_issues = issues_from_validator_messages(
            baseline_errors,
            subject="screenplay",
            stage="screenplay",
        )
        candidate_script = document_to_screenplay(candidate_document)
        _normalize_screenplay_narrative_graph(
            candidate_script,
            authorized_source_chapters=None,
        )
        candidate_errors = targeted_errors(candidate_script)
    except Exception as exc:  # noqa: BLE001 - local candidate validation must fail closed
        if rejection_feedback is not None:
            rejection_feedback.append(
                f"候选隔离复验失败：{type(exc).__name__}: {exc}",
            )
        return []
    if issue.message in candidate_errors:
        if rejection_feedback is not None:
            rejection_feedback.append(
                "以下候选操作隔离应用后，当前错误仍然存在，说明缺失关系没有被"
                "实际补齐。候选操作="
                + json.dumps(
                    [operation.model_dump(mode="json") for operation in safe],
                    ensure_ascii=False,
                    separators=(",", ":"),
                )[:2400]
                + "；错误="
                + issue.message,
            )
        return []
    candidate_issues = issues_from_validator_messages(
        candidate_errors,
        subject="screenplay",
        stage="screenplay",
    )
    if _target_issue_signature_still_open(issue, candidate_issues):
        if rejection_feedback is not None:
            rejection_feedback.append(
                "候选仅把当前确定性字段错误替换成同一 "
                "code/severity/subject/path/rule 下的另一错误，"
                "目标不变量仍未关闭。"
            )
        return []
    introduced = _introduced_issue_messages(
        baseline_issues,
        candidate_issues,
    )
    if introduced:
        if rejection_feedback is not None:
            rejection_feedback.append(
                "候选引入了新的确定性校验错误："
                + "；".join(introduced[:4]),
            )
        return []
    return safe
