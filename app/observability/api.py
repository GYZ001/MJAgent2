"""Strictly project-scoped observability routes.

The legacy system/run endpoints remain available for compatibility, but the project
workspace UI only consumes this router.  Every detail and mutation resolves the
object back to one project before returning data or dispatching an action.
"""
from __future__ import annotations

import json
import re
import time
from collections import Counter
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query
from fastapi.responses import PlainTextResponse

from app.db import get_conn
from app.evidence import repository
from app.orchestration import api as orchestration_api
from app import system_api


router = APIRouter(prefix="/api")


def _project(project_id: str) -> dict[str, Any]:
    row = get_conn().execute(
        "SELECT id,name,created_at FROM projects WHERE id=?", (project_id,),
    ).fetchone()
    if not row:
        raise HTTPException(404, "项目不存在")
    return dict(row)


def _single_project(candidates: list[str | None]) -> str | None:
    values = {str(value) for value in candidates if value}
    return next(iter(values)) if len(values) == 1 else None


def _scope_project(scope_type: str | None, scope_id: str | None) -> str | None:
    if not scope_type or not scope_id:
        return None
    conn = get_conn()
    if scope_type == "project":
        row = conn.execute("SELECT id FROM projects WHERE id=?", (scope_id,)).fetchone()
        return str(row["id"]) if row else None
    if scope_type == "episode":
        row = conn.execute("SELECT project_id FROM episodes WHERE id=?", (scope_id,)).fetchone()
        return str(row["project_id"]) if row else None
    if scope_type == "shot":
        row = conn.execute(
            """SELECT e.project_id FROM shots s JOIN episodes e ON e.id=s.episode_id
               WHERE s.id=?""", (scope_id,),
        ).fetchone()
        return str(row["project_id"]) if row else None
    return None


def _run_project(run_id: str) -> str | None:
    run = repository.get_run(run_id)
    if not run:
        return None
    return _scope_project(run.get("scope_type"), run.get("scope_id"))


def _run_context(run: dict[str, Any]) -> dict[str, Any]:
    project_id = _scope_project(run.get("scope_type"), run.get("scope_id"))
    project = _project(project_id) if project_id else None
    context: dict[str, Any] = {
        "project_id": project_id,
        "project_name": project.get("name") if project else None,
    }
    conn = get_conn()
    if run.get("scope_type") == "episode":
        row = conn.execute(
            "SELECT id AS episode_id,episode_no,title AS episode_title FROM episodes WHERE id=?",
            (run.get("scope_id"),),
        ).fetchone()
        if row:
            context.update(dict(row))
    elif run.get("scope_type") == "shot":
        row = conn.execute(
            """SELECT s.id AS shot_id,s.shot_no,e.id AS episode_id,e.episode_no,
                      e.title AS episode_title FROM shots s JOIN episodes e ON e.id=s.episode_id
               WHERE s.id=?""", (run.get("scope_id"),),
        ).fetchone()
        if row:
            context.update(dict(row))
    return context


def _artifact_project(artifact_id: str) -> str | None:
    artifact = repository.get_artifact(artifact_id)
    if not artifact:
        return None
    candidates = [_scope_project(artifact.get("scope_type"), artifact.get("scope_id"))]
    step_id = artifact.get("created_by_step_run_id")
    if step_id:
        row = get_conn().execute("SELECT run_id FROM step_runs WHERE id=?", (step_id,)).fetchone()
        if row:
            candidates.append(_run_project(str(row["run_id"])))
    return _single_project(candidates)


def _job_summary(job_id: str, source: str = "auto") -> dict[str, Any] | None:
    if source == "auto":
        return next(
            (row for row in system_api.jobs_overview(include_all=True)["recent"]
             if str(row.get("id")) == job_id),
            None,
        )
    if source == "run":
        run = repository.get_run(job_id)
        return {**run, "id": job_id, "source": "run", "run_id": job_id} if run else None
    if source == "screenplay" or job_id.startswith("screenplay_"):
        episode_id = job_id.removeprefix("screenplay_")
        row = get_conn().execute(
            "SELECT id,project_id FROM episodes WHERE id=?", (episode_id,),
        ).fetchone()
        return {"id": job_id, "source": "screenplay", **dict(row)} if row else None
    row = get_conn().execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    return {**dict(row), "source": "job"} if row else None


def _job_project(job_id: str, source: str = "auto") -> str | None:
    summary = _job_summary(job_id, source)
    if not summary:
        return None
    return system_api._job_project_id(summary)


def _call_row(call_id: int) -> dict[str, Any] | None:
    row = get_conn().execute("SELECT * FROM provider_calls WHERE id=?", (call_id,)).fetchone()
    return dict(row) if row else None


def _call_project(call_id: int) -> str | None:
    row = _call_row(call_id)
    if not row:
        return None
    try:
        meta = json.loads(row.get("meta") or "{}")
    except (TypeError, json.JSONDecodeError):
        meta = {}
    meta = meta if isinstance(meta, dict) else {}
    return system_api._call_project_id(row, meta)


def _raw_call_detail_payload(call_id: int) -> dict[str, Any]:
    row = _call_row(call_id)
    if not row:
        raise HTTPException(404, "调用记录不存在")
    item = dict(row)
    item["effective_status"] = system_api._effective_call_status(item)
    item["category"] = system_api._call_category(str(item.get("kind") or ""))
    item["context"] = system_api._call_meta_summary(item.get("meta"))
    item["model_label"] = item.get("model") or "未记录模型"
    for field in ("request_json", "response_json", "meta"):
        raw = item.get(field)
        item[f"{field}_size"] = (
            len(raw.encode("utf-8"))
            if isinstance(raw, str)
            else 0
        )
    item["raw_access"] = True
    return item


