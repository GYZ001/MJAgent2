"""``_enqueue_shot_impl`` 的提示词编译、参考图画廊解析、幂等键与复用查询阶段。

拆自 ``app/media_exec/enqueue.py``（原 743 行的单一函数），移动未重写。命名
与循环导入规避手法同 ``.enqueue_context``（见其模块 docstring）。

``ensure_source_excerpt_in_prompt`` 在模块顶层导入（不像本文件其余大多数
外部调用那样延迟到函数体内）：它原来就是 ``enqueue.py`` 顶层的模块级导入，
``tests/conftest.py`` 的 ``patch_worker_everywhere`` 靠 ``hasattr(submodule,
name)`` 的动态扫描打桩——这个名字的真正定义在 ``app.compiler``（``media_exec``
包之外，扫描永远碰不到），只有当某个 ``media_exec`` 子模块把它做成**模块级**
再导出、且调用点用裸名（同模块全局查找）引用时，打桩才摸得到。若改成函数体内
局部导入，会绕过这份再导出直接从 ``app.compiler`` 现读，打桩因此静默失效
（2026-09-01 实测：``test_source_excerpt_failure_gets_bounded_retry_if_
scrubber_cannot_repair`` 打这个名字后仍读到未打桩的真实实现，断言失败）。
"""
from __future__ import annotations

import json
from typing import Any

from app.compiler import ensure_source_excerpt_in_prompt


def storyboard_pack_prompt_text(shot) -> str:
    """分镜台 2.0.0 段：原样复用模型已产出的 prompt_text，不重新编译。"""
    # shot_contract_json 才是权威来源，这里直接读 shot 模型上已解析好的
    # storyboard_pack_segment；不再插入占位「已采纳」版本，也不再依赖
    # adopted_version_id（见 app.production.storyboard_pack 模块文档）。
    prompt_text = str((shot.storyboard_pack_segment or {}).get("prompt_text") or "")
    if not prompt_text.strip():
        raise ValueError(
            "[STORYBOARD_PACK_PROMPT_MISSING] 该分镜台 2.0.0 段没有已产出的 "
            "prompt_text，请先在分镜台重新生成本段"
        )
    return prompt_text


def _preflight_gate_before_compile(shot, prev_shot, screenplay) -> None:
    from app.continuity import preflight_seedance_gates
    from app.compiler import CompileError

    preflight_errors = preflight_seedance_gates(
        shot, prev=prev_shot, prompt_text=None, screenplay=screenplay,
    )
    if preflight_errors:
        raise CompileError("；".join(preflight_errors))


def _compile_raw_prompt(
    shot, bible, extra_negative, critique, *, chain_after_shot_id, continuity_mode,
    incoming_transition, outgoing_transition, prompt_prev_state_out, screenplay,
    shot_plan, decision, first_frame_source, boundary_relation_edit,
    boundary_relation_action, boundary_start_state, previous_prompt_text,
) -> str:
    from app.compiler import compile_prompt

    return compile_prompt(
        shot,
        bible,
        extra_negative,
        with_refs=True,
        from_scene=False,
        chained=bool(chain_after_shot_id),
        critique=critique,
        prev_tail_action=None,
        with_last_frame=False,
        incoming_transition=incoming_transition,
        outgoing_transition=(
            outgoing_transition["transition"] if outgoing_transition else None
        ),
        next_scene=(
            outgoing_transition["next_scene"] if outgoing_transition else None
        ),
        next_first_frame_desc=(
            outgoing_transition["next_first_frame_desc"] if outgoing_transition else None
        ),
        continuity_mode=continuity_mode,
        prev_state_out=prompt_prev_state_out,
        voice_bible=screenplay.voice_bible,
        screenplay=screenplay,
        video_generation_mode=(
            shot_plan.mode.value if shot_plan is not None else decision.mode
        ),
        first_frame_source=first_frame_source,
        boundary_relation_edit=boundary_relation_edit,
        boundary_relation_action=boundary_relation_action,
        boundary_start_state=boundary_start_state,
        previous_prompt_text=previous_prompt_text,
    )


def _scrub_prompt_source(raw_prompt_text: str, shot, preflight_repair):
    from app.continuity import prompt_source_provenance_errors

    raw_source_errors = prompt_source_provenance_errors(raw_prompt_text, shot)
    prompt_text = ensure_source_excerpt_in_prompt(raw_prompt_text, shot)
    if raw_source_errors:
        prompt_scrub = {
            "repair": "source_excerpt_prompt_scrub",
            "matched_rules": raw_source_errors,
        }
        if preflight_repair:
            preflight_repair = {
                **preflight_repair,
                "prompt_scrubbed": True,
                "prompt_scrub_rules": raw_source_errors,
            }
        else:
            preflight_repair = prompt_scrub
    return prompt_text, preflight_repair


