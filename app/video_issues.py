"""视频失败 → 标准 Issue 纯翻译层（无副作用、不写库）。"""
from __future__ import annotations

from typing import Any

from app.harness.types import Issue, IssueSeverity

VIDEO_QUALITY_REVIEW_THRESHOLD = 0.6


def is_fatal(issue: Issue) -> bool:
    """致命性来自结构化 severity/runtime gate，不来自失败码集合。"""
    return bool(
        issue.severity == IssueSeverity.BLOCKER
        and (issue.evidence or {}).get("runtime_blocking") is True
    )


def _mk(
    code: str,
    severity: IssueSeverity,
    *,
    shot_id: str,
    message: str,
    shot_no: int | None = None,
    version_id: str | None = None,
    job_id: str | None = None,
    rule_id: str | None = None,
    repair_hint: str | None = None,
    repairable: bool = True,
    category: str = "quality",
    extra: dict[str, Any] | None = None,
) -> Issue:
    evidence: dict[str, Any] = dict(extra or {})
    if shot_no is not None:
        evidence["shot_no"] = shot_no
        evidence["path"] = str(shot_no)
    if version_id:
        evidence["version_id"] = version_id
    if job_id:
        evidence["job_id"] = job_id
    if rule_id:
        evidence["rule_id"] = rule_id
    return Issue(
        code=code,
        severity=severity,
        category=category,
        subject=shot_id,
        message=message,
        evidence=evidence,
        repair_hint=repair_hint,
        repairable=repairable,
    )


def issues_from_qa(
    qa: dict | None,
    technical: dict | None,
    *,
    shot_id: str,
    version_id: str | None = None,
    shot_no: int | None = None,
    job_id: str | None = None,
) -> list[Issue]:
    """把结构化 QA/技术合同翻译为 Issue；不解析诊断文案。"""
    from app.continuity import classify_video_hard_failures

    qa = qa or {}
    technical = technical or {}
    out: list[Issue] = []
    for raw in technical.get("issues") or []:
        if isinstance(raw, dict):
            tech_code = str(raw.get("code") or "")
            message = str(raw.get("message") or tech_code)
        else:
            tech_code = str(getattr(raw, "code", "") or "")
            message = str(getattr(raw, "message", "") or tech_code)
        out.append(_mk(
            "VIDEO_TECHNICAL_CONTRACT_FAILED",
            IssueSeverity.BLOCKER,
            shot_id=shot_id,
            message=message or "视频技术合同未通过",
            shot_no=shot_no,
            version_id=version_id,
            job_id=job_id,
            rule_id=tech_code or "technical_contract",
            category="operational",
            extra={"runtime_blocking": True, "recommended_level": "L1"},
        ))

    facts = classify_video_hard_failures(qa, technical=technical)
    for fact in facts:
        out.append(_mk(
            "VIDEO_QA_CONTRACT_FACT",
            IssueSeverity.WARNING,
            shot_id=shot_id,
            message=f"视频 QA 结构化合同事实：{fact}",
            shot_no=shot_no,
            version_id=version_id,
            job_id=job_id,
            rule_id=fact,
            category="quality",
            repair_hint="依据本次结构化诊断复核镜头合同",
        ))

    if qa.get("whole_clip_usable") is False or qa.get("runtime_blocking") is True:
        out.append(_mk(
            "VIDEO_QA_CLIP_CONTRACT_FAILED",
            IssueSeverity.BLOCKER,
            shot_id=shot_id,
            message="完整片段生产合同未通过，禁止自动采用",
            shot_no=shot_no,
            version_id=version_id,
            job_id=job_id,
            rule_id="whole_clip_usable",
            repair_hint="按本次 QA 的结构化诊断决定是否重抽",
            category="quality",
            extra={"runtime_blocking": True, "recommended_level": "L1"},
        ))

    if qa.get("qa_recovered"):
        out.append(_mk(
            "VIDEO_QA_UNAVAILABLE",
            IssueSeverity.WARNING,
            shot_id=shot_id,
            message="VLM QA 不可用或已恢复占位，结果不可信",
            shot_no=shot_no,
            version_id=version_id,
            job_id=job_id,
            rule_id="qa_recovered",
            category="quality",
        ))

    try:
        overall = float(qa.get("overall")) if qa.get("overall") is not None else None
    except (TypeError, ValueError):
        overall = None
    threshold = VIDEO_QUALITY_REVIEW_THRESHOLD
    if overall is not None and overall < threshold and not facts:
        out.append(_mk(
            "VIDEO_QA_LOW_SCORE",
            IssueSeverity.WARNING,
            shot_id=shot_id,
            message=f"QA 总分 {overall:.3f} 低于阈值 {threshold:.3f}",
            shot_no=shot_no,
            version_id=version_id,
            job_id=job_id,
            rule_id="low_score",
            category="quality",
            extra={"overall": overall, "threshold": threshold},
        ))
    return out


