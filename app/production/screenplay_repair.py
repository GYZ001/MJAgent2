"""剧本 Production Repair Agent：Baseline 一次生成后只做局部 Patch。"""
from __future__ import annotations

import hashlib
import json
import re
from difflib import SequenceMatcher
from typing import Any

from app.db import get_conn, get_setting, now
from app.evidence import repository as evidence_repository
from app.harness.types import Evaluation, EvidenceArtifact, Issue
from app.production.grant import assert_grant_allows, issue_production_grant
from app.production.metrics import (
    record_activation,
    record_baseline_generation,
    record_issue_reopened,
)
from app.production.patch import (
    PatchOperation,
    PatchRequest,
    apply_screenplay_patch,
    load_screenplay_from_artifact,
    screenplay_artifact_payload,
)
from app.production.policy import assert_baseline_allowed
from app.production.publish import can_issue_certificate, publish_screenplay
from app.production.revision import (
    ensure_production_revision,
    get_production_revision,
    mark_baseline_generated,
    mark_first_evaluation,
    save_checkpoint,
    update_working_artifact,
)
from app.production.structured_issues import (
    blocker_count,
    enrich_issues,
    issue_set_hash,
    issues_from_validator_messages,
    must_fix_count,
    structured_issue,
)
from app.schemas import Bible, EpisodeScreenplay
from app.renderability import OVERDETAIL_TERMS


MAX_REPAIR_ACTIVATION_PATCHES = 12
MAX_REPAIR_ACTIVATION_PASSES = 32
MAX_STRATEGY_ATTEMPTS_PER_ISSUE = 3
SCREENPLAY_REPAIR_PLANNER_VERSION = "screenplay-repair-10"
NON_WAIVABLE_SCREENPLAY_ISSUE_CODES = frozenset({
    "CHARACTER_IDENTITY_UNRESOLVED",
})


class ScreenplayIdentityGateError(RuntimeError):
    """人物身份未解决时保留可操作的剧本阶段诊断。"""


class ScreenplayNarrativeGateError(RuntimeError):
    """修复耗尽仍未通过硬门禁；工作稿保留，但绝不发布。"""

_SCENE_STORY_FUNCTION_CODES = {
    "SCENE_STORY_FUNCTION_TOO_SHORT",
}
_SCENE_NUMBER_RE = re.compile(r"scene_outline\s*第\s*(\d+)\s*场|/scene_blocks/SC(\d+)", re.I)
_DIALOGUE_SOURCE_MISMATCH_RE = re.compile(
    r"dialogue_chains\[(\d+)\]\.turns\[(\d+)\]\.source_text\s+未在本集原文中找到"
)
_SOURCE_SENTENCE_RE = re.compile(r"[^。！？!?\n]+[。！？!?]?")
_SOURCE_EVIDENCE_STOP_CHARS = set(
    "的一了是在有和与把被就都还又只这那个人我们你他她它说问答"
)


def _eval_id_from_create(evaluation_row: dict[str, Any] | str | None) -> str:
    if isinstance(evaluation_row, dict):
        return str(evaluation_row.get("id") or "")
    return str(evaluation_row or "")


def non_waivable_screenplay_issues(issues: list[Issue]) -> list[Issue]:
    """只有会让下游身份/资产合同失真的问题禁止降级发布。"""
    return [issue for issue in issues if issue.code in NON_WAIVABLE_SCREENPLAY_ISSUE_CODES]


def _strategy_was_tried(entries: list[str], strategy: str) -> bool:
    """Recognize both current keys and legacy keys such as ``rederive:``."""
    return any(
        entry == strategy or entry.startswith(f"{strategy}:")
        for entry in entries
        if not entry.startswith(("fail:", "exhausted"))
    )


def _patch_strategy_key(ops: list[PatchOperation]) -> str:
    op = ops[0]
    kind = str((op.target or {}).get("kind") or "")
    if op.op == "rederive":
        return "rederive"
    if op.op == "normalize_overdetail":
        return "normalize_overdetail"
    if op.op == "split_dialogue_chain_by_scene":
        return f"split_dialogue_chain_{(op.target or {}).get('chain_id') or 'unknown'}"
    if kind == "metadata":
        return f"fill_{op.path}"
    if kind in {"scene", "screenplay_scene"}:
        return f"fill_scene_{op.target.get('id')}_{op.path}"
    if kind == "information" and op.path == "event_id":
        return "fix_ledger_event"
    if kind == "dialogue_chain_turn" and op.path == "source_text":
        return str(
            (op.target or {}).get("strategy")
            or (
                f"fix_dialogue_source_{op.target.get('chain_id')}_"
                f"{op.target.get('turn_index')}"
            )
        )
    if kind == "dialogue_chain_turn" and op.path == "function":
        return (
            f"fix_dialogue_function_{op.target.get('chain_id')}_"
            f"{op.target.get('turn_index')}"
        )
    if op.op == "create_node" and kind == "dialogue_turn":
        return "insert_trigger"
    locator = op.path or str((op.target or {}).get("id") or "")
    return f"{op.op}:{locator}" if locator else op.op


def _strategy_attempt_count(entries: list[str]) -> int:
    return sum(
        1 for entry in entries
        if entry and not entry.startswith(("fail:", "exhausted"))
    )


