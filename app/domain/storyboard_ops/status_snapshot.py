"""分镜整体状态快照投影（镜头级证据/发布/生成态的权威聚合）。

从 app/domain/storyboard_ops.py 按原样搬移；依赖 evidence 与 task_run。单个函数 _storyboard_status_snapshot 334 行，是分镜台
状态字段的唯一权威聚合点，拆分会打散字段间的一致性推导顺序，不做拆分（移动未拆分）。
"""
from __future__ import annotations

import json
import re

from app.db import (
    get_conn,
    get_setting,
)
from app.domain.common import (
    _episode_or_404,
    _project_bible_or_placeholder,
    router,
    storyboard_pack_prompts_complete,
)
from app.schemas import (
    EpisodeScreenplay,
    Shot,
    Storyboard,
)

from .evidence import _storyboard_publication_evidence_state
from .task_run import _storyboard_generation_is_live


def _storyboard_issue_targets_shot(message: str, index: int, shot_no: int) -> bool:
    """精确定位镜头诊断，避免 shot_no=1 误匹配 shot_no=10～19。"""
    if f"shots[{index}](shot_no={shot_no})" in message:
        return True
    return bool(
        re.search(rf"(?<!\d)shot_no\s*=\s*{shot_no}(?!\d)", message)
        or re.search(rf"第\s*{shot_no}\s*镜", message)
    )

