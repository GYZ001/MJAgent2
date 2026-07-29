from __future__ import annotations

import json
import threading

import pytest

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


def test_force_confirm_is_available_only_after_complete_board_with_repair_issues(
    confirm_db, monkeypatch,
):
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

    with pytest.raises(video_ops.HTTPException) as caught:
        video_ops.create_storyboard_confirmation_preview("e1")

    detail = caught.value.detail
    assert detail["hard_gates"]["passed"] is False
    assert detail["force_confirmation"]["allowed"] is True
    assert detail["preview_token"].startswith("sbpv_")

    with pytest.raises(ValueError, match="未通过结构完整性门禁"):
        video_ops.confirm_episode_core("e1", preview_token=detail["preview_token"])

    result = video_ops.confirm_episode_core(
        "e1",
        preview_token=detail["preview_token"],
        force=True,
        force_reason="接受本镜构图风险，先进入生成台验证",
    )

    assert result["confirmed"] is True
    assert result["forced"] is True
    decision = confirm_db.execute(
        "SELECT decision,reason FROM gate_decisions WHERE gate_key='storyboard'",
    ).fetchone()
    assert decision["decision"] == "approve_with_risk"
    assert "接受本镜构图风险" in decision["reason"]


def test_can_issue_certificate_ignores_qa_quality_blockers():
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

    assert can_issue_certificate([qa_issue]) is True
    assert can_issue_certificate([structural_issue]) is False
