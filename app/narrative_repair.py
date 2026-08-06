"""AI semantic diagnosis and constraint-based repair candidate selection."""
from __future__ import annotations

from collections import Counter
import json
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from app.harness import model_gateway
from app.harness.types import Issue
from app.narrative import index_narrative_plan
from app.repair_router import normalize_strategy
from app.schemas import (
    EpisodeScreenplay,
    Storyboard,
    StoryboardOutline,
    StoryboardOutlineShot,
    extract_json,
)

REPAIR_DIAGNOSIS_VERSION = "narrative-repair-diagnosis.v3"

OutlineExecutorOp = Literal[
    "replace_outline_shot",
    "insert_outline_shot",
    "delete_outline_shot",
    "move_outline_shot",
]
_OUTLINE_EXECUTOR_OPS = {
    "replace_outline_shot",
    "insert_outline_shot",
    "delete_outline_shot",
    "move_outline_shot",
}


class SemanticOutlineTarget(BaseModel):
    """Typed coordinates understood by the bounded outline executor."""

    model_config = ConfigDict(extra="forbid")

    shot_id: str | None = Field(default=None, min_length=1)
    shot_no: int | None = Field(default=None, gt=0)
    after_shot_id: str | None = Field(default=None, min_length=1)
    after_shot_no: int | None = Field(default=None, gt=0)
    to_index: int | None = Field(default=None, ge=0)


class SemanticOutlineOperation(BaseModel):
    """One bounded outline edit proposed from semantic relations.

    ``target`` uses stable ``shot_id`` whenever one exists and may additionally
    carry ``shot_no``, ``after_shot_id`` or ``to_index``.  The operation set is
    structural and story-agnostic; the value is schema-validated before it can
    enter a repair checkpoint.
    """

    # ``op`` is an open semantic intent.  ``executor`` is the bounded runtime
    # capability that makes an unfamiliar intent executable.  Existing clients
    # may keep using a capability name directly as ``op``.
    op: str
    executor: OutlineExecutorOp | None = None
    target: SemanticOutlineTarget = Field(default_factory=SemanticOutlineTarget)
    value: StoryboardOutlineShot | None = None

    def executable_op(self) -> OutlineExecutorOp:
        intent = str(self.op or "").strip()
        if not intent:
            raise ValueError("semantic outline operation has an empty intent")
        if self.executor is not None:
            if intent in _OUTLINE_EXECUTOR_OPS and intent != self.executor:
                raise ValueError(
                    "semantic outline operation conflicts with its executor"
                )
            return self.executor
        if intent in _OUTLINE_EXECUTOR_OPS:
            return cast(OutlineExecutorOp, intent)
        raise ValueError(
            f"semantic operation requires an unavailable executor: {intent}"
        )

    def execution_target(self, executable_op: OutlineExecutorOp) -> dict[str, Any]:
        target = self.target.model_dump(mode="json", exclude_none=True)
        identity_count = sum(
            key in target for key in ("shot_id", "shot_no")
        )
        insertion_count = sum(
            key in target
            for key in ("after_shot_id", "after_shot_no", "to_index")
        )
        if executable_op == "insert_outline_shot":
            if identity_count:
                raise ValueError(
                    "insert_outline_shot target cannot use shot_id/shot_no"
                )
            if insertion_count > 1:
                raise ValueError(
                    "insert_outline_shot requires at most one insertion coordinate"
                )
        elif executable_op == "move_outline_shot":
            if identity_count != 1 or target.get("to_index") is None:
                raise ValueError(
                    "move_outline_shot requires one stable target and to_index"
                )
            if "after_shot_id" in target or "after_shot_no" in target:
                raise ValueError("move_outline_shot received an insertion coordinate")
        else:
            if identity_count != 1:
                raise ValueError(
                    f"{executable_op} requires exactly one stable target"
                )
            if insertion_count:
                raise ValueError(
                    f"{executable_op} received an unrelated position coordinate"
                )
        return target


class SemanticCandidateAssessment(BaseModel):
    strategy: str
    expected_narrative_gain: float = 0.0
    destructive_cost: float = 0.0
    satisfies_gap_test: bool = False
    passes_deletion_test: bool = False
    passes_marginal_gain_test: bool = False
    preserves_invariants: bool = False
    rationale: str = ""
    outline_operations: list[SemanticOutlineOperation] = Field(default_factory=list)


