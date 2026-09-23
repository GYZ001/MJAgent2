"""人工越过质量判定采纳后，交付检查与成片台权威校验必须放行这一镜
（2026-09-15 用户拍板「人工采纳最高优先级」：有可播放视频即可人工采纳，质量
判定只留痕）。反向用例锁住"真技术失败、无人采纳"仍然拦截，防止改动放宽过头。

留痕字段结构（app.domain.video_ops.adopt._persist_human_override 写入）：
``shot_versions.technical_validation_json`` 里新增 ``human_override`` 子对象
``{overridden_issue_codes, by, at, reason}``，不改写 ``passed``/``issues`` 原值。
``app.downstream_authority.human_override_marker`` 是读取这份标记的唯一判据。
"""
from __future__ import annotations

import json
import sqlite3
import time

import pytest

from app import db, delivery, downstream_authority
from app.domain import common as domain_common
from app.evidence import repository
from app.harness.types import Evaluation, EvidenceArtifact


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    for statement in db.MIGRATIONS:
        try:
            conn.execute(statement)
        except sqlite3.OperationalError:
            pass
    return conn


def _seed_episode(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT INTO projects(id,name,created_at) VALUES('p','P',0)")
    conn.execute(
        "INSERT INTO episodes(id,project_id,episode_no,title,status,created_at) "
        "VALUES('e','p',1,'E','confirmed',0)"
    )


_QUALITY_ISSUES = [{
    "code": "subtitle_overlay", "severity": "blocker", "category": "quality",
    "subject": "video", "message": "画面叠加了字幕",
}]


def _seed_shot_with_video(
    conn: sqlite3.Connection, tmp_path, *, shot_no: int, technical_passed: bool,
    human_override: dict | None,
) -> tuple[str, str, str]:
    """建一镜 + 已采纳版本 + 真实 Artifact + file 评估证据，evaluations 的
    status/hard_gate_passed 如实反映 technical_passed（与
    app.evidence.media.record_video_candidate 的真实行为一致：技术校验没过，
    自动评估就是 failed，不因为后面可能被人工越过而预先造假）。返回
    (shot_id, version_id, artifact_id)。
    """
    video_path = tmp_path / f"shot-{shot_no}.mp4"
    video_path.write_bytes(b"real-adopted-video")
    shot_id, version_id = f"s{shot_no}", f"v{shot_no}"
    conn.execute(
        "INSERT INTO shots(id,episode_id,shot_no,duration_s) VALUES(?,?,?,5)",
        (shot_id, "e", shot_no),
    )
    conn.execute(
        """INSERT INTO shot_versions(
               id,shot_id,version_no,prompt_text,idem_key,status,video_path,created_at
           ) VALUES(?,?,1,'prompt',?,'succeeded',?,0)""",
        (version_id, shot_id, f"key-{shot_no}", str(video_path)),
    )
    conn.execute("UPDATE shots SET adopted_version_id=? WHERE id=?", (version_id, shot_id))
    # 状态如实反映真实流转：技术校验通过 -> validated（record_video_candidate
    # 的真实产出）；技术校验没过但确有人工越过 -> approved（_adopt_version_core
    # 的 commit_artifact 会把 Artifact 提升到 approved）；技术校验没过又没有
    # 人工越过 -> candidate（从未走到过采用那一步）。
    artifact_status = "validated" if technical_passed else ("approved" if human_override else "candidate")
    artifact = repository.create_artifact(
        EvidenceArtifact(
            type="shot_video", scope_type="shot", scope_id=shot_id,
            content={"kind": "shot_video", "version_id": version_id},
            file_path=str(video_path), status=artifact_status, trust_level="T2",
            contract_version="video-2.0.0",
        ),
        conn=conn,
    )
    technical: dict = {"passed": technical_passed, "issues": [] if technical_passed else _QUALITY_ISSUES}
    if human_override is not None:
        technical["human_override"] = human_override
    # 用 create_evaluation 而不是 commit_artifact：commit_artifact 要求所有
    # 评估都 hard_gate_passed，技术校验没过时这里如实记一条 failed 的 file
    # 评估——与 app.evidence.media.record_video_candidate 的真实行为一致。
    repository.create_evaluation(artifact["id"], Evaluation(
        evaluator_type="file", evaluator_name="video_technical_validator",
        evaluator_version="1", status="passed" if technical_passed else "failed",
        hard_gate_passed=technical_passed, score=100 if technical_passed else 0,
    ), conn=conn)
    conn.execute(
        "UPDATE shot_versions SET artifact_id=?,technical_validation_json=? WHERE id=?",
        (artifact["id"], json.dumps(technical, ensure_ascii=False), version_id),
    )
    conn.commit()
    return shot_id, version_id, artifact["id"]


_VALID_OVERRIDE = {
    "overridden_issue_codes": ["subtitle_overlay"], "by": "lnuyasha",
    "at": 1758000000.0, "reason": "生成台预览时人工选定 v1",
}


def test_readiness_adopted_videos_check_passes_with_human_override(tmp_path, monkeypatch) -> None:
    """红→绿目标行为：人工越过质量判定采纳后，delivery_readiness 的
    adopted_videos 检查必须放行，且交付质量报告的 warnings 里如实列出
    "人工越过质量判定采纳"，带上原技术校验的问题码。"""
    conn = _conn()
    monkeypatch.setattr(repository, "get_conn", lambda: conn)
    monkeypatch.setattr(delivery, "get_conn", lambda: conn)
    monkeypatch.setattr(domain_common, "get_conn", lambda: conn)
    _seed_episode(conn)
    _seed_shot_with_video(
        conn, tmp_path, shot_no=1, technical_passed=False, human_override=_VALID_OVERRIDE,
    )

    readiness = delivery.delivery_readiness("e")

    adopted_videos_check = next(c for c in readiness["checks"] if c["key"] == "adopted_videos")
    assert adopted_videos_check["passed"] is True
    override_warnings = [w for w in readiness["warnings"] if w.get("code") == "HUMAN_OVERRIDE_QUALITY"]
    assert len(override_warnings) == 1
    assert override_warnings[0]["shot_no"] == 1
    assert "人工越过质量判定采纳" in override_warnings[0]["message"]
    assert "subtitle_overlay" in override_warnings[0]["message"]


def test_readiness_adopted_videos_check_still_blocks_real_failure_without_override(
    tmp_path, monkeypatch,
) -> None:
    """反向：技术校验真的没过、且没有人工越过标记时，adopted_videos 检查必须
    继续拦截——不能因为这次改动而放过真失败。"""
    conn = _conn()
    monkeypatch.setattr(repository, "get_conn", lambda: conn)
    monkeypatch.setattr(delivery, "get_conn", lambda: conn)
    monkeypatch.setattr(domain_common, "get_conn", lambda: conn)
    _seed_episode(conn)
    _seed_shot_with_video(conn, tmp_path, shot_no=1, technical_passed=False, human_override=None)

    readiness = delivery.delivery_readiness("e")

    adopted_videos_check = next(c for c in readiness["checks"] if c["key"] == "adopted_videos")
    assert adopted_videos_check["passed"] is False
    assert 1 in adopted_videos_check["evidence"]["missing_or_invalid"]
    assert not [w for w in readiness["warnings"] if w.get("code") == "HUMAN_OVERRIDE_QUALITY"]


def test_authority_accepts_valid_human_override(tmp_path, monkeypatch) -> None:
    """downstream_authority 的采纳权威校验（build_delivery_package 的严格路径
    与成片台的容错路径共用）必须同样放行人工越过——不能只改了 delivery.py 的
    展示层，真正决定"能不能交付"的权威判据没跟上。"""
    conn = _conn()
    monkeypatch.setattr(repository, "get_conn", lambda: conn)
    _seed_episode(conn)
    _seed_shot_with_video(
        conn, tmp_path, shot_no=1, technical_passed=False, human_override=_VALID_OVERRIDE,
    )

    manifest = downstream_authority.current_adopted_video_delivery_manifest("e", conn=conn)
    assert manifest["items"][0]["shot_no"] == 1

    partial = downstream_authority.current_partial_adopted_video_delivery_manifest("e", conn=conn)
    assert partial["skipped_shot_nos"] == []
    assert [item["shot_no"] for item in partial["items"]] == [1]


@pytest.mark.parametrize("broken_override", [
    {"overridden_issue_codes": [], "by": "lnuyasha"},  # 越过的 issue code 为空
    {"overridden_issue_codes": ["subtitle_overlay"], "by": ""},  # 操作人为空
    {"overridden_issue_codes": ["subtitle_overlay"]},  # 缺 by 字段
    "not-a-dict",  # 类型不对
])
def test_authority_rejects_malformed_override_marker(tmp_path, monkeypatch, broken_override) -> None:
    """反向：结构不完整的越过标记（半写坏数据）不能被当成有效越过——这是
    human_override_marker 的核心判据，必须逐个字段校验，不能只查键存在。"""
    conn = _conn()
    monkeypatch.setattr(repository, "get_conn", lambda: conn)
    _seed_episode(conn)
    _seed_shot_with_video(
        conn, tmp_path, shot_no=1, technical_passed=False, human_override=broken_override,
    )

    with pytest.raises(ValueError, match="镜 1 的视频技术门禁未通过"):
        downstream_authority.current_adopted_video_delivery_manifest("e", conn=conn)


def test_skip_reason_surfaces_provider_rejection_detail_for_never_adopted_shot(
    tmp_path, monkeypatch,
) -> None:
    """红→绿：镜头被供应商确定性拒收后从未被采纳，skip_reasons 必须带出
    jobs.reason_text 里的拒收原文与处理指引，而不是通用的"缺少已采纳的有效
    视频权威"——那句通用文案在成片台看不出该做什么。"""
    conn = _conn()
    monkeypatch.setattr(repository, "get_conn", lambda: conn)
    _seed_episode(conn)
    # 镜 2：始终有效的对照组——manifest 至少要有一镜成功，否则函数本身会因为
    # "本集当前没有任何镜头具备已采纳且通过技术校验的有效视频"整体失败，
    # 那是另一条独立分支，不是本用例要验证的目标。
    _seed_shot_with_video(conn, tmp_path, shot_no=2, technical_passed=True, human_override=None)
    conn.execute("INSERT INTO shots(id,episode_id,shot_no,duration_s) VALUES('s1','e',1,5)")
    rejection_text = (
        "视频供应商对本镜连续 3 次独立任务给出了完全相同的拒绝结果，判定为真实的模型拒绝。"
        "系统已停止对本镜的自动重试，并把本镜按「跳过」处理"
        "（VIDEO_PROVIDER_CONTENT_REJECTED · err-abc123）"
    )
    conn.execute(
        """INSERT INTO jobs(id,kind,shot_id,episode_id,status,reason_code,reason_text,
                             created_at,updated_at)
           VALUES('j1','video','s1','e','failed','VIDEO_PROVIDER_CONTENT_REJECTED',?,0,?)""",
        (rejection_text, time.time()),
    )
    conn.commit()

    partial = downstream_authority.current_partial_adopted_video_delivery_manifest("e", conn=conn)

    assert partial["skipped_shot_nos"] == [1]
    assert partial["skip_reasons"]["1"] == rejection_text


def test_skip_reason_keeps_specific_authority_message_when_shot_was_adopted(
    tmp_path, monkeypatch,
) -> None:
    """反向：镜头确实被采纳过、只是权威链另有问题（这里用最直接的一种——从未
    补上 artifact_id）时，即便这一镜历史上有过供应商拒收记录，skip_reasons
    也不能被那条旧记录覆盖——"已采纳但权威链失效"和"从未采纳过"是两种不同的
    故障，各自的具体原因都要如实展示，不能互相顶替。"""
    conn = _conn()
    monkeypatch.setattr(repository, "get_conn", lambda: conn)
    _seed_episode(conn)
    # 镜 2：始终有效的对照组，理由同上一个用例。
    _seed_shot_with_video(conn, tmp_path, shot_no=2, technical_passed=True, human_override=None)
    # 镜 1：先有一次旧的供应商拒收记录（同一镜后续又生成成功并被采纳）。
    conn.execute(
        """INSERT INTO jobs(id,kind,shot_id,episode_id,status,reason_code,reason_text,
                             created_at,updated_at)
           VALUES('j1','video','s1','e','failed','VIDEO_PROVIDER_CONTENT_REJECTED','旧的拒收记录',0,1)"""
    )
    video_path = tmp_path / "shot-1.mp4"
    video_path.write_bytes(b"adopted-but-no-artifact")
    conn.execute("INSERT INTO shots(id,episode_id,shot_no,duration_s) VALUES('s1','e',1,5)")
    conn.execute(
        """INSERT INTO shot_versions(
               id,shot_id,version_no,prompt_text,idem_key,status,video_path,created_at
           ) VALUES('v1','s1',1,'prompt','key','succeeded',?,0)""",
        (str(video_path),),
    )
    conn.execute("UPDATE shots SET adopted_version_id='v1' WHERE id='s1'")
    conn.commit()

    partial = downstream_authority.current_partial_adopted_video_delivery_manifest("e", conn=conn)

    assert partial["skipped_shot_nos"] == [1]
    assert partial["skip_reasons"]["1"] == "镜 1 缺少已采纳的有效视频权威"
