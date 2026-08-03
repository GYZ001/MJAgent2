from __future__ import annotations

import json
import threading

import pytest
from fastapi import HTTPException

from app import db
from app.domain import video_ops
from app.domain.video_ops import ConfirmationEvaluation
from app.evidence import repository
from app.harness.types import EvidenceArtifact, Issue, IssueSeverity
from app.production.publish import can_issue_certificate


@pytest.fixture()
def confirm_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "qa-score-only-confirm.db")
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "_local", threading.local())
    db.init_db()
    conn = db.get_conn()
    conn.execute(
        """INSERT INTO projects(id,name,bible_json,bible_status,plan_status,created_at)
           VALUES('p1','测试项目','', 'ready','ready',1)"""
    )
    screenplay = {
        "id": "script-1",
        "episode_no": 1,
        "title": "测试",
        "full_script_text": "少年推开房门，看见桌上的信，神色骤然一沉。",
    }
    conn.execute(
        """INSERT INTO episodes(
               id,project_id,episode_no,title,source_chapters,target_duration_s,
               screenplay_json,screenplay_status,screenplay_artifact_id,status,created_at
           ) VALUES('e1','p1',1,'第一集','[1]',10,?,'ready','screenplay-v1','scripted',1)""",
        (json.dumps(screenplay, ensure_ascii=False),),
    )
    source = "少年推开房门，看见桌上的信，神色骤然一沉。"
    conn.execute(
        "INSERT INTO chapters(project_id,idx,title,content) VALUES('p1',1,'第一章',?)",
        (source,),
    )
    artifact = repository.create_artifact(EvidenceArtifact(
        type="storyboard",
        scope_type="episode",
        scope_id="e1",
        status="validated",
        trust_level="T2",
        content={"episode_no": 1, "shots": [{"shot_no": 1}]},
    ))
    conn.execute(
        """INSERT INTO shots(
               id,episode_id,shot_no,duration_s,shot_size,camera_move,scene_setting,
               characters,action_desc,first_frame_desc,last_frame_desc,source_excerpt,
               narration,dialogues,transition,continuity_from_prev,shot_contract_json,
               storyboard_artifact_id
           ) VALUES('s1','e1',1,5,'中景','固定','白天，房间','["少年"]',?,?,?,?,
                    '', '[]','硬切',0,?,?)""",
        (
            "少年推开房门并拿起桌上的信件查看。",
            "少年站在紧闭的房门外。",
            "少年拿着信件神色骤然一沉。",
            source,
            json.dumps({"is_final": True}, ensure_ascii=False),
            artifact["id"],
        ),
    )
    conn.execute("UPDATE episodes SET storyboard_artifact_id=? WHERE id='e1'", (artifact["id"],))
    conn.commit()
    yield conn
    conn.close()


def test_confirm_preview_passes_with_low_quality_business_warnings(confirm_db, monkeypatch):
    monkeypatch.setattr(
        video_ops,
        "validate_storyboard",
        lambda *_args, **_kwargs: ["口播容量偏低；节奏覆盖不足；可拍性评分较低"],
    )
    monkeypatch.setattr(video_ops, "validate_storyboard_soundtrack", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        video_ops,
        "validate_storyboard_preserves_key_content",
        lambda *_args, **_kwargs: ["主线覆盖不足，作为 QA 评分警告"],
    )

    preview = video_ops.create_storyboard_confirmation_preview("e1")

    assert preview["hard_gates"]["passed"] is True
    assert preview["hard_gates"]["errors"] == []
    assert "口播容量偏低；节奏覆盖不足；可拍性评分较低" in preview["warnings"]
    assert "主线覆盖不足，作为 QA 评分警告" in preview["warnings"]
    assert preview["score_only"]["runtime_blocking"] is False


def test_must_keep_spine_delivery_warning_is_promoted_to_blocker(confirm_db, monkeypatch):
    issue = (
        "主线节拍主体已入画但未完成对应动作/对白交付："
        "S03/少年:在密室修炼一夜并明确修为进展"
    )
    monkeypatch.setattr(video_ops, "validate_storyboard", lambda *_args, **_kwargs: [issue])
    monkeypatch.setattr(video_ops, "validate_storyboard_soundtrack", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        video_ops, "validate_storyboard_preserves_key_content", lambda *_args, **_kwargs: [],
    )

    with pytest.raises(HTTPException) as caught:
        video_ops.create_storyboard_confirmation_preview("e1")

    preview = caught.value.detail
    assert preview["hard_gates"]["passed"] is False
    assert preview["hard_gates"]["errors"] == [issue]
    assert issue not in preview["warnings"]


