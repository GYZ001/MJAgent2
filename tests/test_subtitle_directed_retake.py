"""字幕命中的重抽必须是定向的：修正句进 Issue、进路由、进分镜包提示词。

2026-09-14 我欲封天第 2 集镜 5：三次同输入重抽都把「拜见师兄」烧在画面底部，L1 三连败
直接 L6 转人工、整集失败。2.x 镜头的提示词原样转发，定向重抽的批注以前只进幂等键。
"""
from __future__ import annotations

from types import SimpleNamespace

import json
import sqlite3

from app import db as db_mod
from app.evidence import subtitle_overlay
from app.media_exec import enqueue_prompt
from app.video_issues import issues_from_qa
from app.video_supervisor import coverage
from app.video_repair_router import route

HINT = "台词只以声音呈现，画面上不出现任何字幕、名条或标题条（上一版画面叠加了『拜见师兄』）"


def _technical(code: str = "subtitle_overlay", hint: str | None = HINT) -> dict:
    issue = {"code": code, "severity": "blocker", "subject": "video", "message": "画面叠加了字幕", "repairable": True}
    if hint:
        issue["repair_hint"] = hint
    return {"passed": False, "issues": [issue], "evidence": {}}


def test_subtitle_issue_carries_hint_and_requests_directed_level() -> None:
    issue = issues_from_qa({}, _technical(), shot_id="shot_1", version_id="ver_1", shot_no=5)[0]
    assert issue.code == "VIDEO_TECHNICAL_CONTRACT_FAILED" and issue.repair_hint == HINT
    assert issue.evidence["recommended_level"] == "L2" and issue.evidence["rule_id"] == "subtitle_overlay"


def test_other_technical_issues_keep_same_input_retake() -> None:
    issue = issues_from_qa({}, _technical(code="duration_mismatch", hint=None), shot_id="shot_1")[0]
    assert issue.evidence["recommended_level"] == "L1" and issue.repair_hint is None


def test_router_plans_directed_retake_with_the_hint_from_the_first_repeat() -> None:
    issue = issues_from_qa({}, _technical(), shot_id="shot_1", version_id="ver_1", shot_no=5)[0]
    plan = route([issue], fingerprint_counts={})
    assert (plan.level, plan.strategy) == ("L2", "retake_directed")
    assert plan.critique == [HINT] and plan.is_paid


def test_pack_prompt_appends_critique_as_must_fix_line(monkeypatch) -> None:
    monkeypatch.setattr(enqueue_prompt, "assert_segment_submission", lambda segment, *, source_text: None)
    shot = SimpleNamespace(storyboard_pack_segment={"prompt_text": "电影级预告片质感。镜头1：……约束：面部一致。\n"}, source_excerpt="原文")
    assert enqueue_prompt.storyboard_pack_prompt_text(shot) == "电影级预告片质感。镜头1：……约束：面部一致。\n"
    directed = enqueue_prompt.storyboard_pack_prompt_text(shot, critique=[HINT, " ", "手指五根"])
    assert directed == "电影级预告片质感。镜头1：……约束：面部一致。\n上一版必须改正：" + HINT + "；手指五根"


def test_gate_verdict_hint_names_the_seen_text() -> None:
    verdict = {"checked": True, "subtitle_overlay": True, "frames_checked": 10, "frames_reported": 10,
               "overlay_frames": [{"index": 2, "text_seen": "拜见师兄", "where": "画面底部"}]}
    merged = subtitle_overlay.technical_with_verdict({"passed": True, "issues": [], "evidence": {}}, {"subtitle_gate": verdict})
    hint = merged["issues"][0].repair_hint
    assert hint.startswith("台词只以声音呈现") and "拜见师兄" in hint


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db_mod.SCHEMA)
    for statement in db_mod.MIGRATIONS:
        try:
            conn.execute(statement)
        except sqlite3.OperationalError:
            pass
    return conn


def _insert_version(conn, version_id: str, *, status: str, passed: bool | None) -> None:
    technical = json.dumps({"passed": passed, "issues": []}) if passed is not None else None
    conn.execute(
        "INSERT INTO shot_versions (id, shot_id, version_no, status, technical_validation_json, prompt_text, idem_key, created_at)"
        " VALUES (?,?,?,?,?,'',?,1.0)",
        (version_id, "shot_1", int(version_id[-1]), status, technical, f"idem_{version_id}"),
    )


def test_resumed_run_does_not_treat_technically_rejected_shot_as_first_attempt() -> None:
    conn = _conn()
    assert coverage._technically_rejected(conn, "shot_1") is False
    _insert_version(conn, "ver_1", status="succeeded", passed=False)
    assert coverage._technically_rejected(conn, "shot_1") is True


def test_only_finished_rejections_count() -> None:
    conn = _conn()
    _insert_version(conn, "ver_1", status="waiting_human", passed=False)
    _insert_version(conn, "ver_2", status="succeeded", passed=True)
    _insert_version(conn, "ver_3", status="running", passed=None)
    assert coverage._technically_rejected(conn, "shot_1") is False
