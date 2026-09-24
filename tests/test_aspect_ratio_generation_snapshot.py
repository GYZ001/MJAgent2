"""画幅可配置（2026-09-23）：入队快照 + 场景图尺寸 + 切换影响披露 + 生成台序列化。

覆盖单元 B（enqueue_persist.build_base_image_meta 把 resolve_aspect_ratio 的结果
写进版本 meta）、单元 D（场景图尺寸随画幅、定妆照/道具图不受影响）、单元 E（只读
影响披露接口）、单元 F（生成台版本列表带出 aspect_ratio）。
"""
from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from app import config, hiagent
from app import scenes
from app.db import get_conn, now
from app.domain.storyboard_ops.public_shot_versions import _public_shot_versions
from app.main import app
from app.media_exec.enqueue_persist import build_base_image_meta
from app.schemas import Bible, Character, Shot, World
from app.video_prompt_profiles import SEEDANCE_2_PROFILE
from app.video_modes.mode_selection import ShotVideoModeDecision
from tests.conftest import SessionTestClient


def _admin_client() -> SessionTestClient:
    return SessionTestClient(TestClient(app))


def _shot(**overrides) -> Shot:
    data = dict(
        shot_no=1, duration_s=5, shot_size="中景", camera_move="固定",
        scene_setting="夜，山门前", characters=["林风"],
        action_desc="林风抬手按住山门铜环。", first_frame_desc="林风站在山门前。",
        last_frame_desc="林风按住铜环。", source_excerpt="",
        state_in="林风站在山门前。", primary_action="林风抬手按住山门铜环。",
        state_out="林风按住铜环。", continuity_mode="same_scene_cut",
        characters_visible=["林风"], audio_cast=[],
    )
    data.update(overrides)
    return Shot(**data)


# ---------------------------------------------------------------------------
# 单元 B：入队 meta 快照
# ---------------------------------------------------------------------------

def test_build_base_image_meta_snapshots_aspect_ratio() -> None:
    decision = ShotVideoModeDecision(mode="REFERENCE_IMAGE_MODE", reason="测试", confidence=1.0)
    meta = build_base_image_meta(
        decision, _shot(), "prompt", False,
        chain_after_shot_id=None, chain_after_version_id=None, continuity_mode=None,
        prompt_prev_state_out=None, incoming_transition=None, outgoing_transition=None,
        auto_retake_count=0, supervisor_run_id=None, target_prompt_profile=SEEDANCE_2_PROFILE,
        target_video_provider="hiagent", target_video_model="", prompt_override=None,
        critique=None, previous_prompt_version=None, previous_prompt_fingerprint=None,
        previous_prompt_text=None, first_frame_source=None, boundary_source_shot_id=None,
        boundary_relation_edit=None, boundary_relation_action=None, boundary_relation_reason=None,
        boundary_start_state=None, aspect_ratio="16:9",
    )
    assert meta["aspect_ratio"] == "16:9"


# ---------------------------------------------------------------------------
# 单元 D：场景图尺寸随画幅；定妆照/道具图不受影响
# ---------------------------------------------------------------------------

def test_scene_ref_sizes_constant_maps_both_ratios() -> None:
    assert config.SCENE_REF_SIZES["9:16"] == config.REF_IMAGE_SIZE
    assert config.SCENE_REF_SIZES["16:9"] == "2560x1440"


def test_generate_scene_image_uses_916_scene_size(monkeypatch) -> None:
    captured: dict = {}

    async def fake_generate_image(_prompt, *, size, **_kw):
        captured["size"] = size
        return {"url": "https://example.test/a.jpg"}

    monkeypatch.setattr(hiagent, "generate_image", fake_generate_image)
    asyncio.run(scenes._generate_scene_image("场景描述", aspect_ratio="9:16"))
    assert captured["size"] == config.REF_IMAGE_SIZE


def test_generate_scene_image_uses_16_9_scene_size(monkeypatch) -> None:
    captured: dict = {}

    async def fake_generate_image(_prompt, *, size, **_kw):
        captured["size"] = size
        return {"url": "https://example.test/a.jpg"}

    monkeypatch.setattr(hiagent, "generate_image", fake_generate_image)
    asyncio.run(scenes._generate_scene_image("场景描述", aspect_ratio="16:9"))
    assert captured["size"] == "2560x1440"
    assert captured["size"] != config.REF_IMAGE_SIZE  # 与定妆照/道具图尺寸不同