class SemanticRepairDiagnosis(BaseModel):
    diagnosis_id: str
    semantic_gap: str
    affected_shot_nos: list[int] = Field(default_factory=list)
    affected_relation_ids: list[str] = Field(default_factory=list)
    assimilation_task_ids: list[str] = Field(default_factory=list)
    scope: str = "adjacent_window"
    candidate_assessments: list[SemanticCandidateAssessment] = Field(default_factory=list)
    selected_strategy: str = "repair_window"
    selection_reason: str
    unclassified_dimensions: list[dict | str] = Field(default_factory=list)

    def router_payload(self, *, execution_verified: bool = False) -> dict[str, Any]:
        candidate_scores: dict[str, float] = {}
        for item in self.candidate_assessments:
            # Public ``split_shot`` and its executable alias share one score;
            # this is transport normalization, never issue/content routing.
            candidate_scores[normalize_strategy(item.strategy)] = (
                item.expected_narrative_gain - item.destructive_cost
            )
        return {
            **self.model_dump(mode="json"),
            "candidate_scores": candidate_scores,
            "execution_verified": execution_verified,
        }


def _outline_target_index(
    outline: StoryboardOutline,
    target: dict[str, Any],
) -> int:
    shot_id = str(target.get("shot_id") or "").strip()
    if shot_id:
        index = next((
            position
            for position, shot in enumerate(outline.shots)
            if str(shot.shot_id or "").strip() == shot_id
        ), -1)
        if index >= 0:
            return index
        raise KeyError(f"semantic outline target shot_id not found: {shot_id}")
    if target.get("shot_no") is None:
        raise KeyError("semantic outline operation requires shot_id or shot_no")
    shot_no = int(target["shot_no"])
    index = next((
        position
        for position, shot in enumerate(outline.shots)
        if int(shot.shot_no) == shot_no
    ), -1)
    if index < 0:
        raise KeyError(f"semantic outline target shot_no not found: {shot_no}")
    return index


def apply_semantic_outline_operations(
    outline: StoryboardOutline,
    operations: list[SemanticOutlineOperation],
) -> tuple[StoryboardOutline, list[dict[str, Any]]]:
    """Apply bounded, schema-validated operations without story classification."""
    candidate = outline.model_copy(deep=True)
    events: list[dict[str, Any]] = []
    for operation in operations:
        executable_op = operation.executable_op()
        target = operation.execution_target(executable_op)
        event_prefix: dict[str, Any] = {"op": executable_op}
        if operation.op != executable_op:
            event_prefix["intent_op"] = operation.op
        if executable_op == "replace_outline_shot":
            if operation.value is None:
                raise ValueError("replace_outline_shot missing value")
            index = _outline_target_index(candidate, target)
            before_id = candidate.shots[index].shot_id
            candidate.shots[index] = operation.value.model_copy(deep=True)
            events.append({
                **event_prefix,
                "index": index,
                "before_shot_id": before_id,
                "after_shot_id": operation.value.shot_id,
            })
        elif executable_op == "insert_outline_shot":
            if operation.value is None:
                raise ValueError("insert_outline_shot missing value")
            if target.get("after_shot_id"):
                index = _outline_target_index(
                    candidate, {"shot_id": target["after_shot_id"]},
                ) + 1
            elif target.get("after_shot_no") is not None:
                index = _outline_target_index(
                    candidate, {"shot_no": target["after_shot_no"]},
                ) + 1
            elif target.get("to_index") is not None:
                index = max(0, min(len(candidate.shots), int(target["to_index"])))
            else:
                index = len(candidate.shots)
            candidate.shots.insert(index, operation.value.model_copy(deep=True))
            events.append({
                **event_prefix,
                "index": index,
                "after_shot_id": operation.value.shot_id,
            })
        elif executable_op == "delete_outline_shot":
            if len(candidate.shots) <= 1:
                raise ValueError("semantic repair cannot delete the only outline shot")
            index = _outline_target_index(candidate, target)
            removed = candidate.shots.pop(index)
            events.append({
                **event_prefix,
                "index": index,
                "before_shot_id": removed.shot_id,
            })
        elif executable_op == "move_outline_shot":
            index = _outline_target_index(candidate, target)
            moved = candidate.shots.pop(index)
            to_index = max(
                0,
                min(len(candidate.shots), int(target.get("to_index") or 0)),
            )
            candidate.shots.insert(to_index, moved)
            events.append({
                **event_prefix,
                "from_index": index,
                "to_index": to_index,
                "shot_id": moved.shot_id,
            })

    for position, shot in enumerate(candidate.shots, start=1):
        shot.shot_no = position
    stable_ids = [str(shot.shot_id or "").strip() for shot in candidate.shots]
    if any(not shot_id for shot_id in stable_ids):
        raise ValueError("semantic outline operation produced an empty shot_id")
    if len(stable_ids) != len(set(stable_ids)):
        raise ValueError("semantic outline operation produced duplicate shot_id")
    return candidate, events


