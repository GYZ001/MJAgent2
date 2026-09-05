"""合片发布必须全程持有 BEGIN IMMEDIATE 写锁。

复核链路上任何在调用方连接上隐式提交的助手都会把锁放掉：租约 upsert 被提交、
另一发布者接着 upsert 成自己的 owner，先到者最后的 ``owner=?`` CAS 就会失败
（第 24 集实测「合片 publish owner CAS 冲突」）。修法有两半：助手不再隐式提交
（见 test_ensure_tables_no_implicit_commit），发布路径复核完还要确认事务仍在——
锁没了就在动文件之前失败，而不是带着假锁去覆盖成片。
"""
from __future__ import annotations

import sqlite3

import pytest

from app import db, downstream_authority, worker
from app.media_exec.concat import ConcatOperationConflict
from tests.conftest import patch_worker_everywhere


def _memory_database() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    conn.execute("INSERT INTO projects(id,name,created_at) VALUES('p','P',0)")
    conn.execute(
        """INSERT INTO episodes(id,project_id,episode_no,title,status,created_at)
           VALUES('e','p',1,'E','confirmed',0)"""
    )
    conn.commit()
    return conn


_RELEASE = {
    "published_storyboard_artifact_id": "storyboard-1",
    "release_qualification_hash": "release-hash-1",
}
_MANIFEST = {"manifest_hash": "video-manifest-1", "items": []}


def _publish(conn, tmp_path, monkeypatch, verify):
    projects = tmp_path / "projects"
    final_path = projects / "p" / "episodes" / "1" / "final" / "episode.mp4"
    final_path.parent.mkdir(parents=True)
    candidate = tmp_path / "candidate.mp4"
    candidate.write_bytes(b"direct-final-video")
    patch_worker_everywhere(monkeypatch, "get_conn", lambda: conn)
    monkeypatch.setattr(worker.config, "PROJECTS_DIR", projects)
    monkeypatch.setattr(downstream_authority, "verify_current_storyboard_release_authority", verify)
    monkeypatch.setattr(
        downstream_authority,
        "current_partial_adopted_video_delivery_manifest",
        lambda episode_id, conn=None: _MANIFEST,
    )
    result: dict = {}
    worker._publish_concat_output(
        conn,
        episode_id="e",
        candidate_path=candidate,
        final_path=final_path,
        report={"mode": "draft_concat"},
        result=result,
        release_authority=_RELEASE,
        video_delivery_manifest=_MANIFEST,
    )
    return final_path, result


def test_direct_publish_refuses_when_verification_commits_callers_transaction(
    tmp_path, monkeypatch,
) -> None:
    conn = _memory_database()

    def verify_and_commit(episode_id, conn=None):
        # 模拟复核链路里的隐式提交（完成凭证建表助手曾经就这么做）。
        conn.commit()
        return _RELEASE

    with pytest.raises(ConcatOperationConflict, match="写锁丢失"):
        _publish(conn, tmp_path, monkeypatch, verify_and_commit)
    final_path = tmp_path / "projects" / "p" / "episodes" / "1" / "final" / "episode.mp4"
    assert not final_path.exists(), "锁已丢失还去覆盖成片"
    assert not conn.in_transaction
    lease = conn.execute(
        "SELECT status FROM episode_video_publish_leases WHERE episode_id='e'"
    ).fetchone()
    assert lease is None or lease["status"] != "published"


def test_direct_publish_succeeds_when_transaction_survives_verification(
    tmp_path, monkeypatch,
) -> None:
    conn = _memory_database()

    def verify_in_lock(episode_id, conn=None):
        assert conn.in_transaction
        return _RELEASE

    final_path, result = _publish(conn, tmp_path, monkeypatch, verify_in_lock)
    assert final_path.read_bytes() == b"direct-final-video"
    assert result["final_video_sha256"]
    lease = conn.execute(
        "SELECT status, video_manifest_hash FROM episode_video_publish_leases WHERE episode_id='e'"
    ).fetchone()
    assert lease["status"] == "published"
    assert lease["video_manifest_hash"] == _MANIFEST["manifest_hash"]
    assert not conn.in_transaction