def _assert_scope(project_id: str, actual: str | None, label: str = "观测对象") -> None:
    _project(project_id)
    # 使用同一个 404，避免通过详情或动作接口探测其他项目的对象是否存在。
    if not actual or actual != project_id:
        raise HTTPException(404, f"{label}不存在")


def _scope(payload: dict[str, Any], project: dict[str, Any]) -> dict[str, Any]:
    return {**payload, "scope": {"type": "project", "project_id": project["id"], "project_name": project["name"]}}


_TRACE_WORKFLOW_LABELS = {
    "character_bible": "人物谱生成",
    "character_references": "人物定妆照",
    "scene_bible": "场景设定",
    "scene_references": "场景参考图",
    "episode_mapping": "分集规划",
    "screenplay": "剧本生成",
    "storyboard": "分镜生成",
    "scene_generation": "关键帧生成",
    "video_generation": "视频生成",
    "episode_video_completion": "全片视频补齐",
    "project_video_completion_queue": "全项目视频补齐",
    "delivery": "交付",
    "delivery_package": "交付候选生成",
}
_TRACE_STEP_LABELS = {
    "generate": "生成内容",
    "validate": "校验内容",
    "evaluate": "质量评估",
    "repair": "定向修复",
    "screenplay": "生成剧本",
    "storyboard": "生成分镜",
    "build_delivery_snapshot": "生成交付快照",
    "apply_delivery_gate": "应用交付决定",
    "character_references": "生成人物参考图",
    "media_generation": "媒体生成",
    "character_discovery": "识别剧本角色",
    "character_discovery_resume_audit": "核对角色识别恢复状态",
    "character_bible": "生成人物设定",
    "scene_bible": "生成场景设定",
    "scene_references": "生成场景参考图",
    "video_generation": "生成镜头视频",
}
_TRACE_CALL_LABELS = {
    "chat": "生成文本内容",
    "vlm": "理解画面内容",
    "vlm_qa": "检查视频画面质量",
    "video_create": "提交视频生成",
    "video_poll": "查询视频生成进度",
    "image": "生成图片",
    "image_generate": "生成图片",
    "image_edit": "编辑图片",
    "scene_image": "生成关键帧",
    "screenplay_prompt": "生成剧本内容",
    "plan_prompt": "规划分集内容",
    "bible_prompt": "生成人物设定",
    "references_prompt": "规划参考图",
    "storyboard_shot_prompt": "生成逐镜分镜",
    "storyboard_outline_prompt": "生成分镜大纲",
    "val422_metric": "记录结构校验指标",
    "storyboard_candidate_normalization": "规范化分镜候选",
    "screenplay_ir_local_recompile": "本地重编译剧本",
    "reference_keyframe_checkpoint_auto_repair": "自动修复关键帧检查点",
    "screenplay_blueprint_shard_local_recompile": "本地重编译剧本分片",
    "storyboard_outline_local_compile": "本地编译分镜大纲",
    "reference_keyframe_gate_repair_required": "检查关键帧修复要求",
    "episode_video_mode_plan_normalization": "规范化视频生成方案",
    "episode_video_boundary_strategy": "规划镜头衔接策略",
    "storyboard_outline_split": "拆分分镜大纲",
    "screenplay_ir_candidate_normalization": "规范化剧本候选",
    "provider_cache_hit": "复用已有模型结果",
    "screenplay_blueprint_local_recompile": "本地重编译剧本蓝图",
    "storyboard_source_evidence_repair": "修复分镜原文证据",
    "storyboard_outline_spoken_duration": "计算分镜口播时长",
    "reference_keyframe_gate_exhausted_fallback": "执行关键帧门禁降级",
    "storyboard_outline_authority_projection": "投影分镜权威数据",
    "storyboard_outline_action_completion_projection": "补充分镜动作结果",
    "episode_video_mode_plan_cache": "复用视频生成方案",
    "storyboard_repair_field_preservation": "保留分镜修复字段",
    "character_bible_candidate_normalization": "规范化人物设定候选",
    "screenplay_candidate_normalization": "规范化剧本候选",
    "storyboard_outline_semantic_split": "按语义拆分分镜大纲",
    "场景分镜_loop": "执行场景分镜自动修复",
    "分镜脚本_loop": "执行分镜脚本自动修复",
    "剧本首次整版 Baseline_loop": "执行剧本初稿自动修复",
    "分镜大纲_loop": "执行分镜大纲自动修复",
    "角色圣经_loop": "执行人物设定自动修复",
}
_TRACE_MODEL_CALL_KINDS = {
    "chat", "vlm", "vlm_qa", "video_create", "image", "image_generate",
    "image_edit", "scene_image", "screenplay_prompt", "plan_prompt",
    "bible_prompt", "references_prompt", "storyboard_shot_prompt",
    "storyboard_outline_prompt",
}
_TRACE_CALL_METHODS = {
    "chat": "通过文本生成模型",
    "vlm": "通过视觉理解模型",
    "vlm_qa": "通过视觉理解模型",
    "video_create": "通过视频生成模型",
    "image": "通过图像生成模型",
    "image_generate": "通过图像生成模型",
    "image_edit": "通过图像生成模型",
    "scene_image": "通过图像生成模型",
    "screenplay_prompt": "通过文本生成模型",
    "plan_prompt": "通过文本生成模型",
    "bible_prompt": "通过文本生成模型",
    "references_prompt": "通过文本生成模型",
    "storyboard_shot_prompt": "通过文本生成模型",
    "storyboard_outline_prompt": "通过文本生成模型",
    "video_poll": "通过视频平台接口",
    "val422_metric": "通过本地结构校验",
    "provider_cache_hit": "通过本地缓存",
}
_TRACE_JOB_LABELS = {
    "video": "执行镜头视频生成",
    "scene": "执行关键帧生成",
    "image": "执行图片生成",
    "reference": "执行参考图生成",
}
_TRACE_DISCOVERY_PHASE_LABELS = {
    "current": "提取本集人物候选",
    "future_identity": "解析候选人物真实身份",
    "coverage_audit": "复核人物识别完整性",
}
_TRACE_STAGE_PURPOSE_LABELS = {
    "discover_character_candidates": "识别本集人物",
    "剧本首次整版 Baseline": "生成剧本完整初稿",
    "剧本时空因果蓝图分片": "生成剧本时空因果蓝图",
    "剧本蓝图语义审稿": "审核剧本蓝图语义",
    "narrative_repair_diagnosis": "诊断叙事修复方案",
    "screenplay_narrative_patch": "修补剧本叙事结构",
    "分镜脚本": "生成分镜脚本",
    "场景分镜": "生成场景分镜",
    "剧本蓝图局部语义修复": "修复剧本蓝图局部语义",
    "screenplay_ir_identity_adjudication": "裁定剧本角色身份",
    "剧本来源保真局部补写": "补写剧本来源保真内容",
    "分镜大纲": "生成分镜大纲",
    "episode_video_mode_plan": "规划本集视频生成方式",
    "screen_appearance_changes": "识别人物外观变化",
    "screen_scene_state_changes": "识别场景状态变化",
    "assess_new_scene": "评估新增场景",
    "assess_new_character": "评估新增人物",
    "角色圣经": "生成人物设定",
}
_TRACE_METRIC_PURPOSE_LABELS = {
    "repair_activation_total": "记录剧本修复启动次数",
    "baseline_generation_calls_total": "记录剧本初稿生成次数",
    "production_repair_patch_total": "记录剧本局部修复次数",
    "time_to_completion_certificate_seconds": "记录剧本验收耗时",
    "certified_screenplay_delivery_rate": "记录剧本交付通过率",
}