def _compact_context(
    issues: list[Issue],
    screenplay: EpisodeScreenplay,
    board: Storyboard,
    outline: StoryboardOutline | None,
) -> dict[str, Any]:
    from app.validators import key_line_catalog

    plan = screenplay.narrative_plan
    index = index_narrative_plan(plan) if plan else None
    return {
        "violated_invariants": [
            {
                "code": issue.code or "SEMANTIC_GAP_OTHER",
                "message": issue.message,
                "subject": issue.subject,
                "evidence": issue.evidence,
            }
            for issue in issues
        ],
        "narrative_graph": ({
            "events": [item.model_dump(mode="json") for item in plan.events],
            "actions": [item.model_dump(mode="json") for item in plan.atomic_actions],
            "experience_intents": [item.model_dump(mode="json") for item in plan.experience_intents],
            "assimilation_tasks": [item.model_dump(mode="json") for item in plan.assimilation_tasks],
            "readability_windows": [item.model_dump(mode="json") for item in plan.readability_windows],
            "known_ids": {
                "events": list(index.events),
                "actions": list(index.actions),
                "target_deltas": list(index.deltas),
            },
        } if plan and index else {}),
        "shots": [
            {
                "shot_no": shot.shot_no,
                "shot_id": shot.shot_id,
                "event_ids": shot.event_ids,
                "primary_action_id": shot.primary_action_id,
                "contribution": (
                    shot.shot_contribution.model_dump(mode="json")
                    if shot.shot_contribution else None
                ),
                "audience_paths": [item.model_dump(mode="json") for item in shot.audience_state_paths],
                "duration_s": shot.duration_s,
            }
            for shot in board.shots
        ],
        "outline": outline.model_dump(mode="json") if outline else None,
        "key_line_catalog": key_line_catalog(screenplay),
        "outline_local_hard_errors": (
            _outline_local_hard_errors(outline, screenplay)
            if outline is not None else []
        ),
    }


def _outline_local_hard_errors(
    outline: StoryboardOutline,
    screenplay: EpisodeScreenplay,
) -> list[str]:
    from app.validators import (
        outline_key_line_capacity_errors,
        outline_key_line_speaker_errors,
    )

    return list(dict.fromkeys([
        *outline_key_line_capacity_errors(outline, screenplay),
        *outline_key_line_speaker_errors(outline, screenplay),
    ]))


def _error_code_counts(messages: list[str]) -> Counter[str]:
    from app.evaluations.issues import issue_code

    return Counter(issue_code(message) or message for message in messages)


