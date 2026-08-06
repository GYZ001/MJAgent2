"""视频失败 → 标准 Issue 纯翻译层（无副作用、不写库）。"""
from __future__ import annotations

from typing import Any

from app.db import get_setting
from app.harness.types import Issue, IssueSeverity

DEFAULT_FATAL_FAILURE_TYPES = (
    "character_duplicate",
    "wrong_identity",
    "wrong_outfit",
    "text_error",
)
VIDEO_QUALITY_REVIEW_THRESHOLD = 0.6

# QA / classify 失败码 → Issue code。内容问题保持 WARNING，仅用于评分和排序。
_QA_CODE_MAP: dict[str, tuple[str, IssueSeverity]] = {
    "character_duplicate": ("VIDEO_QA_CHARACTER_DUPLICATE", IssueSeverity.WARNING),
    "text_error": ("VIDEO_QA_TEXT_ARTIFACT", IssueSeverity.WARNING),
    "state_mismatch": ("VIDEO_QA_STATE_MISMATCH", IssueSeverity.WARNING),
    "story_repeat": ("VIDEO_QA_STORY_REPEAT", IssueSeverity.WARNING),
    "future_leak": ("VIDEO_QA_FUTURE_LEAK", IssueSeverity.WARNING),
    "wrong_dialogue": ("VIDEO_QA_WRONG_DIALOGUE", IssueSeverity.WARNING),
    "needs_crop": ("VIDEO_QA_NEEDS_CROP", IssueSeverity.WARNING),
    "wrong_identity": ("VIDEO_QA_WRONG_IDENTITY", IssueSeverity.WARNING),
    "wrong_outfit": ("VIDEO_QA_WRONG_OUTFIT", IssueSeverity.WARNING),
    "subject_occlusion": ("VIDEO_QA_SUBJECT_OCCLUSION", IssueSeverity.WARNING),
    "action_missing": ("VIDEO_QA_ACTION_MISSING", IssueSeverity.WARNING),
    "prop_identity_mismatch": ("VIDEO_QA_PROP_IDENTITY", IssueSeverity.WARNING),
    "prop_state_mismatch": ("VIDEO_QA_PROP_STATE", IssueSeverity.WARNING),
    "object_count_mismatch": ("VIDEO_QA_OBJECT_COUNT", IssueSeverity.WARNING),
    "wrong_camera_axis": ("VIDEO_QA_CAMERA_AXIS", IssueSeverity.WARNING),
    "geometry_guard_unverified": ("VIDEO_QA_GEOMETRY", IssueSeverity.WARNING),
}

_TECHNICAL_CODE_MAP: dict[str, tuple[str, IssueSeverity]] = {
    "FILE_MISSING": ("VIDEO_FILE_INVALID", IssueSeverity.BLOCKER),
    "FILE_EMPTY": ("VIDEO_FILE_INVALID", IssueSeverity.BLOCKER),
    "VIDEO_CONTAINER_INVALID": ("VIDEO_FILE_INVALID", IssueSeverity.BLOCKER),
    "VIDEO_DURATION_CONTRACT": ("VIDEO_DURATION_CONTRACT", IssueSeverity.BLOCKER),
    "VIDEO_PROBE_UNAVAILABLE": ("VIDEO_PROBE_UNAVAILABLE", IssueSeverity.WARNING),
    "VIDEO_DURATION_UNVERIFIED": ("VIDEO_PROBE_UNAVAILABLE", IssueSeverity.WARNING),
}


def fatal_failure_types() -> set[str]:
    raw = get_setting("video_fatal_failure_types")
    if not raw:
        return set(DEFAULT_FATAL_FAILURE_TYPES)
    return {part.strip() for part in str(raw).split(",") if part.strip()} or set(DEFAULT_FATAL_FAILURE_TYPES)


def is_fatal_failure_code(code: str) -> bool:
    """硬失败码（classify 返回）是否致命。"""
    return code in fatal_failure_types()


