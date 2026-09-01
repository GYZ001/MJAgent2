"""``_enqueue_shot_impl`` 的输入解析阶段：目标模型方言、绑定校验、分镜/剧本
上下文、模式决策、依赖镜头与连贯性/边界关系的解析。

拆自 ``app/media_exec/enqueue.py``（原 743 行的单一函数），移动未重写。这些
helper 只做只读解析与本地变量整理，不接触数据库写事务；真正的持久化落在
``.enqueue_persist``。为避免与 ``enqueue.py`` 之间的循环导入（``enqueue.py``
在模块顶层 import 本文件，本文件若在模块顶层反向 import ``.enqueue`` 的名字
会形成环），凡是需要 ``.enqueue`` 里定义的小工具函数（``_row_value`` /
``_load_shot_model`` / ``_decision_from_mode_plan`` / ``_outgoing_transition_
context`` / ``_transition_value``）都在各自函数体内部延迟导入，与
``enqueue.py`` 自己规避 ``.dispatch``/``.job_recovery`` 环的手法同构（见该
文件模块 docstring）。
"""
from __future__ import annotations

from typing import Any

from app import hiagent


def resolve_target_video_profile() -> tuple[str | None, str | None, Any, str]:
    """解析当前生效的视频供应商/模型，及其对应的提示词方言与目标指纹。"""
    from app.video_prompt_profiles import (
        resolve_video_prompt_profile,
        video_prompt_target_fingerprint,
    )

    target_video_provider = hiagent.active_provider("video")
    target_video_model = hiagent.active_model(
        "video",
        target_video_provider,
    )
    target_prompt_profile = resolve_video_prompt_profile(
        provider=target_video_provider,
        model=target_video_model,
    )
    target_prompt_fingerprint = video_prompt_target_fingerprint(
        provider=target_video_provider,
        model=target_video_model,
    )
    return (
        target_video_provider,
        target_video_model,
        target_prompt_profile,
        target_prompt_fingerprint,
    )


def load_video_binding_context(conn, shot_id: str, target_video_provider: str | None):
    """加载镜头/分集/项目行；校验 Harness 开关与本集视频模型绑定一致性。"""
    from .enqueue import _row_value

    shot_row = conn.execute("SELECT * FROM shots WHERE id=?", (shot_id,)).fetchone()
    if not shot_row:
        raise ValueError(f"镜头不存在：{shot_id}")
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (shot_row["episode_id"],)).fetchone()
    project = conn.execute("SELECT * FROM projects WHERE id=?", (ep["project_id"],)).fetchone()
    if not bool(_row_value(project, "harness_engine_enabled", 1)):
        raise ValueError("该项目的 Harness Engine 已由灰度开关隔离")
    episode_bound_provider = str(_row_value(ep, "target_video_model") or "").strip() or "hiagent"
    # 按适配器族比较，不按 provider key 原始字符串比：自建实例（custom:xxx）
    # 复用内置协议实现，字符串比较会把"同协议、不同连接"误判成绑定不一致。
    from app import video_providers

    if not video_providers.same_family(episode_bound_provider, target_video_provider):
        raise ValueError(
            f"[VIDEO_MODEL_BINDING_MISMATCH] 本集绑定的视频模型是 {episode_bound_provider}，"
            f"当前生效模型是 {target_video_provider or '(未配置)'}"
            "（两者提示词方言不兼容，不能混投）；"
            "请在分镜台切换回本集绑定的模型，或先在模型中心把生效模型切到该值再重试"
        )
    return shot_row, ep, project


