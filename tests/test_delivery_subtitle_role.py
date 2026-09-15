"""``app.delivery_package_build._collect_delivery_media_files`` 的字幕角色文件。

新文件而不是加进 ``tests/test_delivery.py``：该文件行数基线已顶格 765 行
（``app/FILE_CONVENTIONS.toml`` 的 ``[baseline.test_line_count]``），零行数
余量，见 CLAUDE.md「文件规范」棘轮只降不升。直接单测
``_collect_delivery_media_files``（而不是整条 ``build_delivery_package`` 流程）
是有意的收窄：交付包发布的门禁/lease/权威漂移已有 ``test_delivery.py`` 覆盖，
这里只聚焦字幕两个新角色的收集逻辑，照抄 ``final_edit_report`` 角色同样的
``_copy_if_present`` 写法（见 ``app/delivery_package_build.py``）。
"""
from __future__ import annotations

import hashlib

from app import config
from app import delivery_package_build as dpb


def _setup(tmp_path, monkeypatch, *, with_srt: bool, with_ass: bool):
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path)
    final_dir = tmp_path / "p" / "episodes" / "1" / "final"
    final_dir.mkdir(parents=True)
    (final_dir / "episode.mp4").write_bytes(b"fake-mp4")
    if with_srt:
        (final_dir / "episode.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\n你好\n\n", encoding="utf-8")
    if with_ass:
        (final_dir / "episode.ass").write_text("[Script Info]\n", encoding="utf-8")
    package_dir = tmp_path / "pkg"
    snapshots = package_dir / "snapshots"
    snapshots.mkdir(parents=True)
    return package_dir, snapshots, {"project_id": "p", "episode_no": 1}, {"videos": []}


def test_collects_subtitle_srt_and_ass_when_present(tmp_path, monkeypatch) -> None:
    package_dir, snapshots, ep, readiness = _setup(tmp_path, monkeypatch, with_srt=True, with_ass=True)

    files = dpb._collect_delivery_media_files(package_dir, snapshots, ep, readiness)

    srt_entry = next(f for f in files if f["role"] == "subtitle_srt")
    ass_entry = next(f for f in files if f["role"] == "subtitle_ass")
    assert srt_entry["path"] == "media/episode.srt"
    assert ass_entry["path"] == "media/episode.ass"
    srt_bytes = (package_dir / srt_entry["path"]).read_bytes()
    assert srt_entry["sha256"] == hashlib.sha256(srt_bytes).hexdigest()
    assert srt_entry["size_bytes"] == len(srt_bytes)


def test_no_subtitle_roles_when_sidecars_absent(tmp_path, monkeypatch) -> None:
    package_dir, snapshots, ep, readiness = _setup(tmp_path, monkeypatch, with_srt=False, with_ass=False)

    files = dpb._collect_delivery_media_files(package_dir, snapshots, ep, readiness)

    assert not any(f["role"] in ("subtitle_srt", "subtitle_ass") for f in files)
    assert any(f["role"] == "final_video" for f in files)