def issues_from_job_failure(
    job: dict | Any,
    version: dict | Any | None = None,
    *,
    shot_id: str | None = None,
    shot_no: int | None = None,
) -> list[Issue]:
    """从持久化任务状态翻译 Issue；错误正文只展示，不参与分类。"""
    from app.hiagent import ProviderFailureCategory, ProviderFailureDisposition

    def _get(obj: Any, key: str, default=None):
        if obj is None:
            return default
        if isinstance(obj, dict):
            return obj.get(key, default)
        try:
            return obj[key]
        except (KeyError, IndexError, TypeError):
            return getattr(obj, key, default)

    sid = shot_id or str(_get(job, "shot_id") or "")
    jid = str(_get(job, "id") or "") or None
    vid = str(_get(version, "id") or "") or None
    message = str(_get(job, "error") or _get(version, "error") or "")
    status = str(_get(job, "status") or "")
    stage = str(_get(job, "pipeline_stage") or "")
    reason_code = str(_get(job, "reason_code") or "")
    provider_state = str(_get(job, "provider_create_state") or "")
    failure_category = str(_get(job, "provider_failure_category") or "")
    failure_kind = str(_get(job, "provider_failure_kind") or "")
    failure_disposition = str(_get(job, "provider_failure_disposition") or "")
    failure_retryable = bool(_get(job, "provider_failure_retryable"))

    if reason_code == "VIDEO_PROMPT_PROVIDER_REJECTED":
        return [_mk(
            "VIDEO_PROMPT_PROVIDER_REJECTED",
            IssueSeverity.BLOCKER,
            shot_id=sid,
            message=message or "AI 视频提示词服务明确拒绝当前内容",
            shot_no=shot_no,
            version_id=vid,
            job_id=jid,
            rule_id=reason_code,
            repairable=False,
            category="operational",
            extra={
                "provider_create_state": provider_state,
                "provider_reason_code": reason_code,
                "pause_state": "PAUSED_EXTERNAL",
                "recommended_level": "L6",
                "runtime_blocking": True,
            },
        )]

    if (
        failure_category == ProviderFailureCategory.MODEL_REJECTION.value
        or provider_state == "model_rejected"
    ):
        return [_mk(
            "VIDEO_PROVIDER_MODEL_REJECTED",
            IssueSeverity.BLOCKER,
            shot_id=sid,
            message=message or "视频模型明确拒绝本次输入",
            shot_no=shot_no,
            version_id=vid,
            job_id=jid,
            rule_id=provider_state,
            repairable=False,
            category="operational",
            extra={
                "provider_create_state": provider_state,
                "provider_failure_category": failure_category or None,
                "provider_failure_kind": failure_kind or None,
                "provider_failure_disposition": failure_disposition or None,
                "provider_reason_code": reason_code or None,
                "pause_state": "PAUSED_EXTERNAL",
                "recommended_level": "L6",
                "runtime_blocking": True,
            },
        )]

    if (
        failure_category == ProviderFailureCategory.TECHNICAL.value
        and failure_disposition == ProviderFailureDisposition.MANUAL_REVIEW.value
    ):
        return [_mk(
            "VIDEO_PROVIDER_TECHNICAL_FAILURE",
            IssueSeverity.BLOCKER,
            shot_id=sid,
            message=message or "视频供应商发生技术失败，等待人工处理",
            shot_no=shot_no,
            version_id=vid,
            job_id=jid,
            rule_id=failure_kind or reason_code,
            repairable=False,
            category="operational",
            extra={
                "provider_create_state": provider_state,
                "provider_failure_category": failure_category,
                "provider_failure_kind": failure_kind,
                "provider_failure_disposition": failure_disposition,
                "provider_failure_retryable": failure_retryable,
                "provider_reason_code": reason_code or None,
                "pause_state": "WAITING_HUMAN",
                "recommended_level": "L6",
                "runtime_blocking": True,
            },
        )]

    if status in {"waiting_human", "paused"} or stage.endswith("waiting_human"):
        return [_mk(
            "VIDEO_OPERATION_WAITING_HUMAN",
            IssueSeverity.BLOCKER,
            shot_id=sid,
            message=message or "视频任务等待人工处理",
            shot_no=shot_no,
            version_id=vid,
            job_id=jid,
            rule_id=status or stage,
            repairable=False,
            category="operational",
            extra={
                "provider_reason_code": reason_code or None,
                "pause_state": "WAITING_HUMAN",
                "recommended_level": "L6",
                "runtime_blocking": True,
            },
        )]

    severity = (
        IssueSeverity.WARNING if status == "waiting_retry" else IssueSeverity.BLOCKER
    )
    return [_mk(
        "VIDEO_OPERATION_FAILED",
        severity,
        shot_id=sid,
        message=message or "视频任务未完成",
        shot_no=shot_no,
        version_id=vid,
        job_id=jid,
        rule_id=status or "operation_failed",
        category="operational",
        extra={
            "provider_reason_code": reason_code or None,
            "recommended_level": "L0" if status == "waiting_retry" else "L1",
        },
    )]