def resolve_shot_context(conn, shot_row, ep, project, authority_context):
    """解析人设圣经、镜头模型、分镜台 2.0.0 标记、剧本与前序镜头列表。"""
    from .enqueue import _load_shot_model
    from app.continuity import resolve_do_not_repeat_texts
    from app.domain.common import _project_bible_or_placeholder

    bible = _project_bible_or_placeholder(project)
    # Compile the paid video request from the accepted per-episode portrait
    # revision, not from a possibly older project-Bible appearance string.
    from app.portraits import bible_for_episode

    bible = bible_for_episode(ep["project_id"], bible, ep["episode_no"])
    shot = _load_shot_model(shot_row)
    # 分镜台 2.0.0 行：prompt_text 已在分镜台阶段由模型直接产出并原样持久化，
    # 这里必须原样复用，不能再走 compile_prompt 重新拼装。
    is_storyboard_pack_shot = shot.storyboard_pack_segment is not None
    screenplay = authority_context.screenplay
    prior_rows = conn.execute(
        "SELECT * FROM shots WHERE episode_id=? AND shot_no<? ORDER BY shot_no",
        (shot_row["episode_id"], int(shot_row["shot_no"])),
    ).fetchall()
    prior_shots = [_load_shot_model(row) for row in prior_rows]
    # Persisted legacy boards may contain snake_case ledger IDs. Resolve them
    # to Chinese semantics at the final model boundary; unresolved IDs vanish.
    shot.do_not_repeat = resolve_do_not_repeat_texts(shot, screenplay, prior_shots)
    return bible, shot, is_storyboard_pack_shot, screenplay, prior_shots


def resolve_mode_decision(conn, shot_id: str, shot_row, authority_context):
    """narrative 权威镜头绑定当前已验证的视频计划；否则回退参考图模式决策。"""
    from app import video_modes

    shot_plan = None
    if authority_context.narrative_authority_required:
        from app.video_plan import get_shot_plan

        shot_plan = get_shot_plan(shot_id, conn=conn)
        if shot_plan is None:
            raise ValueError(
                "[VIDEO_PLAN_REQUIRED] 叙事镜头必须绑定当前已验证的 "
                "EpisodeVideoGenerationPlan，禁止默认回退参考图模式"
            )
        decision = video_modes.dict_to_decision(
            shot_plan.model_dump(mode="json")
        )
    else:
        from .enqueue import _decision_from_mode_plan

        decision = (
            _decision_from_mode_plan(shot_row)
            or video_modes.default_reference_decision()
        )
        if decision.mode != video_modes.REFERENCE_IMAGE_MODE:
            decision = video_modes.default_reference_decision()
    return shot_plan, decision


def resolve_first_frame_requirement(shot_plan):
    """当计划模式为首帧/首尾帧时，解析首帧素材来源与来源镜头。"""
    from app import video_modes

    first_frame_requirement = None
    first_frame_source = None
    boundary_source_shot_id = None
    if shot_plan is not None and shot_plan.mode.value in {
        video_modes.FIRST_FRAME_MODE,
        video_modes.FIRST_LAST_FRAME_MODE,
    }:
        first_frame_requirement = next(
            (
                item
                for item in shot_plan.required_assets
                if item.role == "first_frame"
            ),
            None,
        )
        if first_frame_requirement is not None:
            first_frame_source = first_frame_requirement.source.value
            boundary_source_shot_id = first_frame_requirement.source_shot_id
    return first_frame_requirement, first_frame_source, boundary_source_shot_id