def validate_semantic_diagnosis(diagnosis: SemanticRepairDiagnosis) -> list[str]:
    errors: list[str] = []
    if len(diagnosis.candidate_assessments) < 2:
        errors.append("语义诊断必须比较至少两个候选，不能把问题码直接映射成唯一修复")
    selected_strategy = normalize_strategy(diagnosis.selected_strategy)
    selected = next((
        item for item in diagnosis.candidate_assessments
        if normalize_strategy(item.strategy) == selected_strategy
    ), None)
    if selected is None:
        errors.append("selected_strategy 没有对应候选评估")
    if selected is not None:
        if len(selected.outline_operations) > 3:
            errors.append("选中候选最多允许 3 个局部大纲操作")
        executable_ops: list[OutlineExecutorOp] = []
        for operation in selected.outline_operations:
            try:
                executable_op = operation.executable_op()
                operation.execution_target(executable_op)
                executable_ops.append(executable_op)
            except ValueError as exc:
                errors.append(str(exc))
                continue
            if (
                executable_op in {"replace_outline_shot", "insert_outline_shot"}
                and operation.value is None
            ):
                errors.append(
                    f"{executable_op} 必须携带完整 StoryboardOutlineShot"
                )
        direct_shot_repairs = {"normalize", "repair_current", "repair_window"}
        if not executable_ops and selected_strategy not in direct_shot_repairs:
            errors.append("开放语义策略必须提供可执行、类型安全的大纲操作")
        if "insert_outline_shot" in executable_ops and not (
            selected.satisfies_gap_test and selected.passes_marginal_gain_test
        ):
            errors.append("增镜候选未通过真实缺口与边际增益测试")
        if "delete_outline_shot" in executable_ops and not (
            selected.passes_deletion_test and selected.preserves_invariants
        ):
            errors.append("删镜候选未通过删除测试与全局不变量保护")
        if "move_outline_shot" in executable_ops and not selected.preserves_invariants:
            errors.append("移镜候选未证明事件 DAG、前置状态与观众交接仍成立")
    if not diagnosis.semantic_gap.strip() or not diagnosis.selection_reason.strip():
        errors.append("语义缺口和选择理由不能为空")
    return errors


def _focus_operation_errors(
    diagnosis: SemanticRepairDiagnosis,
    *,
    focus_shot_no: int | None,
    outline: StoryboardOutline | None = None,
) -> list[str]:
    if focus_shot_no is None:
        return []
    selected_strategy = normalize_strategy(diagnosis.selected_strategy)
    selected = next((
        item
        for item in diagnosis.candidate_assessments
        if normalize_strategy(item.strategy) == selected_strategy
    ), None)
    if selected is None or not selected.outline_operations:
        return []

    allowed = {
        shot_no
        for shot_no in (
            focus_shot_no - 1,
            focus_shot_no,
            focus_shot_no + 1,
        )
        if shot_no > 0
    }
    errors: list[str] = []
    targeted: set[int] = set()
    for operation in selected.outline_operations:
        executable_op = operation.executable_op()
        target = operation.execution_target(executable_op)
        if outline is None:
            if operation.target.shot_no is not None:
                targeted.add(int(operation.target.shot_no))
            if operation.target.after_shot_no is not None:
                targeted.update({
                    int(operation.target.after_shot_no),
                    int(operation.target.after_shot_no) + 1,
                })
            if operation.target.to_index is not None:
                targeted.add(int(operation.target.to_index) + 1)
            continue
        try:
            if executable_op in {
                "replace_outline_shot",
                "delete_outline_shot",
            }:
                targeted.add(
                    int(outline.shots[_outline_target_index(outline, target)].shot_no)
                )
            elif executable_op == "insert_outline_shot":
                if target.get("after_shot_id"):
                    anchor = _outline_target_index(
                        outline, {"shot_id": target["after_shot_id"]},
                    )
                    targeted.update({
                        int(outline.shots[anchor].shot_no),
                        anchor + 2,
                    })
                elif target.get("after_shot_no") is not None:
                    anchor = _outline_target_index(
                        outline, {"shot_no": target["after_shot_no"]},
                    )
                    targeted.update({
                        int(outline.shots[anchor].shot_no),
                        anchor + 2,
                    })
                else:
                    insertion_index = max(
                        0,
                        min(
                            len(outline.shots),
                            int(target.get("to_index", len(outline.shots))),
                        ),
                    )
                    targeted.add(insertion_index + 1)
            else:
                source_index = _outline_target_index(outline, target)
                destination_index = max(
                    0,
                    min(
                        len(outline.shots) - 1,
                        int(target.get("to_index") or 0),
                    ),
                )
                targeted.update({
                    int(outline.shots[source_index].shot_no),
                    destination_index + 1,
                })
        except (KeyError, ValueError) as exc:
            errors.append(f"逐镜局部修复目标无法解析：{exc}")
    outside = sorted(targeted - allowed)
    if outside:
        errors.append(
            f"逐镜局部修复只能触及 focus_shot_no={focus_shot_no} 及相邻镜，"
            f"禁止跨到远端镜头：{outside}"
        )
    if targeted and focus_shot_no not in targeted:
        errors.append(
            f"逐镜局部修复未触及当前失败镜 focus_shot_no={focus_shot_no}"
        )
    return errors


