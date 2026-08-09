from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from app.db import get_conn, now
from app.evidence import repository
from app.harness.contracts import get_contract
from app.harness.types import EvidenceArtifact
from app.orchestration.state_machine import transition_run, transition_step
from app.observability.tracing import bind_trace


T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class WorkflowStepPresentation:
    name: str
    description: str


# This is domain metadata owned by orchestration, not an observability fallback.
# Step keys remain the audit identity while these fields explain the workflow to users.
_STEP_PRESENTATIONS: dict[str, WorkflowStepPresentation] = {
    "generate": WorkflowStepPresentation(
        "生成业务内容", "根据当前环节的输入生成候选结果",
    ),
    "validate": WorkflowStepPresentation(
        "检查内容完整性", "检查候选结果是否满足结构与业务要求",
    ),
    "evaluate": WorkflowStepPresentation(
        "评估内容质量", "评估结果质量并确定是否需要修复",
    ),
    "repair": WorkflowStepPresentation(
        "修复未通过项", "根据检查结果定向修复问题",
    ),
    "screenplay": WorkflowStepPresentation(
        "生成完整剧本", "把本集原文转化为可审核、可继续拆镜的剧本",
    ),
    "character_discovery": WorkflowStepPresentation(
        "识别本集出场人物", "从本集原文识别人名、身份和人物关系",
    ),
    "character_discovery_resume_audit": WorkflowStepPresentation(
        "复核人物识别结果", "续跑前确认人物识别结果完整且仍然可用",
    ),
    "screenplay_blueprint": WorkflowStepPresentation(
        "规划全剧剧情结构", "梳理事件顺序、场景归属和关键叙事关系",
    ),
    "screenplay_identity_freeze": WorkflowStepPresentation(
        "统一人物身份与别名", "把原文中的称呼统一到确定的人物身份",
    ),
    "screenplay_envelope": WorkflowStepPresentation(
        "规划全剧叙事框架", "确定整集的开场、体验目标和结尾承接",
    ),
    "screenplay_scene_shards": WorkflowStepPresentation(
        "逐场撰写剧本", "按剧情结构并行撰写每个场次的动作与对白",
    ),
    "screenplay_merge": WorkflowStepPresentation(
        "合并并校验完整剧本", "合并全部场次并检查原文、人物和剧情一致性",
    ),
    "screenplay_document": WorkflowStepPresentation(
        "生成并验收完整剧本", "统筹剧本生成、质量修复和最终发布验收",
    ),
    "storyboard": WorkflowStepPresentation(
        "生成可执行分镜", "把剧本拆解为可以生成画面和视频的镜头",
    ),
    "character_bible": WorkflowStepPresentation(
        "建立人物设定", "整理人物身份、外观和贯穿全片的一致性要求",
    ),
    "character_references": WorkflowStepPresentation(
        "生成人物定妆照", "根据人物设定生成后续画面使用的外观参考",
    ),
    "scene_bible": WorkflowStepPresentation(
        "建立场景设定", "整理场景空间、时间和视觉一致性要求",
    ),
    "scene_references": WorkflowStepPresentation(
        "生成场景参考图", "根据场景设定生成后续画面使用的视觉参考",
    ),
    "episode_mapping": WorkflowStepPresentation(
        "规划分集内容", "把原始故事规划为连续且完整的分集结构",
    ),
    "scene_generation": WorkflowStepPresentation(
        "生成镜头关键帧", "为分镜生成视频制作所需的关键画面",
    ),
    "media_generation": WorkflowStepPresentation(
        "生成镜头素材", "生成当前镜头所需的图片或视频素材",
    ),
    "video_generation": WorkflowStepPresentation(
        "生成镜头视频", "根据分镜和关键帧生成单个镜头视频",
    ),
    "episode_video_completion": WorkflowStepPresentation(
        "补齐整集视频", "检查并补齐本集中尚未完成的视频镜头",
    ),
    "delivery": WorkflowStepPresentation(
        "执行成片交付", "检查成片条件并输出可交付结果",
    ),
    "delivery_package": WorkflowStepPresentation(
        "生成交付候选", "汇总剧本、分镜和视频形成交付候选",
    ),
    "build_delivery_snapshot": WorkflowStepPresentation(
        "汇总交付版本", "固定本次交付所使用的全部业务版本",
    ),
    "apply_delivery_gate": WorkflowStepPresentation(
        "确认交付条件", "根据质量门禁决定是否允许正式交付",
    ),
}