def _trace_label(value: str | None, labels: dict[str, str], fallback: str) -> str:
    return labels.get(str(value or ""), fallback)


def _trace_step_label(step_key: str | None, iteration_no: int | None = None) -> str:
    key = str(step_key or "")
    if key in _TRACE_STEP_LABELS:
        return _TRACE_STEP_LABELS[key]
    patterns = (
        (r"screenplay\.iteration", "执行第{iteration}轮剧本生成"),
        (r"character_bible\.iteration", "执行第{iteration}轮人物设定生成"),
        (r"scene_bible\.iteration", "执行第{iteration}轮场景设定生成"),
        (r"storyboard_outline\.iteration", "执行第{iteration}轮分镜大纲生成"),
        (r"storyboard_scene_(\d+)\.iteration", "生成第{number}个场景分镜"),
        (r"storyboard_shot_(\d+)\.iteration", "生成第{number}镜分镜"),
    )
    for pattern, template in patterns:
        match = re.fullmatch(pattern, key)
        if match:
            return template.format(
                iteration=max(1, int(iteration_no or 1)),
                number=match.group(1) if match.groups() else "",
            )
    return "执行程序处理"


def _trace_meta(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _trace_call_suffix(meta: dict[str, Any]) -> str:
    parts: list[str] = []
    shard_index = meta.get("shard_index")
    shard_count = meta.get("shard_count")
    if shard_index is not None and shard_count is not None:
        parts.append(f"第 {shard_index}/{shard_count} 片")
    source_batch = meta.get("source_batch")
    source_batches = meta.get("source_batches")
    if source_batch is not None and source_batches and int(source_batches) > 1:
        parts.append(f"第 {source_batch}/{source_batches} 批")
    attempt = int(meta.get("attempt") or 0)
    if attempt > 1:
        parts.append(f"第 {attempt} 次尝试")
    repair_round = int(meta.get("repair_round") or 0)
    if repair_round > 0:
        parts.append(f"第 {repair_round} 轮修复")
    return f"（{'，'.join(parts)}）" if parts else ""


def _trace_call_semantics(
    kind: str | None,
    raw_meta: Any = None,
    business_stage_name: str | None = None,
) -> tuple[str, str, str]:
    key = str(kind or "")
    meta = _trace_meta(raw_meta)
    node_role = (
        "model_processing"
        if key in _TRACE_MODEL_CALL_KINDS
        else "program_processing"
    )
    discovery_phase = str(meta.get("discovery_phase") or "")
    stage = str(meta.get("stage") or "")
    metric = str(meta.get("metric") or "")
    name = _TRACE_DISCOVERY_PHASE_LABELS.get(discovery_phase)
    if not name and key == "val422_metric":
        name = _TRACE_METRIC_PURPOSE_LABELS.get(metric, "记录结构校验指标")
    if not name:
        name = _TRACE_STAGE_PURPOSE_LABELS.get(stage)
    if not name:
        name = _TRACE_CALL_LABELS.get(key)
    if not name:
        if key.endswith("_prompt"):
            node_role = "model_processing"
            name = "生成业务内容"
        elif key.endswith("_loop"):
            name = "执行自动修复循环"
        elif "normalization" in key:
            name = "规范化业务数据"
        elif "compile" in key or "recompile" in key:
            name = "本地编译业务数据"
        elif "metric" in key:
            name = "记录运行指标"
        else:
            name = (
                f"为“{business_stage_name}”生成业务内容"
                if node_role == "model_processing" and business_stage_name
                else "执行程序处理"
            )
    if node_role == "model_processing":
        name += _trace_call_suffix(meta)
    method = _TRACE_CALL_METHODS.get(key)
    if not method:
        method = (
            "通过业务生成模型"
            if node_role == "model_processing"
            else "通过本地业务规则"
        )
    return name, node_role, method


def _trace_job_label(kind: str | None) -> str:
    return _TRACE_JOB_LABELS.get(str(kind or ""), "执行异步任务")


def _trace_run_id(
    project_id: str,
    object_type: str,
    object_id: str,
    source: str,
) -> tuple[str | None, dict[str, Any]]:
    if object_type == "runs":
        _assert_scope(project_id, _run_project(object_id), "运行")
        run = repository.get_run(object_id)
        if not run:
            raise HTTPException(404, "运行不存在")
        return object_id, run
    if object_type == "jobs":
        summary = _job_summary(object_id, source)
        _assert_scope(project_id, _job_project(object_id, source), "任务")
        if not summary:
            raise HTTPException(404, "任务不存在")
        run_id = summary.get("run_id")
        if not run_id and summary.get("step_run_id"):
            row = get_conn().execute(
                "SELECT run_id FROM step_runs WHERE id=?",
                (summary["step_run_id"],),
            ).fetchone()
            run_id = row["run_id"] if row else None
        if not run_id:
            run_id = summary.get("owner_run_id")
        return str(run_id) if run_id else None, summary
    if object_type == "calls":
        try:
            call_id = int(object_id)
        except ValueError as exc:
            raise HTTPException(422, "调用记录标识无效") from exc
        row = _call_row(call_id)
        _assert_scope(project_id, _call_project(call_id), "调用记录")
        if not row:
            raise HTTPException(404, "调用记录不存在")
        run_id = row.get("run_id")
        if not run_id and row.get("step_run_id"):
            step = get_conn().execute(
                "SELECT run_id FROM step_runs WHERE id=?",
                (row["step_run_id"],),
            ).fetchone()
            run_id = step["run_id"] if step else None
        return str(run_id) if run_id else None, row
    raise HTTPException(404, "链路对象类型不存在")


def _trace_related_runs(project_id: str, primary_run_id: str) -> set[str]:
    """Include child/recovery/media runs while preserving the project boundary."""
    conn = get_conn()
    run_ids = {primary_run_id}
    while True:
        marks = ",".join("?" for _ in run_ids)
        related = {
            str(row["id"])
            for row in conn.execute(
                f"SELECT id FROM workflow_runs WHERE parent_run_id IN ({marks})",
                tuple(run_ids),
            ).fetchall()
        }
        related.update(
            str(row["run_id"])
            for row in conn.execute(
                f"""SELECT DISTINCT run_id FROM jobs
                    WHERE owner_run_id IN ({marks}) AND run_id IS NOT NULL""",
                tuple(run_ids),
            ).fetchall()
            if row["run_id"]
        )
        related = {
            run_id
            for run_id in related
            if run_id not in run_ids and _run_project(run_id) == project_id
        }
        if not related:
            return run_ids
        run_ids.update(related)


def _trace_tree(
    project_id: str,
    object_type: str,
    object_id: str,
    source: str = "auto",
) -> dict[str, Any]:
    primary_run_id, source_row = _trace_run_id(
        project_id, object_type, object_id, source,
    )
    selected_node_id = (
        f"run:{primary_run_id}"
        if object_type == "runs" or (object_type == "jobs" and source_row.get("source") == "run")
        else f"job:{object_id}"
        if object_type == "jobs"
        else f"call:{object_id}"
    )
    nodes: list[dict[str, Any]] = []
    if not primary_run_id:
        if object_type == "jobs":
            nodes.append({
                "id": f"job:{object_id}",
                "parent_id": None,
                "kind": "job",
                "node_role": "business_stage",
                "name": _trace_label(
                    source_row.get("workflow_type") or source_row.get("kind"),
                    _TRACE_WORKFLOW_LABELS,
                    "执行历史业务任务",
                ),
                "subtitle": "历史任务记录",
                "status": source_row.get("status") or "unknown",
                "started_at": source_row.get("created_at"),
                "finished_at": source_row.get("updated_at"),
                "latency_ms": max(
                    0,
                    int(
                        (
                            float(source_row.get("updated_at") or 0)
                            - float(source_row.get("created_at") or 0)
                        )
                        * 1000
                    ),
                ),
            })
        else:
            call_name, call_role, call_method = _trace_call_semantics(
                source_row.get("kind"),
                source_row.get("meta"),
            )
            nodes.append({
                "id": f"call:{object_id}",
                "parent_id": None,
                "kind": "call",
                "node_role": call_role,
                "name": call_name,
                "subtitle": call_method,
                "status": source_row.get("status") or "unknown",
                "started_at": source_row.get("ts"),
                "finished_at": float(source_row.get("ts") or 0)
                + float(source_row.get("latency_ms") or 0) / 1000,
                "latency_ms": int(source_row.get("latency_ms") or 0),
            })
        return _scope({
            "source": {"type": object_type, "id": object_id},
            "run_id": None,
            "title": nodes[0]["name"],
            "status": nodes[0]["status"],
            "selected_node_id": selected_node_id,
            "nodes": nodes,
            "server_time": time.time(),
        }, _project(project_id))

    run_ids = _trace_related_runs(project_id, primary_run_id)
    conn = get_conn()
    run_marks = ",".join("?" for _ in run_ids)
    runs = [
        repository.get_run(run_id)
        for run_id in run_ids
    ]
    runs = [run for run in runs if run]
    steps = [
        step
        for run_id in run_ids
        for step in repository.get_steps(run_id)
    ]
    step_ids = {str(step["id"]) for step in steps}
    step_labels = {
        str(step["id"]): _trace_step_label(
            step.get("step_key"), step.get("iteration_no"),
        )
        for step in steps
    }
    jobs = [
        dict(row)
        for row in conn.execute(
            f"""SELECT * FROM jobs
                WHERE run_id IN ({run_marks}) OR owner_run_id IN ({run_marks})
                ORDER BY created_at,id""",
            (*run_ids, *run_ids),
        ).fetchall()
    ]
    call_clauses = [f"run_id IN ({run_marks})"]
    call_params: list[Any] = list(run_ids)
    if step_ids:
        step_marks = ",".join("?" for _ in step_ids)
        call_clauses.append(f"step_run_id IN ({step_marks})")
        call_params.extend(step_ids)
    calls = [
        dict(row)
        for row in conn.execute(
            f"""SELECT id,ts,kind,model,status,http_status,latency_ms,error,run_id,
                       step_run_id,trace_id,operation_id,attempt_no,meta
                FROM provider_calls
                WHERE {' OR '.join(call_clauses)}
                ORDER BY ts,id""",
            tuple(call_params),
        ).fetchall()
    ]
    owner_parent: dict[str, str] = {}
    for job in jobs:
        if job.get("run_id") and job.get("owner_run_id"):
            owner_parent[str(job["run_id"])] = str(job["owner_run_id"])
    for run in sorted(
        runs,
        key=lambda item: (
            str(item["id"]) != primary_run_id,
            float(item.get("started_at") or item.get("updated_at") or 0),
        ),
    ):
        run_id = str(run["id"])
        parent_run_id = run.get("parent_run_id") or owner_parent.get(run_id)
        parent_id = (
            f"run:{parent_run_id}"
            if parent_run_id in run_ids and run_id != primary_run_id
            else None
        )
        nodes.append({
            "id": f"run:{run_id}",
            "parent_id": parent_id,
            "kind": "run",
            "node_role": (
                "task"
                if run_id == primary_run_id
                else "business_stage"
            ),
            "name": _trace_label(
                run.get("workflow_type"), _TRACE_WORKFLOW_LABELS, "执行业务任务",
            ),
            "subtitle": (
                "汇总全部业务环节"
                if run_id == primary_run_id
                else "关联执行任务"
            ),
            "status": run.get("status") or "unknown",
            "started_at": run.get("started_at"),
            "finished_at": run.get("finished_at"),
            "latency_ms": max(
                0,
                int(
                    (
                        float(
                            run.get("finished_at")
                            or run.get("updated_at")
                            or time.time()
                        )
                        - float(run.get("started_at") or run.get("updated_at") or 0)
                    )
                    * 1000
                ),
            ),
        })
    for step in steps:
        parent_step = str(step.get("parent_step_run_id") or "")
        is_business_stage = parent_step not in step_ids
        nodes.append({
            "id": f"step:{step['id']}",
            "parent_id": (
                f"step:{parent_step}"
                if parent_step in step_ids
                else f"run:{step['run_id']}"
            ),
            "kind": "step",
            "node_role": (
                "business_stage"
                if is_business_stage
                else "program_processing"
            ),
            "name": _trace_step_label(
                step.get("step_key"), step.get("iteration_no"),
            ),
            "subtitle": (
                "组织模型与程序处理"
                if is_business_stage
                else "通过智能体执行流程"
            ),
            "status": step.get("status") or "unknown",
            "started_at": step.get("started_at"),
            "finished_at": step.get("finished_at"),
            "latency_ms": int(step.get("latency_ms") or 0),
        })
    for job in jobs:
        step_id = str(job.get("step_run_id") or "")
        run_id = str(job.get("run_id") or job.get("owner_run_id") or primary_run_id)
        nodes.append({
            "id": f"job:{job['id']}",
            "parent_id": f"step:{step_id}" if step_id in step_ids else f"run:{run_id}",
            "kind": "job",
            "node_role": "program_processing",
            "name": _trace_job_label(job.get("kind")),
            "subtitle": "通过持久化异步任务",
            "status": job.get("stage_status") or job.get("status") or "unknown",
            "started_at": job.get("stage_started_at") or job.get("created_at"),
            "finished_at": job.get("stage_updated_at") or job.get("updated_at"),
            "latency_ms": max(
                0,
                int(
                    (
                        float(job.get("stage_updated_at") or job.get("updated_at") or 0)
                        - float(job.get("stage_started_at") or job.get("created_at") or 0)
                    )
                    * 1000
                ),
            ),
        })
    for call in calls:
        step_id = str(call.get("step_run_id") or "")
        run_id = str(call.get("run_id") or primary_run_id)
        call_name, call_role, call_method = _trace_call_semantics(
            call.get("kind"),
            call.get("meta"),
            step_labels.get(step_id),
        )
        nodes.append({
            "id": f"call:{call['id']}",
            "parent_id": f"step:{step_id}" if step_id in step_ids else f"run:{run_id}",
            "kind": "call",
            "node_role": call_role,
            "name": call_name,
            "subtitle": call_method,
            "status": call.get("status") or "unknown",
            "started_at": call.get("ts"),
            "finished_at": float(call.get("ts") or 0)
            + float(call.get("latency_ms") or 0) / 1000,
            "latency_ms": int(call.get("latency_ms") or 0),
        })
    primary = next(run for run in runs if str(run["id"]) == primary_run_id)
    return _scope({
        "source": {"type": object_type, "id": object_id},
        "run_id": primary_run_id,
        "title": _trace_label(
            primary.get("workflow_type"), _TRACE_WORKFLOW_LABELS, "业务执行链路",
        ),
        "status": primary.get("status") or "unknown",
        "started_at": primary.get("started_at"),
        "finished_at": primary.get("finished_at"),
        "latency_ms": next(
            (node["latency_ms"] for node in nodes if node["id"] == f"run:{primary_run_id}"),
            0,
        ),
        "cost_cny": float(primary.get("cost_cny") or 0),
        "selected_node_id": (
            selected_node_id
            if any(node["id"] == selected_node_id for node in nodes)
            else f"run:{primary_run_id}"
        ),
        "nodes": nodes,
        "server_time": time.time(),
    }, _project(project_id))


def _trace_json_value(raw: Any) -> Any:
    if raw is None or not isinstance(raw, str):
        return raw
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def _trace_artifact(artifact_id: str | None) -> dict[str, Any] | None:
    if not artifact_id:
        return None
    artifact = repository.get_artifact(artifact_id)
    if not artifact:
        return {"id": artifact_id, "missing": True}
    return {
        "id": artifact["id"],
        "type": artifact.get("type"),
        "version": artifact.get("version"),
        "status": artifact.get("status"),
        "trust_level": artifact.get("trust_level"),
        "content_hash": artifact.get("content_hash"),
        "content": artifact.get("content"),
    }


def _trace_node_detail(
    project_id: str,
    object_type: str,
    object_id: str,
    node_id: str,
    source: str,
) -> dict[str, Any]:
    tree = _trace_tree(project_id, object_type, object_id, source)
    node = next((item for item in tree["nodes"] if item["id"] == node_id), None)
    if not node:
        raise HTTPException(404, "链路节点不存在")
    try:
        kind, raw_id = node_id.split(":", 1)
    except ValueError as exc:
        raise HTTPException(422, "链路节点标识无效") from exc
    if kind == "run":
        row = repository.get_run(raw_id)
        if not row:
            raise HTTPException(404, "运行节点不存在")
        input_value = {
            "workflow_type": row.get("workflow_type"),
            "scope": {"type": row.get("scope_type"), "id": row.get("scope_id")},
            "requested_by": row.get("requested_by"),
            "trigger_type": row.get("trigger_type"),
            "input_fingerprint": row.get("input_fingerprint"),
            "policy_snapshot": row.get("policy_snapshot"),
            "config_snapshot": row.get("config_snapshot"),
        }
        output_value = {
            "status": row.get("status"),
            "current_step_key": row.get("current_step_key"),
            "cost_cny": row.get("cost_cny"),
            "failure_code": row.get("failure_code"),
            "failure_message": row.get("failure_message"),
            "resume_from_step": row.get("resume_from_step"),
        }
        metadata = {
            "run_id": raw_id,
            "parent_run_id": row.get("parent_run_id"),
            "started_at": row.get("started_at"),
            "updated_at": row.get("updated_at"),
            "finished_at": row.get("finished_at"),
            "budget_limit_cny": row.get("budget_limit_cny"),
        }
    elif kind == "step":
        row = get_conn().execute(
            "SELECT * FROM step_runs WHERE id=?", (raw_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "步骤节点不存在")
        item = dict(row)
        input_ids = _trace_json_value(item.get("input_artifact_ids_json")) or []
        input_value = {
            "context_manifest": _trace_json_value(item.get("context_manifest_json")),
            "artifacts": [
                _trace_artifact(str(artifact_id))
                for artifact_id in input_ids
            ],
        }
        output_value = {
            "artifact": _trace_artifact(item.get("output_artifact_id")),
            "decision": item.get("decision"),
            "exit_reason": item.get("exit_reason"),
            "error_code": item.get("error_code"),
            "error_message": item.get("error_message"),
        }
        metadata = {
            "step_run_id": raw_id,
            "run_id": item.get("run_id"),
            "step_key": item.get("step_key"),
            "iteration_no": item.get("iteration_no"),
            "parent_step_run_id": item.get("parent_step_run_id"),
            "agent_name": item.get("agent_name"),
            "contract_version": item.get("contract_version"),
            "prompt_version": item.get("prompt_version"),
            "policy_version": item.get("policy_version"),
        }
    elif kind == "job":
        detail_source = (
            source
            if object_type == "jobs" and raw_id == object_id
            else "job"
        )
        item = system_api.job_detail(raw_id, detail_source)
        if item.get("source") == "screenplay":
            input_value = {
                "kind": "screenplay",
                "project_id": item.get("project_id"),
                "episode_id": raw_id.removeprefix("screenplay_"),
                "episode_no": item.get("episode_no"),
                "title": item.get("title"),
            }
            output_value = {
                "status": item.get("screenplay_status"),
                "artifact_id": item.get("screenplay_artifact_id"),
                "started_at": item.get("screenplay_started_at"),
                "updated_at": item.get("screenplay_updated_at"),
                "error": item.get("screenplay_error"),
            }
        else:
            input_value = {
                "kind": item.get("kind"),
                "project_id": item.get("project_id"),
                "episode_id": item.get("episode_id"),
                "shot_id": item.get("shot_id"),
                "version_id": item.get("version_id"),
                "after_shot_id": item.get("after_shot_id"),
                "after_version_id": item.get("after_version_id"),
                "scene_kinds": _trace_json_value(item.get("scene_kinds")),
            }
            output_value = {
                "status": item.get("status"),
                "pipeline_stage": item.get("pipeline_stage"),
                "stage_status": item.get("stage_status"),
                "stage_progress": _trace_json_value(item.get("stage_progress_json")),
                "reason_code": item.get("reason_code"),
                "reason_text": item.get("reason_text"),
                "error": item.get("error"),
            }
        metadata = {
            "job_id": raw_id,
            "run_id": item.get("run_id"),
            "owner_run_id": item.get("owner_run_id"),
            "step_run_id": item.get("step_run_id"),
            "provider_operation_id": item.get("provider_operation_id"),
            "retry_count": item.get("retry_count"),
            "max_retries": item.get("max_retries"),
            "scheduler_lane": item.get("scheduler_lane"),
            "priority_class": item.get("priority_class"),
            "state_revision": item.get("state_revision"),
        }
    elif kind == "call":
        item = _call_row(int(raw_id))
        if not item:
            raise HTTPException(404, "调用节点不存在")
        input_value = _trace_json_value(item.get("request_json"))
        output_value = {
            "response": _trace_json_value(item.get("response_json")),
            "status": system_api._effective_call_status(item),
            "http_status": item.get("http_status"),
            "error": item.get("error"),
        }
        metadata = {
            "call_id": item.get("id"),
            "kind": item.get("kind"),
            "model": item.get("model"),
            "run_id": item.get("run_id"),
            "step_run_id": item.get("step_run_id"),
            "trace_id": item.get("trace_id"),
            "operation_id": item.get("operation_id"),
            "attempt_no": item.get("attempt_no"),
            "received_chars": item.get("received_chars"),
            "meta": _trace_json_value(item.get("meta")),
        }
    else:
        raise HTTPException(404, "链路节点类型不存在")
    return {
        **node,
        "input": input_value,
        "output": output_value,
        "metadata": metadata,
    }


@router.get("/projects/{project_id}/observability/runs")
def scoped_runs(
    project_id: str, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
    search: str = "", status: str | None = None, workflow: str | None = None,
    episode_no: int | None = None, from_ts: float | None = None, to_ts: float | None = None,
    include_history: bool = False, sort: str = "desc",
):
    project = _project(project_id)
    return _scope(orchestration_api.query_runs(
        page, page_size, search, status, project_id, workflow, episode_no,
        from_ts, to_ts, include_history, sort,
    ), project)


@router.get("/projects/{project_id}/observability/runs/{run_id}")
def scoped_run(project_id: str, run_id: str):
    _assert_scope(project_id, _run_project(run_id), "运行")
    run = orchestration_api.get_run(run_id)
    return {**run, **_run_context(run)}


@router.get("/projects/{project_id}/observability/runs/{run_id}/steps")
def scoped_run_steps(project_id: str, run_id: str):
    _assert_scope(project_id, _run_project(run_id), "运行")
    return orchestration_api.get_steps(run_id)


@router.get("/projects/{project_id}/observability/runs/{run_id}/events")
def scoped_run_events(project_id: str, run_id: str, after: float | None = None, limit: int = Query(500, ge=1, le=1000)):
    _assert_scope(project_id, _run_project(run_id), "运行")
    return orchestration_api.get_events(run_id, after=after, limit=limit)


@router.post("/projects/{project_id}/observability/runs/{run_id}/{action}")
async def scoped_run_action(project_id: str, run_id: str, action: str, body: dict | None = Body(None)):
    _assert_scope(project_id, _run_project(run_id), "运行")
    if action == "cancel":
        return await orchestration_api.cancel_run_route(run_id)
    if action == "resume":
        return await orchestration_api.resume_run_route(run_id, body)
    if action == "retry":
        return await orchestration_api.retry_run_route(run_id, body)
    raise HTTPException(404, "运行动作不存在")


@router.get("/projects/{project_id}/observability/jobs")
def scoped_jobs(
    project_id: str, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
    search: str = "", status: str | None = None, workflow: str | None = None,
    from_ts: float | None = None, to_ts: float | None = None, sort: str = "desc",
):
    project = _project(project_id)
    payload = system_api.query_jobs(
        page, page_size, search, status, project_id, workflow, from_ts, to_ts, sort,
    )
    # Keep the lightweight summary contract used by the existing UI while the
    # canonical field remains ``items`` for paginated consumers.
    payload["recent"] = payload["items"]
    return _scope(payload, project)


@router.get("/projects/{project_id}/observability/jobs/{job_id}")
def scoped_job(project_id: str, job_id: str, source: str = "auto"):
    _assert_scope(project_id, _job_project(job_id, source), "任务")
    return system_api.job_detail(job_id, source)


@router.post("/projects/{project_id}/observability/jobs/{job_id}/{action}")
async def scoped_job_action(project_id: str, job_id: str, action: str, source: str = "auto", body: dict | None = Body(None)):
    summary = _job_summary(job_id, source)
    _assert_scope(project_id, _job_project(job_id, source), "任务")
    effective_source = str((summary or {}).get("source") or source)
    run_id = str((summary or {}).get("run_id") or job_id)
    if effective_source == "run":
        return await scoped_run_action(project_id, run_id, action, body)
    if effective_source == "job" and action == "cancel":
        return await orchestration_api.cancel_media_job_route(job_id)
    if effective_source == "job" and action in {"retry", "resume"}:
        return system_api.retry_job(job_id, body)
    raise HTTPException(409, "当前任务不支持该操作")


@router.get("/projects/{project_id}/observability/calls")
def scoped_calls(
    project_id: str, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
    search: str = "", status: str | None = None, category: str | None = None,
    function: str | None = None, model: str | None = None, from_ts: float | None = None,
    to_ts: float | None = None, sort: str = "desc", ids: str | None = None,
):
    project = _project(project_id)
    payload = system_api.query_calls(
        page, page_size, search, status, category, project_id, function, model,
        from_ts, to_ts, sort, ids,
    )
    for item in payload["items"]:
        item["context"] = {
            **(item.get("context") or {}),
            "project_id": project_id,
            "project_name": project["name"],
        }
    for aggregate in payload["aggregates"]:
        aggregate["project_id"] = project_id
        aggregate["project_name"] = project["name"]
    return _scope(payload, project)


@router.get("/projects/{project_id}/observability/calls/{call_id}")
def scoped_call(project_id: str, call_id: int):
    _assert_scope(project_id, _call_project(call_id), "调用记录")
    return _raw_call_detail_payload(call_id)


@router.get("/projects/{project_id}/observability/calls/{call_id}/download", response_class=PlainTextResponse)
def scoped_call_download(project_id: str, call_id: int):
    _assert_scope(project_id, _call_project(call_id), "调用记录")
    return json.dumps(
        _raw_call_detail_payload(call_id),
        ensure_ascii=False,
        indent=2,
    )


@router.get("/projects/{project_id}/observability/traces/{object_type}/{object_id}")
def scoped_trace(
    project_id: str,
    object_type: str,
    object_id: str,
    source: str = "auto",
):
    return _trace_tree(project_id, object_type, object_id, source)


@router.get("/projects/{project_id}/observability/traces/{object_type}/{object_id}/nodes/{node_id}")
def scoped_trace_node(
    project_id: str,
    object_type: str,
    object_id: str,
    node_id: str,
    source: str = "auto",
):
    return _trace_node_detail(
        project_id, object_type, object_id, node_id, source,
    )


@router.get("/projects/{project_id}/observability/gates")
def scoped_gates(project_id: str, limit: int = Query(100, ge=1, le=500)):
    _project(project_id)
    return orchestration_api.list_pending_gates(project_id=project_id, limit=limit)


@router.post("/projects/{project_id}/observability/gates/{artifact_id}/decision")
def scoped_gate_decision(project_id: str, artifact_id: str, body: dict = Body(...)):
    _assert_scope(project_id, _artifact_project(artifact_id), "门禁产物")
    return orchestration_api.decide_gate(artifact_id, body)


@router.get("/projects/{project_id}/observability/artifacts/{artifact_id}")
def scoped_artifact(project_id: str, artifact_id: str):
    _assert_scope(project_id, _artifact_project(artifact_id), "证据产物")
    return orchestration_api.get_artifact(artifact_id)


@router.get("/projects/{project_id}/observability/artifacts/{artifact_id}/{part}")
def scoped_artifact_part(project_id: str, artifact_id: str, part: str):
    _assert_scope(project_id, _artifact_project(artifact_id), "证据产物")
    if part == "evals":
        return orchestration_api.get_artifact_evaluations(artifact_id)
    if part == "lineage":
        return orchestration_api.get_artifact_lineage(artifact_id)
    raise HTTPException(404, "证据视图不存在")


@router.get("/observability/resolve")
def resolve_legacy_observability(
    run_id: str | None = None, job_id: str | None = None, call_id: int | None = None,
    source: str = "auto",
):
    provided = sum(value is not None for value in (run_id, job_id, call_id))
    if provided != 1:
        raise HTTPException(422, "必须且只能提供 run_id、job_id、call_id 之一")
    if run_id:
        project_id, section, object_id = _run_project(run_id), "jobs", run_id
    elif job_id:
        project_id, section, object_id = _job_project(job_id, source), "jobs", job_id
    else:
        project_id, section, object_id = _call_project(int(call_id)), "calls", str(call_id)
    if not project_id:
        raise HTTPException(404, "观测对象未关联有效项目")
    _project(project_id)
    return {"project_id": project_id, "section": section, "object_id": object_id}


@router.get("/system/overview")
def system_overview():
    """System-wide aggregate only: no raw run, job, or call records are returned."""
    conn = get_conn()
    projects = [dict(row) for row in conn.execute(
        "SELECT id,name,created_at FROM projects ORDER BY created_at DESC"
    ).fetchall()]
    jobs = system_api.jobs_overview(include_all=True)["recent"]
    by_project: dict[str, Counter[str]] = {item["id"]: Counter() for item in projects}
    unattributed_jobs = 0
    for row in jobs:
        pid = row.get("project_id")
        if pid in by_project:
            by_project[pid][str(row.get("status") or "unknown")] += 1
        else:
            unattributed_jobs += 1
    call_rows = [dict(row) for row in conn.execute(
        "SELECT id,meta,run_id,step_run_id FROM provider_calls ORDER BY id DESC"
    ).fetchall()]
    scope_maps = system_api._project_scope_maps()
    call_counts: Counter[str] = Counter()
    unattributed_calls = 0
    for row in call_rows:
        try:
            meta = json.loads(row.get("meta") or "{}")
        except (TypeError, json.JSONDecodeError):
            meta = {}
        pid = system_api._call_project_id(row, meta if isinstance(meta, dict) else {}, scope_maps)
        if pid in by_project:
            call_counts[pid] += 1
        else:
            unattributed_calls += 1
    return {
        "projects": [
            {**project, "job_counts": dict(by_project[project["id"]]), "call_count": call_counts[project["id"]]}
            for project in projects
        ],
        "totals": {
            "projects": len(projects), "jobs": len(jobs), "calls": len(call_rows),
            "unattributed_jobs": unattributed_jobs, "unattributed_calls": unattributed_calls,
        },
        "server_time": time.time(),
    }
