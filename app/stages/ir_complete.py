"""剧本 IR 保真——IR 生成主循环 _complete_screenplay_ir_fidelity。"""
from __future__ import annotations

import hashlib
import json
import math
from typing import Any


from app import textmatch
from app.db import get_setting
from app.harness import model_gateway
from app.narrative_blueprint import (
    NarrativeBlueprint,
    derive_blueprint_scene_plans,
)
from app.schemas import (Bible)
from app.screenplay_ir import (
    IR_COMPILER_VERSION,
    IR_MIN_ADAPTED_SOURCE_RATIO,
    IR_VERSION,
    ScreenplayGenerationIR,
    ScreenplayIRFidelityError,
    compile_screenplay_ir,
)

from .constants import (
    IR_FIDELITY_PATCH_MAX_TOKENS,
    SCREENPLAY_BASELINE_PROMPT_VERSION,
    SYSTEM_PREFIX,
)
from .ir_patch import (
    _IRFidelityPatch,
    _ir_fidelity_patch_context,
    _merge_ir_fidelity_patch,
    _select_fidelity_blueprint_plans,
)
from .ir_snapshot import _narrative_blueprint_content_hash


async def _complete_screenplay_ir_fidelity(
    candidate: ScreenplayGenerationIR,
    *,
    episode: dict[str, Any],
    source_text: str,
    bible: Bible,
    parent_artifact_id: str | None,
    narrative_blueprint: NarrativeBlueprint | None = None,
) -> ScreenplayGenerationIR:
    from app.evidence import repository as evidence_repository
    from app.harness.types import EvidenceArtifact
    from app.observability.tracing import current_trace
    from app.identity_adjudication import adjudicate_screenplay_ir_identities

    candidate = await adjudicate_screenplay_ir_identities(
        candidate,
        episode=episode,
        source_text=source_text,
        bible=bible,
    )

    patched = False
    consecutive_empty_patches = 0
    fidelity_error: ValueError | None = None
    initial_gap_context = _ir_fidelity_patch_context(candidate, source_text)
    missing_scene_plan_count = (
        max(
            0,
            len(derive_blueprint_scene_plans(narrative_blueprint))
            - len(candidate.scenes),
        )
        if narrative_blueprint is not None else 0
    )
    configured_max_rounds = max(
        1, min(8, int(get_setting("screenplay_fidelity_max_rounds") or 8))
    )
    max_rounds = min(
        configured_max_rounds,
        max(
            2,
            len(initial_gap_context["windows_requiring_expansion"])
            + missing_scene_plan_count,
        ),
    )
    for round_no in range(1, max_rounds + 1):
        try:
            compile_screenplay_ir(
                candidate.model_copy(deep=True),
                episode=episode,
                source_text=source_text,
                bible=bible,
            )
            if patched:
                trace = current_trace()
                completed_artifact = evidence_repository.create_artifact(
                    EvidenceArtifact(
                        type="screenplay_generation_ir",
                        scope_type="episode",
                        scope_id=str(episode.get("id") or ""),
                        status="candidate",
                        trust_level="T1",
                        content=candidate.model_dump(mode="json"),
                        parent_artifact_ids=(
                            [parent_artifact_id]
                            if parent_artifact_id else []
                        ),
                        contract_version=IR_VERSION,
                        prompt_version=SCREENPLAY_BASELINE_PROMPT_VERSION,
                        model_snapshot={
                            "compiler_version": IR_COMPILER_VERSION,
                            "fidelity_completion_rounds": round_no - 1,
                            "blueprint_hash": (
                                _narrative_blueprint_content_hash(
                                    narrative_blueprint
                                )
                            ),
                        },
                    ),
                    step_run_id=trace.step_run_id,
                )
                object.__setattr__(
                    candidate,
                    "evidence_artifact_id",
                    completed_artifact["id"],
                )
            return candidate
        except ValueError as exc:
            if not isinstance(exc, ScreenplayIRFidelityError):
                raise
            fidelity_error = exc

        context = _ir_fidelity_patch_context(candidate, source_text)
        if narrative_blueprint is not None:
            plans = derive_blueprint_scene_plans(narrative_blueprint)
            (
                _remaining_plans,
                internal_plans,
                selected_plans,
                _repair_source_ids,
            ) = _select_fidelity_blueprint_plans(
                context,
                plans,
                candidate_scene_count=len(candidate.scenes),
            )
            original_windows = context["windows_requiring_expansion"]
            original_missing_source_ids = context["missing_source_ids"]

            def project_windows(
                allowed_source_ids: set[str],
            ) -> list[dict[str, Any]]:
                projected: list[dict[str, Any]] = []
                for window in original_windows:
                    source_segments = [
                        segment
                        for segment in window["source_segments"]
                        if segment["source_segment_id"]
                        in allowed_source_ids
                    ]
                    if not source_segments:
                        continue
                    source_ids = {
                        segment["source_segment_id"]
                        for segment in source_segments
                    }
                    existing_units = [
                        unit
                        for unit in window["existing_units"]
                        if source_ids.intersection(
                            unit["source_segment_ids"]
                        )
                    ]
                    source_chars = sum(
                        len(textmatch.condense(segment["text"]))
                        for segment in source_segments
                    )
                    adapted_chars = sum(
                        len(textmatch.condense(unit["text"]))
                        for unit in existing_units
                    )
                    target_chars = math.ceil(
                        source_chars * IR_MIN_ADAPTED_SOURCE_RATIO
                    )
                    missing_source_ids = [
                        source_id
                        for source_id in window["missing_source_ids"]
                        if source_id in allowed_source_ids
                    ]
                    if (
                        adapted_chars >= target_chars
                        and not missing_source_ids
                    ):
                        continue
                    projected.append({
                        **window,
                        "source_range": (
                            f"{source_segments[0]['source_segment_id']}-"
                            f"{source_segments[-1]['source_segment_id']}"
                        ),
                        "source_chars": source_chars,
                        "existing_adapted_chars": adapted_chars,
                        "minimum_final_adapted_chars": target_chars,
                        "minimum_additional_chars": max(
                            0, target_chars - adapted_chars,
                        ),
                        "missing_source_ids": missing_source_ids,
                        "source_segments": source_segments,
                        "existing_units": existing_units,
                    })
                return projected

            allowed_source_ids = {
                source_id
                for plan in (
                    internal_plans[:6]
                    if internal_plans
                    else selected_plans
                )
                for source_id in plan.source_segment_ids
            }
            selected_windows = project_windows(allowed_source_ids)
            if not selected_windows and internal_plans and _remaining_plans:
                # A batch that happens to contain no gap is not a failure --
                # the gap simply lives in a later batch.  Only inspecting the
                # first six remaining plans made that an episode-ending
                # ValueError whenever the missing source sat further along
                # (production EP2 died at IR_MERGE this way).  Walk the
                # remaining plans batch by batch until one actually has work.
                for start in range(0, len(_remaining_plans), 6):
                    batch = _remaining_plans[start:start + 6]
                    if not batch:
                        break
                    batch_source_ids = {
                        source_id
                        for plan in batch
                        for source_id in plan.source_segment_ids
                    }
                    batch_windows = project_windows(batch_source_ids)
                    if batch_windows:
                        selected_plans = batch
                        allowed_source_ids = batch_source_ids
                        selected_windows = batch_windows
                        break
            context["required_remaining_scene_plans"] = [
                plan.model_dump(mode="json")
                for plan in selected_plans
            ]
            context["missing_source_ids"] = [
                source_id
                for source_id in original_missing_source_ids
                if source_id in allowed_source_ids
            ]
            context["windows_requiring_expansion"] = selected_windows
        else:
            context["windows_requiring_expansion"] = context[
                "windows_requiring_expansion"
            ][:2]
        windows = context["windows_requiring_expansion"]
        if not windows:
            # 编译器报了保真缺口，窗口投影器却找不到任何可补窗口——两者对
            # "还缺什么"的判断不一致。裸抛 "没有可处理的缺口窗口" 会把编译器
            # 的诊断整个吞掉，让这一类失败无从下手；把它原样带出来。
            raise ValueError(
                "IR 保真补写没有可处理的缺口窗口；编译器报告的缺口："
                + str(fidelity_error)[:400]
            )
        if candidate.source_scene_owners:
            context["source_scene_owners"] = dict(
                candidate.source_scene_owners
            )
            context["scene_derivations"] = list(
                candidate.scene_derivations
            )
        prompt = (
            "任务：只补写现有剧本 IR 中缺失或过度压缩的剧情单元，不重写整集。\n"
            f"这是第 {round_no} 轮局部补写；只要上下文仍列出缺口，禁止返回空数组。\n"
            "每个窗口都给出了原文、已有 units 和最低补写字符数。新增 units 必须把"
            "遗漏的动作、人物反应、对白关系、因果桥梁和场景转换真正写进 text；"
            "禁止重复已有内容凑字数。\n"
            "source_segment_ids 只能引用对应窗口内 SRC，必须按原文顺序且连续；"
            "若上下文含 source_scene_owners，每个 SRC 只能写入其 owner scene；"
            "跨场信息只能读取 scene_derivations，不得重复消费来源场 SRC。"
            "dialogue.source_text 必须逐字来自声明的 SRC，并使用 identities 中已有"
            " speaker_key。scene_key 从现有 scenes 选择。每个 insertion 的 units 按"
            "播放顺序输出，event_key 可使用任意临时唯一值，后端会重编号。"
            "若缺失 SRC 是现有正文之后的连续尾段，必须通过 new_scenes 续写必要的新场次，"
            "不得把不同时空强塞进最后一个旧场；非尾段缺口才使用 insertions。\n\n"
            "若上下文包含 required_remaining_scene_plans，new_scenes 必须逐项使用其"
            " key、scene_heading、顺序和 source_segment_ids 分配；禁止合并、跳过或"
            "自行改名蓝图场次。每个新 scene 的 units 只能引用该 plan 允许的 SRC。\n\n"
            "保真缺口上下文：\n"
            + json.dumps(context, ensure_ascii=False, separators=(",", ":"))
            + "\n\n只输出 JSON："
            '{"new_scenes":[{"key":"sc_next",'
            '"scene_heading":"【场】日 / 地点","story_function":"",'
            '"summary":"","conflict":"","turn":"","units":['
            '{"kind":"action","text":"尾段可拍动作",'
            '"event_key":"tail1","source_segment_ids":["SRC0100"]}]}],'
            '"insertions":[{"scene_key":"sc1",'
            '"insert_after_event_key":"ev1","units":['
            '{"kind":"action","text":"新增可拍动作",'
            '"event_key":"patch1","source_segment_ids":["SRC0003"]},'
            '{"kind":"dialogue","text":"改编台词","event_key":"patch2",'
            '"source_segment_ids":["SRC0003"],"speaker_key":"person_a",'
            '"function":"statement","source_text":"原文逐字话语",'
            '"chain_key":"dc_patch"}]}]}'
        )
        structured_patch = await model_gateway.chat_structured(
            [
                {"role": "system", "content": SYSTEM_PREFIX},
                {"role": "user", "content": prompt},
            ],
            model_type=_IRFidelityPatch,
            validate=None,
            operation_id=(
                f"screenplay.ir-fidelity:{IR_VERSION}:"
                f"{episode.get('id') or episode.get('episode_no')}:"
                f"{round_no}:"
                + hashlib.sha256(
                    json.dumps(context, ensure_ascii=False, sort_keys=True).encode("utf-8")
                ).hexdigest()
            ),
            temperature=0.3,
            # 推理模型的 reasoning token 计入 completion_tokens，固定 8192 会在
            # 写出补丁之前就被思考耗尽（生产上 EP3 拿到 finish_reason=length /
            # completion_tokens=8193）。预算只是上限，真实成本按实际用量结算。
            max_tokens=IR_FIDELITY_PATCH_MAX_TOKENS,
            format_retry_limit=int(
                get_setting("screenplay_format_retry_limit") or 1
            ),
            semantic_retry_limit=0,
            call_meta={
                "stage": "剧本来源保真局部补写",
                "stage_key": "screenplay_ir_fidelity_patch",
                "call_role": "stage_repair",
                "call_role_label": "局部剧情补写",
                "repair_round": round_no,
                "episode_id": str(episode.get("id") or ""),
                "generation_contract": IR_VERSION,
                "compiler_version": IR_COMPILER_VERSION,
                "expected_json": True,
                "reuse_successful_operation": True,
            },
            repair_context=json.dumps(
                context, ensure_ascii=False, separators=(",", ":")
            ),
        )
        raw = structured_patch.model_dump_json()
        trace = current_trace()
        raw_artifact = evidence_repository.create_artifact(
            EvidenceArtifact(
                type="screenplay_generation_ir_fidelity_patch_raw",
                scope_type="episode",
                scope_id=str(episode.get("id") or ""),
                status="candidate",
                trust_level="T0",
                content={"raw_output": raw, "round": round_no},
                parent_artifact_ids=(
                    [parent_artifact_id] if parent_artifact_id else []
                ),
                contract_version=IR_VERSION,
                prompt_version=SCREENPLAY_BASELINE_PROMPT_VERSION,
            ),
            step_run_id=trace.step_run_id,
        )
        payload = structured_patch.model_dump(mode="json")
        for new_scene in payload.get("new_scenes", []):
            if not isinstance(new_scene, dict):
                continue
            new_scene.setdefault(
                "story_function",
                str(new_scene.get("summary") or "推进本场剧情"),
            )
        patch = _IRFidelityPatch.model_validate(payload)
        inserted = _merge_ir_fidelity_patch(
            candidate,
            patch,
            source_text,
            round_no=round_no,
        )
        if not inserted:
            consecutive_empty_patches += 1
            if consecutive_empty_patches >= 2:
                raise ValueError(
                    "IR 保真补写连续两轮未返回任何可合并 unit"
                )
            continue
        consecutive_empty_patches = 0
        patched = True
        normalized_artifact = evidence_repository.create_artifact(
            EvidenceArtifact(
                type="screenplay_generation_ir_fidelity_patch",
                scope_type="episode",
                scope_id=str(episode.get("id") or ""),
                status="validated",
                trust_level="T1",
                content=patch.model_dump(mode="json"),
                parent_artifact_ids=[raw_artifact["id"]],
                contract_version=IR_VERSION,
                prompt_version=SCREENPLAY_BASELINE_PROMPT_VERSION,
                model_snapshot={
                    "inserted_units": inserted,
                    "resolved_source_ids": sorted(set(
                        source_id
                        for insertion in patch.insertions
                        for unit in insertion.units
                        for source_id in unit.source_segment_ids
                    ).union(
                        source_id
                        for scene in patch.new_scenes
                        for unit in scene.units
                        for source_id in unit.source_segment_ids
                    )),
                    "missing_source_ids_before": context["missing_source_ids"],
                },
            ),
            step_run_id=trace.step_run_id,
        )
        parent_artifact_id = normalized_artifact["id"]

    compile_screenplay_ir(
        candidate.model_copy(deep=True),
        episode=episode,
        source_text=source_text,
        bible=bible,
    )
    trace = current_trace()
    completed_artifact = evidence_repository.create_artifact(
        EvidenceArtifact(
            type="screenplay_generation_ir",
            scope_type="episode",
            scope_id=str(episode.get("id") or ""),
            status="candidate",
            trust_level="T1",
            content=candidate.model_dump(mode="json"),
            parent_artifact_ids=(
                [parent_artifact_id] if parent_artifact_id else []
            ),
            contract_version=IR_VERSION,
            prompt_version=SCREENPLAY_BASELINE_PROMPT_VERSION,
            model_snapshot={
                "compiler_version": IR_COMPILER_VERSION,
                "fidelity_completion_rounds": max_rounds,
                "blueprint_hash": _narrative_blueprint_content_hash(
                    narrative_blueprint
                ),
            },
        ),
        step_run_id=trace.step_run_id,
    )
    object.__setattr__(
        candidate,
        "evidence_artifact_id",
        completed_artifact["id"],
    )
    return candidate
