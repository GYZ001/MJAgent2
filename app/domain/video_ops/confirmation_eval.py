"""分镜是否具备发起视频生成资格的评估（结构性/运营投影双重校验）。

从 app/domain/video_ops.py 按原样搬移；被本包其余大多数子模块依赖，是本包唯一没有反向依赖的基础层之一。单个函数
evaluate_storyboard_for_confirmation 264 行，是确认判据的唯一权威聚合点，不做拆分（移动未拆分）。
"""
from __future__ import annotations

import json

from app.compiler import (
    compile_prompt,
    shot_cost_cny,
)
from app.domain.storyboard_ops.mutation_primitives import _board_from_shot_rows
from app.episode_target import _compact_episode_target
from app.schemas import (
    Bible,
    EpisodeScreenplay,
    Shot,
    Storyboard,
)
from app.validators import (
    normalize_continuity,
    normalize_offbible_characters,
    normalize_transition_visuals,
    validate_storyboard,
    validate_storyboard_preserves_key_content,
    validate_storyboard_soundtrack,
)


def _shot_contract_json(shot: Shot) -> str:
    from app.continuity import shot_contract_dict
    return json.dumps(shot_contract_dict(shot), ensure_ascii=False)

class ConfirmationEvaluation:
    """只读确认评估结果；不写数据库。"""

    __slots__ = (
        "passed", "errors", "warnings", "issues", "board", "compact_target",
        "estimated_cost_cny",
    )

    def __init__(
        self,
        *,
        passed: bool,
        errors: list[str],
        warnings: list[str],
        issues: list,
        board: Storyboard,
        compact_target: int,
        estimated_cost_cny: float,
    ):
        self.passed = passed
        self.errors = errors
        self.warnings = list(warnings or [])
        self.issues = issues
        self.board = board
        self.compact_target = compact_target
        self.estimated_cost_cny = estimated_cost_cny

def _is_storyboard_terminal_for_confirmation(
    episode,
    checkpoint,
    *,
    shot_count: int,
    planned_shots: int,
    final_shot_valid: bool,
) -> bool:
    """只允许人工确认已经停止写入且通过 Supervisor 门禁的完整分镜。"""
    if shot_count <= 0 or shot_count != planned_shots or not final_shot_valid:
        return False
    if episode["status"] == "confirmed":
        return not bool(episode["script_error"])
    if episode["status"] != "scripted" or episode["script_error"]:
        return False
    if checkpoint is not None:
        phase = str(getattr(checkpoint, "phase", "") or "")
        validated = int(getattr(checkpoint, "validated_prefix_end", 0) or 0)
        expected = int(getattr(checkpoint, "expected_total", 0) or planned_shots)
        return bool(
            phase == "SUCCEEDED"
            and validated == shot_count
            and expected == shot_count
        )
    # 兼容人工编辑后没有 Supervisor checkpoint 的既有分镜；完整门禁仍会在下方重算。
    return True