def _quota_detail(exc: Exception) -> dict:
    detail = getattr(exc, "detail", None)
    return detail if isinstance(detail, dict) else {}


def _is_concurrency_quota_wait(exc: Exception) -> bool:
    from app.quota import QuotaExceeded  # 延迟导入：app.quota 与 db 链有环，模块级会拉起

    return isinstance(exc, QuotaExceeded) and _quota_detail(exc).get("gate") == "concurrency"


def issues_from_enqueue_error(
    exc: Exception,
    *,
    shot_id: str,
    shot_no: int | None = None,
) -> list[Issue]:
    """入队异常翻译为结构化 Issue；异常正文不参与策略选择。"""
    from app.compiler import CompileError

    message = str(exc) or exc.__class__.__name__
    retryable = bool(getattr(exc, "retryable", False))
    failure_kind = str(getattr(exc, "failure_kind", "") or "")
    if _is_concurrency_quota_wait(exc):
        # 账号视频并发触顶不是这一镜的错：等现有任务结束就能入队，按可等待的
        # 告警（L0，不付费）处理，不能升级成阻断或人工介入（2026-09-04 连播集级并行后
        # 一小时 38 次触顶全被记成错误）。
        return [_mk(
            "VIDEO_ENQUEUE_WAIT_CONCURRENCY",
            IssueSeverity.WARNING,
            shot_id=shot_id,
            message=str(_quota_detail(exc).get("message") or message),
            shot_no=shot_no,
            rule_id="quota_concurrency",
            repair_hint="等待账号内其它视频任务结束后自动重新入队",
            category="operational",
            extra={"recommended_level": "L0", "wait": True},
        )]
    if isinstance(exc, CompileError):
        return [_mk(
            "VIDEO_PREFLIGHT_BLOCKED",
            IssueSeverity.BLOCKER,
            shot_id=shot_id,
            message=message,
            shot_no=shot_no,
            rule_id=failure_kind or "compile_contract",
            repair_hint="通过受控分镜候选修订解决结构合同错误",
            category="structural",
            extra={
                "recommended_level": "L0" if retryable else "L5",
                "runtime_blocking": not retryable,
            },
        )]
    return [_mk(
        "VIDEO_ENQUEUE_OPERATION_FAILED",
        IssueSeverity.WARNING if retryable else IssueSeverity.BLOCKER,
        shot_id=shot_id,
        message=message,
        shot_no=shot_no,
        rule_id=failure_kind or exc.__class__.__name__,
        category="operational",
        extra={"recommended_level": "L0" if retryable else "L1"},
    )]


def persist_shot_issue(
    *,
    episode_id: str,
    shot_id: str,
    shot_no: int | None,
    issues: list[Issue],
    source: str = "enqueue",
    run_id: str | None = None,
) -> str | None:
    """把入队/失败 Issue 持久化为 episode 级 artifact，供 Coverage Ledger 合并。

    返回 artifact id；无 issues 时返回 None。
    """
    if not issues:
        return None
    from app.evidence import repository as evidence_repository
    from app.harness.types import Evaluation, EvidenceArtifact

    artifact = evidence_repository.create_artifact(EvidenceArtifact(
        type="video_shot_issue",
        scope_type="shot",
        scope_id=shot_id,
        status="validated",
        trust_level="T2",
        content={
            "episode_id": episode_id,
            "shot_id": shot_id,
            "shot_no": shot_no,
            "source": source,
            "run_id": run_id,
            "issues": [i.model_dump(mode="json") for i in issues],
        },
        contract_version="video-issue-1.0.0",
    ))
    evidence_repository.create_evaluation(
        artifact["id"],
        Evaluation(
            evaluator_type="deterministic",
            evaluator_name="video_issue_recorder",
            evaluator_version="1.0.0",
            status="failed",
            hard_gate_passed=False,
            score=0,
            issues=issues,
            evidence={"source": source, "shot_no": shot_no},
        ),
    )
    return artifact["id"]


def load_persisted_shot_issues(
    shot_id: str,
    *,
    run_id: str | None = None,
) -> list[Issue]:
    """读取该镜最近持久化的 Issue 列表。

    Supervisor 传入当前 ``run_id`` 时，只消费本运行产生的诊断，
    避免已取消任务的历史预检错误污染新的手动重跑。
    """
    import json
    from app.db import get_conn

    rows = get_conn().execute(
        """SELECT content_json FROM artifacts
           WHERE type='video_shot_issue' AND scope_type='shot' AND scope_id=?
             AND status IN ('candidate','validated','approved')
           ORDER BY created_at DESC LIMIT 32""",
        (shot_id,),
    ).fetchall()
    for row in rows:
        try:
            raw = json.loads(row["content_json"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if run_id is not None and str(raw.get("run_id") or "") != run_id:
            continue
        try:
            return [
                Issue.model_validate(item) for item in (raw.get("issues") or [])
            ]
        except (TypeError, ValueError):
            continue
    return []