async def diagnose_narrative_repair(
    *,
    episode_id: str,
    issues: list[Issue],
    screenplay: EpisodeScreenplay,
    board: Storyboard,
    outline: StoryboardOutline | None = None,
    focus_shot_no: int | None = None,
    validated_prefix_end: int = 0,
    max_attempts: int = 3,
) -> SemanticRepairDiagnosis:
    """Ask AI to compare general edit operations using semantic relations.

    The prompt contains no story-category/action whitelist.  Unknown semantic
    dimensions are retained in ``unclassified_dimensions`` and may still route
    through ``SEMANTIC_GAP_OTHER``.
    """
    prompt = {
        "task": "诊断叙事缺口并比较多个最小修复候选；问题码只说明不变量，不代表修复动作",
        "context": _compact_context(issues, screenplay, board, outline),
        "repair_focus": {
            "focus_shot_no": focus_shot_no,
            "validated_prefix_end": max(0, int(validated_prefix_end)),
            "rule": (
                "逐镜生成时，focus_shot_no 是当前失败候选；"
                "大于 validated_prefix_end 的 outline.shots 是尚未生成的任务 brief，"
                "其缺少最终 Shot 字段不代表整集已有同类错误"
            ),
        },
        "semantic_intent_contract": (
            "strategy 和 op 是开放语义意图，可根据当前关系自由命名；"
            "不得用 issue code 选定唯一修复"
        ),
        "available_execution_capabilities": [
            "replace_outline_shot", "insert_outline_shot",
            "delete_outline_shot", "move_outline_shot",
        ],
        "non_structural_examples": ["repair_current", "repair_window"],
        "tests": {
            "gap_test": "现有镜头证据是否真的无法让对应观众路径达到目标",
            "deletion_test": "删除候选内容后，理解/情绪/因果/时空是否无损",
            "marginal_gain_test": "候选相对改现镜是否有新增可验证收益",
            "minimality_test": "是否是通过全部不变量的最低破坏方案",
        },
        "output_contract": {
            "diagnosis_id": f"NRD-{episode_id}",
            "semantic_gap": "自由语义描述",
            "affected_shot_nos": [],
            "affected_relation_ids": [],
            "assimilation_task_ids": [],
            "scope": "normalize|current_shot|adjacent_window|structure|multi_shot_structure|human",
            "candidate_assessments": [
                {
                    "strategy": "自由命名的候选语义意图",
                    "expected_narrative_gain": 0.0,
                    "destructive_cost": 0.0,
                    "satisfies_gap_test": False,
                    "passes_deletion_test": False,
                    "passes_marginal_gain_test": False,
                    "preserves_invariants": False,
                    "rationale": "基于关系和证据的理由",
                    "outline_operations": [
                        {
                            "op": "自由命名的语义操作意图",
                            "executor": "当前可用执行能力之一",
                            "target": "按 executor 选择且只选一组定位字段",
                            "value": "新建/替换时输出完整 StoryboardOutlineShot，其他操作为 null",
                        }
                    ],
                }
            ],
            "selected_strategy": "候选操作之一",
            "selection_reason": "为什么最小充分",
            "unclassified_dimensions": [],
        },
        "hard_rules": [
            "至少比较两个候选，不得由 issue code 直接选唯一动作",
            "能改现镜时不增镜；增镜必须通过 gap 与 marginal gain",
            "key_line_catalog 中的 KL* 是逐字必保留合同；只能在相邻镜间重分配，禁止删除、改写或遗漏，且顺序不变",
            "单镜时长必须为 5~10 秒；当必保留台词总字数超过 10 秒容量时，原镜压缩在数学上不可行，结构拆分具有可验证边际收益",
            "选中候选只能包含 1~3 个局部 outline_operations，不得借局部问题重写整集大纲",
            (
                "逐镜失败必须先只修 focus_shot_no；只有关系证据证明相邻镜也必须改变时，"
                "才可扩到最多 3 个明确 shot_no，禁止批量 target、value 数组或改写全部未来 brief"
            ),
            "删除/移动必须证明因果、状态、角色信念、观众路径与铺垫兑现不变量",
            "outline_operations 只能引用当前权威图的稳定 ID；新建/替换节点必须输出完整字段",
            "未预设的 strategy/op 必须绑定当前可用 executor；需要新执行器能力时不得猜测或降级",
            "操作后必须仍满足事件拓扑、状态方程、唯一 owner、deadline、readability window 互指与不重放动作",
            (
                "state_delta_transitions 每项必须使用 "
                "transition_id/basis_type/source_fact_id/target_fact_id/"
                "basis_action_phase_id/custom_basis/reason；"
                "不得输出 from_fact_id、to_fact_id、trigger_action_id、"
                "transition_type 等未声明字段"
            ),
            (
                "未变化的事实只能放 required_state_invariants；"
                "source_fact_id 与 target_fact_id 相同或 actual state delta 为空时，"
                "state_delta_transitions 与 allowed_state_deltas 都必须为空"
            ),
            "无法归类的语义写入 unclassified_dimensions，不得丢弃",
        ],
        "target_contracts": {
            "replace/delete": "shot_id 或 shot_no 二选一",
            "insert": "after_shot_id、after_shot_no、to_index 最多选一；全空表示末尾",
            "move": "shot_id 或 shot_no 二选一，并必须给出 to_index",
        },
        "boundary_state_transition_contract": {
            "transition_id": "本集唯一的结构关系 ID，必填",
            "basis_type": (
                "timeline_change|viewpoint_visibility_change|"
                "spatial_reorientation|action_phase_handoff|other，必填"
            ),
            "source_fact_id": "变化前 fact_id 或 null",
            "target_fact_id": "变化后 fact_id 或 null",
            "basis_action_phase_id": (
                "仅 action_phase_handoff 时引用 phase_id，否则 null"
            ),
            "custom_basis": "仅 other 时说明开放关系，否则 null",
            "reason": "该结构关系为何允许此状态变化，必填",
        },
    }
    prior = ""
    errors: list[str] = []
    baseline_graph_errors: list[str] = []
    baseline_local_errors: list[str] = []
    if outline is not None:
        from app.narrative import validate_storyboard_narrative

        baseline_graph_errors = validate_storyboard_narrative(
            board=None,
            screenplay=screenplay,
            outline=outline,
            complete=True,
            expected_scope_id=episode_id,
        )
        baseline_local_errors = _outline_local_hard_errors(
            outline,
            screenplay,
        )
    baseline_graph_counts = _error_code_counts(baseline_graph_errors)
    for attempt in range(1, max_attempts + 1):
        user = json.dumps(prompt, ensure_ascii=False)
        if errors:
            user += "\n修正下列合同问题：\n- " + "\n- ".join(errors) + "\n上一候选：\n" + prior[:12000]
        prior = await model_gateway.chat(
            [
                {"role": "system", "content": "你是叙事修复诊断导演。只按关系、状态和观众证据判断，只输出 JSON。"},
                {"role": "user", "content": user},
            ],
            temperature=0.2,
            max_tokens=8192,
            call_meta={
                "stage": "narrative_repair_diagnosis",
                "stage_key": "semantic_repair_planner",
                "episode_id": episode_id,
                "repair_round": attempt - 1,
                "contract_version": REPAIR_DIAGNOSIS_VERSION,
            },
        )
        try:
            diagnosis = SemanticRepairDiagnosis.model_validate(extract_json(prior))
        except Exception as exc:  # noqa: BLE001 - LLM boundary
            errors = [f"JSON/Schema 无效：{exc}"]
            continue
        errors = validate_semantic_diagnosis(diagnosis)
        errors.extend(_focus_operation_errors(
            diagnosis,
            focus_shot_no=focus_shot_no,
            outline=outline,
        ))
        selected_strategy = normalize_strategy(diagnosis.selected_strategy)
        selected = next((
            item
            for item in diagnosis.candidate_assessments
            if normalize_strategy(item.strategy) == selected_strategy
        ), None)
        if selected is not None and selected.outline_operations:
            if outline is None:
                errors.append("大纲结构操作缺少当前 StoryboardOutline")
            else:
                try:
                    candidate_outline, _events = apply_semantic_outline_operations(
                        outline,
                        selected.outline_operations,
                    )
                    from app.narrative import validate_storyboard_narrative

                    candidate_graph_errors = validate_storyboard_narrative(
                        board=None,
                        screenplay=screenplay,
                        outline=candidate_outline,
                        complete=True,
                        expected_scope_id=episode_id,
                    )
                    candidate_graph_counts = _error_code_counts(
                        candidate_graph_errors,
                    )
                    regressed_codes = {
                        code
                        for code, count in candidate_graph_counts.items()
                        if count > baseline_graph_counts.get(code, 0)
                    }
                    errors.extend([
                        message
                        for message in candidate_graph_errors
                        if _error_code_counts([message]).keys()
                        & regressed_codes
                    ])
                    candidate_local_errors = _outline_local_hard_errors(
                        candidate_outline,
                        screenplay,
                    )
                    introduced_local_errors = list(
                        (
                            Counter(candidate_local_errors)
                            - Counter(baseline_local_errors)
                        ).elements()
                    )
                    if introduced_local_errors:
                        errors.append(
                            "候选引入了新的必保留台词容量/说话人硬错误："
                            + "；".join(introduced_local_errors[:4])
                        )
                    elif (
                        baseline_local_errors
                        and len(candidate_local_errors)
                        >= len(baseline_local_errors)
                    ):
                        errors.append(
                            "候选没有减少当前大纲的必保留台词容量/说话人硬错误："
                            + "；".join(candidate_local_errors[:4])
                        )
                except Exception as exc:  # noqa: BLE001 - candidate boundary
                    errors.append(f"大纲结构候选无法安全应用：{exc}")
        if not errors:
            return diagnosis
    raise ValueError("叙事修复语义诊断失败：" + "；".join(errors[:6]))


