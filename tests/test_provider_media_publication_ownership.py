"""``ProviderMediaPublicationService.publish()`` 的项目归属校验证伪测试。

背景（2026-09-12 P0 后续修复）：``POST /api/provider-media-publications`` 的
豁免理由文案写着「仅发布项目自有媒体」，但代码里没有这回事——``local_path`` 只
校验落在 ``PROJECTS_DIR`` 之下（任意项目都算数），从未校验它是否落在
``source_revision_id`` 实际归属的那一个项目目录内。这意味着调用方可以拿项目 A
的 ``source_revision_id``，配上项目 B 的 ``local_path``，把项目 B 的私有媒体
发布成一枚公网可访问的签名 URL。

修法（见 ``app/video_plan/publication_service.py``）：
``_resolve_owning_project_id`` 从 ``source_revision_id``（``shot_versions.id``）
走 shot/version → episode → project 既有解析链得到真实归属；
``_require_path_within_owning_project`` 校验 ``local_path`` 必须落在这一个项目
目录内，``Path.resolve()`` 之后仍要能 ``relative_to`` 该项目根，``../`` 穿越与
跨项目引用都在这里被拒绝（fail closed，查不到就拒绝，不是查不到就放行）。

四条证伪测试对应派单里明确要求的四个场景：他人项目 local_path 拒绝、``../``
穿越拒绝、``source_revision_id`` 解析不出拒绝、本项目正常路径放行。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app import config
from app.db import get_conn, now, set_setting
from app.video_plan.publication_service import ProviderMediaPublicationService


def _seed_project_with_shot_version(
    conn, *, project_id: str, episode_id: str, shot_id: str, version_id: str,
) -> None:
    """落一条最小但完整的 project -> episode -> shot -> shot_version 链，供
    ``_resolve_owning_project_id`` 的 join 查询命中。"""
    t = now()
    conn.execute(
        "INSERT INTO projects(id, name, status, created_at) VALUES(?,?, 'created', ?)",
        (project_id, f"proj-{project_id}", t),
    )
    conn.execute(
        """INSERT INTO episodes(id, project_id, episode_no, title, status, created_at)
           VALUES(?,?,1,'ep1','confirmed',?)""",
        (episode_id, project_id, t),
    )
    conn.execute(
        """INSERT INTO shots(id, episode_id, shot_no, duration_s, action_desc)
           VALUES(?,?,1,5,'test shot')""",
        (shot_id, episode_id),
    )
    conn.execute(
        """INSERT INTO shot_versions(
               id, shot_id, version_no, prompt_text, idem_key, status,
               video_path, created_at
           ) VALUES(?,?,1,'p','idem-1','succeeded',NULL,?)""",
        (version_id, shot_id, t),
    )
    conn.commit()


@pytest.fixture()
def project_root(monkeypatch, tmp_path) -> Path:
    root = tmp_path / "projects"
    root.mkdir()
    monkeypatch.setattr(config, "PROJECTS_DIR", root)
    return root


async def test_publish_rejects_local_path_from_another_project(project_root: Path) -> None:
    """他人项目的 local_path → 拒绝：source_revision_id 归属项目 A，local_path
    却指向项目 B 磁盘上的真实文件——修复前会被当成项目 A 自有媒体公开发布。"""
    conn = get_conn()
    _seed_project_with_shot_version(
        conn, project_id="proj_a", episode_id="ep_a", shot_id="shot_a", version_id="ver_a",
    )
    project_b_dir = project_root / "proj_b" / "episodes" / "1" / "shots" / "1"
    project_b_dir.mkdir(parents=True)
    other_project_file = project_b_dir / "video.mp4"
    other_project_file.write_bytes(b"other-project-video-bytes")

    set_setting("provider_media_public_base_url", "https://cdn.example.com")

    with pytest.raises(ValueError, match="项目"):
        await ProviderMediaPublicationService().publish(
            source_revision_id="ver_a",
            local_path=str(other_project_file),
        )


async def test_publish_rejects_path_traversal(project_root: Path) -> None:
    """``../`` 穿越 → 拒绝：local_path 用 .. 试图逃出 source_revision_id 所属
    项目目录，指向 PROJECTS_DIR 之外的任意文件。"""
    conn = get_conn()
    _seed_project_with_shot_version(
        conn, project_id="proj_a", episode_id="ep_a", shot_id="shot_a", version_id="ver_a",
    )
    project_a_dir = project_root / "proj_a"
    project_a_dir.mkdir(parents=True, exist_ok=True)
    outside_file = project_root.parent / "outside.mp4"
    outside_file.write_bytes(b"escaped-bytes")
    traversal_path = project_a_dir / ".." / ".." / "outside.mp4"

    set_setting("provider_media_public_base_url", "https://cdn.example.com")

    with pytest.raises(ValueError, match="项目"):
        await ProviderMediaPublicationService().publish(
            source_revision_id="ver_a",
            local_path=str(traversal_path),
        )


async def test_publish_rejects_unresolvable_source_revision_id(project_root: Path) -> None:
    """归属解析不出 → 拒绝：source_revision_id 在 shot_versions 里查不到任何一
    行，无论 local_path 本身是否合法，都必须 fail closed 而不是放行。"""
    project_a_dir = project_root / "proj_a"
    project_a_dir.mkdir(parents=True)
    media_file = project_a_dir / "video.mp4"
    media_file.write_bytes(b"video-bytes")

    set_setting("provider_media_public_base_url", "https://cdn.example.com")

    with pytest.raises(ValueError, match="source_revision_id"):
        await ProviderMediaPublicationService().publish(
            source_revision_id="ver_does_not_exist",
            local_path=str(media_file),
        )


async def test_publish_allows_matching_project_local_path(
    monkeypatch, project_root: Path,
) -> None:
    """本项目正常路径 → 放行：source_revision_id 与 local_path 同属一个项目，
    校验通过后应继续走到发布逻辑，返回真实的 published_url。"""
    conn = get_conn()
    _seed_project_with_shot_version(
        conn, project_id="proj_a", episode_id="ep_a", shot_id="shot_a", version_id="ver_a",
    )
    project_a_dir = project_root / "proj_a" / "episodes" / "1" / "shots" / "1"
    project_a_dir.mkdir(parents=True)
    media_file = project_a_dir / "video.mp4"
    media_file.write_bytes(b"video-bytes")

    set_setting("provider_media_public_base_url", "https://cdn.example.com")

    async def _fake_check_accessible(_url: str) -> None:
        return None

    monkeypatch.setattr(
        ProviderMediaPublicationService, "_check_accessible", staticmethod(_fake_check_accessible),
    )

    result = await ProviderMediaPublicationService().publish(
        source_revision_id="ver_a",
        local_path=str(media_file),
    )
    assert result["published_url"] == (
        "https://cdn.example.com/proj_a/episodes/1/shots/1/video.mp4"
    )
    assert result["source_revision_id"] == "ver_a"