def _storyboard_status_snapshot(
    ep: dict,
    shots: list[dict],
    supervisor: dict | None,
    screenplay: EpisodeScreenplay | None = None,
    screenplay_rebuild_error: Exception | None = None,
) -> dict:
    """返回供所有分镜台区域共同消费的 v1 原子状态投影。"""
    from app.storyboard_workspace import episode_fingerprint, monotonic_snapshot_version

    screenplay_ready = bool(
        screenplay_rebuild_error is None
        and ep.get("screenplay_status") == "ready"
        and ep.get("screenplay_artifact_id")
    )
    shot_count = len(shots)
    outline_count = 0
    try:
        outline_count = len(json.loads(ep.get("storyboard_outline_json") or "{}").get("shots") or [])
    except (TypeError, ValueError, json.JSONDecodeError):
        outline_count = 0
    # 结构编辑会撤销整集分镜产物，并把新顺序写回大纲。此时旧 supervisor
    # checkpoint 只描述上一次生成，不能继续覆盖用户刚批准的新计划镜数。
    structural_draft = ep.get("storyboard_artifact_id") is None and outline_count > 0
    planned = int(
        (outline_count if structural_draft else 0)
        or (supervisor or {}).get("expected_total")
        or ep.get("storyboard_planned_shots")
        or outline_count
        or shot_count
        or 0
    )
    # ``validated_prefix_end`` is a safe-resume boundary, not the number of rows
    # currently visible in the draft.  In particular, an explicit zero must not
    # fall back to ``shot_count`` or the UI would claim that every draft shot is
    # safe after a repair invalidated the whole prefix.
    passed = (
        min(shot_count, max(0, int(supervisor.get("validated_prefix_end") or 0)))
        if supervisor is not None
        else shot_count
    )
    final_valid = bool(shots and shots[-1].get("is_final"))
    phase = str((supervisor or {}).get("phase") or "")
    active_run_live = _storyboard_generation_is_live(ep)
    # ``episodes.status`` 是业务投影，不是任务存活证明。Run 已 FAILED/CANCELLED
    # 或活动指针已清理后，即使旧 checkpoint 仍停在 GENERATING_SHOTS，也绝不能
    # 继续向前端报告 running。
    running = ep.get("status") == "scripting" and active_run_live
    incomplete_terminal_checkpoint = bool(
        phase == "SUCCEEDED"
        and (
            shot_count != planned
            or passed != shot_count
            or not final_valid
        )
    )
    paused = not active_run_live and (
        phase in {
            "PAUSED_EXTERNAL", "PAUSED_BUDGET",
            "WAITING_HUMAN", "WAITING_AUTHORIZATION",
        }
        or incomplete_terminal_checkpoint
    )
    confirmed = ep.get("status") in {"confirmed", "generating", "done"}
    if confirmed:
        passed = shot_count
    resume_from = max(
        1,
        int((supervisor or {}).get("next_shot_no") or (passed + 1)),
    )
    complete_structure = bool(
        ep.get("status") in {"scripted", "confirmed", "generating", "done"}
        and shot_count > 0
        and planned == shot_count
        and passed == shot_count
        and final_valid
    )
    terminal_structure = bool(
        complete_structure
        and (
            not supervisor
            or phase == "SUCCEEDED"
        )
    )
    repair = (supervisor or {}).get("last_repair") or {}
    repair_touched = {
        int(value) for value in (repair.get("touched_shot_nos") or [])
        if str(value).isdigit()
    }
    raw_repair_errors = [
        str(message) for message in (repair.get("issue_messages") or [])
        if str(message).strip()
    ]
    # Only typed repair records are current authority. Historical records that
    # contain prose messages without issue codes predate the structural gates
    # and must be re-evaluated instead of interpreted through a word blacklist.
    repair_issue_codes = [
        str(code) for code in (repair.get("issue_codes") or [])
        if str(code).strip()
    ]
    active_repair_errors = (
        []
        if phase == "SUCCEEDED" or not repair_issue_codes
        else raw_repair_errors
    )
    obsolete_policy_repair = bool(
        raw_repair_errors and not repair_issue_codes
    )
    if (
        not active_repair_errors
        and paused
        and ep.get("script_error")
        and not obsolete_policy_repair
    ):
        active_repair_errors = [
            value.strip() for value in str(ep.get("script_error") or "").split("；") if value.strip()
        ]
    gate_errors: list[str] = list(dict.fromkeys(active_repair_errors))
    score_warnings: list[str] = []
    gate_system_error: str | None = None
    published_release_bound = bool(
        ep.get("storyboard_artifact_id")
        and ep.get("storyboard_completion_certificate_id")
        and ep.get("storyboard_production_revision_id")
    )
    publication_evidence_ready = published_release_bound
    evidence_refinalize_only = False
    if complete_structure:
        try:
            # 见本文件顶部注释：storyboard_ops <-> video_ops 是真实的双向依赖，
            # 这个方向只能延迟到调用时才导入，否则是模块级导入环。
            from app.domain.video_ops import (
                evaluate_storyboard_for_confirmation as current_gate_evaluator,
            )
            board = Storyboard(
                episode_no=int(ep["episode_no"]),
                shots=[Shot.model_validate(shot) for shot in shots],
            )
            (
                publication_evidence_ready,
                evidence_refinalize_only,
            ) = _storyboard_publication_evidence_state(ep, board)
            project = get_conn().execute(
                "SELECT * FROM projects WHERE id=?", (ep["project_id"],),
            ).fetchone()
            bible = _project_bible_or_placeholder(project)
            evaluation = current_gate_evaluator(
                ep,
                board,
                screenplay,
                bible,
                has_real_bible=bool((project["bible_json"] or "").strip()) if project else False,
                record_metrics=False,
                allow_evidence_refinalize=evidence_refinalize_only,
            )
            # 完整镜头投影的当前同源评估才是门禁真值。
            # 暂停 checkpoint 仅是当时的恢复点，不能让已被确定性
            # 对账修复的旧问题永久覆盖当前数据。
            gate_errors = list(dict.fromkeys(evaluation.errors))
            score_warnings.extend(evaluation.warnings)
            terminal_structure = complete_structure
            if not gate_errors and (raw_repair_errors or ep.get("script_error")):
                obsolete_policy_repair = True
            # 历史 source_excerpt 能否逐字回绑只属于来源审计，不是用户可修复的
            # 分镜结构错误。发布证据仍会记录该 finding，但不能让状态快照误报
            # 一个没有镜号、没有修复入口的整集门禁。
        except Exception as exc:  # noqa: BLE001
            gate_system_error = (
                f"确认门禁执行失败（{type(exc).__name__}）：{exc}"
            )
    for index, shot in enumerate(shots):
        shot_no = int(shot.get("shot_no") or index + 1)
        localized = [
            message for message in gate_errors
            if _storyboard_issue_targets_shot(message, index, shot_no)
        ]
        if shot_no in repair_touched:
            localized.extend(active_repair_errors)
            localized = list(dict.fromkeys(localized))
        # Score-only：质量 warning 仍挂到镜头供 UI 展示，但不进入确认硬门禁。
        localized_scores = [
            message for message in score_warnings
            if _storyboard_issue_targets_shot(message, index, shot_no)
        ]
        if localized_scores:
            shot["qa_warnings"] = localized_scores
        if localized:
            shot["preflight_errors"] = localized
    full_terminal = bool(terminal_structure and not gate_errors)
    repairing_existing = bool(
        final_valid
        and gate_errors
        and (planned <= 0 or shot_count >= planned)
    )
    invalid = bool(
        (running and confirmed)
        or (full_terminal and running)
        or (confirmed and not shots)
    )
    if gate_system_error:
        state, headline, action = (
            "syncing",
            "确认门禁服务异常，暂不可执行写操作",
            "refresh_status",
        )
    elif invalid:
        state, headline, action = "syncing", "状态同步中，暂不可执行高影响操作", "refresh_status"
    elif not screenplay_ready:
        state, headline, action = (
            "no_screenplay",
            "当前剧本需要按新合同重建后才能生成分镜"
            if screenplay_rebuild_error is not None
            else "尚无可用于分镜的剧本",
            "go_screenplay",
        )
    elif running:
        state, headline, action = "running", f"分镜任务进行中，当前处理第 {resume_from} 镜", "view_progress"
    elif confirmed and published_release_bound:
        state = "confirmed"
        headline = (
            "已确认正式版存在证据异常，禁止原地续跑"
            if gate_errors or not publication_evidence_ready
            else "当前分镜已确认"
        )
        action = "go_review_wall"
    elif full_terminal and published_release_bound and storyboard_pack_prompts_complete(
        get_conn(), ep["id"],
    ):
        # 分镜台 2.0.0（app.production.storyboard_pack）路径：发布证据在生成
        # 完成时已自动落盘（published_release_bound），本集视频提示词也已
        # 全部生成（storyboard_pack_prompts_complete）。旧版需要用户额外点一
        # 次"完成发布证据/确认视频提示词"才能把 episodes.status 推到
        # confirmed 的仪式，在这条管线上不做任何这里还没做过的额外校验
        # （见 app.domain.review_wall._review_upstream_snapshot 同一处改动的
        # 注释）——产物齐了就直接可进生成台，不再停下来等一次点击。
        state = "confirmed"
        headline = f"{shot_count}/{planned} 段视频提示词已全部生成，可进入生成台"
        action = "go_review_wall"
    elif paused and not terminal_structure:
        state, headline, action = (
            "paused",
            "整集修复已暂停，可继续修复现有问题镜"
            if repairing_existing
            else f"局部修复已暂停，将从第 {resume_from} 镜继续",
            "resume_storyboard",
        )
    elif terminal_structure and gate_errors:
        state, headline, action = "failed", f"还有 {len(gate_errors)} 个确认门禁问题，可继续修改", "resume_storyboard"
    elif ep.get("status") == "script_failed" or (ep.get("script_error") and not full_terminal):
        state, headline, action = "failed", f"生成停在第 {max(1, passed + 1)} 镜，可继续处理", "resume_storyboard"
    elif confirmed and not publication_evidence_ready:
        state, headline, action = (
            "paused",
            f"{shot_count}/{planned} 镜已通过，待更新发布证据",
            "resume_storyboard",
        )
    elif not shots:
        state, headline, action = "empty", "剧本已就绪，尚未生成分镜", "generate_storyboard"
    elif full_terminal and not publication_evidence_ready:
        state, headline, action = (
            "paused",
            f"{shot_count}/{planned} 镜已通过，待完成发布证据",
            "resume_storyboard",
        )
    elif full_terminal:
        state, headline, action = "ready_to_confirm", f"{shot_count}/{planned} 镜已通过，等待确认", "confirm_storyboard"
    else:
        state, headline, action = "syncing", "分镜尚未达到完整终态", "refresh_status"
    fingerprint = episode_fingerprint(ep["id"])
    feature_flags = {
        "safe_readonly": str(get_setting("storyboard_workspace_safe_readonly") or "false").lower() == "true",
        "structure_edit": str(get_setting("storyboard_structure_edit_enabled") or "true").lower() == "true",
        "source_rebind": str(get_setting("storyboard_source_rebind_enabled") or "true").lower() == "true",
    }
    if feature_flags["safe_readonly"]:
        state = "syncing"
        headline = "分镜台处于安全只读模式，可继续审阅"
        action = "refresh_status"
    resume_mode = None
    if action == "resume_storyboard":
        resume_mode = (
            "finalize_evidence"
            if full_terminal and not publication_evidence_ready
            else "repair_existing"
            if repairing_existing
            else "continue_generation"
        )
    return {
        "contract_version": "storyboard-workspace.v1",
        "snapshot_version": monotonic_snapshot_version(ep["id"], fingerprint),
        "state_fingerprint": fingerprint,
        "state": state,
        "headline": headline,
        "screenplay_available": screenplay_ready,
        "task_phase": phase or None,
        "planned_shots": planned,
        "produced_shots": shot_count,
        "validated_shots": passed,
        # Explicit semantic aliases for new clients.  Keep the v1 fields above
        # for compatibility, but do not force UI copy to guess their meaning.
        "draft_shots": shot_count,
        "safe_checkpoint_shots": passed,
        "pending_revalidation_shots": max(0, shot_count - passed),
        "resume_from_shot": resume_from,
        "resume_mode": resume_mode,
        "final_shot_valid": final_valid,
        "hard_gates_passed": bool(not gate_errors and (full_terminal or confirmed)),
        "hard_gate_issue_count": len(gate_errors),
        "hard_gate_issues": gate_errors[:30],
        "system_error": gate_system_error,
        "feature_flags": feature_flags,
        "confirmed": confirmed,
        "editable": bool(
            screenplay_ready
            and not running
            and not invalid
            and not gate_system_error
            and not feature_flags["safe_readonly"]
        ),
        "confirmable": bool(
            full_terminal
            and publication_evidence_ready
            and not feature_flags["safe_readonly"]
        ),
        "recommended_action": action,
        "write_block_reason": (
            "分镜正在生成或修复，请先暂停" if running
            else gate_system_error
            if gate_system_error
            else "状态组合不安全，请刷新" if invalid or state == "syncing"
            else None
        ),
        "_obsolete_policy_repair": obsolete_policy_repair,
    }

@router.get("/episodes/{episode_id}/storyboard/source")
def storyboard_authorized_source(episode_id: str):
    from app.storyboard_workspace import chapter_sources
    _episode_or_404(episode_id)
    enabled = str(get_setting("storyboard_source_rebind_enabled") or "true").lower() == "true"
    return {
        "episode_id": episode_id,
        "enabled": enabled,
        "chapters": chapter_sources(episode_id) if enabled else [],
        "disabled_reason": None if enabled else "原文重绑定正在灰度回滚；现有证据仍可只读审阅",
    }
