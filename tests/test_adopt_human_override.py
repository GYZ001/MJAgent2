"""人工采纳是最高优先级（2026-09-15 用户拍板）：字幕闸门等质量判定只记录不拦；系统代采仍不越过。"""
from __future__ import annotations

import json

import pytest
from fastapi import HTTPException

from app import api
from tests.conftest import patch_api_everywhere
from tests.test_episode_partial_concat import _database, _version

_GATED = json.dumps({"passed": False, "issues": [{"code": "subtitle_overlay", "severity": "blocker", "category": "quality", "subject": "video", "message": "画面叠加了字幕"}]})
_BROKEN = json.dumps({"passed": False, "issues": [{"code": "container_invalid", "severity": "blocker", "category": "structural", "subject": "video", "message": "容器损坏"}]})


def _setup(monkeypatch, tmp_path, *, status: str, technical: str):
    conn = _database()
    video_path = tmp_path / "candidate.mp4"
    video_path.write_bytes(b"video")
    _version(conn, shot_no=1, path=video_path, adopted=False)
    conn.execute("UPDATE shot_versions SET technical_validation_json=?, status=? WHERE id='v1'", (technical, status))
    conn.commit()
    patch_api_everywhere(monkeypatch, "get_conn", lambda: conn)
    patch_api_everywhere(monkeypatch, "_review_assert_shot_positive", lambda *_args: None)
    monkeypatch.setattr(api.evidence_repository, "commit_artifact", lambda *_args, **_kwargs: {})
    patch_api_everywhere(monkeypatch, "_review_write_audit", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(api.worker, "invalidate_episode_final", lambda *_args: None)
    from app.evidence import media as media_evidence
    monkeypatch.setattr(media_evidence, "record_video_candidate", lambda *_args, **_kwargs: {"id": "art-v1"})
    return conn


def test_human_adoption_overrides_subtitle_gate_and_records_it(tmp_path, monkeypatch) -> None:
    conn = _setup(monkeypatch, tmp_path, status="succeeded", technical=_GATED)
    result = api._adopt_version_core("s1", {"version_id": "v1", "reason": "生成台预览时人工选定 v1", "human_override": True})
    assert result["adopted"] == "v1"
    assert conn.execute("SELECT adopted_version_id FROM shots WHERE id='s1'").fetchone()[0] == "v1"
    assert "subtitle_overlay" in conn.execute("SELECT adoption_reason FROM shot_versions WHERE id='v1'").fetchone()[0]


def test_human_adoption_settles_waiting_human_version(tmp_path, monkeypatch) -> None:
    conn = _setup(monkeypatch, tmp_path, status="waiting_human", technical=_GATED)
    api._adopt_version_core("s1", {"version_id": "v1", "reason": "人工选定这一版", "human_override": True})
    assert conn.execute("SELECT status FROM shot_versions WHERE id='v1'").fetchone()[0] == "succeeded"


def test_auto_adoption_still_respects_the_gate(tmp_path, monkeypatch) -> None:
    _setup(monkeypatch, tmp_path, status="succeeded", technical=_GATED)
    with pytest.raises(HTTPException) as exc:
        api._adopt_version_core("s1", {"version_id": "v1", "reason": "成片合成时自动采纳"})
    assert "技术门禁" in str(exc.value.detail)


def test_broken_file_cannot_be_adopted_even_by_human(tmp_path, monkeypatch) -> None:
    _setup(monkeypatch, tmp_path, status="succeeded", technical=_BROKEN)
    with pytest.raises(HTTPException) as exc:
        api._adopt_version_core("s1", {"version_id": "v1", "reason": "人工选定这一版", "human_override": True})
    assert "不可用" in str(exc.value.detail)


def test_human_override_persists_marker_without_mutating_original_evidence(tmp_path, monkeypatch) -> None:
    """留痕：越过标记写进 technical_validation_json 的 human_override 子对象，
    passed/issues 等生成时原值原样保留（那是证据，交付质量报告要如实转述）。"""
    conn = _setup(monkeypatch, tmp_path, status="succeeded", technical=_GATED)
    api._adopt_version_core(
        "s1", {"version_id": "v1", "reason": "生成台预览时人工选定 v1", "human_override": True},
    )
    technical = json.loads(
        conn.execute("SELECT technical_validation_json FROM shot_versions WHERE id='v1'").fetchone()[0]
    )
    assert technical["passed"] is False
    assert technical["issues"] == json.loads(_GATED)["issues"]
    override = technical["human_override"]
    assert override["overridden_issue_codes"] == ["subtitle_overlay"]
    assert override["by"]
    assert override["reason"] == "生成台预览时人工选定 v1"
    assert override["at"]


def test_declined_human_override_leaves_no_marker(tmp_path, monkeypatch) -> None:
    """反向：采纳被拒时（人工越过一个结构性/文件级问题）不会留下 human_override
    标记——没有发生的越过不能留痕，留痕要跟真实发生的动作对齐。"""
    conn = _setup(monkeypatch, tmp_path, status="succeeded", technical=_BROKEN)
    with pytest.raises(HTTPException):
        api._adopt_version_core(
            "s1", {"version_id": "v1", "reason": "人工选定这一版", "human_override": True},
        )
    technical = json.loads(
        conn.execute("SELECT technical_validation_json FROM shot_versions WHERE id='v1'").fetchone()[0]
    )
    assert "human_override" not in technical