# ---------------------------------------------------------------------------
# 单元 E：切换画幅影响披露（只读接口，冻结契约）
# ---------------------------------------------------------------------------

def _project_with_bible(project_id: str, *, aspect_ratio: str = "9:16") -> None:
    conn = get_conn()
    bible = Bible(
        characters=[Character(name="林风", role="主角", appearance_canonical="黑发青年")],
        world=World(visual_style_canonical="3D国漫"),
    )
    conn.execute(
        "INSERT INTO projects(id, name, created_at, bible_json, aspect_ratio) VALUES(?,?,?,?,?)",
        (project_id, "画幅影响测试", now(), bible.model_dump_json(), aspect_ratio),
    )
    conn.commit()


def _insert_episode_shot_version(
    conn, project_id: str, *, ep_id: str, shot_id: str, version_id: str,
    aspect_ratio_meta: str | None, episode_no: int = 1, image_inputs_override: str | None = None,
) -> None:
    import json as _json
    conn.execute(
        "INSERT INTO episodes(id, project_id, episode_no, target_duration_s, created_at) "
        "VALUES(?,?,?,?,?)",
        (ep_id, project_id, episode_no, 90, now()),
    )
    conn.execute(
        "INSERT INTO shots(id, episode_id, shot_no, duration_s) VALUES(?,?,?,?)",
        (shot_id, ep_id, 1, 5),
    )
    if image_inputs_override is not None:
        image_inputs_text = image_inputs_override
    else:
        meta = {} if aspect_ratio_meta is None else {"aspect_ratio": aspect_ratio_meta}
        image_inputs_text = _json.dumps(meta, ensure_ascii=False)
    conn.execute(
        "INSERT INTO shot_versions(id, shot_id, version_no, prompt_text, idem_key, status, "
        "image_inputs, created_at) VALUES(?,?,?,?,?,?,?,?)",
        (version_id, shot_id, 1, "p", "idem-" + version_id, "succeeded", image_inputs_text, now()),
    )
    conn.execute("UPDATE shots SET adopted_version_id=? WHERE id=?", (version_id, shot_id))
    conn.commit()