def test_unlocatable_legacy_excerpt_stays_internal_audit_only(confirm_db, monkeypatch):
    confirm_db.execute(
        "UPDATE shots SET source_excerpt=? WHERE id='s1'",
        ("这是一段长度足够但并非授权章节逐字原文的历史证据",),
    )
    confirm_db.commit()
    monkeypatch.setattr(video_ops, "validate_storyboard", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(video_ops, "validate_storyboard_soundtrack", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(video_ops, "validate_storyboard_preserves_key_content", lambda *_args, **_kwargs: [])

    preview = video_ops.create_storyboard_confirmation_preview("e1")

    assert preview["hard_gates"]["passed"] is True
    assert not any("授权原文中定位" in warning for warning in preview["warnings"])
    result = video_ops.confirm_episode_core("e1", preview_token=preview["preview_token"])
    assert result["confirmed"] is True
    assert confirm_db.execute(
        "SELECT storyboard_warning FROM episodes WHERE id='e1'",
    ).fetchone()["storyboard_warning"] is None


def test_hard_gate_failure_blocks_confirmation_preview(confirm_db, monkeypatch):
    def repairable_evaluation(_ep, board, *_args, **_kwargs):
        return ConfirmationEvaluation(
            passed=False,
            errors=["shot_no=1.dialogue_framing 需要修复"],
            warnings=[],
            issues=[],
            board=board,
            compact_target=5,
            estimated_cost_cny=3.6,
        )

    monkeypatch.setattr(video_ops, "evaluate_storyboard_for_confirmation", repairable_evaluation)

    with pytest.raises(HTTPException) as caught:
        video_ops.create_storyboard_confirmation_preview("e1")

    assert caught.value.status_code == 409
    preview = caught.value.detail
    assert preview["hard_gates"]["passed"] is False
    assert preview["hard_gates"]["errors"] == ["shot_no=1.dialogue_framing 需要修复"]
    assert preview["unlocks"] == []
    assert "继续修复" in preview["recovery_action"]
    assert "preview_token" not in preview
    assert confirm_db.execute(
        "SELECT COUNT(*) AS c FROM gate_decisions WHERE gate_key='storyboard'",
    ).fetchone()["c"] == 0


def test_confirmation_submit_rechecks_hard_gates_after_preview(confirm_db, monkeypatch):
    def passed_evaluation(_ep, board, *_args, **_kwargs):
        return ConfirmationEvaluation(
            passed=True,
            errors=[],
            warnings=[],
            issues=[],
            board=board,
            compact_target=5,
            estimated_cost_cny=3.6,
        )

    def failed_evaluation(_ep, board, *_args, **_kwargs):
        return ConfirmationEvaluation(
            passed=False,
            errors=["主线节拍仍未完成"],
            warnings=[],
            issues=[],
            board=board,
            compact_target=5,
            estimated_cost_cny=3.6,
        )

    monkeypatch.setattr(video_ops, "evaluate_storyboard_for_confirmation", passed_evaluation)
    preview = video_ops.create_storyboard_confirmation_preview("e1")
    monkeypatch.setattr(video_ops, "evaluate_storyboard_for_confirmation", failed_evaluation)

    with pytest.raises(ValueError, match="主线节拍仍未完成"):
        video_ops.confirm_episode_core("e1", preview_token=preview["preview_token"])

    assert confirm_db.execute(
        "SELECT status FROM episodes WHERE id='e1'",
    ).fetchone()["status"] == "scripted"
    assert confirm_db.execute(
        "SELECT COUNT(*) AS c FROM gate_decisions WHERE gate_key='storyboard'",
    ).fetchone()["c"] == 0


def test_legacy_confirmed_board_is_blocked_before_paid_generation(confirm_db, monkeypatch):
    def failed_evaluation(_ep, board, *_args, **_kwargs):
        return ConfirmationEvaluation(
            passed=False,
            errors=["主线节拍仍未完成"],
            warnings=[],
            issues=[],
            board=board,
            compact_target=5,
            estimated_cost_cny=3.6,
        )

    confirm_db.execute("UPDATE episodes SET status='confirmed' WHERE id='e1'")
    confirm_db.commit()
    monkeypatch.setattr(video_ops, "evaluate_storyboard_for_confirmation", failed_evaluation)

    with pytest.raises(HTTPException) as caught:
        video_ops._assert_storyboard_generation_gate("e1")

    assert caught.value.status_code == 409
    assert caught.value.detail["code"] == "STORYBOARD_CONFIRMATION_REQUIRED"
    assert caught.value.detail["errors"] == ["主线节拍仍未完成"]
    assert "返回分镜台" in caught.value.detail["recovery_action"]


def test_screenplay_certificate_requires_all_runtime_gate_issues_to_be_fixed():
    qa_issue = Issue(
        code="BUSINESS_RULE_FAILED",
        severity=IssueSeverity.BLOCKER,
        subject="screenplay",
        message="剧情节奏评分低，需要优化但不影响结构交付",
        evidence={"must_fix": True},
        repairable=True,
    )
    structural_issue = Issue(
        code="SCHEMA_INVALID",
        severity=IssueSeverity.BLOCKER,
        subject="screenplay",
        message="schema 缺少必填字段",
        evidence={"must_fix": True},
        repairable=True,
    )

    assert can_issue_certificate([qa_issue]) is False
    assert can_issue_certificate([structural_issue]) is False