def _preflight_gate_after_compile(shot, prev_shot, prompt_text, screenplay) -> None:
    from app.compiler import CompileError
    from app.continuity import preflight_seedance_gates, prompt_source_provenance_errors

    preflight_errors = preflight_seedance_gates(
        shot, prev=prev_shot, prompt_text=prompt_text, screenplay=screenplay,
    )
    if preflight_errors:
        source_errors = prompt_source_provenance_errors(prompt_text, shot)
        raise CompileError(
            "；".join(preflight_errors),
            retryable=bool(source_errors),
            failure_kind="prompt_source_provenance" if source_errors else None,
        )


def compile_legacy_prompt(
    shot, prev_shot, screenplay, bible, extra_negative, critique, preflight_repair, *,
    chain_after_shot_id, continuity_mode, incoming_transition, outgoing_transition,
    prompt_prev_state_out, shot_plan, decision, first_frame_source,
    boundary_relation_edit, boundary_relation_action, boundary_start_state,
    previous_prompt_text,
):
    """非分镜台 2.0.0 镜头：跑校验门 -> compile_prompt -> 原文擦除 -> 再校验。"""
    _preflight_gate_before_compile(shot, prev_shot, screenplay)
    raw_prompt_text = _compile_raw_prompt(
        shot, bible, extra_negative, critique,
        chain_after_shot_id=chain_after_shot_id, continuity_mode=continuity_mode,
        incoming_transition=incoming_transition, outgoing_transition=outgoing_transition,
        prompt_prev_state_out=prompt_prev_state_out, screenplay=screenplay,
        shot_plan=shot_plan, decision=decision, first_frame_source=first_frame_source,
        boundary_relation_edit=boundary_relation_edit,
        boundary_relation_action=boundary_relation_action,
        boundary_start_state=boundary_start_state,
        previous_prompt_text=previous_prompt_text,
    )
    prompt_text, preflight_repair = _scrub_prompt_source(raw_prompt_text, shot, preflight_repair)
    _preflight_gate_after_compile(shot, prev_shot, prompt_text, screenplay)
    return prompt_text, preflight_repair


def resolve_reference_gallery(conn, shot_id, shot_row, ep, shot, screenplay, bible):
    """参考图是分镜级素材：加载既有画廊，并核对当前依赖清单是否仍匹配。"""
    from .enqueue import _load_reference_gallery
    from app.multiview import manifest_revisions_match, resolve_shot_asset_dependencies

    reference_gallery = _load_reference_gallery(conn, shot_row)
    current_reference_manifest = resolve_shot_asset_dependencies(
        project_id=ep["project_id"],
        episode_no=ep["episode_no"],
        shot_id=shot_id,
        shot=shot,
        scene_name=getattr(shot, "scene_name", "") or None,
        conn=conn,
        bible=bible,
        screenplay=screenplay,
    )
    if (
        reference_gallery
        and isinstance(reference_gallery.get("reference_manifest"), dict)
        and not manifest_revisions_match(
            reference_gallery["reference_manifest"], current_reference_manifest,
        )
    ):
        # A new portrait/scene revision invalidates the copied shot gallery even
        # when a user previously edited that gallery.
        reference_gallery = None
    return reference_gallery, current_reference_manifest