def _resolve_prev_and_boundary_rows(conn, shot_row, shot_plan, after_shot_id, boundary_source_shot_id):
    """解析连贯依赖的前序镜头行（计划依赖优先，其次是序号前一镜）与边界来源行。"""
    prev_row = None
    planned_dependency_id = (
        str(shot_plan.depends_on_shot_id)
        if shot_plan is not None and shot_plan.depends_on_shot_id
        else None
    )
    if (
        shot_plan is not None
        and after_shot_id is not None
        and after_shot_id != planned_dependency_id
    ):
        raise ValueError("请求的前序镜头与已发布视频计划不一致")
    dependency_id = planned_dependency_id if shot_plan is not None else after_shot_id
    if dependency_id:
        prev_row = conn.execute(
            "SELECT * FROM shots WHERE id=? AND episode_id=?",
            (dependency_id, shot_row["episode_id"]),
        ).fetchone()
        if prev_row is None and shot_plan is not None:
            raise ValueError("视频计划引用的前序镜头不存在或不属于本集")
    if prev_row is None and shot_plan is None and int(shot_row["shot_no"]) > 1:
        prev_row = conn.execute(
            "SELECT * FROM shots WHERE episode_id=? AND shot_no<? ORDER BY shot_no DESC LIMIT 1",
            (shot_row["episode_id"], int(shot_row["shot_no"])),
        ).fetchone()
    boundary_prev_row = None
    if boundary_source_shot_id:
        boundary_prev_row = conn.execute(
            "SELECT * FROM shots WHERE id=? AND episode_id=?",
            (boundary_source_shot_id, shot_row["episode_id"]),
        ).fetchone()
        if boundary_prev_row is None:
            raise ValueError("视频计划的首帧来源镜头不存在或不属于本集")
    return prev_row, boundary_prev_row, planned_dependency_id


def resolve_dependency_rows(conn, shot_row, shot_plan, after_shot_id, boundary_source_shot_id):
    """解析前序/边界镜头行，及承载 prompt 衔接上下文的镜头行。"""
    from .enqueue import _load_shot_model

    prev_row, boundary_prev_row, planned_dependency_id = _resolve_prev_and_boundary_rows(
        conn, shot_row, shot_plan, after_shot_id, boundary_source_shot_id,
    )
    continuity_prev_row = boundary_prev_row or prev_row
    prev_shot = (
        _load_shot_model(continuity_prev_row)
        if continuity_prev_row is not None
        else None
    )
    sequence_prev_row = None
    if int(shot_row["shot_no"]) > 1:
        sequence_prev_row = conn.execute(
            """SELECT * FROM shots
               WHERE episode_id=? AND shot_no<?
               ORDER BY shot_no DESC LIMIT 1""",
            (shot_row["episode_id"], int(shot_row["shot_no"])),
        ).fetchone()
    prompt_context_row = continuity_prev_row or sequence_prev_row
    return (
        prev_row,
        boundary_prev_row,
        prev_shot,
        prompt_context_row,
        planned_dependency_id,
    )


def resolve_previous_prompt(conn, prompt_context_row):
    """取上一镜（或链首）已采纳版本的 prompt_text，作为衔接参照与指纹。"""
    import hashlib

    from .enqueue import _row_value

    previous_prompt_version = None
    if prompt_context_row is not None:
        adopted_version_id = _row_value(prompt_context_row, "adopted_version_id")
        if adopted_version_id:
            previous_prompt_version = conn.execute(
                """SELECT id,prompt_text FROM shot_versions
                   WHERE id=? AND shot_id=?""",
                (adopted_version_id, prompt_context_row["id"]),
            ).fetchone()
        if previous_prompt_version is None:
            previous_prompt_version = conn.execute(
                """SELECT id,prompt_text FROM shot_versions
                   WHERE shot_id=? AND prompt_text IS NOT NULL
                   ORDER BY version_no DESC LIMIT 1""",
                (prompt_context_row["id"],),
            ).fetchone()
    previous_prompt_text = (
        str(previous_prompt_version["prompt_text"] or "")
        if previous_prompt_version is not None
        else ""
    )
    previous_prompt_fingerprint = (
        hashlib.sha256(previous_prompt_text.encode("utf-8")).hexdigest()
        if previous_prompt_text
        else ""
    )
    return previous_prompt_version, previous_prompt_text, previous_prompt_fingerprint