def _storyboard_confirmation_progress(episode, rows) -> dict:
    """Return the current terminal-state facts used by preview and submit."""
    from app.storyboard_supervisor import load_latest_checkpoint

    checkpoint = load_latest_checkpoint(episode["id"])
    outline_count = 0
    try:
        outline_count = len(
            json.loads(episode["storyboard_outline_json"] or "{}").get("shots") or []
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        outline_count = 0
    structural_draft = episode["storyboard_artifact_id"] is None and outline_count > 0
    planned = int(
        (outline_count if structural_draft else 0)
        or (checkpoint.expected_total if checkpoint else 0)
        or outline_count
        or len(rows)
    )
    board = _board_from_shot_rows(rows, episode["episode_no"])
    final_valid = bool(board.shots and board.shots[-1].is_final)
    return {
        "checkpoint": checkpoint,
        "planned_shots": planned,
        "board": board,
        "final_shot_valid": final_valid,
        "terminal": _is_storyboard_terminal_for_confirmation(
            episode,
            checkpoint,
            shot_count=len(rows),
            planned_shots=planned,
            final_shot_valid=final_valid,
        ),
    }

def _storyboard_structural_errors(storyboard: Storyboard) -> list[str]:
    errors: list[str] = []
    shots = list(storyboard.shots or [])
    if not shots:
        return ["本集还没有分镜"]
    seen: set[int] = set()
    for index, shot in enumerate(shots, start=1):
        shot_no = int(shot.shot_no or 0)
        if shot_no <= 0:
            errors.append(f"第 {index} 个镜头缺少有效 shot_no")
        elif shot_no in seen:
            errors.append(f"shot_no={shot_no} 重复")
        else:
            seen.add(shot_no)
        if shot_no and shot_no != index:
            errors.append(f"shot_no={shot_no} 与顺序 {index} 不一致")
        if shot.storyboard_pack_segment is not None:
            # 分镜台 2.0.0（app.production.storyboard_pack）行：一行 = 一个 15
            # 秒段，段内 3-4 镜写进 prompt_text 文本。shot_size/camera_move/
            # first_frame_desc/last_frame_desc 描述单个连续镜头，在这类行上
            # 结构性不存在，不能再要求非空——那会把这个字段集合从"必填"错误
            # 地变成"永远不可能满足"。改要求这类行真正必须有的东西：
            # prompt_text（模型产出、未经代码加工，见该模块 persist_storyboard_pack
            # 的文档）与来源原文回指。
            segment = shot.storyboard_pack_segment
            if not str(segment.get("prompt_text") or "").strip():
                errors.append(f"第 {shot_no or index} 镜（分镜台 2.0.0 段）缺少 prompt_text")
            if not segment.get("source_segment_indexes"):
                errors.append(f"第 {shot_no or index} 镜（分镜台 2.0.0 段）缺少 source_segment_indexes")
            if not str(shot.scene_name or "").strip() and not str(shot.action_desc or "").strip():
                errors.append(f"第 {shot_no or index} 镜缺少 scene_name/action_desc")
            if int(shot.duration_s or 0) <= 0:
                errors.append(f"第 {shot_no or index} 镜缺少有效 duration_s")
            continue
        required = {
            "shot_size": shot.shot_size,
            "camera_move": shot.camera_move,
            "scene_name": shot.scene_name or shot.scene_setting,
            "action_desc": shot.action_desc,
            "first_frame_desc": shot.first_frame_desc,
            "last_frame_desc": shot.last_frame_desc,
            "source_excerpt": shot.source_excerpt,
        }
        missing = [name for name, value in required.items() if not str(value or "").strip()]
        if missing:
            errors.append(f"第 {shot_no or index} 镜缺少必填字段：{', '.join(missing)}")
        if int(shot.duration_s or 0) <= 0:
            errors.append(f"第 {shot_no or index} 镜缺少有效 duration_s")
    return errors

def _storyboard_operational_projection_errors(
    storyboard: Storyboard,
    screenplay: EpisodeScreenplay,
) -> list[str]:
    """Validate legacy delivery IDs and adjacent-scene routing as hard structure."""
    from app.scene_contract import scene_name_of, scene_time_of
    from app.validators import _scene_time_changed

    legacy_event_ids = {
        str(event.event_id or "").strip()
        for event in (screenplay.events or [])
        if str(event.event_id or "").strip()
    }
    errors: list[str] = []
    for index, shot in enumerate(storyboard.shots):
        event_id = str(shot.story_event_id or "").strip()
        if event_id and event_id not in legacy_event_ids:
            errors.append(
                "[STORYBOARD_OPERATIONAL_EVENT_ID_INVALID] "
                f"第 {shot.shot_no} 镜 story_event_id=「{event_id}」"
                "未映射到 screenplay.events 的唯一事件 ID"
            )
        if index == 0:
            continue
        previous = storyboard.shots[index - 1]
        same_scene = (
            scene_name_of(previous) == scene_name_of(shot)
            and not _scene_time_changed(
                scene_time_of(previous),
                scene_time_of(shot),
            )
        )
        mode = str(shot.continuity_mode or "").strip()
        if same_scene and mode == "scene_change":
            errors.append(
                "[STORYBOARD_OPERATIONAL_CONTINUITY_INVALID] "
                f"第 {shot.shot_no} 镜与上一镜同场同时却使用 scene_change"
            )
        elif not same_scene and mode != "scene_change":
            errors.append(
                "[STORYBOARD_OPERATIONAL_CONTINUITY_INVALID] "
                f"第 {shot.shot_no} 镜已跨场或跨时却使用 {mode or '空模式'}"
            )
        if same_scene and mode != "scene_change" and shot.transition != "硬切":
            errors.append(
                "[STORYBOARD_OPERATIONAL_TRANSITION_INVALID] "
                f"第 {shot.shot_no} 镜同场切换必须使用硬切"
            )
    return errors

def _evaluate_storyboard_pack_for_confirmation(
    episode, storyboard: Storyboard, bible: Bible, *, target_duration_s: int | None = None,
) -> ConfirmationEvaluation:
    """确认评估：分镜台 2.0.0（app.production.storyboard_pack）产出的分支。

    这条分支存在的原因：``evaluate_storyboard_for_confirmation`` 主体的
    ``normalize_offbible_characters`` / ``prefer_default_shot_durations`` /
    ``validate_storyboard_direction_contract`` / 逐镜 ``compile_prompt`` 等一整
    条链路，都是为「一行 = 一个连续镜头、有完整叙事契约字段」的旧架构写的。
    尤其是 ``compile_prompt`` 会用代码把 Shot 字段重新拼成一条提示词——这正是
    分镜台 2.0.0 要去掉的行为（prompt_text 已由模型在分镜台阶段直接产出并
    原样持久化，见 app.production.storyboard_pack 模块文档"交付前必须回答 #2"
    的答案）。对分镜台 2.0.0 的行重新跑一遍旧链路，轻则产生一堆对空字段的
    错误噪音，重则用编译器悄悄覆盖模型产出的 prompt_text，两者都不可接受，
    所以整体短路成这一条专用、轻量的评估，而不是在旧函数里散落十几个
    ``if shot.storyboard_pack_segment is not None`` 补丁。

    2026-08-26（用户拍板，第一版分镜提示词不设任何内容门禁）：
    ``validate_storyboard_pack_dialogue``（说话人在场 + 台词来源可溯源）不再
    计入 ``structural_errors``——校验本身照算，只是不再让确认失败。判据
    "校验照算，只是结论不再是拦截"：这里把它挪进 ``warnings``，和旧架构里
    ``dialogue_framing_errors`` 的既有先例（同一函数下方的 legacy 分支，那
    条规则也是算出来但只喂 score_warnings、不喂 structural_errors）完全同构，
    不是新发明的机制。``warnings`` 仍然转成 WARNING 级 Issue 返回，"记录下来
    但不拦截"在这里的意思是可见、不是消失。
    """
    from app.evaluations.issues import issues_from_messages
    from app.harness.types import IssueSeverity
    from app.validators import validate_storyboard, validate_storyboard_pack_dialogue

    board = Storyboard.model_validate(storyboard.model_dump(mode="json"))
    structural_errors = _storyboard_structural_errors(board)
    warnings = validate_storyboard_pack_dialogue(board)
    # validate_storyboard 对这类行本身也已经短路成同一条最小结构检查（见其
    # 函数体开头 storyboard_pack_segment 分支：段时长必须 15s、shot_no 连续
    # 递增），这两条是格式/结构问题，不是内容判断，保留阻断，复用而不是
    # 重复实现，保持"判据只有一处"。
    structural_errors.extend(
        validate_storyboard(board, bible, target_duration_s or 0)
    )
    compact_target = sum(int(s.duration_s or 0) for s in board.shots) or _compact_episode_target(
        target_duration_s if target_duration_s is not None else episode["target_duration_s"]
    )
    # 与 evaluate_storyboard_for_confirmation 主体的 legacy 分支同构：只把
    # warnings（非阻断）转成 Issue，severity=WARNING；structural_errors 仍
    # 通过 errors= / passed=False 直接暴露给调用方，不重复包一层 Issue。
    issues = issues_from_messages(
        warnings,
        subject=f"episode:{episode['id']}",
        severity=IssueSeverity.WARNING,
    )
    est = sum(shot_cost_cny(s.duration_s) for s in board.shots)
    return ConfirmationEvaluation(
        passed=not structural_errors,
        errors=structural_errors,
        warnings=warnings,
        issues=issues,
        board=board,
        compact_target=compact_target,
        estimated_cost_cny=round(est, 2),
    )

def evaluate_storyboard_for_confirmation(
    episode,
    storyboard: Storyboard,
    screenplay: EpisodeScreenplay | None,
    bible: Bible,
    *,
    has_real_bible: bool = True,
    target_duration_s: int | None = None,
    record_metrics: bool = True,
    allow_evidence_refinalize: bool = False,
) -> ConfirmationEvaluation:
    """与 confirm_episode_core 同源的只读确认评估（不写库）。

    Supervisor 与确认门必须共用此函数，避免「Supervisor 认为通过、确认门又用另一套规则失败」。
    """
    if storyboard.shots and all(
        shot.storyboard_pack_segment is not None for shot in storyboard.shots
    ):
        return _evaluate_storyboard_pack_for_confirmation(
            episode, storyboard, bible, target_duration_s=target_duration_s,
        )

    from app.evaluations.issues import issues_from_messages
    from app.harness.types import IssueSeverity
    from app.continuity import dialogue_framing_errors
    from app.validators import (
        prefer_default_shot_durations,
        score_storyboard_direction_readability,
        validate_storyboard_direction_contract,
        validate_storyboard_screenplay_scene_alignment,
    )

    # Evaluation is a read-only gate.  A shallow list copy still shares every
    # Shot instance with the caller, so normalizers could mutate the CAS
    # baseline while validating a repair candidate and create a false conflict.
    board = Storyboard.model_validate(storyboard.model_dump(mode="json"))
    narrative_plan = screenplay.narrative_plan if screenplay is not None else None
    character_changes = (
        [] if narrative_plan is not None else normalize_offbible_characters(board, bible)
    )
    if narrative_plan is None:
        normalize_continuity(board)
    prefer_default_shot_durations(
        board,
        narrative_authority=narrative_plan is not None,
        narrative_plan=narrative_plan,
    )
    normalize_transition_visuals(board)
    compact_target = _compact_episode_target(
        target_duration_s if target_duration_s is not None else episode["target_duration_s"]
    )
    actual_total = sum(int(s.duration_s or 0) for s in board.shots)
    compact_target = actual_total or compact_target

    structural_errors = _storyboard_structural_errors(board)
    outline = None
    if screenplay is not None and screenplay.narrative_plan is not None:
        structural_errors.extend(
            _storyboard_operational_projection_errors(
                board,
                screenplay,
            )
        )
        from app.identity_contracts import (
            IdentityContractError,
            storyboard_visual_identity_relation,
        )
        task_visible_ids: dict[int, list[str]] = {}
        try:
            from app.schemas import StoryboardOutline

            outline = StoryboardOutline.model_validate_json(
                episode["storyboard_outline_json"] or "{}"
            )
            task_visible_ids = {
                int(brief.shot_no): list(brief.visible_entity_ids)
                for brief in outline.shots
            }
        except (KeyError, IndexError, TypeError, ValueError):
            task_visible_ids = {}

        try:
            for shot in board.shots:
                relation = storyboard_visual_identity_relation(
                    shot,
                    task_visible_ids.get(
                        int(shot.shot_no),
                        list(shot.visible_entity_ids or []),
                    ),
                    bible,
                    screenplay,
                )
                unexpected = list(relation["unexpected_display_names"])
                binding_mismatches = list(
                    relation["identity_binding_mismatches"]
                )
                unresolved_tokens = [
                    *relation["unresolved_visible_tokens"],
                    *relation["unresolved_visible_entity_ids"],
                ]
                if unexpected or binding_mismatches or unresolved_tokens:
                    detail = (
                        f"可见身份 {unexpected} 不属于本镜叙事任务"
                        if unexpected
                        else (
                            "characters_visible 与 visible_entity_ids "
                            "未按同一身份、同一顺序绑定"
                        )
                    )
                    if unresolved_tokens:
                        detail += (
                            f"，包含未登记 token "
                            f"{list(dict.fromkeys(unresolved_tokens))}"
                        )
                    structural_errors.append(
                        "[SHOT_VISIBLE_IDENTITY_NOT_GROUNDED] "
                        f"第 {shot.shot_no} 镜{detail}"
                    )
        except IdentityContractError:
            # The existing narrative identity validator reports the complete
            # malformed-contract diagnostic.  Do not duplicate partial text.
            pass
        try:
            active_storyboard_run_id = episode["active_storyboard_run_id"]
            storyboard_artifact_id = episode["storyboard_artifact_id"]
            completion_certificate_id = episode[
                "storyboard_completion_certificate_id"
            ]
        except (KeyError, IndexError, TypeError):
            active_storyboard_run_id = getattr(
                episode,
                "active_storyboard_run_id",
                None,
            )
            storyboard_artifact_id = getattr(
                episode,
                "storyboard_artifact_id",
                None,
            )
            completion_certificate_id = getattr(
                episode,
                "storyboard_completion_certificate_id",
                None,
            )
        if (
            not active_storyboard_run_id
            and storyboard_artifact_id
            and completion_certificate_id
            and not allow_evidence_refinalize
        ):
            from app.production.certificate import (
                verify_current_storyboard_completion_authority,
            )

            try:
                verify_current_storyboard_completion_authority(
                    episode=episode,
                    current_storyboard_content=board.model_dump(mode="json"),
                )
            except ValueError as exc:
                structural_errors.append(
                    "[STORYBOARD_AUTHORITY_PROJECTION_DRIFT] "
                    f"当前正式镜头投影与已发布 Artifact/完成凭证不一致：{exc}"
                )
    stripped = sorted({
        str(change.get("stripped") or "").strip()
        for change in character_changes
        if str(change.get("stripped") or "").strip()
    })
    if stripped:
        structural_errors.append(
            "分镜残留未在剧本阶段解析的人物身份："
            + "、".join(stripped)
            + "；禁止确认和视频生产"
        )
    score_warnings: list[str] = []
    if screenplay is not None:
        structural_errors.extend(
            validate_storyboard_screenplay_scene_alignment(board, screenplay, bible)
        )
        if screenplay.narrative_plan is not None:
            from app.narrative import validate_storyboard_narrative

            try:
                raw_outline = episode["storyboard_outline_json"]
            except (KeyError, IndexError, TypeError):
                raw_outline = getattr(
                    episode,
                    "storyboard_outline_json",
                    None,
                )
            if raw_outline:
                try:
                    outline = StoryboardOutline.model_validate_json(
                        raw_outline
                    )
                except (TypeError, ValueError):
                    structural_errors.append(
                        "[STORYBOARD_OUTLINE_INVALID] 当前分镜大纲无法解析"
                    )
            score_warnings.extend(validate_storyboard_narrative(
                board,
                screenplay,
                outline=outline,
                complete=True,
                expected_scope_id=str(episode["id"]),
            ))
    if outline is not None:
        structural_errors.extend(
            validate_storyboard_direction_contract(board, outline)
        )
        score_warnings.extend(
            score_storyboard_direction_readability(board, outline)
        )
    score_warnings.extend(validate_storyboard(
        board,
        bible,
        compact_target,
        narrative_authority=narrative_plan is not None,
        narrative_plan=narrative_plan,
        screenplay=screenplay,
    ))
    dialogue_findings = [
        message
        for shot in board.shots
        for message in dialogue_framing_errors(
            shot,
            narrative_authority=narrative_plan is not None,
        )
    ]
    score_warnings.extend(dialogue_findings)
    if screenplay is not None:
        score_warnings.extend(validate_storyboard_soundtrack(board, screenplay, compact_target))
        score_warnings.extend(validate_storyboard_preserves_key_content(board, screenplay))
    if has_real_bible and not structural_errors:
        try:
            for s in board.shots:
                compile_prompt(
                    s.model_copy(deep=True),
                    bible,
                    screenplay=screenplay,
                )
        except Exception as exc:  # noqa: BLE001
            structural_errors.append(f"Prompt 编译失败：{exc}")
    try:
        ep_id = episode["id"]
    except Exception:  # noqa: BLE001
        ep_id = getattr(episode, "id", "") or ""
    _ = record_metrics
    issues = issues_from_messages(
        score_warnings,
        subject=f"episode:{ep_id}",
        severity=IssueSeverity.WARNING,
    )
    est = sum(shot_cost_cny(s.duration_s) for s in board.shots)
    return ConfirmationEvaluation(
        passed=not structural_errors,
        errors=structural_errors,
        warnings=score_warnings,
        issues=issues,
        board=board,
        compact_target=compact_target,
        estimated_cost_cny=round(est, 2),
    )
