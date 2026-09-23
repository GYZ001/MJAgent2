"""合成前自动代采的镜头必须能被成片台看见（2026-09-23 AI 批量成片对标差距
分析第 3 条）：`_auto_adopt_playable_candidates_before_mix` 算出的
`auto_adopted_shot_nos` 此前在唯一的真实调用点（`concatenate_episode` 内部）
被丢弃——本文件红→绿验证它现在会进 `result`/`final_edit.timeline`。

判据实现在 `app/media_exec/concat_auto_adopt.py`：从
`shot_versions.adoption_reason` 是否带自动采纳的落库文案反查（覆盖
`concatenate_episode` 直接调用与命令总线预调用两条路径，见该模块 docstring），
不依赖调用点是否把返回值接住——所以这里既测直接调用路径，也单独测"人工采纳
不会被误标成自动采纳"的反向断言。
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from app import artifacts, worker
from tests.conftest import patch_api_everywhere, patch_worker_everywhere
from tests.test_episode_partial_concat import _database, _probe_result, _version


def _wire_lightweight_authority(monkeypatch, conn) -> None:
    """本文件只测拼接机制里"自动采纳是否可见"，不测
    downstream_authority 的强校验链路（那条链路有 tests/test_delivery_human_
    override.py 与 tests/test_episode_partial_concat.py 的 CON-409 用例专门
    覆盖）——直通 mock 与 test_episode_partial_concat.py 的
    `_published_authority_for_concat_mechanics` 同构，但那是别的模块文件里的
    autouse fixture，不会跨文件自动生效，这里按需要显式接一份。"""
    from app import downstream_authority

    monkeypatch.setattr(
        downstream_authority,
        "verify_current_storyboard_release_authority",
        lambda episode_id, conn=None: {
            "published_storyboard_artifact_id": f"storyboard:{episode_id}",
            "release_qualification_hash": "release-current",
        },
    )

    def video_manifest(episode_id, conn=None):
        rows = (conn or worker.get_conn()).execute(
            """SELECT s.id,s.shot_no,s.adopted_version_id,v.playback_rate,v.video_path
                 FROM shots s LEFT JOIN shot_versions v ON v.id=s.adopted_version_id
                WHERE s.episode_id=? ORDER BY s.shot_no""",
            (episode_id,),
        ).fetchall()
        items = [
            {
                "shot_id": row["id"], "shot_no": row["shot_no"],
                "adopted_version_id": row["adopted_version_id"],
                "playback_rate": float(row["playback_rate"] or 1), "video_path": row["video_path"],
            }
            for row in rows
        ]
        return {"manifest_hash": json.dumps(items, sort_keys=True), "items": items}

    monkeypatch.setattr(downstream_authority, "current_adopted_video_delivery_manifest", video_manifest)
    monkeypatch.setattr(downstream_authority, "current_partial_adopted_video_delivery_manifest", video_manifest)


def test_concat_result_reports_auto_adopted_shot_nos_in_edit_report(tmp_path, monkeypatch) -> None:
    """红→绿：合成前自动代采的镜头必须出现在 result 与 final_edit.timeline 的
    auto_adopted_shot_nos 里；人工已采纳的镜头不能被误标成自动采纳。"""
    from app.evidence import media as media_evidence
    from app.evidence import repository

    conn = _database((1, 2))
    _wire_lightweight_authority(monkeypatch, conn)
    project_root = tmp_path / "projects"
    shot_dir = project_root / "p" / "episodes" / "1" / "shots"
    shot_dir.mkdir(parents=True)
    ftyp_bytes = b"\x00\x00\x00\x18ftypmp42" + b"x" * 64

    # 镜 1：人工已经采纳过（对照组，不该被算进自动采纳）。
    human_path = shot_dir / "shot-1.mp4"
    human_path.write_bytes(ftyp_bytes)
    _version(conn, shot_no=1, path=human_path, adopted=True)

    # 镜 2：只有候选、没人采纳——合成前会被自动代采。
    auto_path = shot_dir / "shot-2.mp4"
    auto_path.write_bytes(ftyp_bytes)
    _version(conn, shot_no=2, path=auto_path, adopted=False)
    conn.commit()

    patch_worker_everywhere(monkeypatch, "get_conn", lambda: conn)
    monkeypatch.setattr(artifacts, "get_conn", lambda: conn)
    monkeypatch.setattr(media_evidence, "get_conn", lambda: conn)
    monkeypatch.setattr(repository, "get_conn", lambda: conn)
    patch_api_everywhere(monkeypatch, "get_conn", lambda: conn)
    patch_api_everywhere(monkeypatch, "_review_assert_shot_positive", lambda *a, **k: {})
    monkeypatch.setattr(worker.config, "PROJECTS_DIR", project_root)
    monkeypatch.setattr(worker.shutil, "which", lambda _name: "/usr/bin/ffmpeg")

    def successful_run(command, **_kwargs):
        if command[0] == "ffprobe":
            duration_s = 2 * 5.0 if Path(command[-1]).name == "concat.mp4" else 5.0
            return _probe_result(duration_s)
        if command[-1] == "-":
            return SimpleNamespace(stdout=b"", stderr=b"")
        Path(command[-1]).write_bytes(b"generated-video")
        return SimpleNamespace(stdout="", stderr=b"")

    monkeypatch.setattr(worker.subprocess, "run", successful_run)

    result = worker.concatenate_episode("e")

    assert result["auto_adopted_shot_nos"] == [2]
    assert result["final_edit"]["timeline"]["auto_adopted_shot_nos"] == [2]
    assert 1 not in result["auto_adopted_shot_nos"]


def test_no_auto_adoption_reports_empty_list(tmp_path, monkeypatch) -> None:
    """反向：全部镜头都已由人工采纳时，auto_adopted_shot_nos 必须是空列表，
    不能因为字段一律存在就被误判"有自动采纳发生"。"""
    conn = _database((1,))
    _wire_lightweight_authority(monkeypatch, conn)
    project_root = tmp_path / "projects"
    shot_dir = project_root / "p" / "episodes" / "1" / "shots"
    shot_dir.mkdir(parents=True)
    human_path = shot_dir / "shot-1.mp4"
    human_path.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"x" * 64)
    _version(conn, shot_no=1, path=human_path, adopted=True)
    conn.commit()

    patch_worker_everywhere(monkeypatch, "get_conn", lambda: conn)
    monkeypatch.setattr(artifacts, "get_conn", lambda: conn)
    monkeypatch.setattr(worker.config, "PROJECTS_DIR", project_root)
    monkeypatch.setattr(worker.shutil, "which", lambda _name: "/usr/bin/ffmpeg")

    def successful_run(command, **_kwargs):
        if command[0] == "ffprobe":
            return _probe_result(5.0)
        if command[-1] == "-":
            return SimpleNamespace(stdout=b"", stderr=b"")
        Path(command[-1]).write_bytes(b"generated-video")
        return SimpleNamespace(stdout="", stderr=b"")

    monkeypatch.setattr(worker.subprocess, "run", successful_run)

    result = worker.concatenate_episode("e")

    assert result["auto_adopted_shot_nos"] == []
    assert result["final_edit"]["timeline"]["auto_adopted_shot_nos"] == []