def test_aspect_ratio_impact_counts_mismatched_videos_and_legacy_defaults_to_916() -> None:
    project_id = "proj_ar_impact_1"
    _project_with_bible(project_id, aspect_ratio="9:16")
    conn = get_conn()
    # 版本 1：有画幅快照 9:16——切到 16:9 时应计入 mismatched
    _insert_episode_shot_version(
        conn, project_id, ep_id="ep_ar1", shot_id="shot_ar1", version_id="ver_ar1",
        aspect_ratio_meta="9:16",
    )
    # 版本 2：没有画幅快照（老任务）——按 9:16 计，切到 16:9 时同样 mismatched
    _insert_episode_shot_version(
        conn, project_id, ep_id="ep_ar2", shot_id="shot_ar2", version_id="ver_ar2",
        aspect_ratio_meta=None, episode_no=2,
    )
    client = _admin_client()
    resp = client.get(
        f"/api/projects/{project_id}/aspect-ratio-impact", params={"aspect_ratio": "16:9"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["project_id"] == project_id
    assert body["current_aspect_ratio"] == "9:16"
    assert body["target_aspect_ratio"] == "16:9"
    assert body["adopted_videos_total"] == 2
    assert body["adopted_videos_mismatched"] == 2
    assert body["scene_images_total"] == 0
    assert body["scene_images_mismatched"] is None


def test_aspect_ratio_impact_same_target_as_current_not_mismatched() -> None:
    project_id = "proj_ar_impact_2"
    _project_with_bible(project_id, aspect_ratio="9:16")
    conn = get_conn()
    _insert_episode_shot_version(
        conn, project_id, ep_id="ep_ar3", shot_id="shot_ar3", version_id="ver_ar3",
        aspect_ratio_meta="9:16",
    )
    client = _admin_client()
    resp = client.get(
        f"/api/projects/{project_id}/aspect-ratio-impact", params={"aspect_ratio": "9:16"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["adopted_videos_total"] == 1
    assert body["adopted_videos_mismatched"] == 0


def test_aspect_ratio_impact_rejects_invalid_target() -> None:
    project_id = "proj_ar_impact_3"
    _project_with_bible(project_id)
    client = _admin_client()
    resp = client.get(
        f"/api/projects/{project_id}/aspect-ratio-impact", params={"aspect_ratio": "4:3"},
    )
    assert resp.status_code == 422


def test_aspect_ratio_impact_project_not_found() -> None:
    client = _admin_client()
    resp = client.get(
        "/api/projects/proj_does_not_exist_ar_impact/aspect-ratio-impact",
        params={"aspect_ratio": "16:9"},
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 单元 F：生成台版本列表带出 aspect_ratio
# ---------------------------------------------------------------------------

def test_public_shot_versions_serializes_aspect_ratio_snapshot() -> None:
    project_id = "proj_ar_public_versions"
    _project_with_bible(project_id)
    conn = get_conn()
    _insert_episode_shot_version(
        conn, project_id, ep_id="ep_pv1", shot_id="shot_pv1", version_id="ver_pv1",
        aspect_ratio_meta="16:9",
    )
    versions = _public_shot_versions(conn, "shot_pv1", include_inputs=True)
    assert len(versions) == 1
    assert versions[0]["aspect_ratio"] == "16:9"


def test_public_shot_versions_defaults_legacy_version_to_916() -> None:
    project_id = "proj_ar_public_versions_legacy"
    _project_with_bible(project_id)
    conn = get_conn()
    _insert_episode_shot_version(
        conn, project_id, ep_id="ep_pv2", shot_id="shot_pv2", version_id="ver_pv2",
        aspect_ratio_meta=None,
    )
    versions = _public_shot_versions(conn, "shot_pv2", include_inputs=True)
    assert versions[0]["aspect_ratio"] == "9:16"


def test_public_shot_versions_lightweight_query_still_reads_real_snapshot() -> None:
    """episode_detail.py 的轻量查询（include_inputs=False，SQL 里 NULL AS
    image_inputs）不能因为不带完整 meta 就把 16:9 版本误标成 9:16。"""
    project_id = "proj_ar_public_versions_light"
    _project_with_bible(project_id)
    conn = get_conn()
    _insert_episode_shot_version(
        conn, project_id, ep_id="ep_pv3", shot_id="shot_pv3", version_id="ver_pv3",
        aspect_ratio_meta="16:9",
    )
    versions = _public_shot_versions(conn, "shot_pv3", include_inputs=False)
    assert versions[0]["aspect_ratio"] == "16:9"
    assert versions[0]["image_inputs"] is None or versions[0].get("prompt_text") == ""


def test_public_shot_versions_size_omitted_image_inputs_falls_back_to_916() -> None:
    """image_inputs 超 _MAX_PUBLIC_IMAGE_INPUT_CHARS 时，aspect_ratio_snapshot
    这一列的提取同样受同一尺寸阈值约束（不扫描超长历史 blob，与既有
    image_inputs_omitted 同一护栏），按 9:16 兜底而不是逐字解析整段超长文本；
    图片省略标记本身应保持 True，行为与画幅无关部分不受影响。"""
    project_id = "proj_ar_public_versions_omitted"
    _project_with_bible(project_id)
    conn = get_conn()
    oversized_padding = "x" * 1_000_001
    oversized_meta = (
        '{"aspect_ratio":"16:9","padding":"' + oversized_padding + '"}'
    )
    _insert_episode_shot_version(
        conn, project_id, ep_id="ep_pv4", shot_id="shot_pv4", version_id="ver_pv4",
        aspect_ratio_meta=None, image_inputs_override=oversized_meta,
    )
    versions = _public_shot_versions(conn, "shot_pv4", include_inputs=True)
    assert versions[0]["image_inputs"]["omitted_for_size"] is True
    assert versions[0]["aspect_ratio"] == "9:16"


def test_public_shot_versions_malformed_image_inputs_does_not_crash() -> None:
    """畸形 image_inputs 不能让整条查询报错；轻量查询路径本就不对 image_inputs
    做 Python 侧 json.loads，只有新增的 SQL 列会摸到这段畸形文本，靠
    json_valid 兜底成 NULL，画幅按 9:16 兜底。"""
    project_id = "proj_ar_public_versions_malformed"
    _project_with_bible(project_id)
    conn = get_conn()
    _insert_episode_shot_version(
        conn, project_id, ep_id="ep_pv5", shot_id="shot_pv5", version_id="ver_pv5",
        aspect_ratio_meta=None, image_inputs_override="not a valid json {{{",
    )
    versions = _public_shot_versions(conn, "shot_pv5", include_inputs=False)
    assert versions[0]["aspect_ratio"] == "9:16"