def apply_continuity_mode(shot, prev_shot, is_storyboard_pack_shot: bool) -> str:
    """确定并写回本镜的连贯模式；分镜台 2.0.0 段固定为跨段硬切场景变化。"""
    from app.continuity import derive_continuity_mode, uses_previous_tail_frame

    if is_storyboard_pack_shot:
        # 冻结的设计决策：分镜台 2.0.0 只用参考图模式，跨段不做首尾帧链
        # （docs/STORYBOARD_PROMPT_IR_DESIGN.md「跨段一致性」）。
        continuity_mode = "scene_change"
        shot.continuity_mode = continuity_mode
        shot.continuity_from_prev = False
        shot.transition = "硬切"
    else:
        continuity_mode = derive_continuity_mode(shot, prev_shot)
        shot.continuity_mode = continuity_mode
        shot.continuity_from_prev = uses_previous_tail_frame(continuity_mode)
        if continuity_mode != "scene_change":
            shot.transition = "硬切"
    return continuity_mode


def resolve_boundary_relation(
    shot, prev_shot, shot_plan, first_frame_requirement, first_frame_source, boundary_prev_row,
):
    """解析首尾帧边界的剪辑/动作衔接关系与起始状态描述。"""
    from app.continuity import effective_state_out, resolve_first_last_boundary_relation

    boundary_relation_edit = (
        shot_plan.relations.edit if shot_plan is not None else None
    )
    boundary_relation_action = (
        shot_plan.relations.action if shot_plan is not None else None
    )
    boundary_relation_reason = "planned_relation"
    if first_frame_requirement is not None:
        (
            boundary_relation_edit,
            boundary_relation_action,
            boundary_relation_reason,
        ) = resolve_first_last_boundary_relation(
            shot,
            prev_shot,
            planned_edit=boundary_relation_edit,
            planned_action=boundary_relation_action,
        )
    prev_state_out = effective_state_out(prev_shot) if prev_shot else None
    boundary_start_state = None
    if boundary_prev_row is not None and prev_shot is not None:
        if first_frame_source == "PREVIOUS_STATIC_TAIL":
            boundary_start_state = (
                (prev_shot.last_frame_desc or "").strip()
                or prev_state_out
            )
        else:
            boundary_start_state = prev_state_out
    return (
        boundary_relation_edit,
        boundary_relation_action,
        boundary_relation_reason,
        boundary_start_state,
        prev_state_out,
    )


def resolve_chain_dependency(shot, shot_plan, continuity_mode, prev_row, prev_state_out, planned_dependency_id):
    """解析跨镜连贯只继承的上一镜尾状态，与视频链的前序镜头/版本引用。"""
    from app.continuity import uses_previous_tail_frame
    from .enqueue import _row_value

    planned_state_dependency = (
        shot_plan is not None and shot_plan.state_dependency != "none"
    )
    prompt_prev_state_out = (
        prev_state_out
        if planned_state_dependency or uses_previous_tail_frame(continuity_mode)
        else None
    )
    if prompt_prev_state_out:
        shot.state_in = prompt_prev_state_out
    chain_after_shot_id = (
        planned_dependency_id
        if shot_plan is not None
        else (
            (prev_row["id"] if prev_row else None)
            if uses_previous_tail_frame(continuity_mode) else None
        )
    )
    chain_after_version_id = (
        _row_value(prev_row, "adopted_version_id")
        if chain_after_shot_id else None
    )
    return prompt_prev_state_out, chain_after_shot_id, chain_after_version_id


def resolve_transitions(conn, shot_row, continuity_mode):
    """解析出场转场（由下一镜决定）与入场转场（由本镜自身决定）。"""
    from app.continuity import uses_previous_tail_frame
    from .enqueue import _outgoing_transition_context, _transition_value

    outgoing_transition = _outgoing_transition_context(conn, shot_row)
    incoming_transition = None
    if int(shot_row["shot_no"]) > 1 and not uses_previous_tail_frame(continuity_mode):
        incoming_transition = _transition_value(shot_row)
        if incoming_transition == "硬切":
            incoming_transition = None
    return outgoing_transition, incoming_transition