async def route_narrative_issues(
    issues: list[Issue] | list[str],
    *,
    episode_id: str,
    screenplay: EpisodeScreenplay,
    board: Storyboard,
    outline: StoryboardOutline | None = None,
    validated_prefix_end: int = 0,
    next_shot_no: int | None = None,
    issue_fingerprint_counts: dict[str, int] | None = None,
    current_level=None,
    uncommitted_candidate: bool = False,
):
    """Semantic front door used by the live Supervisor.

    If diagnosis or execution validation is unavailable, routing pauses for
    review.  It never substitutes a fixed strategy for an unknown intent.
    """
    from app.evaluations.issues import issue_code
    from app.harness.types import IssueSeverity
    from app.repair_router import route_issues

    normalized = [
        item if isinstance(item, Issue) else Issue(
            code=issue_code(str(item)) or "SEMANTIC_GAP_OTHER",
            severity=IssueSeverity.BLOCKER,
            subject="storyboard",
            message=str(item),
            repairable=True,
        )
        for item in issues
    ]
    if uncommitted_candidate:
        local_plan = route_issues(
            normalized,
            validated_prefix_end=validated_prefix_end,
            next_shot_no=next_shot_no,
            issue_fingerprint_counts=issue_fingerprint_counts,
            current_level=current_level,
            semantic_diagnosis={
                "scope": "current_shot",
                "selected_strategy": "repair_current",
                "selection_reason": (
                    "候选尚未提交，首次修复只重试当前大纲槽位"
                ),
                "execution_verified": True,
            },
        )
        prior_attempts = int(
            (issue_fingerprint_counts or {}).get(local_plan.fingerprint, 0)
        )
        if prior_attempts == 0:
            return local_plan

    diagnosis_payload: dict[str, Any] | None = None
    try:
        diagnosis = await diagnose_narrative_repair(
            episode_id=episode_id,
            issues=normalized,
            screenplay=screenplay,
            board=board,
            outline=outline,
            focus_shot_no=next_shot_no,
            validated_prefix_end=validated_prefix_end,
        )
        diagnosis_payload = diagnosis.router_payload(execution_verified=True)
    except Exception as exc:  # noqa: BLE001 - fail closed at semantic boundary
        diagnosis_payload = {
            "selected_strategy": "semantic_diagnosis_needs_review",
            "candidate_assessments": [],
            "selection_error": str(exc),
            "execution_verified": False,
        }
    return route_issues(
        normalized,
        validated_prefix_end=validated_prefix_end,
        next_shot_no=next_shot_no,
        issue_fingerprint_counts=issue_fingerprint_counts,
        current_level=current_level,
        semantic_diagnosis=diagnosis_payload,
    )
