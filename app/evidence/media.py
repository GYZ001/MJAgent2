from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from app.continuity import classify_video_hard_failures
from app.db import get_conn, get_setting
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
                    blocker=False,
                ))
        except (OSError, ValueError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
            evidence["probe_error"] = str(exc)[:300]
            issues.append(_issue(
                "VIDEO_PROBE_UNAVAILABLE", "ffprobe 未能完成解码/时长复验", str(file_path),
                blocker=False,
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
    hard_failures = [str(message) for message in (qa.get("hard_failures") or []) if str(message).strip()]
    issues = [
        _issue("MEDIA_QUALITY", str(message), subject, blocker=False)
        for message in (qa.get("issues") or [])[:20]
    ]
    issues.extend(
        _issue("MEDIA_QUALITY_WARNING", message, subject, blocker=False)
        for message in hard_failures[:20]
    )
    if recovered:
        issues.append(_issue(
            "EVALUATOR_RECOVERED", "评估器输出经保守恢复，不能独立触发自动采用", subject
        ))
    explicit_unverified = qa.get("status") in {"unverified", "pending"}
    status = "error" if recovered or explicit_unverified else (
        "failed" if hard_failures else ("warning" if issues else "passed")
    )
    return Evaluation(
        evaluator_type="model",
        evaluator_name=evaluator_name,
        evaluator_version="1.0.0",
        status=status,
        hard_gate_passed=not recovered and not explicit_unverified and not hard_failures
        and qa.get("hard_gate_passed") is not False,
        evaluation_role=(qa.get("evaluation_role") or "retry_and_rank"),
        score_status=(
            "unavailable" if recovered or explicit_unverified or score is None else "scored"
        ),
        runtime_blocking=False,
        retry_eligible=bool(qa.get("retry_eligible")),
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


def merge_observed_state_out_into_shot_contract(shot_id: str, observed_state_out: str) -> None:
    observed = (observed_state_out or "").strip()
    if not observed:
        return
    conn = get_conn()
    row = conn.execute(
        "SELECT shot_contract_json FROM shots WHERE id=?", (shot_id,)
    ).fetchone()
    contract: dict[str, Any] = {}
    if row and row["shot_contract_json"]:
        try:
            loaded = json.loads(row["shot_contract_json"])
            if isinstance(loaded, dict):
                contract = loaded
        except (TypeError, ValueError):
            contract = {}
    contract["observed_state_out"] = observed
    conn.execute(
        "UPDATE shots SET observed_state_out=?, shot_contract_json=? WHERE id=?",
        (observed, json.dumps(contract, ensure_ascii=False), shot_id),
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


def _qa_overall(qa: dict[str, Any]) -> float | None:
    try:
        return float(qa.get("overall"))
    except (TypeError, ValueError):
        return None


def _version_is_delivery_fallback(row: Any) -> bool:
    """历史图片/静音占位版本不具备模型视频资格。"""
    if row is None:
        return False
    try:
        raw_meta = row.get("image_inputs") if isinstance(row, dict) else row["image_inputs"]
        meta = json.loads(raw_meta or "{}")
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError):
        return False
    return bool(isinstance(meta, dict) and meta.get("delivery_fallback"))


def grade_shot_video(
    shot_id: str | None = None,
    *,
    technical: dict[str, Any] | None = None,
    qa: dict[str, Any] | None = None,
    version_row: dict[str, Any] | None = None,
    continuity_degraded: bool = False,
) -> dict[str, Any]:
    """可用视频三级判定（A/B/C），确定性、不调用模型。

    可传入已加载的 technical/qa，或 shot_id（读采用版/最佳成功版）。
    """
    from app.video_issues import fatal_failure_types, is_fatal_failure_code

    conn = get_conn()
    row = dict(version_row) if version_row is not None else None
    rejected_fallback = _version_is_delivery_fallback(row)
    if rejected_fallback:
        row = None
    if row is None and shot_id:
        shot = conn.execute(
            "SELECT adopted_version_id FROM shots WHERE id=?", (shot_id,)
        ).fetchone()
        vid = shot["adopted_version_id"] if shot else None
        if vid:
            row = conn.execute("SELECT * FROM shot_versions WHERE id=?", (vid,)).fetchone()
            if _version_is_delivery_fallback(row):
                row = None
                rejected_fallback = True
        if row is None:
            candidates = conn.execute(
                """SELECT * FROM shot_versions
                   WHERE shot_id=? AND status='succeeded'
                   ORDER BY version_no DESC""",
                (shot_id,),
            ).fetchall()
            row = next(
                (candidate for candidate in candidates if not _version_is_delivery_fallback(candidate)),
                None,
            )
    if row is not None and not isinstance(row, dict):
        row = dict(row)

    # 调用方若显式传入了历史占位版本的检测结果，不能让它们在
    # 已拒绝该版本后继续伪装成 A/B 级视频。找到真实候选时由其落库证据重算。
    if rejected_fallback:
        technical = None
        qa = None

    if technical is None and row is not None:
        technical = json.loads(row.get("technical_validation_json") or "{}")
    if qa is None and row is not None:
        qa = json.loads(row.get("qa_json") or "{}")
    technical = technical or {}
    qa = qa or {}

    if row is not None and not continuity_degraded:
        try:
            meta = json.loads(row.get("image_inputs") or "{}")
            continuity_degraded = bool(meta.get("continuity_degraded"))
        except (TypeError, ValueError):
            pass

    try:
        threshold = float(get_setting("auto_retake_threshold") or 0.6)
    except (TypeError, ValueError):
        threshold = 0.6

    hard_failures = classify_video_hard_failures(qa, technical=technical) if technical or qa else []
    fatal = [f for f in hard_failures if is_fatal_failure_code(f)]
    non_fatal = [f for f in hard_failures if f not in fatal]
    score = _qa_overall(qa)
    qa_recovered = bool(qa.get("qa_recovered"))
    passed = bool(technical.get("passed"))
    version_id = (row or {}).get("id") if row else None

    fallback_reason: str | None = None
    if not passed or fatal:
        grade = "C"
    elif continuity_degraded:
        grade = "B"
        fallback_reason = "连续性已降链（纯参考图模式），衔接可能变弱"
    elif (
        not fatal
        and not qa_recovered
        and not non_fatal
        and score is not None
        and score >= threshold
    ):
        grade = "A"
    elif passed and not fatal:
        grade = "B"
        reasons = []
        if score is None or score < threshold:
            reasons.append(f"QA {score if score is not None else 'n/a'} < 阈值 {threshold:.2f}")
        if non_fatal:
            reasons.append("非致命硬失败：" + ",".join(non_fatal))
        if qa_recovered:
            reasons.append("QA 结果为 recovered 占位")
        fallback_reason = "；".join(reasons) or "技术合格但未达 A 级标准"
    else:
        grade = "C"

    return {
        "grade": grade,
        "version_id": version_id,
        "technical_passed": passed,
        "hard_failures": hard_failures,
        "fatal_failures": fatal,
        "qa_overall": score,
        "threshold": threshold,
        "qa_recovered": qa_recovered,
        "continuity_degraded": continuity_degraded,
        "fallback_reason": fallback_reason,
        "fatal_failure_types": sorted(fatal_failure_types()),
    }


_SELECTION_RISK_WEIGHTS = {
    "character_duplicate": 0.45,
    "wrong_identity": 0.45,
    "action_missing": 0.40,
    "wrong_dialogue": 0.35,
    "future_leak": 0.30,
    "prop_identity_mismatch": 0.28,
    "object_count_mismatch": 0.28,
    "prop_state_mismatch": 0.24,
    "state_mismatch": 0.22,
    "story_repeat": 0.30,
    "wrong_outfit": 0.18,
    "wrong_camera_axis": 0.16,
    "geometry_guard_unverified": 0.14,
    # 文字与轻裁切有确定性后期路径，不能压过错人、分身或动作缺失。
    "text_error": 0.10,
    "subject_occlusion": 0.10,
    "needs_crop": 0.06,
}


def video_candidate_selection_score(
    score: float, hard_failures: list[str], *, qa_recovered: bool
) -> float:
    """跨 Worker/Supervisor 共用的候选排序分；不是交付门禁。"""
    base = score if score >= 0 else 0.0
    penalty = sum(_SELECTION_RISK_WEIGHTS.get(code, 0.12) for code in set(hard_failures))
    if qa_recovered:
        penalty += 0.08
    return round(base - penalty, 4)


def select_best_video_candidate(
    shot_id: str, *, force_best: bool = False
) -> dict[str, Any] | None:
    """比较技术合格候选并落盘采用理由。

    ``force_best=False`` 只在达到质量目标或 QA 全部不可用时采用；否则返回
    ``None`` 让 Supervisor/Worker 使用剩余预算定向重试。``force_best=True``
    表示预算已耗尽，必须选择当前综合风险最低的可播放候选完成交付。
    """
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
    technical_pool: list[dict[str, Any]] = []
    for row in rows:
        # 确定性交付兜底不是模型候选，不能参与 best-of-N，也不能因版本号较新
        # 覆盖已经有动作和声音的真实视频。
        if _version_is_delivery_fallback(row):
            continue
        technical = json.loads(row["technical_validation_json"] or "{}")
        if not technical:
            try:
                record_video_candidate(row["id"])
            except ValueError:
                continue
            row = conn.execute("SELECT * FROM shot_versions WHERE id=?", (row["id"],)).fetchone()
            technical = json.loads(row["technical_validation_json"] or "{}")
        if not technical.get("passed"):
            continue
        qa = json.loads(row["qa_json"] or "{}")
        score = _qa_overall(qa)
        hard_failures = classify_video_hard_failures(qa, technical=technical)
        entry = {
            "id": row["id"],
            "version_no": row["version_no"],
            "score": score if score is not None else -1.0,
            "qa": qa,
            "hard_failures": hard_failures,
            "qa_recovered": bool(
                qa.get("qa_recovered")
                or qa.get("status") in {"unverified", "pending"}
                or score is None
            ),
        }
        entry["selection_score"] = video_candidate_selection_score(
            entry["score"], hard_failures, qa_recovered=entry["qa_recovered"]
        )
        technical_pool.append(entry)

    if not technical_pool:
        return None

    # 优先推荐分数达标者；否则取技术合格中最高分（不阻塞采用）。
    qualified = [
        entry for entry in technical_pool
        if not entry["qa_recovered"]
        and not entry["hard_failures"]
        and entry["score"] >= threshold
    ]
    scored_pool = [entry for entry in technical_pool if not entry["qa_recovered"] and entry["score"] >= 0]
    if not qualified and not force_best and scored_pool:
        return None

    candidates = qualified or technical_pool
    fallback = not bool(qualified)

    ordered = sorted(
        candidates,
        key=lambda item: (
            not item["qa_recovered"],
            item["selection_score"],
            item["score"],
            item["version_no"],
        ),
        reverse=True,
    )
    best = ordered[0]
    margin = (
        best["selection_score"] - ordered[1]["selection_score"]
        if len(ordered) > 1 else best["selection_score"]
    )
    if fallback:
        reason = (
            f"质量预算耗尽/QA 不可用后自动比较 {len(ordered)} 个技术合格视频；"
            f"采纳综合风险最低的 v{best['version_no']}，QA={best['score']:.3f}，"
            f"选择分={best['selection_score']:.3f}（目标 {threshold:.3f}），"
            f"领先次优 {margin:.3f}，剩余问题={best['hard_failures']}。"
        )
    else:
        reason = (
            f"自动比较 {len(ordered)} 个技术合格候选；选择 v{best['version_no']}，"
            f"QA={best['score']:.3f}，选择分={best['selection_score']:.3f}，"
            f"领先次优 {margin:.3f}。"
        )
    previous = conn.execute(
        "SELECT adopted_version_id FROM shots WHERE id=?", (shot_id,)
    ).fetchone()
    observed_state_out = (best.get("qa") or {}).get("observed_state_out")
    if observed_state_out:
        merge_observed_state_out_into_shot_contract(shot_id, str(observed_state_out))
    conn.execute("UPDATE shots SET adopted_version_id=? WHERE id=?", (best["id"], shot_id))
    conn.execute("UPDATE shot_versions SET adoption_reason=? WHERE id=?", (reason, best["id"]))
    conn.commit()
    if previous and previous["adopted_version_id"] != best["id"]:
        from app.artifacts import invalidate_episode_final

        shot = conn.execute("SELECT episode_id FROM shots WHERE id=?", (shot_id,)).fetchone()
        if shot:
            invalidate_episode_final(shot["episode_id"])
    image_inputs = "{}"
    try:
        img_row = conn.execute(
            "SELECT image_inputs FROM shot_versions WHERE id=?", (best["id"],)
        ).fetchone()
        if img_row:
            image_inputs = img_row["image_inputs"] or "{}"
    except Exception:  # noqa: BLE001 旧库无 image_inputs 列
        image_inputs = "{}"
    tech_row = conn.execute(
        "SELECT technical_validation_json FROM shot_versions WHERE id=?",
        (best["id"],),
    ).fetchone()
    graded = grade_shot_video(
        shot_id,
        technical=json.loads((tech_row["technical_validation_json"] if tech_row else None) or "{}"),
        qa=best.get("qa") or {},
        version_row={"id": best["id"], "image_inputs": image_inputs},
    )
    grade = graded["grade"]
    # force_best 兜底最多 B 级（不可伪装成 A）
    if fallback and grade == "A":
        grade = "B"
        graded["fallback_reason"] = reason
    return {
        "version_id": best["id"],
        "reason": reason,
        "comparison": ordered,
        "fallback": fallback,
        "grade": grade,
        "fallback_reason": graded.get("fallback_reason") if grade == "B" else None,
    }


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
    from app.rejected_media import discard_file
    if not technical["passed"]:
        discard_file(file_path)
        return {
            "id": None,
            "type": asset_type,
            "scope_id": scope_id,
            "status": "rejected_deleted",
            "file_path": None,
        }
    # 所有内容 QA 都只评分。文件技术可用时，即使 QA 明确失败也必须保留并采用，
    # 避免付费产物在门禁耗尽后被删除。
    if model_eval is not None:
        if (
            model_eval.score is None
            or model_eval.score < min_score * 100
            or model_eval.status in {"failed", "error", "warning"}
        ):
            if model_eval.score is None or model_eval.score < min_score * 100:
                model_eval.issues.append(_issue(
                    "REFERENCE_QUALITY_SCORE_ONLY",
                    f"参考资产质量分 {None if model_eval.score is None else model_eval.score / 100:.2f} "
                    f"低于展示阈值 {min_score:.2f}（仅评分，不拦截）",
                    scope_id,
                    blocker=False,
                ))
            model_eval.status = "scored"
        demoted: list[Issue] = []
        for issue in model_eval.issues:
            if issue.severity == IssueSeverity.BLOCKER:
                demoted.append(issue.model_copy(update={"severity": IssueSeverity.WARNING}))
            else:
                demoted.append(issue)
        model_eval.issues = demoted
        model_eval.evaluation_role = "score_only"
        model_eval.runtime_blocking = False
        model_eval.retry_eligible = False
        model_eval.status = "scored" if model_eval.status in {"failed", "error", "warning"} else model_eval.status
        if model_eval.status not in {"passed", "scored", "warning"}:
            model_eval.status = "scored"
    acceptable = technical["passed"]
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
