from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from app.db import get_conn
from app.evidence import repository
from app.harness.types import Evaluation, EvidenceArtifact, Issue, IssueSeverity


def _issue(code: str, message: str, subject: str, *, blocker: bool = True) -> Issue:
    return Issue(
        code=code,
        severity=IssueSeverity.BLOCKER if blocker else IssueSeverity.WARNING,
        subject=subject,
        message=message,
        repair_hint="重新生成该媒体候选，或人工复验后明确承担风险",
        repairable=True,
    )


def validate_video_file(path: str, *, expected_duration_s: float = 5.0) -> dict[str, Any]:
    file_path = Path(path)
    issues: list[Issue] = []
    evidence: dict[str, Any] = {
        "path": str(file_path),
        "expected_duration_s": expected_duration_s,
        "exists": file_path.is_file(),
    }
    if not file_path.is_file():
        issues.append(_issue("FILE_MISSING", "视频文件不存在", str(file_path)))
        return {"passed": False, "issues": issues, "evidence": evidence}
    evidence["size_bytes"] = file_path.stat().st_size
    if file_path.stat().st_size <= 0:
        issues.append(_issue("FILE_EMPTY", "视频文件为空", str(file_path)))
    with file_path.open("rb") as handle:
        header = handle.read(32)
    evidence["container_signature"] = "mp4" if b"ftyp" in header else "unknown"
    if b"ftyp" not in header:
        issues.append(_issue("VIDEO_CONTAINER_INVALID", "视频缺少 MP4 容器签名", str(file_path)))

    if shutil.which("ffprobe"):
        try:
            raw = subprocess.run(
                [
                    "ffprobe", "-v", "error", "-show_entries",
                    "format=duration,format_name", "-of", "json", str(file_path),
                ],
                capture_output=True,
                text=True,
                check=True,
                timeout=20,
            ).stdout
            probe = json.loads(raw or "{}")
            fmt = probe.get("format") or {}
            duration = float(fmt.get("duration") or 0)
            evidence.update({
                "duration_s": round(duration, 3),
                "format_name": fmt.get("format_name"),
                "duration_verified": True,
            })
            tolerance = max(0.35, expected_duration_s * 0.1)
            if abs(duration - expected_duration_s) > tolerance:
                issues.append(_issue(
                    "VIDEO_DURATION_CONTRACT",
                    f"视频实测 {duration:.2f}s，不符合分镜选择的 {expected_duration_s:.0f}s 合同",
                    str(file_path),
                ))
        except (OSError, ValueError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
            evidence["probe_error"] = str(exc)[:300]
            issues.append(_issue(
                "VIDEO_PROBE_UNAVAILABLE", "ffprobe 未能完成解码/时长复验", str(file_path)
            ))
    else:
        evidence["duration_verified"] = False
        issues.append(_issue(
            "VIDEO_DURATION_UNVERIFIED",
            f"当前环境缺少 ffprobe，分镜选择的 {expected_duration_s:.0f} 秒时长尚未独立复验",
            str(file_path),
            blocker=False,
        ))
    return {
        "passed": not any(issue.severity == IssueSeverity.BLOCKER for issue in issues),
        "issues": issues,
        "evidence": evidence,
    }


def validate_image_file(path: str) -> dict[str, Any]:
    file_path = Path(path)
    issues: list[Issue] = []
    evidence: dict[str, Any] = {"path": str(file_path), "exists": file_path.is_file()}
    if not file_path.is_file():
        issues.append(_issue("FILE_MISSING", "图片文件不存在", str(file_path)))
        return {"passed": False, "issues": issues, "evidence": evidence}
    size = file_path.stat().st_size
    evidence["size_bytes"] = size
    with file_path.open("rb") as handle:
        header = handle.read(16)
    valid_signature = header.startswith(b"\xff\xd8\xff") or header.startswith(b"\x89PNG\r\n\x1a\n")
    evidence["signature_valid"] = valid_signature
    if size <= 0 or not valid_signature:
        issues.append(_issue("IMAGE_DECODE_CONTRACT", "图片为空或不是可识别的 JPEG/PNG", str(file_path)))
    return {"passed": not issues, "issues": issues, "evidence": evidence}


def _model_evaluation(qa: dict[str, Any] | None, *, subject: str, evaluator_name: str) -> Evaluation:
    qa = qa or {}
    recovered = bool(qa.get("qa_recovered"))
    raw_score = qa.get("overall")
    try:
        score = max(0.0, min(100.0, float(raw_score) * 100))
    except (TypeError, ValueError):
        score = None
    issues = [
        _issue("MEDIA_QUALITY", str(message), subject, blocker=False)
        for message in (qa.get("issues") or [])[:20]
    ]
    if recovered:
        issues.append(_issue(
            "EVALUATOR_RECOVERED", "评估器输出经保守恢复，不能独立触发自动采用", subject
        ))
    status = "error" if recovered else ("warning" if issues else "passed")
    return Evaluation(
        evaluator_type="model",
        evaluator_name=evaluator_name,
        evaluator_version="1.0.0",
        status=status,
        hard_gate_passed=not recovered,
        score=score,
        dimension_scores={
            key: float(value) * 100
            for key, value in qa.items()
            if key not in {"overall", "issues", "qa_recovered"} and isinstance(value, (int, float))
        },
        issues=issues,
        evidence={"qa": qa},
        recovered=recovered,
    )


def record_video_candidate(version_id: str, *, step_run_id: str | None = None) -> dict[str, Any]:
    conn = get_conn()
    row = conn.execute(
        """SELECT v.*, s.episode_id, s.duration_s, s.storyboard_artifact_id
           FROM shot_versions v JOIN shots s ON s.id=v.shot_id WHERE v.id=?""",
        (version_id,),
    ).fetchone()
    if not row or not row["video_path"]:
        raise ValueError("视频候选尚未落盘")
    if row["artifact_id"]:
        artifact = repository.get_artifact(row["artifact_id"])
        if artifact:
            return artifact
    technical = validate_video_file(row["video_path"], expected_duration_s=row["duration_s"])
    qa = json.loads(row["qa_json"] or "{}")
    artifact = repository.create_artifact(
        EvidenceArtifact(
            type="shot_video",
            scope_type="shot",
            scope_id=row["shot_id"],
            status="validated" if technical["passed"] else "candidate",
            trust_level="T3" if technical["passed"] and qa and not qa.get("qa_recovered") else "T1",
            file_path=row["video_path"],
            content={
                "version_id": version_id,
                "version_no": row["version_no"],
                "prompt_text": row["prompt_text"],
                "provider_task_id": row["provider_task_id"],
            },
            parent_artifact_ids=[row["storyboard_artifact_id"]] if row["storyboard_artifact_id"] else [],
            contract_version="video-2.0.0",
        ),
        step_run_id=step_run_id,
    )
    file_eval = Evaluation(
        evaluator_type="file",
        evaluator_name="video_technical_validator",
        evaluator_version="1.0.0",
        status="passed" if technical["passed"] else "failed",
        hard_gate_passed=technical["passed"],
        score=100 if technical["passed"] else 0,
        issues=technical["issues"],
        evidence=technical["evidence"],
    )
    repository.create_evaluation(artifact["id"], file_eval, step_run_id=step_run_id)
    if qa:
        repository.create_evaluation(
            artifact["id"],
            _model_evaluation(qa, subject=version_id, evaluator_name="video_vlm_qa"),
            step_run_id=step_run_id,
        )
    conn.execute(
        "UPDATE shot_versions SET artifact_id=?, technical_validation_json=? WHERE id=?",
        (
            artifact["id"],
            json.dumps({
                **technical,
                "issues": [issue.model_dump(mode="json") for issue in technical["issues"]],
            }, ensure_ascii=False),
            version_id,
        ),
    )
    conn.commit()
    return artifact


def select_best_video_candidate(shot_id: str) -> dict[str, Any] | None:
    """Compare every technically valid candidate and persist an explicit adoption reason."""
    conn = get_conn()
    threshold_row = conn.execute(
        "SELECT value FROM settings WHERE key='auto_retake_threshold'"
    ).fetchone()
    try:
        threshold = float(threshold_row["value"] if threshold_row else 0.6)
    except (TypeError, ValueError):
        threshold = 0.6
    rows = conn.execute(
        "SELECT * FROM shot_versions WHERE shot_id=? AND status='succeeded' ORDER BY version_no",
        (shot_id,),
    ).fetchall()
    candidates: list[dict[str, Any]] = []
    for row in rows:
        technical = json.loads(row["technical_validation_json"] or "{}")
        if not technical:
            try:
                record_video_candidate(row["id"])
            except ValueError:
                continue
            row = conn.execute("SELECT * FROM shot_versions WHERE id=?", (row["id"],)).fetchone()
            technical = json.loads(row["technical_validation_json"] or "{}")
        qa = json.loads(row["qa_json"] or "{}")
        if not technical.get("passed") or qa.get("qa_recovered"):
            continue
        try:
            score = float(qa.get("overall")) if qa else 0.5
        except (TypeError, ValueError):
            score = 0.5
        if qa and score < threshold:
            continue
        candidates.append({"id": row["id"], "version_no": row["version_no"], "score": score})
    if not candidates:
        return None
    ordered = sorted(candidates, key=lambda item: (item["score"], item["version_no"]), reverse=True)
    best = ordered[0]
    margin = best["score"] - ordered[1]["score"] if len(ordered) > 1 else best["score"]
    reason = (
        f"自动比较 {len(ordered)} 个技术门禁通过候选；选择 v{best['version_no']}，"
        f"质量分 {best['score']:.3f}，领先次优 {margin:.3f}。"
    )
    previous = conn.execute(
        "SELECT adopted_version_id FROM shots WHERE id=?", (shot_id,)
    ).fetchone()
    conn.execute("UPDATE shots SET adopted_version_id=? WHERE id=?", (best["id"], shot_id))
    conn.execute("UPDATE shot_versions SET adoption_reason=? WHERE id=?", (reason, best["id"]))
    conn.commit()
    if previous and previous["adopted_version_id"] != best["id"]:
        from app.artifacts import invalidate_episode_final

        shot = conn.execute("SELECT episode_id FROM shots WHERE id=?", (shot_id,)).fetchone()
        if shot:
            invalidate_episode_final(shot["episode_id"])
    return {"version_id": best["id"], "reason": reason, "comparison": ordered}


def record_scene_candidate(scene_id: str, *, step_run_id: str | None = None) -> dict[str, Any]:
    conn = get_conn()
    row = conn.execute(
        """SELECT sc.*, s.storyboard_artifact_id FROM shot_scenes sc
           JOIN shots s ON s.id=sc.shot_id WHERE sc.id=?""",
        (scene_id,),
    ).fetchone()
    if not row or not row["image_path"]:
        raise ValueError("关键帧候选尚未落盘")
    if row["artifact_id"]:
        artifact = repository.get_artifact(row["artifact_id"])
        if artifact:
            return artifact
    technical = validate_image_file(row["image_path"])
    qa = json.loads(row["qa_json"] or "{}")
    artifact = repository.create_artifact(
        EvidenceArtifact(
            type="shot_keyframe",
            scope_type="shot",
            scope_id=row["shot_id"],
            status="validated" if technical["passed"] else "candidate",
            trust_level="T3" if technical["passed"] and qa and not qa.get("qa_recovered") else "T1",
            file_path=row["image_path"],
            content={"scene_id": scene_id, "kind": row["kind"], "version_no": row["version_no"]},
            parent_artifact_ids=[row["storyboard_artifact_id"]] if row["storyboard_artifact_id"] else [],
            contract_version="keyframe-1.0.0",
        ),
        step_run_id=step_run_id,
    )
    repository.create_evaluation(
        artifact["id"],
        Evaluation(
            evaluator_type="file",
            evaluator_name="image_technical_validator",
            evaluator_version="1.0.0",
            status="passed" if technical["passed"] else "failed",
            hard_gate_passed=technical["passed"],
            score=100 if technical["passed"] else 0,
            issues=technical["issues"],
            evidence=technical["evidence"],
        ),
        step_run_id=step_run_id,
    )
    if qa:
        repository.create_evaluation(
            artifact["id"],
            _model_evaluation(qa, subject=scene_id, evaluator_name="keyframe_vlm_qa"),
            step_run_id=step_run_id,
        )
    conn.execute("UPDATE shot_scenes SET artifact_id=? WHERE id=?", (artifact["id"], scene_id))
    conn.commit()
    return artifact


def record_reference_asset(
    *,
    asset_type: str,
    scope_id: str,
    file_path: str,
    content: dict[str, Any],
    parent_artifact_ids: list[str] | None = None,
    qa: dict[str, Any] | None = None,
    min_score: float = 0.6,
    step_run_id: str | None = None,
) -> dict[str, Any]:
    technical = validate_image_file(file_path)
    model_eval = _model_evaluation(
        qa,
        subject=scope_id,
        evaluator_name=f"{asset_type}_consistency_qa",
    ) if qa else None
    if model_eval and (model_eval.score is None or model_eval.score < min_score * 100):
        model_eval.hard_gate_passed = False
        model_eval.status = "failed"
        model_eval.issues.append(_issue(
            "REFERENCE_QUALITY_THRESHOLD",
            f"参考资产质量分未达到 {min_score:.2f}",
            scope_id,
        ))
    acceptable = technical["passed"] and (model_eval is None or model_eval.hard_gate_passed)
    artifact = repository.create_artifact(
        EvidenceArtifact(
            type=asset_type,
            scope_type="reference_asset",
            scope_id=scope_id,
            status="validated" if acceptable else "candidate",
            trust_level="T3" if acceptable and model_eval else ("T2" if acceptable else "T1"),
            file_path=file_path,
            content=content,
            parent_artifact_ids=parent_artifact_ids or [],
            contract_version="reference-1.0.0",
        ),
        step_run_id=step_run_id,
    )
    file_eval = Evaluation(
        evaluator_type="file", evaluator_name="image_technical_validator",
        evaluator_version="1.0.0", status="passed" if technical["passed"] else "failed",
        hard_gate_passed=technical["passed"], score=100 if technical["passed"] else 0,
        issues=technical["issues"], evidence=technical["evidence"],
    )
    evaluations = [file_eval, *([model_eval] if model_eval else [])]
    if acceptable:
        artifact = repository.commit_artifact(step_run_id, artifact["id"], evaluations)
    else:
        for evaluation in evaluations:
            repository.create_evaluation(artifact["id"], evaluation, step_run_id=step_run_id)
    return artifact