def run_screenplay_qa(
    script: EpisodeScreenplay,
    *,
    bible: Bible,
    source_text: str,
    episode: dict[str, Any],
    artifact_id: str | None = None,
    artifact_hash: str | None = None,
) -> tuple[list[Issue], Evaluation]:
    from app import config
    from app.narrative import validate_screenplay_narrative
    from app.stages import adaptation_hook_errors
    from app.validators import validate_screenplay
    from app.harness.contracts import get_contract
    from app.production.screenplay_authority import (
        SCREENPLAY_QA_PROFILE_VERSION,
        screenplay_authority_fingerprint,
    )

    authority_error = ""
    try:
        authority_input_fingerprint = screenplay_authority_fingerprint(
            str(episode.get("id") or ""),
            source_text=source_text,
            bible=bible,
            contract_version=get_contract("screenplay").version,
            qa_profile_version=SCREENPLAY_QA_PROFILE_VERSION,
        )
    except ValueError as exc:
        # Isolated unit tests may validate a detached screenplay fixture.  A
        # real persisted episode, however, must never fall back when its source
        # authority disagrees with the QA input.
        if "episode not found" in str(exc):
            fallback_material = {
                "authority_contract": "screenplay-source-authority.v1",
                "episode_id": str(episode.get("id") or ""),
                "source_text": source_text,
                "bible": bible.model_dump(mode="json"),
                "constraints": {
                    key: episode.get(key)
                    for key in (
                        "title", "hook", "cliffhanger", "synopsis",
                        "target_duration_s", "required_dialogue_lines",
                        "required_dialogue_occurrences", "character_resolutions",
                    )
                },
                "contract_version": get_contract("screenplay").version,
                "qa_profile_version": SCREENPLAY_QA_PROFILE_VERSION,
            }
            authority_input_fingerprint = hashlib.sha256(
                json.dumps(
                    fallback_material,
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
        else:
            authority_error = str(exc)
            authority_input_fingerprint = ""

    expected = max(1, int(episode.get("target_duration_s") or 50) // config.VIDEO_DURATION_MIN_S)
    messages = validate_screenplay(
        script, bible, expected,
        episode_no=episode.get("episode_no"),
        source_text=source_text,
        require_dialogue_chains=True,
        required_dialogue_lines=episode.get("required_dialogue_lines") or [],
    )
    source_chapter_contract_present = "source_chapters" in episode
    raw_source_chapters = episode.get("source_chapters") or []
    if isinstance(raw_source_chapters, str):
        try:
            raw_source_chapters = json.loads(raw_source_chapters)
        except (TypeError, ValueError, json.JSONDecodeError):
            raw_source_chapters = []
    authorized_source_chapter_ids = [
        str(value)
        for value in raw_source_chapters
        if str(value or "").strip()
    ]
    messages.extend(
        validate_screenplay_narrative(
            script,
            require=True,
            source_text=source_text,
            expected_scope_id=str(episode.get("id") or script.id or "") or None,
            authorized_source_chapter_ids=(
                authorized_source_chapter_ids
                if source_chapter_contract_present
                else None
            ),
        )
    )
    from app.portraits import (
        screenplay_character_resolution_errors,
        screenplay_unknown_identity_errors,
    )
    identity_messages = screenplay_character_resolution_errors(
        script,
        episode.get("character_resolutions") or [],
    )
    identity_messages.extend(screenplay_unknown_identity_errors(script, bible))
    messages.extend(adaptation_hook_errors(script, episode))
    if authority_error:
        messages.append(f"[SCREENPLAY_SOURCE_AUTHORITY_MISMATCH] {authority_error}")
    issues = enrich_issues(issues_from_validator_messages(
        list(dict.fromkeys(messages)),
        subject="screenplay",
        stage="screenplay",
    ), stage="screenplay", artifact_id=artifact_id)
    issues.extend(
        structured_issue(
            code="CHARACTER_IDENTITY_UNRESOLVED",
            message=message,
            subject="screenplay",
            path="/character_identities",
            rule_id="character_identity_must_resolve_before_publish",
            repairable=True,
            requires_user_input=False,
            must_fix=True,
            artifact_id=artifact_id,
            artifact_hash=artifact_hash,
            stage="screenplay",
        )
        for message in dict.fromkeys(identity_messages)
    )
    for issue in issues:
        if artifact_hash:
            issue.evidence["artifact_hash"] = artifact_hash
        if issue.code == "KEY_LINE_MISSING" and "dialogue_chains 共" in issue.message:
            issue.evidence["required_dialogue_lines"] = list(
                episode.get("required_dialogue_lines") or []
            )
    score = 100.0 if not issues else max(0.0, 100.0 - 10.0 * len(issues))
    try:
        pass_score = min(100.0, max(0.0, float(get_setting("screenplay_qa_pass_score") or 80)))
    except (TypeError, ValueError):
        pass_score = 80.0
    passed = blocker_count(issues) == 0 and must_fix_count(issues) == 0 and score >= pass_score
    status = "passed" if passed else "failed"
    hard_identity_issues = non_waivable_screenplay_issues(issues)
    narrative_gate = script.narrative_plan is not None
    evaluation_role = (
        "business_safety" if hard_identity_issues
        else "runtime_gate" if narrative_gate
        else "score_only"
    )
    runtime_blocking = bool(hard_identity_issues or narrative_gate)
    evaluation = Evaluation(
        evaluator_type="deterministic",
        evaluator_name="screenplay_production_qa",
        evaluator_version="screenplay-qa-gate-2",
        status=status,
        hard_gate_passed=passed,
        evaluation_role=evaluation_role,
        score_status="scored",
        runtime_blocking=runtime_blocking,
        retry_eligible=bool(issues),
        score=score,
        issues=issues,
        evidence={
            "artifact_id": artifact_id,
            "artifact_hash": artifact_hash,
            "authority_input_fingerprint": authority_input_fingerprint,
            "blocker_count": blocker_count(issues),
            "must_fix_count": must_fix_count(issues),
            "evaluation_role": evaluation_role,
            "runtime_blocking": runtime_blocking,
            "pass_score": pass_score,
            "verdict": "passed" if passed else "repair_or_needs_review",
        },
    )
    return issues, evaluation


def plan_screenplay_patch(
    issue: Issue,
    script: EpisodeScreenplay,
    *,
    source_text: str = "",
    strategy_history: dict[str, list[str]] | None = None,
) -> list[PatchOperation]:
    """最小范围 Patch 规划（确定性优先，避免整对象替换）。"""
    history = strategy_history or {}
    fp = issue.fingerprint
    tried = list(history.get(fp) or [])
    path = str((issue.evidence or {}).get("path") or "")
    code = issue.code
    related = list((issue.evidence or {}).get("related_node_ids") or [])

    ops: list[PatchOperation] = []

    # 场级字段必须直接 Patch 源节点。rederive 只会重建投影，无法修复源字段。
    if (
        code in _SCENE_STORY_FUNCTION_CODES
        or "story_function" in path
        or "story_function" in (issue.message or "")
    ):
        scene_id, scene = _scene_from_issue(issue, script)
        strategy = f"fill_scene_{scene_id}_story_function" if scene_id else ""
        if scene is not None and strategy and not _strategy_was_tried(tried, strategy):
            value = _derive_scene_story_function(scene)
            if value and value != (scene.story_function or "").strip():
                return [PatchOperation(
                    op="replace_field",
                    path="story_function",
                    value=value,
                    target={"kind": "scene", "id": scene_id},
                )]

    # S0：派生字段问题 → rederive
    if code in {"FORMAT_CONTRACT_INVALID"} and "scene_outline" in (issue.message or ""):
        if not _strategy_was_tried(tried, "rederive"):
            return [PatchOperation(op="rederive")]

    # S1：戏剧契约单字段
    for field in ("stakes", "obstacle", "protagonist_goal", "dramatic_question"):
        if field in path or field in (issue.message or "") or (
            code == "DRAMATIC_CONTRACT_INCOMPLETE" and field in (issue.message or "").lower()
        ):
            strategy = f"fill_{field}"
            if _strategy_was_tried(tried, strategy):
                continue
            value = _heuristic_fill_dramatic_field(field, script)
            if value:
                return [PatchOperation(
                    op="replace_field",
                    path=field,
                    value=value,
                    target={"kind": "metadata", "id": field},
                )]

    # ledger event_id
    if code == "LEDGER_INVALID" or "event_id" in (issue.message or ""):
        info_id = next((n for n in related if n.startswith("I")), "")
        event_ids = [e.event_id for e in (script.events or []) if e.event_id]
        if info_id and event_ids and not _strategy_was_tried(tried, "fix_ledger_event"):
            return [PatchOperation(
                op="replace_field",
                path="event_id",
                value=event_ids[0],
                target={"kind": "information", "id": info_id},
            )]

    # missing dramatic fields from message patterns
    if code == "DRAMATIC_CONTRACT_INCOMPLETE":
        msg = issue.message or ""
        for field in ("stakes", "obstacle", "protagonist_goal", "dramatic_question"):
            if field in msg:
                value = _heuristic_fill_dramatic_field(field, script)
                if value:
                    return [PatchOperation(
                        op="replace_field",
                        path=field,
                        value=value,
                        target={"kind": "metadata", "id": field},
                    )]

    # 普通话轮的原文依据必须按报错索引精确修复。模型偶尔会写入
    # “原文叙述转为对白”之类说明性占位词；此时只能替换为本集原文中的
    # 可核验句子，不能误改开场话轮，也不能用占位词绕过 SOURCE_FIDELITY。
    mismatch = _DIALOGUE_SOURCE_MISMATCH_RE.search(issue.message or "")
    if code == "SOURCE_FIDELITY" and mismatch and source_text:
        chain_index, turn_index = map(int, mismatch.groups())
        turn_ref = _dialogue_turn_at(script, chain_index, turn_index)
        if turn_ref is not None:
            chain, turn = turn_ref
            strategy = f"fix_dialogue_source_{chain.chain_id}_{turn_index}"
            if not _strategy_was_tried(tried, strategy):
                evidence = _best_source_evidence_for_turn(
                    script,
                    chain_index=chain_index,
                    turn_index=turn_index,
                    source_text=source_text,
                )
                if evidence and evidence != (turn.source_text or "").strip():
                    return [PatchOperation(
                        op="replace_field",
                        path="source_text",
                        value=evidence,
                        target={
                            "kind": "dialogue_chain_turn",
                            "id": f"{chain.chain_id}-T{turn_index + 1}",
                            "chain_id": chain.chain_id,
                            "turn_index": turn_index,
                            "strategy": strategy,
                        },
                    )]

    # 原文开场对白锚点：只处理明确的开场锚点错误。
    if (
        code == "SOURCE_FIDELITY"
        and "原文开场第一句对白未作为" in (issue.message or "")
        and not _strategy_was_tried(tried, "fix_opening_source_anchor")
    ):
        opening = _opening_anchor_from_issue(issue.message or "")
        if opening and script.dialogue_chains and script.dialogue_chains[0].turns:
            chain_id = script.dialogue_chains[0].chain_id
            return [PatchOperation(
                op="replace_field",
                path="source_text",
                value=opening,
                target={
                    "kind": "dialogue_chain_turn",
                    "id": f"{chain_id}-T1",
                    "chain_id": chain_id,
                    "turn_index": 0,
                    "strategy": "fix_opening_source_anchor",
                },
            )]

    # 模型把同一人物的连续自语误标成 response 时，只改结构标签，不改台词。
    response_match = re.search(
        r"dialogue_chains\[(\d+)\]\.turns\[(\d+)\]\s*是\s*response",
        issue.message or "",
    )
    if code == "KEY_LINE_MISSING" and response_match:
        chain_index, turn_index = map(int, response_match.groups())
        if 0 <= chain_index < len(script.dialogue_chains or []):
            chain = script.dialogue_chains[chain_index]
            turns = chain.turns or []
            if 0 <= turn_index < len(turns):
                strategy = f"fix_dialogue_function_{chain.chain_id}_{turn_index}"
                if not _strategy_was_tried(tried, strategy):
                    return [PatchOperation(
                        op="replace_field",
                        path="function",
                        value="statement",
                        target={
                            "kind": "dialogue_chain_turn",
                            "id": f"{chain.chain_id}-T{turn_index + 1}",
                            "chain_id": chain.chain_id,
                            "turn_index": turn_index,
                        },
                    )]

    # 同一对白链被正文场次切开：按台词的实际场次拆成多条完整链。
    # 只修结构归属，不移动正文，比 rederive 更小且能真正消除问题。
    cross_scene_match = re.search(
        r"dialogue_chains\[(\d+)\]\s*被拆到多个场次",
        issue.message or "",
    )
    if code == "KEY_LINE_MISSING" and cross_scene_match:
        chain_index = int(cross_scene_match.group(1))
        if 0 <= chain_index < len(script.dialogue_chains or []):
            chain = script.dialogue_chains[chain_index]
            strategy = f"split_dialogue_chain_{chain.chain_id}"
            if not _strategy_was_tried(tried, strategy):
                return [PatchOperation(
                    op="split_dialogue_chain_by_scene",
                    target={"kind": "dialogue_chain", "id": chain.chain_id,
                            "chain_id": chain.chain_id},
                )]

    # key_lines / full_script_text 等派生投影才允许 rederive。
    if code == "KEY_LINE_MISSING" and not _strategy_was_tried(tried, "rederive"):
        return [PatchOperation(op="rederive")]

    # 对白链触发：尝试在首场插入 trigger turn
    if code == "KEY_LINE_MISSING" and not _strategy_was_tried(tried, "insert_trigger"):
        scene_id = next((n for n in related if n.startswith("SC")), None)
        if not scene_id and script.scene_outline:
            scene_id = f"SC{int(script.scene_outline[0].scene_no):02d}"
        line = _extract_quoted_fragment(issue.message or "")
        if scene_id and line:
            return [PatchOperation(
                op="create_node",
                target={"kind": "dialogue_turn", "scene_id": scene_id},
                value={
                    "chain_id": "DC_FIX",
                    "speaker": _guess_speaker(line, script),
                    "line": line,
                    "function": "trigger",
                    "source_text": line,
                },
            )]

    # 可拍性细节词不值得再次调用模型；只清理画面描述字段，保留对白和原文证据。
    if code == "OVERDETAIL" and not _strategy_was_tried(tried, "normalize_overdetail"):
        terms = [term for term in OVERDETAIL_TERMS if term in (issue.message or "")]
        if terms:
            return [PatchOperation(
                op="normalize_overdetail",
                target={"kind": "renderability_text"},
                value={"terms": terms},
            )]

    return ops


def _heuristic_fill_dramatic_field(field: str, script: EpisodeScreenplay) -> str:
    spine = script.plot_spine
    premise = (spine.episode_premise if spine else "") or script.episode_premise or script.logline or ""
    ending = (spine.must_keep_ending if spine else "") or script.ending_hook or ""
    if field == "stakes":
        if script.stakes.strip():
            return ""
        base = premise or ending or script.title
        return f"若失败将无法推进「{base[:40]}」，失去本集目标与立场。"
    if field == "obstacle":
        if script.obstacle.strip():
            return ""
        return f"外部阻力与内心犹豫阻碍实现：{premise[:60] or script.title}"
    if field == "protagonist_goal":
        if script.protagonist_goal.strip():
            return ""
        who = ""
        if spine and spine.spine_beats:
            who = spine.spine_beats[0].who
        return f"{who or '主角'}完成本集目标：{premise[:60] or ending[:60]}"
    if field == "dramatic_question":
        if script.dramatic_question.strip():
            return ""
        return f"主角能否在阻力下完成：{premise[:50] or script.title}？"
    return ""


def _scene_from_issue(issue: Issue, script: EpisodeScreenplay) -> tuple[str, Any | None]:
    evidence = issue.evidence or {}
    candidates = [
        str(node).upper()
        for node in (evidence.get("related_node_ids") or [])
        if re.fullmatch(r"SC\d+", str(node), re.I)
    ]
    if not candidates:
        text = f"{evidence.get('path') or ''} {issue.message or ''}"
        match = _SCENE_NUMBER_RE.search(text)
        if match:
            number = int(match.group(1) or match.group(2))
            candidates.append(f"SC{number:02d}")
    if not candidates:
        return "", None
    scene_id = candidates[0]
    scene_no = int(scene_id[2:])
    scene = next(
        (item for item in (script.scene_outline or []) if int(item.scene_no) == scene_no),
        None,
    )
    return scene_id, scene


def _derive_scene_story_function(scene: Any) -> str:
    """从本场已有事实确定性补全功能描述，不引入新剧情。"""

    def compact(value: Any, limit: int) -> str:
        text = re.sub(r"\s+", "", str(value or "")).strip("，。；;：:、 ")
        return text[:limit].rstrip("，。；;：:、 ")

    # 场功能是短段元数据，不值得为省几个字截成“情绪从”这类半句。
    # 这里的宽限只防御模型异常长输入，正常的冲突与转折应完整保留。
    conflict = compact(getattr(scene, "conflict", ""), 48)
    turn = compact(getattr(scene, "turn", ""), 48)
    summary = compact(getattr(scene, "summary", ""), 64)
    heading = compact(getattr(scene, "scene_heading", ""), 16)
    if conflict and turn:
        return f"呈现{conflict}，推动{turn}"
    if summary and turn:
        return f"呈现{summary}，推动{turn}"
    if summary:
        return f"呈现{summary}并推进本场局势"
    if heading:
        return f"承接{heading}场景并推动本场局势变化"
    return "推动本场核心冲突并形成后续状态变化"


def _extract_quoted_fragment(message: str) -> str:
    for pattern in (r"[「『]([^」』]+)[」』]", r"[“\"]([^”\"]+)[”\"]", r"：([^\s；;]{2,40})"):
        m = re.search(pattern, message)
        if m:
            return m.group(1).strip()
    return ""


def _opening_anchor_from_issue(message: str) -> str:
    match = re.search(
        r"原文开场第一句对白未作为\s+dialogue_chains\[0\]\.turns\[0\]"
        r"[：:]\s*(.+?)(?:；|;|$)",
        message,
    )
    return match.group(1).strip() if match else ""


def _dialogue_turn_at(
    script: EpisodeScreenplay,
    chain_index: int,
    turn_index: int,
):
    chains = script.dialogue_chains or []
    if not 0 <= chain_index < len(chains):
        return None
    chain = chains[chain_index]
    turns = chain.turns or []
    if not 0 <= turn_index < len(turns):
        return None
    return chain, turns[turn_index]


def _source_sentence_candidates(source_text: str) -> list[str]:
    candidates: list[str] = []
    for paragraph in re.split(r"\n+", source_text or ""):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        sentences = [
            match.group(0).strip()
            for match in _SOURCE_SENTENCE_RE.finditer(paragraph)
            if match.group(0).strip()
        ]
        for index, sentence in enumerate(sentences):
            candidates.append(sentence)
            if index + 1 < len(sentences):
                candidates.append(f"{sentence}{sentences[index + 1]}")
    return list(dict.fromkeys(candidates))


def _source_evidence_score(candidate: str, target: str, context: str) -> float:
    compact_candidate = re.sub(r"\W+", "", candidate)
    compact_target = re.sub(r"\W+", "", target)
    compact_context = re.sub(r"\W+", "", context)
    if not compact_candidate or not compact_target:
        return 0.0

    meaningful_target = {
        char for char in compact_target
        if char not in _SOURCE_EVIDENCE_STOP_CHARS
    }
    meaningful_overlap = meaningful_target & set(compact_candidate)
    if len(meaningful_overlap) < 2:
        return 0.0

    target_bigrams = {
        compact_target[index:index + 2]
        for index in range(max(0, len(compact_target) - 1))
    }
    candidate_bigrams = {
        compact_candidate[index:index + 2]
        for index in range(max(0, len(compact_candidate) - 1))
    }
    context_bigrams = {
        compact_context[index:index + 2]
        for index in range(max(0, len(compact_context) - 1))
    }
    target_coverage = (
        len(target_bigrams & candidate_bigrams) / len(target_bigrams)
        if target_bigrams else 0.0
    )
    context_coverage = (
        len(context_bigrams & candidate_bigrams) / len(context_bigrams)
        if context_bigrams else 0.0
    )
    sequence = SequenceMatcher(None, compact_target, compact_candidate).ratio()
    char_coverage = len(meaningful_overlap) / max(1, len(meaningful_target))
    length_penalty = min(0.2, max(0, len(compact_candidate) - 100) / 500)
    return (
        target_coverage * 5.0
        + char_coverage * 2.0
        + sequence
        + context_coverage * 0.75
        - length_penalty
    )


def _best_source_evidence_for_turn(
    script: EpisodeScreenplay,
    *,
    chain_index: int,
    turn_index: int,
    source_text: str,
) -> str:
    turn_ref = _dialogue_turn_at(script, chain_index, turn_index)
    if turn_ref is None:
        return ""
    chain, turn = turn_ref
    target = (turn.line or "").strip()
    if not target:
        return ""

    context_parts = [chain.topic or ""]
    full_script = script.full_script_text or ""
    line_offset = full_script.find(target)
    if line_offset >= 0:
        headings = list(re.finditer(r"【场\s*(\d+)】", full_script[:line_offset]))
        if headings:
            scene_no = int(headings[-1].group(1))
            scene = next(
                (
                    item for item in (script.scene_outline or [])
                    if int(item.scene_no) == scene_no
                ),
                None,
            )
            if scene is not None:
                context_parts.extend([
                    scene.source_basis or "",
                    scene.summary or "",
                    scene.conflict or "",
                    scene.turn or "",
                ])
    context = " ".join(part for part in context_parts if part)

    ranked = sorted(
        (
            (_source_evidence_score(candidate, target, context), candidate)
            for candidate in _source_sentence_candidates(source_text)
        ),
        key=lambda item: (item[0], -len(item[1])),
        reverse=True,
    )
    if not ranked or ranked[0][0] < 1.0:
        return ""
    return ranked[0][1]


def _guess_speaker(line: str, script: EpisodeScreenplay) -> str:
    for chain in script.dialogue_chains or []:
        for turn in chain.turns:
            if turn.line and turn.line in line:
                return turn.speaker or "角色"
    if script.scene_outline and script.scene_outline[0].characters:
        return script.scene_outline[0].characters[0]
    return "角色"


async def ensure_source_characters_incremental(
    episode_id: str,
    source_text: str,
    draft_text: str = "",
) -> dict[str, Any]:
    """增量追加 source-backed 角色，不触发完整 regenerate。"""
    from app.domain import screenplay_ops
    return await screenplay_ops._screenplay_character_discovery(
        episode_id, source_text, draft_text=draft_text,
    )


async def run_screenplay_production(
    *,
    episode_id: str,
    episode: dict[str, Any],
    source_text: str,
    bible: Bible,
    prev_ending: str = "",
    run_id: str | None = None,
    resume: bool = True,
) -> EpisodeScreenplay:
    """一次 Baseline + 局部修复直到证书发布（或 WAITING_INPUT）。"""
    from app.harness.contracts import get_contract
    from app.stages import generate_screenplay_baseline

    conn = get_conn()
    contract = get_contract("screenplay")
    from app.production.screenplay_authority import screenplay_authority_fingerprint

    input_fp = screenplay_authority_fingerprint(
        episode_id,
        conn=conn,
        source_text=source_text,
        bible=bible,
        contract_version=contract.version,
        qa_profile_version="screenplay-qa-gate-2",
    )

    rev = ensure_production_revision(
        episode_id=episode_id,
        kind="screenplay",
        input_fingerprint=input_fp,
        contract_version=contract.version,
        qa_profile_version="screenplay-qa-gate-2",
        resume=resume,
    )
    # 签发 Production Grant
    if not rev.grant_id:
        grant, _token = issue_production_grant(
            episode_id=episode_id,
            project_id=episode["project_id"],
            production_revision_id=rev.id,
            kind="screenplay",
            input_artifact_hash="",
        )
        rev = get_production_revision(rev.id)  # type: ignore[assignment]

    checkpoint = dict(rev.checkpoint_json or {})
    if checkpoint.get("planner_version") != SCREENPLAY_REPAIR_PLANNER_VERSION:
        # 新规划器接管旧 working artifact 时重置已耗尽策略，不再恢复退役的固定对白上限。
        checkpoint = {
            **checkpoint,
            "planner_version": SCREENPLAY_REPAIR_PLANNER_VERSION,
            "issue_strategy_history": {},
            "yield_reason": "planner_upgraded",
        }
    strategy_history: dict[str, list[str]] = dict(checkpoint.get("issue_strategy_history") or {})
    patch_ids: list[str] = list(checkpoint.get("patch_artifact_ids") or [])
    activation_no = int(checkpoint.get("activation_no") or 0) + 1
    record_activation(kind="screenplay", episode_id=episode_id, activation_no=activation_no)

    def _publish_retry_exhausted_fallback(
        current_rev,
        *,
        working_id: str,
        artifact_hash: str,
        evaluation_id: str | None,
        open_issues: list[Issue],
        reason: str,
    ) -> EpisodeScreenplay:
        """Preserve the working artifact and stop when hard gates remain open.

        This function intentionally keeps its historical name so persisted
        checkpoints can resume across the upgrade.  It no longer issues a
        completion certificate or publishes an unvalidated candidate.
        """
        hard_identity_issues = non_waivable_screenplay_issues(open_issues)
        if hard_identity_issues:
            message = (
                "剧本人物身份预检未通过，已在剧本阶段停止："
                + "；".join(issue.message for issue in hard_identity_issues[:5])
            )[:800]
            conn.execute(
                "UPDATE episodes SET screenplay_status='failed',screenplay_error=?,screenplay_updated_at=? "
                "WHERE id=?",
                (message, now(), episode_id),
            )
            conn.commit()
            save_checkpoint(current_rev.id, {
                **checkpoint,
                "phase": "FAILED",
                "activation_no": activation_no,
                "working_artifact_id": working_id,
                "open_issue_ids": [issue.fingerprint for issue in open_issues],
                "issue_strategy_history": strategy_history,
                "patch_artifact_ids": patch_ids,
                "last_issue_fingerprints": [issue.fingerprint for issue in open_issues],
                "yield_reason": "character_identity_hard_gate",
                "fallback_reason": reason,
            })
            if run_id:
                evidence_repository.append_event(
                    run_id,
                    "CHARACTER_IDENTITY_HARD_GATE_BLOCKED",
                    "error",
                    "人物身份未解决，禁止发布剧本和启动分镜",
                    payload={
                        "reason": reason,
                        "issues": [issue.model_dump(mode="json") for issue in hard_identity_issues],
                    },
                )
            raise ScreenplayIdentityGateError(message)
        message = (
            "剧本工作稿已保留，但叙事/质量硬门禁仍未通过，禁止发布："
            + "；".join(issue.message for issue in open_issues[:5])
        )[:1200]
        conn.execute(
            "UPDATE episodes SET screenplay_status='repairing',screenplay_error=?,screenplay_updated_at=? WHERE id=?",
            (message, now(), episode_id),
        )
        conn.commit()
        save_checkpoint(current_rev.id, {
            **checkpoint,
            "phase": "WAITING_HUMAN",
            "activation_no": activation_no,
            "working_artifact_id": working_id,
            "open_issue_ids": [issue.fingerprint for issue in open_issues],
            "issue_strategy_history": strategy_history,
            "patch_artifact_ids": patch_ids,
            "last_issue_fingerprints": [issue.fingerprint for issue in open_issues],
            "yield_reason": "narrative_gate_needs_review",
            "fallback_reason": reason,
        })
        if run_id:
            evidence_repository.append_event(
                run_id,
                "NARRATIVE_GATE_NEEDS_REVIEW",
                "error",
                "剧本门禁重试耗尽；工作稿已保留，未发布",
                payload={
                    "artifact_id": working_id,
                    "reason": reason,
                    "issue_count": len(open_issues),
                },
            )
        raise ScreenplayNarrativeGateError(message)

    # ---- Baseline（仅一次）----
    if not rev.baseline_done:
        assert_baseline_allowed(rev, command="screenplay.generate", episode_id=episode_id)
        if run_id:
            evidence_repository.append_event(
                run_id, "BASELINE_GENERATION_STARTED", "info",
                "剧本 Baseline 生成（本 revision 仅此一次）",
                payload={"revision_id": rev.id},
            )
        script = await generate_screenplay_baseline(
            episode, source_text, bible, prev_ending=prev_ending,
        )
        # 即使模型在正文中沿用了“绿袍男子/大汉”，也要在
        # Baseline 落库与 QA 之前按预检结果原子性改成真名或路人编号。
        from app.portraits import apply_screenplay_character_resolutions
        apply_screenplay_character_resolutions(
            script,
            episode.get("character_resolutions") or [],
        )
        # 增量人物：只追加 Bible，不二次完整生成
        draft_audit = await ensure_source_characters_incremental(
            episode_id, source_text, draft_text=script.model_dump_json(),
        )
        from app.portraits import merge_screenplay_character_resolutions
        episode["character_resolutions"] = merge_screenplay_character_resolutions(
            episode.get("character_resolutions") or [],
            draft_audit.get("resolutions") or [],
        )
        apply_screenplay_character_resolutions(script, episode["character_resolutions"])
        if draft_audit.get("added"):
            # 重新加载 bible，仅 patch 受影响的 speaker/voice（最小）
            p = conn.execute("SELECT * FROM projects WHERE id=?", (episode["project_id"],)).fetchone()
            from app.domain.common import _project_bible_or_placeholder
            bible = _project_bible_or_placeholder(p)
        from app.portraits import bible_with_provisional_characters
        bible = bible_with_provisional_characters(bible, draft_audit)

        from app.validators import normalize_screenplay_candidate
        script = normalize_screenplay_candidate(script)
        payload = screenplay_artifact_payload(script)
        baseline_art = evidence_repository.create_artifact(
            EvidenceArtifact(
                type="screenplay_document",
                scope_type="episode",
                scope_id=episode_id,
                status="candidate",
                trust_level="T1",
                content=payload,
                contract_version=contract.version,
            )
        )
        rev = mark_baseline_generated(
            rev.id,
            baseline_artifact_id=baseline_art["id"],
            working_artifact_id=baseline_art["id"],
        )
        record_baseline_generation(
            kind="screenplay", episode_id=episode_id, revision_id=rev.id,
        )
        conn.execute(
            "UPDATE episodes SET screenplay_status='repairing', screenplay_error=?, "
            "screenplay_updated_at=? WHERE id=?",
            ("首次整版 Baseline 已落库，正在执行只读 QA", now(), episode_id),
        )
        conn.commit()
        if run_id:
            evidence_repository.append_event(
                run_id, "BASELINE_GENERATION_DONE", "info",
                "Baseline 已落库，进入 QA",
                payload={"artifact_id": baseline_art["id"], "revision_id": rev.id},
            )
    elif not rev.working_artifact_id:
        raise RuntimeError("revision 已有 baseline 计数但缺少 working artifact")

    # ---- Repair loop ----
    patches_this_activation = 0
    attempts_this_activation = 0
    passes_this_activation = 0
    prev_issue_fps: set[str] = set(checkpoint.get("last_issue_fingerprints") or [])

    while (
        attempts_this_activation < MAX_REPAIR_ACTIVATION_PATCHES
        and passes_this_activation < MAX_REPAIR_ACTIVATION_PASSES
    ):
        passes_this_activation += 1
        rev = get_production_revision(rev.id)  # type: ignore[assignment]
        working_id = rev.working_artifact_id
        assert working_id
        art = evidence_repository.get_artifact(working_id)
        assert art
        artifact_hash = art.get("content_hash") or evidence_repository.content_hash(art.get("content"))
        script = load_screenplay_from_artifact(working_id)

        # 身份决议可能来自 Baseline 后审计、服务恢复或人工入口。无论从哪条路
        # 进入 Repair，都先把它派生为新的 working artifact，再做 QA/局部 Patch。
        from app.portraits import apply_screenplay_character_resolutions
        identity_changes = apply_screenplay_character_resolutions(
            script,
            episode.get("character_resolutions") or [],
        )
        if identity_changes:
            identity_payload = screenplay_artifact_payload(script)
            identity_hash = evidence_repository.content_hash(identity_payload)
            if identity_hash == artifact_hash:
                # Some labels remain in non-identity prose by design. The
                # resolver may report those replacements on every replay even
                # though the canonical artifact payload is already identical.
                # Hash equality is the authoritative idempotency boundary.
                identity_changes = []
        if identity_changes:
            identity_artifact = evidence_repository.create_artifact(
                EvidenceArtifact(
                    type="screenplay_document",
                    scope_type="episode",
                    scope_id=episode_id,
                    status="candidate",
                    trust_level="T1",
                    content=identity_payload,
                    parent_artifact_ids=[working_id],
                    contract_version=rev.contract_version or None,
                )
            )
            update_working_artifact(
                rev.id,
                identity_artifact["id"],
                expected_hash=artifact_hash,
            )
            if run_id:
                evidence_repository.append_event(
                    run_id,
                    "CHARACTER_IDENTITY_RESOLUTIONS_APPLIED",
                    "info",
                    "已在 QA 前重放剧本人物身份决议",
                    payload={
                        "before_artifact_id": working_id,
                        "after_artifact_id": identity_artifact["id"],
                        "changes": identity_changes,
                    },
                )
            # Continue QA from the normalized artifact in this iteration. A
            # separate loop turn here is unbounded by the patch budget and can
            # persist duplicate artifacts forever if normalization oscillates
            # or a stale worker repeatedly reports the same material change.
            working_id = identity_artifact["id"]
            artifact_hash = identity_hash

        issues, evaluation = run_screenplay_qa(
            script,
            bible=bible,
            source_text=source_text,
            episode=episode,
            artifact_id=working_id,
            artifact_hash=artifact_hash,
        )
        eval_row = evidence_repository.create_evaluation(working_id, evaluation)
        eval_id = _eval_id_from_create(eval_row)
        if not rev.first_evaluation_done:
            rev = mark_first_evaluation(rev.id, eval_id or f"eval-{working_id}")

        current_fps = {i.fingerprint for i in issues}
        reopened = prev_issue_fps & current_fps
        # reopened means previously cleared then came back - track when we had improvement
        if checkpoint.get("cleared_fingerprints"):
            for fp in set(checkpoint["cleared_fingerprints"]) & current_fps:
                record_issue_reopened(kind="screenplay", episode_id=episode_id, fingerprint=fp)
                reopened.add(fp)

        if evaluation.status == "passed" and can_issue_certificate(issues):
            if run_id:
                evidence_repository.append_event(
                    run_id, "CERTIFYING", "info", "剧本 QA 已通过，正在签发完成凭证",
                    payload={
                        "artifact_id": working_id,
                        "evaluation_id": eval_id,
                        "qa_score": evaluation.score,
                    },
                )
            # 首次发布清空下游；若已有 published 且同 hash 则仍走 publish
            result = publish_screenplay(
                episode_id=episode_id,
                revision_id=rev.id,
                artifact_id=working_id,
                artifact_hash=artifact_hash,
                evaluation_ids=[eval_id] if eval_id else [],
                input_fingerprint=rev.input_fingerprint,
                contract_version=rev.contract_version,
                qa_profile_version=rev.qa_profile_version,
                clear_downstream=True,
            )
            if run_id:
                evidence_repository.append_event(
                    run_id, "PUBLISHED", "info", "可交付剧本已发布",
                    payload=result,
                )
            save_checkpoint(rev.id, {
                **checkpoint,
                "phase": "SUCCEEDED",
                "activation_no": activation_no,
                "working_artifact_id": working_id,
                "open_issue_ids": [],
                "issue_strategy_history": strategy_history,
                "patch_artifact_ids": patch_ids,
                "last_issue_fingerprints": [],
                "yield_reason": None,
            })
            return load_screenplay_from_artifact(working_id)

        # 选择最高依赖 Issue
        issue = _choose_issue(issues)
        if issue is None or not issue.repairable:
            return _publish_retry_exhausted_fallback(
                rev,
                working_id=working_id,
                artifact_hash=artifact_hash,
                evaluation_id=eval_id,
                open_issues=issues,
                reason="no_repairable_strategy",
            )

        if script.narrative_plan is not None:
            # New narrative artifacts always use semantic candidate comparison.
            # Issue codes remain diagnostics and never select a patch strategy.
            ops = await _llm_field_patch(issue, script, source_text=source_text)
        else:
            # Read-only compatibility for pre-contract artifacts.  New
            # generation cannot enter this legacy deterministic adapter.
            ops = plan_screenplay_patch(
                issue,
                script,
                source_text=source_text,
                strategy_history=strategy_history,
            )
            if not ops:
                ops = await _llm_field_patch(issue, script, source_text=source_text)
        if ops:
            proposed_key = _patch_strategy_key(ops)
            if _strategy_was_tried(
                strategy_history.get(issue.fingerprint, []), proposed_key
            ):
                ops = []
        if not ops:
            strategy_history.setdefault(issue.fingerprint, []).append("exhausted")
            _mark_repair_failed(
                episode_id,
                issue,
                run_id=run_id,
                activation_no=activation_no,
                patch_count=len(patch_ids),
            )
            return _publish_retry_exhausted_fallback(
                rev,
                working_id=working_id,
                artifact_hash=artifact_hash,
                evaluation_id=eval_id,
                open_issues=issues,
                reason="strategies_exhausted",
            )

        strategy_key = _patch_strategy_key(ops)
        strategy_history.setdefault(issue.fingerprint, []).append(strategy_key)
        attempts_this_activation += 1

        if rev.grant_id:
            assert_grant_allows(rev.grant_id, command="screenplay.patch", episode_id=episode_id)

        if run_id:
            evidence_repository.append_event(
                run_id, "APPLYING_PATCH", "info",
                f"局部修复 {issue.code} @ {issue.evidence.get('path')}",
                payload={
                    "issue": issue.model_dump(mode="json"),
                    "operations": [o.model_dump(mode="json") for o in ops],
                },
            )

        result = apply_screenplay_patch(
            PatchRequest(
                production_revision_id=rev.id,
                expected_artifact_id=working_id,
                expected_hash=artifact_hash,
                issue_set_hash=issue_set_hash(issues),
                operations=ops,
                idempotency_key=f"{rev.id}:{issue.fingerprint}:{strategy_key}:{attempts_this_activation}",
                reason=issue.message[:200],
            ),
            episode_id=episode_id,
            character_resolutions=episode.get("character_resolutions") or [],
        )
        if not result.ok:
            strategy_history.setdefault(issue.fingerprint, []).append(
                f"fail:{strategy_key}:{(result.error or 'patch failed')[:160]}"
            )
            if "no-op" in (result.error or ""):
                prev_issue_fps = current_fps
                save_checkpoint(rev.id, {
                    **checkpoint,
                    "phase": "QA",
                    "activation_no": activation_no,
                    "working_artifact_id": working_id,
                    "open_issue_ids": [i.fingerprint for i in issues],
                    "issue_strategy_history": strategy_history,
                    "patch_artifact_ids": patch_ids,
                    "last_issue_fingerprints": list(current_fps),
                    "yield_reason": "noop_rejected",
                })
                if _strategy_attempt_count(
                    strategy_history.get(issue.fingerprint, [])
                ) >= MAX_STRATEGY_ATTEMPTS_PER_ISSUE:
                    _mark_repair_failed(
                        episode_id,
                        issue,
                        run_id=run_id,
                        activation_no=activation_no,
                        patch_count=len(patch_ids),
                    )
                    return _publish_retry_exhausted_fallback(
                        rev,
                        working_id=working_id,
                        artifact_hash=artifact_hash,
                        evaluation_id=eval_id,
                        open_issues=issues,
                        reason="no_progress",
                    )
                continue
            # CAS 冲突：重新观察
            if "CAS" in (result.error or "") or "hash" in (result.error or "").lower():
                continue
            raise RuntimeError(result.error or "patch failed")

        if result.patch_artifact_id:
            patch_ids.append(result.patch_artifact_id)
        patches_this_activation += 1
        cleared = prev_issue_fps - current_fps
        checkpoint["cleared_fingerprints"] = list(
            set(checkpoint.get("cleared_fingerprints") or []) | cleared
        )
        prev_issue_fps = current_fps
        conn.execute(
            "UPDATE episodes SET screenplay_status='repairing', screenplay_error=?, "
            "screenplay_updated_at=? WHERE id=?",
            (
                f"自动修复中：已处理 {patches_this_activation} 次补丁，剩余问题 {len(issues)}",
                now(),
                episode_id,
            ),
        )
        conn.commit()
        save_checkpoint(rev.id, {
            **checkpoint,
            "phase": "QA",
            "activation_no": activation_no,
            "working_artifact_id": result.after_artifact_id,
            "open_issue_ids": [i.fingerprint for i in issues],
            "issue_strategy_history": strategy_history,
            "patch_artifact_ids": patch_ids,
            "last_issue_fingerprints": list(current_fps),
            "last_touched": result.touched_node_ids,
        })

    # activation 预算用尽：对最终工作副本再评分一次并直接发布，不留失败/等待态。
    rev = get_production_revision(rev.id)  # type: ignore[assignment]
    assert rev and rev.working_artifact_id
    working_id = rev.working_artifact_id
    art = evidence_repository.get_artifact(working_id)
    assert art
    artifact_hash = art.get("content_hash") or evidence_repository.content_hash(art.get("content"))
    script = load_screenplay_from_artifact(working_id)
    issues, evaluation = run_screenplay_qa(
        script, bible=bible, source_text=source_text, episode=episode,
        artifact_id=working_id, artifact_hash=artifact_hash,
    )
    eval_row = evidence_repository.create_evaluation(working_id, evaluation)
    eval_id = _eval_id_from_create(eval_row)
    return _publish_retry_exhausted_fallback(
        rev,
        working_id=working_id,
        artifact_hash=artifact_hash,
        evaluation_id=eval_id,
        open_issues=issues,
        reason="activation_budget_exhausted",
    )


def get_active_safe(episode_id: str):
    from app.production.revision import get_active_production_revision
    return get_active_production_revision(episode_id, "screenplay")


def _choose_issue(issues: list[Issue]) -> Issue | None:
    if not issues:
        return None
    repairable = [i for i in issues if i.repairable]
    pool = repairable or issues

    severity_order = {"blocker": 0, "error": 1, "warning": 2, "info": 3}

    def issue_priority(issue: Issue) -> tuple[float, float, float, str]:
        evidence = issue.evidence or {}
        severity_value = getattr(issue.severity, "value", issue.severity)
        severity = severity_order.get(str(severity_value), 4)
        # Producers may expose graph depth/affected scope, but missing values
        # remain neutral.  These are relation properties, never issue-code or
        # story-word mappings.
        try:
            dependency_depth = float(evidence.get("dependency_depth", 0))
        except (TypeError, ValueError):
            dependency_depth = 0.0
        try:
            affected_scope = float(evidence.get("affected_scope_size", 1))
        except (TypeError, ValueError):
            affected_scope = 1.0
        return severity, dependency_depth, -affected_scope, issue.fingerprint

    return sorted(pool, key=issue_priority)[0]


def _mark_waiting_input(episode_id: str, issues: list[Issue], *, run_id: str | None) -> None:
    if run_id:
        evidence_repository.append_event(
            run_id, "WAITING_INPUT", "warning",
            "存在不可自动修复的真实冲突，需用户决定",
            payload={"issues": [i.model_dump(mode="json") for i in issues[:5]]},
        )


def _mark_repair_failed(
    episode_id: str,
    issue: Issue,
    *,
    run_id: str | None,
    activation_no: int | None = None,
    patch_count: int | None = None,
) -> None:
    """暂停内部修复但保留 working artifact；这不是用户输入冲突。"""
    rev = get_active_safe(episode_id)
    checkpoint = dict(rev.checkpoint_json or {}) if rev else {}
    current_activation = (
        int(activation_no)
        if activation_no is not None
        else int(checkpoint.get("activation_no") or 0)
    )
    applied_patches = (
        int(patch_count)
        if patch_count is not None
        else len(checkpoint.get("patch_artifact_ids") or [])
    )
    progress = f"已启动 {current_activation} 轮、实际应用 {applied_patches} 个补丁"
    message = (
        f"REPAIR_FAILED: 自动修复暂停（{progress}）；当前问题暂无可用策略 "
        f"{issue.code}: {issue.message}"
    )
    conn = get_conn()
    conn.execute(
        "UPDATE episodes SET screenplay_status='repairing', screenplay_error=?, "
        "screenplay_updated_at=? WHERE id=?",
        (message[:800], now(), episode_id),
    )
    conn.commit()
    if run_id:
        evidence_repository.append_event(
            run_id,
            "REPAIR_FAILED",
            "error",
            f"自动修复暂停（{progress}），已保留工作副本",
            payload={
                "issue": issue.model_dump(mode="json"),
                "requires_user_input": False,
            },
        )


def _identity_contract_repair_policy() -> dict[str, Any]:
    """Return the typed, content-agnostic identity rules used by graph repair."""
    return {
        "authority": (
            "identity_contracts 是所有非角色圣经身份的唯一权威声明；"
            "修复不得引入未声明的实体、场次人物或非旁白说话人"
        ),
        "contract_fields": {
            "identity_id": "稳定图引用 ID",
            "display_name": "剧本与对白使用的精确显示名",
            "kind": "基于当前来源和戏剧职责的开放语义",
            "visual_policy": "canonical | contextual | collective | offscreen_only",
            "visual_canonical": "非 offscreen_only 必填的中性视觉锚点",
            "asset_requirement": "required | optional | forbidden",
            "voice_ids": "精确回指 voice_bible.speaker_id",
            "evidence": {
                "source_evidence_ids": [],
                "proposition_ids": [],
                "adaptation_decision_ids": [],
                "rationale": "身份决策的可追溯理由",
            },
        },
        "typed_invariants": [
            "canonical 必须 asset_requirement=required",
            "offscreen_only 必须 asset_requirement=forbidden",
            "除 offscreen_only 外 visual_canonical 必填",
            "纯旁白可由 voice_bible.role_type=narrator 表达；其他画外说话人仍需合同与 voice_ids",
        ],
        "semantic_decision": (
            "具名新角色、一次性功能身份、群体或纯画外身份均按当前语义意图决策；"
            "禁止使用姓名、称谓、身份类型或题材白名单"
        ),
    }


async def _llm_field_patch(
    issue: Issue,
    script: EpisodeScreenplay,
    *,
    source_text: str,
) -> list[PatchOperation]:
    """Compare semantic candidates, then return one bounded candidate patch.

    New narrative artifacts never map an issue code to an operation.  The AI
    compares at least two relation-level candidates and the selected candidate
    is CAS-applied to an isolated working artifact before full-graph QA.
    """
    path = str((issue.evidence or {}).get("path") or "")
    field = path.strip("/").split("/")[-1] if path else ""
    dramatic_fields = {"stakes", "obstacle", "protagonist_goal", "dramatic_question"}
    if script.narrative_plan is None:
        if field not in dramatic_fields:
            for candidate_field in dramatic_fields:
                if candidate_field in (issue.message or ""):
                    field = candidate_field
                    break
        if field not in dramatic_fields:
            return []
        value = _heuristic_fill_dramatic_field(field, script)
        return ([PatchOperation(
            op="replace_field",
            path=field,
            value=value,
            target={"kind": "metadata", "id": field},
        )] if value else [])

    from app.harness import model_gateway
    from app.production.screenplay_document import screenplay_to_document
    from app.schemas import extract_json

    document = screenplay_to_document(script)
    prompt = {
        "task": "诊断当前剧本叙事关系缺口，比较至少两个最小候选，再选择一个局部候选",
        "issue": issue.model_dump(mode="json"),
        "screenplay_document": document.model_dump(mode="json"),
        "authorized_source_excerpt": source_text[:16000],
        "identity_contract_policy": _identity_contract_repair_policy(),
        "operation_contract": {
            "op": "replace_field | create_node | delete_node | move_node | insert_node",
            "path": "单个现存字段；结构操作留空",
            "target": {
                "kind": "narrative_node | metadata | scene | dialogue_chain_turn",
                "collection": "narrative_plan 的 schema 列表字段（包括 identity_contracts）；非叙事节点可省略",
                "id": "现存节点 ID；create_node 时为新节点 ID",
                "parent_id": "创建嵌套节点时的现存父节点 ID，可省略",
                "parent_field": "父节点中的列表字段，可省略",
                "to_index": "移动/插入位置，可省略",
            },
            "value": "replace 的新字段值或 create 的完整单节点",
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
            "选中候选只能含 1~3 个局部操作，不得替换根对象或整个集合",
            "允许创建/删除/移动单个叙事节点，但必须证明全图引用、DAG、状态、信念和观众路径可恢复",
            "新增必须通过缺口与边际增益测试；删除必须通过删除测试；所有候选必须保持不变量",
            "不得修改现存节点的身份 ID",
            "create/replace 一旦引入新 identity_id、display_name 或非旁白 voice ID，同一候选必须以局部操作创建或补齐完整 identity_contracts 节点及 voice_ids 连接；否则候选无效",
            "修复可以更正身份合同本身，但不得借修复器绕过已有角色圣经或已发布身份合同的 ID 权威",
            "来源证据必须逐字来自 authorized_source_excerpt",
            "改写命题不得直接挂原文证据，角色/观众信念不得补入不可感知证据",
            "修复后仍会运行整图 DAG、状态、信念与观众路径全量复验",
        ],
    }
    raw = await model_gateway.chat(
        [
            {"role": "system", "content": "你是叙事图局部修复器。只输出 JSON，不按题材或剧情关键词判断。"},
            {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
        ],
        temperature=0.1,
        max_tokens=4096,
        call_meta={
            "stage": "screenplay_narrative_patch",
            "stage_key": "narrative_graph_patch",
            "call_role": "semantic_patch_planner",
            "contract_version": "narrative-continuity.v1",
        },
    )
    try:
        payload = extract_json(raw)
        candidates = list(payload.get("candidate_plans") or [])
        if len(candidates) < 2:
            return []
        selected_id = str(payload.get("selected_candidate_id") or "")
        selected = next((
            item for item in candidates
            if isinstance(item, dict) and str(item.get("candidate_id") or "") == selected_id
        ), None)
        if selected is None or not bool(selected.get("preserves_invariants")):
            return []
        raw_ops = list(selected.get("operations") or [])
        if not 1 <= len(raw_ops) <= 3:
            return []
        operations = [PatchOperation.model_validate(item) for item in raw_ops]
    except Exception:  # noqa: BLE001 - model output is untrusted
        return []
    if any(operation.op in {"create_node", "insert_node"} for operation in operations) and not (
        bool(selected.get("satisfies_gap_test"))
        and bool(selected.get("passes_marginal_gain_test"))
    ):
        return []
    if any(operation.op == "delete_node" for operation in operations) and not bool(
        selected.get("passes_deletion_test")
    ):
        return []

    plan_data = script.narrative_plan.model_dump(mode="json")
    safe: list[PatchOperation] = []
    for operation in operations:
        target = operation.target or {}
        kind = str(target.get("kind") or "")
        if operation.op not in {
            "replace_field", "create_node", "insert_node", "delete_node", "move_node",
        }:
            return []
        if kind == "narrative_node":
            collection = str(target.get("collection") or "")
            node_id = str(target.get("id") or "")
            nodes = plan_data.get(collection)
            if not isinstance(nodes, list) or not node_id:
                return []

            def find_node(value: Any) -> dict[str, Any] | None:
                if isinstance(value, dict):
                    if any(
                        key.endswith("_id") and str(candidate or "") == node_id
                        for key, candidate in value.items()
                    ):
                        return value
                    for child in value.values():
                        found = find_node(child)
                        if found is not None:
                            return found
                elif isinstance(value, list):
                    for child in value:
                        found = find_node(child)
                        if found is not None:
                            return found
                return None

            node = find_node(nodes)
            if operation.op == "replace_field":
                patch_field = operation.path.split(".")[-1]
                if node is None or patch_field not in node:
                    return []
                if patch_field.endswith("_id") and str(node.get(patch_field) or "") == node_id:
                    return []
                if patch_field in {"verbatim_excerpt", "source_text"} and str(
                    operation.value or ""
                ) not in source_text:
                    return []
            elif operation.op in {"delete_node", "move_node"} and node is None:
                return []
            elif operation.op in {"create_node", "insert_node"}:
                if node is not None or not isinstance(operation.value, dict):
                    return []
        elif operation.op != "replace_field" or kind not in {
            "metadata", "scene", "screenplay_scene", "dialogue_chain_turn",
        }:
            return []
        elif not operation.path or operation.path in {"/", "$", "full_script_text"}:
            return []
        if operation.path.split(".")[-1] in {"source_text", "verbatim_excerpt"} and str(
            operation.value or ""
        ) not in source_text:
            return []
        selection_evidence = {
            "semantic_gap": payload.get("semantic_gap"),
            "candidate_ids": [item.get("candidate_id") for item in candidates if isinstance(item, dict)],
            "selected_candidate_id": selected_id,
            "selection_reason": payload.get("selection_reason"),
            "unclassified_dimensions": payload.get("unclassified_dimensions") or [],
            "expected_narrative_gain": selected.get("expected_narrative_gain"),
            "destructive_cost": selected.get("destructive_cost"),
        }
        operation.target = {**target, "semantic_selection": selection_evidence}
        safe.append(operation)
    try:
        from app.production.patch import _create_node, _delete_node, _structure_op
        from app.production.screenplay_document import apply_field_patch

        candidate_document = document
        for operation in safe:
            if operation.op == "replace_field":
                candidate_document, _ = apply_field_patch(
                    candidate_document,
                    path=operation.path,
                    value=operation.value,
                    target=operation.target,
                )
            elif operation.op in {"create_node", "insert_node"}:
                candidate_document, _ = _create_node(candidate_document, operation)
            elif operation.op == "delete_node":
                candidate_document, _ = _delete_node(candidate_document, operation)
            elif operation.op == "move_node":
                candidate_document, _ = _structure_op(candidate_document, operation)
    except Exception:  # noqa: BLE001 - reject an invalid model-authored candidate
        return []
    return safe
