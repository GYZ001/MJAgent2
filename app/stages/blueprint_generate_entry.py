"""叙事蓝图——_generate_screenplay_narrative_blueprint 生成入口与检查点保存。"""
from __future__ import annotations

import json
from typing import Any


from app import config
from app.db import get_conn, log_provider_call
from app.loops import AgentLoop, AgentLoopFailure, AgentLoopPolicy
from app.narrative_blueprint import (
    BLUEPRINT_MAX_SOURCE_SEGMENTS_PER_NODE,
    BLUEPRINT_VERSION,
    NarrativeBlueprint,
    blueprint_prompt_contract,
    derive_blueprint_scene_plans,
    normalize_blueprint_fact_versions,
    normalize_blueprint_requirement_state_keys,
    normalize_blueprint_raw_json,
    recover_complete_blueprint_prefix,
    validate_narrative_blueprint,
)
from app.schemas import (Bible, extract_json)
from app.source_excerpt import (
    render_indexed_source,
)
from app.screenplay_ir import (
    screenplay_ir_bible_context,
)

from .blueprint_budget_trace import _blueprint_generation_budget_for_trace
from .blueprint_generate_sharded import _generate_sharded_narrative_blueprint
from .blueprint_repair import _repair_narrative_blueprint
from .blueprint_semantic_review import _semantic_review_narrative_blueprint
from .common import _run_with_agent_loop
from .constants import SCREENPLAY_BLUEPRINT_PROMPT_VERSION
from .ir_snapshot import _artifact_json_content_is_sealed
from .screenplay_source import _render_screenplay_source