def step_presentation(step_key: str) -> WorkflowStepPresentation:
    """Return the user-facing contract declared for a persisted workflow step."""
    key = str(step_key or "").strip()
    presentation = _STEP_PRESENTATIONS.get(key)
    if presentation:
        return presentation
    return WorkflowStepPresentation(
        f"业务名称待配置（{key or '未命名步骤'}）",
        "该步骤尚未声明用户可理解的业务名称，请补充编排元数据",
    )


def _step_label(step_key: str) -> str:
    """Return the Chinese display label for a workflow step key."""
    return step_presentation(step_key).name


def fingerprint(*parts: Any) -> str:
    encoded = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class WorkflowRecorder:
    """Sidecar recorder for existing workflows during the Phase 1 migration.

    It owns persisted run/step state while the existing business coroutine remains
    the executor.  This keeps current behavior stable and lets later phases replace
    the executor without changing the evidence model.
    """

    def __init__(self, run_id: str):
        self.run_id = run_id

    @classmethod
    def create(
        cls,
        *,
        workflow_type: str,
        scope_type: str,
        scope_id: str,
        input_fingerprint: str,
        requested_by: str = "user",
        trigger_type: str = "manual",
        policy_snapshot: dict[str, Any] | None = None,
        config_snapshot: dict[str, Any] | None = None,
        budget_limit_cny: float | None = None,
        deadline_at: float | None = None,
        parent_run_id: str | None = None,
    ) -> "WorkflowRecorder":
        run_id = repository.create_run(
            workflow_type=workflow_type,
            scope_type=scope_type,
            scope_id=scope_id,
            input_fingerprint=input_fingerprint,
            requested_by=requested_by,
            trigger_type=trigger_type,
            policy_snapshot=policy_snapshot,
            config_snapshot=config_snapshot,
            budget_limit_cny=budget_limit_cny,
            deadline_at=deadline_at,
            parent_run_id=parent_run_id,
        )
        return cls(run_id)

    def start(self) -> None:
        transition_run(self.run_id, {"CREATED", "PAUSED_EXTERNAL", "WAITING_RETRY"}, "RUNNING", "运行开始")
        repository.append_event(self.run_id, "RUN_STARTED", "info", "运行开始")

    async def step(
        self,
        step_key: str,
        operation: Callable[[], Awaitable[T]],
        *,
        contract_key: str | None = None,
        agent_name: str | None = None,
        iteration_no: int = 1,
        input_artifact_ids: list[str] | None = None,
        context_manifest: dict[str, Any] | None = None,
    ) -> tuple[str, T]:
        contract = get_contract(contract_key) if contract_key else None
        step_id = repository.create_step(
            self.run_id,
            step_key,
            iteration_no=iteration_no,
            agent_name=agent_name,
            contract_version=contract.version if contract else None,
            input_artifact_ids=input_artifact_ids,
            context_manifest=context_manifest,
        )
        transition_step(step_id, "PENDING", "READY", "输入已就绪")
        transition_step(step_id, "READY", "RUNNING", "步骤开始")
        conn = get_conn()
        conn.execute(
            "UPDATE workflow_runs SET current_step_key=?, updated_at=? WHERE id=? AND status='RUNNING'",
            (step_key, now(), self.run_id),
        )
        conn.commit()
        repository.append_event(
            self.run_id, "STEP_STARTED", "info", f"步骤开始：{_step_label(step_key)}", step_run_id=step_id
        )
        try:
            with bind_trace(self.run_id, step_id):
                result = await operation()
        except asyncio.CancelledError:
            transition_step(step_id, "RUNNING", "CANCELLED", "运行被取消", decision="cancel")
            repository.append_event(
                self.run_id, "STEP_CANCELLED", "warning", f"步骤已取消：{_step_label(step_key)}", step_run_id=step_id
            )
            raise
        except Exception as exc:
            transition_step(
                step_id, "RUNNING", "FAILED", str(exc)[:1000], decision="escalate",
                error_code=type(exc).__name__.upper(),
            )
            repository.append_event(
                self.run_id, "STEP_FAILED", "error", f"步骤失败：{_step_label(step_key)}",
                step_run_id=step_id,
                payload={"error_type": type(exc).__name__, "message": str(exc)[:1000]},
            )
            raise
        transition_step(step_id, "RUNNING", "SUCCEEDED", "步骤完成", decision="accept")
        repository.append_event(
            self.run_id, "STEP_SUCCEEDED", "info", f"步骤完成：{_step_label(step_key)}", step_run_id=step_id
        )
        return step_id, result

    def artifact(self, step_run_id: str, artifact: EvidenceArtifact) -> dict[str, Any]:
        created = repository.create_artifact(artifact, step_run_id=step_run_id)
        conn = get_conn()
        conn.execute(
            "UPDATE step_runs SET output_artifact_id=? WHERE id=?",
            (created["id"], step_run_id),
        )
        conn.commit()
        return created

    def refresh_cost(self) -> float:
        """Project the currently attributable media spend onto the persisted run."""
        conn = get_conn()
        row = conn.execute(
            """SELECT COALESCE(SUM(v.cost_cny), 0) AS total
               FROM jobs j
               LEFT JOIN shot_versions v ON v.id=j.version_id
               WHERE j.run_id=?""",
            (self.run_id,),
        ).fetchone()
        total = float(row["total"] if row else 0)
        conn.execute(
            "UPDATE workflow_runs SET cost_cny=?, updated_at=? WHERE id=?",
            (total, now(), self.run_id),
        )
        conn.commit()
        return total

    def succeed(self, message: str = "运行完成") -> None:
        self.refresh_cost()
        transition_run(self.run_id, "RUNNING", "SUCCEEDED", message)
        repository.append_event(self.run_id, "RUN_SUCCEEDED", "info", message)

    def partial(self, message: str) -> None:
        self.refresh_cost()
        transition_run(self.run_id, "RUNNING", "PARTIAL", message, failure_code="PARTIAL_RESULT")
        repository.append_event(self.run_id, "RUN_PARTIAL", "warning", message)

    def fail(self, exc: BaseException) -> None:
        self.refresh_cost()
        transition_run(
            self.run_id, "RUNNING", "FAILED", str(exc)[:1000], failure_code=type(exc).__name__.upper()
        )
        repository.append_event(
            self.run_id, "RUN_FAILED", "error", "运行失败",
            payload={"error_type": type(exc).__name__, "message": str(exc)[:1000]},
        )

    def cancel(self, message: str = "运行已取消") -> None:
        self.refresh_cost()
        transition_run(
            self.run_id,
            {
                "CREATED", "RUNNING", "WAITING_RETRY", "WAITING_HUMAN",
                "WAITING_AUTHORIZATION", "PAUSED_BUDGET", "PAUSED_EXTERNAL",
            },
            "CANCELLED",
            message,
        )
        repository.append_event(self.run_id, "RUN_CANCELLED", "warning", message)

    def pause_external(self, message: str = "服务停机，等待自动恢复") -> None:
        """Persist a recoverable process interruption without pretending the user cancelled."""
        self.refresh_cost()
        transition_run(
            self.run_id,
            "RUNNING",
            "PAUSED_EXTERNAL",
            message,
            failure_code="SERVICE_RESTART",
        )
        repository.append_event(
            self.run_id, "RUN_PAUSED_EXTERNAL", "warning", message,
        )