def is_fatal(issue: Issue) -> bool:
    """非 QA Issue 是否属于需要停止自动处理的致命类。"""
    if issue.code.startswith("VIDEO_QA_"):
        return issue.severity == IssueSeverity.BLOCKER
    rule = str((issue.evidence or {}).get("rule_id") or "")
    return bool(rule and rule in fatal_failure_types())


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
    """从 QA + 技术门禁翻译 Issue。"""
    from app.continuity import classify_video_hard_failures

    qa = qa or {}
    technical = technical or {}
    out: list[Issue] = []

    # 技术门禁 Issue
    for raw in technical.get("issues") or []:
        if isinstance(raw, dict):
            tech_code = str(raw.get("code") or "")
            msg = str(raw.get("message") or tech_code)
        else:
            tech_code = getattr(raw, "code", "") or ""
            msg = getattr(raw, "message", "") or tech_code
        mapped = _TECHNICAL_CODE_MAP.get(tech_code)
        if mapped:
            code, sev = mapped
            out.append(_mk(
                code, sev, shot_id=shot_id, message=msg,
                shot_no=shot_no, version_id=version_id, job_id=job_id, rule_id=tech_code,
            ))

    hard = classify_video_hard_failures(qa, technical=technical)
    for ft in hard:
        mapped = _QA_CODE_MAP.get(ft)
        if not mapped:
            continue
        code, sev = mapped
        out.append(_mk(
            code, sev,
            shot_id=shot_id,
            message=f"视频 QA 质量风险：{ft}",
            shot_no=shot_no, version_id=version_id, job_id=job_id, rule_id=ft,
            repair_hint="仅供具体缺陷定位；是否重试由完整片段生产合同决定",
        ))

    if qa.get("whole_clip_usable") is False:
        out.append(_mk(
            "VIDEO_QA_CLIP_CONTRACT_FAILED",
            IssueSeverity.BLOCKER,
            shot_id=shot_id,
            message="完整片段生产合同未通过，禁止自动采用并进入通用修复路由",
            shot_no=shot_no,
            version_id=version_id,
            job_id=job_id,
            rule_id="whole_clip_usable",
            repair_hint="按本次 QA 的结构化诊断重抽，不依赖失败码白名单",
        ))

    if qa.get("qa_recovered"):
        out.append(_mk(
            "VIDEO_QA_UNAVAILABLE", IssueSeverity.WARNING,
            shot_id=shot_id,
            message="VLM QA 不可用或已恢复占位，结果不可信",
            shot_no=shot_no, version_id=version_id, job_id=job_id, rule_id="qa_recovered",
        ))

    try:
        overall = float(qa.get("overall")) if qa.get("overall") is not None else None
    except (TypeError, ValueError):
        overall = None
    threshold = VIDEO_QUALITY_REVIEW_THRESHOLD
    if overall is not None and overall < threshold and not hard:
        out.append(_mk(
            "VIDEO_QA_LOW_SCORE", IssueSeverity.WARNING,
            shot_id=shot_id,
            message=f"QA 总分 {overall:.3f} 低于阈值 {threshold:.3f}",
            shot_no=shot_no, version_id=version_id, job_id=job_id, rule_id="low_score",
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
    """从失败 job / version 错误文本翻译 Issue。"""
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
    err = str(_get(job, "error") or _get(version, "error") or "")
    status = str(_get(job, "status") or "")
    stage = str(_get(job, "pipeline_stage") or "")
    reason_code = str(_get(job, "reason_code") or "")
    provider_create_state = str(_get(job, "provider_create_state") or "")
    lower = err.lower()

    out: list[Issue] = []
    if provider_create_state == "model_rejected":
        out.append(_mk(
            "VIDEO_PROVIDER_MODEL_REJECTED",
            IssueSeverity.BLOCKER,
            shot_id=sid,
            message=err or "视频模型明确拒绝本次输入",
            shot_no=shot_no,
            version_id=vid,
            job_id=jid,
            rule_id="model_rejected",
            repairable=False,
            extra={
                "provider_reason_code": reason_code,
                "provider_create_state": provider_create_state,
                "pause_state": "PAUSED_EXTERNAL",
            },
        ))
        return out

    if status == "paused_budget" or "预算" in err:
        out.append(_mk(
            "VIDEO_BUDGET_EXHAUSTED", IssueSeverity.BLOCKER,
            shot_id=sid, message=err or "预算不足，任务暂停",
            shot_no=shot_no, version_id=vid, job_id=jid, rule_id="budget",
            repairable=False,
        ))
        return out

    if "waiting_human" in status or stage.endswith("waiting_human") or "连续性" in err or "尾帧" in err:
        out.append(_mk(
            "VIDEO_CHAIN_ANCHOR_BLOCKED", IssueSeverity.BLOCKER,
            shot_id=sid, message=err or "等待上一镜尾帧超时/阻塞",
            shot_no=shot_no, version_id=vid, job_id=jid, rule_id="chain_anchor",
        ))
        return out

    if any(k in err for k in ("内容安全", "安全审核", "safety", "敏感")):
        out.append(_mk(
            "VIDEO_PROVIDER_SAFETY", IssueSeverity.BLOCKER,
            shot_id=sid, message=err or "内容安全拒绝",
            shot_no=shot_no, version_id=vid, job_id=jid, rule_id="safety",
        ))
        return out

    if any(k in err for k in ("版权", "copyright")):
        out.append(_mk(
            "VIDEO_PROVIDER_COPYRIGHT", IssueSeverity.BLOCKER,
            shot_id=sid, message=err or "版权限制",
            shot_no=shot_no, version_id=vid, job_id=jid, rule_id="copyright",
        ))
        return out

    if any(k in lower for k in ("timeout", "超时", "max_wait", "max wait")):
        out.append(_mk(
            "VIDEO_PROVIDER_TIMEOUT", IssueSeverity.BLOCKER,
            shot_id=sid, message=err or "Provider 超时",
            shot_no=shot_no, version_id=vid, job_id=jid, rule_id="timeout",
        ))
        return out

    if any(k in err for k in ("下载", "download")):
        out.append(_mk(
            "VIDEO_DOWNLOAD_FAILED", IssueSeverity.BLOCKER,
            shot_id=sid, message=err or "视频下载失败",
            shot_no=shot_no, version_id=vid, job_id=jid, rule_id="download",
        ))
        return out

    if any(k in err for k in ("技术校验", "文件技术", "VIDEO_DURATION", "FILE_MISSING", "CONTAINER")):
        out.append(_mk(
            "VIDEO_FILE_INVALID", IssueSeverity.BLOCKER,
            shot_id=sid, message=err or "视频文件技术校验失败",
            shot_no=shot_no, version_id=vid, job_id=jid, rule_id="technical",
        ))
        return out

    if any(k in err for k in ("参考图", "reference", "多视角资产包", "造型版本")):
        out.append(_mk(
            "VIDEO_REFERENCE_UNAVAILABLE", IssueSeverity.BLOCKER,
            shot_id=sid, message=err or "参考图生成失败",
            shot_no=shot_no, version_id=vid, job_id=jid, rule_id="reference",
        ))
        return out

    if any(k in lower for k in ("429", "5xx", "502", "503", "504", "限流", "瞬时", "网络", "超时")):
        out.append(_mk(
            "VIDEO_PROVIDER_TRANSIENT", IssueSeverity.WARNING,
            shot_id=sid, message=err or "Provider 瞬时错误",
            shot_no=shot_no, version_id=vid, job_id=jid, rule_id="transient",
        ))
        return out

    if err:
        out.append(_mk(
            "VIDEO_PROVIDER_TRANSIENT", IssueSeverity.WARNING,
            shot_id=sid, message=err,
            shot_no=shot_no, version_id=vid, job_id=jid, rule_id="unknown_failure",
        ))
    return out


def issues_from_enqueue_error(
    exc: Exception,
    *,
    shot_id: str,
    shot_no: int | None = None,
) -> list[Issue]:
    """入队异常 → Issue（禁止静默漏镜）。"""
    msg = str(exc) or exc.__class__.__name__
    name = exc.__class__.__name__
    lower = msg.lower()

    if "Harness" in msg or "灰度" in msg or "隔离" in msg:
        return [_mk(
            "VIDEO_HARNESS_DISABLED", IssueSeverity.BLOCKER,
            shot_id=shot_id, message=msg, shot_no=shot_no, rule_id="harness",
            repairable=False,
        )]

    if name == "CompileError" or "preflight" in lower or any(
        k in msg for k in ("门禁", "容量", "状态链", "必填", "禁止", "口播", "动作过载")
    ):
        return [_mk(
            "VIDEO_PREFLIGHT_BLOCKED", IssueSeverity.BLOCKER,
            shot_id=shot_id, message=msg, shot_no=shot_no, rule_id="preflight",
            repair_hint="需微调分镜或转人工",
        )]

    if "预算" in msg or "budget" in lower:
        return [_mk(
            "VIDEO_BUDGET_EXHAUSTED", IssueSeverity.BLOCKER,
            shot_id=shot_id, message=msg, shot_no=shot_no, rule_id="budget",
            repairable=False,
        )]

    return [_mk(
        "VIDEO_PREFLIGHT_BLOCKED", IssueSeverity.BLOCKER,
        shot_id=shot_id, message=msg, shot_no=shot_no, rule_id="enqueue",
    )]


def persist_shot_issue(
    *,
    episode_id: str,
    shot_id: str,
    shot_no: int | None,
    issues: list[Issue],
    source: str = "enqueue",
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


def load_persisted_shot_issues(shot_id: str) -> list[Issue]:
    """读取该镜最近持久化的 Issue 列表。"""
    import json
    from app.db import get_conn

    row = get_conn().execute(
        """SELECT content_json FROM artifacts
           WHERE type='video_shot_issue' AND scope_type='shot' AND scope_id=?
             AND status IN ('candidate','validated','approved')
           ORDER BY created_at DESC LIMIT 1""",
        (shot_id,),
    ).fetchone()
    if not row:
        return []
    try:
        raw = json.loads(row["content_json"] or "{}")
        return [Issue.model_validate(item) for item in (raw.get("issues") or [])]
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