async def _generate_screenplay_narrative_blueprint(
    episode: dict[str, Any],
    source_text: str,
    bible: Bible,
    *,
    semantic_feedback: dict[str, list[str]] | None = None,
) -> NarrativeBlueprint:
    from app.observability.tracing import current_trace

    trace = current_trace()
    generation_budget = _blueprint_generation_budget_for_trace(
        trace,
        episode_id=str(episode.get("id") or ""),
    )
    current_run = get_conn().execute(
        "SELECT input_fingerprint FROM workflow_runs WHERE id=?",
        (trace.run_id,),
    ).fetchone()
    # 带着下游死结重建时绝不能复用上一版蓝图：那份蓝图正是死结的来源。
    if semantic_feedback:
        current_run = None
    if current_run is not None:
        rows = get_conn().execute(
            """SELECT a.content_json,a.content_hash
                 FROM artifacts a
                 JOIN step_runs sr ON sr.id=a.created_by_step_run_id
                 JOIN workflow_runs wr ON wr.id=sr.run_id
                WHERE a.scope_type='episode' AND a.scope_id=?
                  AND a.type='screenplay_narrative_blueprint'
                  AND a.status='validated'
                  AND a.contract_version=?
                  AND a.prompt_version=?
                  AND wr.input_fingerprint=?
                ORDER BY a.created_at DESC LIMIT 10""",
            (
                str(episode.get("id") or ""),
                BLUEPRINT_VERSION,
                SCREENPLAY_BLUEPRINT_PROMPT_VERSION,
                str(current_run["input_fingerprint"] or ""),
            ),
        ).fetchall()
        for row in rows:
            try:
                content = json.loads(row["content_json"] or "{}")
                if not _artifact_json_content_is_sealed(row, content):
                    continue
                raw = (
                    content.get("raw_output")
                    if isinstance(content, dict)
                    else None
                )
                if isinstance(raw, str):
                    try:
                        payload = extract_json(
                            normalize_blueprint_raw_json(raw),
                            repair_unescaped_inner_quotes=True,
                        )
                    except ValueError:
                        payload = recover_complete_blueprint_prefix(raw)
                else:
                    payload = content
                recovered = NarrativeBlueprint.model_validate(payload)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            normalize_blueprint_fact_versions(recovered)
            normalize_blueprint_requirement_state_keys(recovered)
            log_provider_call(
                "screenplay_blueprint_local_recompile",
                config.MODEL_TEXT,
                "REUSED",
                None,
                0,
                meta={
                    "episode_id": str(episode.get("id") or ""),
                    "contract_version": BLUEPRINT_VERSION,
                    "prompt_version": SCREENPLAY_BLUEPRINT_PROMPT_VERSION,
                },
            )
            if validate_narrative_blueprint(recovered, source_text):
                recovered = await _repair_narrative_blueprint(
                    recovered,
                    episode=episode,
                    source_text=source_text,
                    generation_budget=generation_budget,
                )
            derive_blueprint_scene_plans(recovered)
            return await _semantic_review_narrative_blueprint(
                recovered,
                episode=episode,
                source_text=source_text,
                generation_budget=generation_budget,
            )

    source_with_ids = render_indexed_source(source_text)
    bible_context = screenplay_ir_bible_context(
        bible,
        source_text=source_text,
        episode_no=int(episode["episode_no"]),
        character_resolutions=list(
            episode.get("character_resolutions") or []
        ),
    )
    candidate = await _generate_sharded_narrative_blueprint(
        episode,
        source_text,
        bible_context,
        generation_budget=generation_budget,
        semantic_feedback=semantic_feedback,
    )
    return await _semantic_review_narrative_blueprint(
        candidate,
        episode=episode,
        source_text=source_text,
        generation_budget=generation_budget,
    )

    prompt = f"""任务：先为第 {episode['episode_no']} 集建立写作前叙事蓝图。

这一步不写剧本台词和场景正文，只识别原文中不可机械判断的时间、空间、行动因果、
人物位置、持久状态和重大决定依据。后端会依据节点的时间域与单一地点确定性分场，
再让剧本阶段严格消费分场结果。

硬规则：
1. 按原文顺序覆盖每个非标题 SRC。单节点最多绑定
   {BLUEPRINT_MAX_SOURCE_SEGMENTS_PER_NODE} 个连续 SRC，不得用大节点掩盖事件。
1a. 每个节点必须显式输出 narrative_layer/event_priority/render_policy。
   可表演且形成画面状态变化的故事语义只能使用 story+causal+standalone；
   仅保留完整来源审计、不进入成片的旁文本只能使用
   paratext+connective+exclude_from_spine。不得按 SRC 编号、章节位置、
   characters 是否为空或文本关键词分类。
   paratext 节点的 participants、participant_evidence、
   environment_source_unit_keys、source_unit_deliveries、state_requirements、
   state_changes、released_constraints_for 必须为空列表，decision=null，
   exit_state=空字符串。标题可见文字只放 summary/action_logic/opening_image，
   不得生成 written_text delivery。
2. temporal_domain_key 表示同一连续时间域；回忆必须明确 flashback_enter、
   flashback_continue、flashback_exit。次日、当晚、数日后和蒙太奇必须使用正确
   time_relation，并提供观众可见/可听的 transition_cue。
3. 每节点只有一个主要 location_key/location_label。人物改变地点时，transition_cue
   必须说明走路、乘车、下车、进入房间、字幕或匹配剪辑，禁止瞬移。
   location_label 禁止使用「/」「、」「+」「内外」合并大堂/房间、里间/外间、
   车站/车厢等多个空间。只有地点变化发生在两个 SRC 之间时才能在该 SRC 边界拆节点；
   同一 SRC 内跨越多个地点时仍保持一个节点，只填写核心因果进程的一个主要地点，
   移动写入 transition_cue/action_logic，禁止复合地点和拆 SRC。
   同一 SRC 只能归属一个程序分场；其他场需要该信息时，
   必须通过 state_requirements、decision.setup_node_keys 或 transition_cue 建立
   显式派生关系，不得重复消费原 SRC。
4. 对后文会复用的持久事实建立 state_changes/state_requirements，包括但不限于：
   车辆所有者与司机、人物所在位置、住宿分配、房间结构、关键物品、掩护动作、
   谁知道什么。每个 state_change 必须建立本集唯一且递增的 fact_key（F001...）；
   后续 requirement 必须用 required_fact_key 精确引用此前仍有效的事实，禁止重新用
   自由文本描述一个“差不多”的状态。只有人物谱或前集已明确建立、但本集原文没有
   建立节点的事实才可设 assumed_prior=true，并写清审计依据；不得把为了推动剧情
   临时发明的同谋、开放关系、满房等设定标成 assumed_prior。事实默认并存；司机、
   住宿分配、人物位置等互斥事实发生替换时，必须在新事实的 supersedes_fact_keys
   中明确列出被替代事实。
5. major decision 必须通过 setup_node_keys 引用此前已经发生的压力、欲望、认知或关系
   节点，并写清 pressure/desire。禁止“受一次刺激立即性格突变”。
6. agency_mode 必须区分 voluntary、reluctant、coerced、incapacitated、unclear。
   武器、威胁或失去行为能力不能同时写成自主选择；若自主性后来变化，必须另建节点，
   并提供明确 agency_change_reason 和可见心理过程。coerced/incapacitated 决定必须
   用 constraint_fact_key 引用本节点建立且仍有效的约束事实。从该状态恢复为
   voluntary 时，constraint_release_node_keys 必须引用发生在两次决定之间、真正解除
   武器/威胁/无行为能力约束的节点；该节点还必须把角色 key 写入
   released_constraints_for，并用 state_change.supersedes_fact_keys 终止原约束事实。
   产生快感、停止反抗或自我说服不等于约束解除。
7. scene_boundary_before 只标记创作上必须切场的额外边界。时间域、地点、回忆进出变化
   后端本身就会自动切场。scene_plans 留空，禁止由模型决定场次编号和标题。
8. summary/action_logic 必须交代“为何发生、如何到达、动作完成后改变了什么”，
   不能只罗列事件。不得为修补逻辑发明违背原文的事实；仅当原文本身存在明确矛盾、
   且不补桥就无法成片时，才可使用 adaptation_kind=logic_bridge，并在
   bridge_rationale 中说明必要性及如何保持核心事件/结果；普通视觉过桥使用
   transition_cue，不能冒充剧情事实。
9. 每个 projection=action 的 prose source unit 必须有 typed 状态归属。
   单主体思考、反应、发问或动作在 participant_evidence 中填一条
   usage=state_subject，并用 source_unit_keys 精确绑定该 unit；结构标点切分后
   仍不可拆的共同动作填一条 mode=joint 的 state_subject_assignments，
   identity_keys 列出全部共同主体；真正无人物
   状态所有者的环境单元才写入 environment_source_unit_keys。visible、
   scene roster、content_owner 或文本姓名均不能作为主体推断。
10. participants 中每个 identity 都必须至少有一条 identity_key 完全相同的
    participant_evidence，且 source_segment_ids 非空并只引用本节点 owned SRC；
    不得仅列 roster，不得默认角色。

本集概要：{episode.get('synopsis') or '（无）'}
人物与场景上下文：
{json.dumps(bible_context, ensure_ascii=False, separators=(",", ":"))}

带稳定段 ID 的授权原文：
{_render_screenplay_source(source_with_ids)}

只输出 JSON，不要解释：
程序所有权摘要：
{json.dumps(blueprint_prompt_contract(), ensure_ascii=False)}
完整输出 Schema：
{json.dumps(NarrativeBlueprint.model_json_schema(), ensure_ascii=False)}
"""
    loop = AgentLoop(
        stage_key="screenplay_blueprint",
        contract_key="screenplay",
        goal=f"建立第 {episode['episode_no']} 集叙事时空与因果蓝图",
        scope_type="episode",
        scope_id=str(
            episode.get("id") or f"episode-{episode['episode_no']}"
        ),
        artifact_type="screenplay_narrative_blueprint",
        prompt_version=SCREENPLAY_BLUEPRINT_PROMPT_VERSION,
        policy=AgentLoopPolicy(
            max_iterations=1,
            stall_rounds=2,
            min_quality_gain=0.01,
            no_gain_rounds=2,
            allow_warning_candidate=False,
            repair_all_blockers=True,
        ),
    )
    try:
        candidate = await _run_with_agent_loop(
            "剧本时空因果蓝图",
            "screenplay_blueprint",
            prompt,
            NarrativeBlueprint,
            lambda value: validate_narrative_blueprint(
                value,
                source_text,
            ),
            loop=loop,
            temperature=0.2,
            max_tokens=20480,
            repair_user_prompt_limit=None,
            repair_candidate_limit=None,
            prefill={
                "format_version": BLUEPRINT_VERSION,
                "episode_no": episode["episode_no"],
            },
        )
    except AgentLoopFailure:
        from app.observability.tracing import current_trace

        trace = current_trace()
        row = get_conn().execute(
            """SELECT a.content_json,a.content_hash
                 FROM artifacts a
                 JOIN step_runs sr ON sr.id=a.created_by_step_run_id
                WHERE a.scope_type='episode' AND a.scope_id=?
                  AND a.type='screenplay_narrative_blueprint'
                  AND a.prompt_version=?
                  AND sr.run_id=?
                ORDER BY a.created_at DESC LIMIT 1""",
            (
                str(episode.get("id") or ""),
                SCREENPLAY_BLUEPRINT_PROMPT_VERSION,
                trace.run_id,
            ),
        ).fetchone()
        if row is None:
            raise
        fallback_content = json.loads(row["content_json"] or "{}")
        if not _artifact_json_content_is_sealed(row, fallback_content):
            raise
        candidate = NarrativeBlueprint.model_validate(fallback_content)
        candidate = await _repair_narrative_blueprint(
            candidate,
            episode=episode,
            source_text=source_text,
        )
    derive_blueprint_scene_plans(candidate)
    return await _semantic_review_narrative_blueprint(
        candidate,
        episode=episode,
        source_text=source_text,
    )


def _save_screenplay_generation_checkpoint(
    episode_id: str,
    phase: str,
    **values: Any,
) -> None:
    """Persist resumable pre-Document state without changing baseline_done."""
    from app.production.revision import (
        get_active_production_revision,
        save_checkpoint,
    )

    revision = get_active_production_revision(episode_id, "screenplay")
    if revision is None or revision.baseline_done:
        return
    checkpoint = dict(revision.checkpoint_json or {})
    save_checkpoint(revision.id, {
        **checkpoint,
        "phase": phase,
        **values,
    })