def build_idem_key(
    prompt_text: str, decision, chain_after_shot_id, chain_after_version_id, *,
    target_prompt_fingerprint: str, prompt_override, previous_prompt_fingerprint: str,
    current_reference_manifest: dict, reference_gallery, reroll: bool,
    operation_idempotency_key, supervisor_run_id, auto_retake_count: int,
    critique, critique_sources,
) -> str:
    """构建幂等键：普通重复点击复用历史成功版；reroll 显式打破幂等。"""
    from app import video_modes
    from app.compiler import VIDEO_PROMPT_CONTRACT_VERSION, idem_key as make_idem_key
    from app.video_prompt_ai import AI_VIDEO_PROMPT_CONTRACT_VERSION

    key_material = (
        prompt_text
        + f"|mode:{decision.mode}|plan:{video_modes.decision_to_dict(decision)}"
        + f"|after:{chain_after_shot_id or ''}"
        + f"|after_version:{chain_after_version_id or ''}"
        + f"|keyframe_prompt_contract:{video_modes.KEYFRAME_PROMPT_CONTRACT_VERSION}"
        + f"|video_prompt_contract:{VIDEO_PROMPT_CONTRACT_VERSION}"
        + f"|ai_video_prompt_contract:{AI_VIDEO_PROMPT_CONTRACT_VERSION}"
        + f"|ai_video_prompt_target:{target_prompt_fingerprint}"
        + f"|prompt_user_instruction:{(prompt_override or '').strip()}"
        + f"|previous_prompt:{previous_prompt_fingerprint}"
        + f"|reference_input_policy:{video_modes.REFERENCE_INPUT_POLICY_VERSION}"
        + f"|reference_dependencies:{current_reference_manifest.get('input_fingerprint') or ''}"
    )
    # 只有人工编辑会改变视频输入并打破原幂等键；未编辑画廊沿用历史幂等行为。
    if reference_gallery and reference_gallery["revision"] is not None:
        key_material += (
            f"|reference_gallery:{reference_gallery['source_version_id']}"
            f"@{reference_gallery['revision']}:{reference_gallery['fingerprint']}"
        )
    if reroll:
        reroll_scope = str(operation_idempotency_key or "").strip()
        if not reroll_scope:
            reroll_scope = make_idem_key(
                json.dumps(
                    {
                        "supervisor_run_id": supervisor_run_id or "",
                        "auto_retake_count": max(0, int(auto_retake_count)),
                        "critique": critique or [],
                        "critique_sources": critique_sources or [],
                        "previous_prompt_fingerprint": previous_prompt_fingerprint or "",
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
        return make_idem_key(key_material + f"|reroll_operation:{reroll_scope}")
    return make_idem_key(key_material)


def _lookup_reusable_version_row(conn, shot_id: str, key: str, supervisor_run_id):
    """复用成功版；同时挡住仍在排队/运行中的同键任务，避免双击重复付费。"""
    # waiting_human 不在这里：它是死路，把它当可复用会让同指纹的重新生成
    # 永远指回同一个卡死版本（见 CLAUDE.md「Gates and Criteria」）。
    reusable_statuses = [
        "succeeded", "queued", "running", "waiting_provider",
        "waiting_retry", "paused",
    ]
    if supervisor_run_id:
        reusable_statuses.append("abandoned")
    status_marks = ",".join("?" for _ in reusable_statuses)
    return conn.execute(
        "SELECT * FROM shot_versions WHERE shot_id=? AND idem_key=? "
        f"AND status IN ({status_marks}) "
        "ORDER BY CASE status WHEN 'succeeded' THEN 0 ELSE 1 END, version_no DESC "
        "LIMIT 1",
        (shot_id, key, *reusable_statuses)).fetchone()


def _bind_reused_operation(
    conn, existing, result: dict, shot_plan, *,
    operation_idempotency_key, operation_request_fingerprint, operation_claim_token,
    operation_command,
) -> None:
    if not (operation_idempotency_key and operation_request_fingerprint and operation_claim_token):
        return
    job = conn.execute(
        "SELECT id,provider_operation_id,status FROM jobs WHERE version_id=? ORDER BY created_at DESC LIMIT 1",
        (existing["id"],),
    ).fetchone()
    from app.video_command_operations import bind_video_command_operation

    bind_video_command_operation(
        command=operation_command,
        idempotency_key=operation_idempotency_key,
        request_fingerprint=operation_request_fingerprint,
        claim_token=operation_claim_token,
        binding={
            "plan_id": (shot_plan.episode_video_plan_id if shot_plan else None),
            "version_id": existing["id"],
            "job_id": job["id"] if job else None,
            "provider_operation_id": job["provider_operation_id"] if job else None,
            "result": result,
            **({"append_enqueued": {"shot_id": existing["shot_id"], **result}}
               if operation_command == "video.generate_episode" else {}),
        },
        conn=conn,
        merge=operation_command == "video.generate_episode",
    )
    conn.commit()


def find_reusable_version(
    conn, shot_id: str, key: str, *, reroll: bool, operation_idempotency_key,
    supervisor_run_id, dependency_snapshot, preflight_job_id, preflight_owner,
    preflight_repair, operation_request_fingerprint, operation_claim_token,
    operation_command, shot_plan,
) -> dict[str, Any] | None:
    """A paid command receipt can be lost after the durable enqueue commits.

    The domain key therefore owns replay safety too: the same logical
    operation must recover the exact version/job instead of relying on the
    outer bus. Returns ``None`` when no reusable version exists (caller must
    create a new one).
    """
    from .enqueue import _reused_reason_for_status, _resume_reused_paused_job

    if reroll and not operation_idempotency_key:
        return None
    existing = _lookup_reusable_version_row(conn, shot_id, key, supervisor_run_id)
    if not existing:
        return None
    result: dict[str, Any] = {
        "reused": True,
        "version_id": existing["id"],
        "reused_reason": _reused_reason_for_status(existing["status"]),
    }
    if existing["status"] in {"paused", "abandoned"}:
        resumed = _resume_reused_paused_job(
            existing["id"],
            supervisor_run_id=supervisor_run_id,
            dependency_snapshot=dependency_snapshot,
            preflight_job_id=preflight_job_id,
            preflight_owner=preflight_owner,
        )
        if resumed:
            result.update(resumed)
    if preflight_repair:
        result["preflight_repair"] = preflight_repair
    _bind_reused_operation(
        conn, existing, result, shot_plan,
        operation_idempotency_key=operation_idempotency_key,
        operation_request_fingerprint=operation_request_fingerprint,
        operation_claim_token=operation_claim_token,
        operation_command=operation_command,
    )
    return result
