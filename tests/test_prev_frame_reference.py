"""上一段末帧作空间参考：同场戏串接判据与开关。"""
from __future__ import annotations

from app.video_plan import prev_frame_reference as pfr


def test_flag_defaults_off_and_reads_setting(monkeypatch) -> None:
    monkeypatch.setattr(pfr, "get_setting", lambda key: "")
    assert pfr.prev_frame_reference_enabled() is False
    monkeypatch.setattr(pfr, "get_setting", lambda key: "1" if key == pfr.SETTING_KEY else "")
    assert pfr.prev_frame_reference_enabled() is True
    monkeypatch.setattr(pfr, "get_setting", lambda key: "yes")
    assert pfr.prev_frame_reference_enabled() is False


def test_purpose_note_locks_layout_not_pose() -> None:
    assert "布局" in pfr.PREVIOUS_FRAME_PURPOSE_ZH and "不沿用" in pfr.PREVIOUS_FRAME_PURPOSE_ZH


def test_cut_times_drop_edges_and_merge_close_cuts() -> None:
    stderr = "n:1 pts_time:0.3 x\nn:2 pts_time:4.9 x\nn:3 pts_time:5.4 x\nn:4 pts_time:9.8 x\nn:5 pts_time:14.6 x"
    assert pfr.parse_showinfo_cut_times(stderr, duration_s=15.0) == [4.9, 9.8]


def test_sample_timestamps_uses_cut_midpoints_or_thirds_and_caps_at_three() -> None:
    assert pfr.sample_timestamps(15.0, [4.9, 9.8]) == [2.45, 7.35, 12.4]
    assert pfr.sample_timestamps(15.0, []) == [2.5, 7.5, 12.5]
    # 四镜时保留最长的三段并按时间排序
    assert pfr.sample_timestamps(15.0, [2.0, 8.0, 11.0]) == [5.0, 9.5, 13.0]
    assert pfr.sample_timestamps(0.0, []) == []


def test_previous_frame_assets_fall_back_to_single_tail_when_flag_off(monkeypatch, tmp_path) -> None:
    from types import SimpleNamespace

    from app.video_modes import reference_generate as rg

    monkeypatch.setattr(pfr, "get_setting", lambda key: "")
    sentinel = SimpleNamespace(type="previous_shot_frame")
    monkeypatch.setattr(rg, "previous_tail_reference_asset", lambda conn, prev, dest_dir: sentinel)
    assert rg.previous_frame_reference_assets(None, {"id": "s1"}, dest_dir=tmp_path) == [sentinel]


def test_previous_frame_assets_sample_one_frame_per_internal_shot_when_flag_on(monkeypatch, tmp_path) -> None:
    from app.video_modes import reference_generate as rg

    monkeypatch.setattr(pfr, "get_setting", lambda key: "1")
    monkeypatch.setattr(rg, "previous_tail_source_contract", lambda conn, prev: {"shot_id": "s1", "video_path": "/v.mp4"})
    frames = []
    for i in (1, 2, 3):
        f = tmp_path / f"0{i}.jpg"; f.write_bytes(b"jpg"); frames.append((i, f))
    monkeypatch.setattr(pfr, "sample_previous_segment_frames", lambda video, dest, sig: frames)
    assets = rg.previous_frame_reference_assets(None, {"id": "s1"}, dest_dir=tmp_path)
    assert [a.entity_name for a in assets] == ["第1镜", "第2镜", "第3镜"]
    assert all(a.type == "previous_shot_frame" and a.source == "previous_shot" for a in assets)


def _plan_item(shot_id: str, shot_no: int):
    from types import SimpleNamespace

    from app.video_plan.models import VideoGenerationMode

    return SimpleNamespace(
        shot_id=shot_id, shot_no=shot_no, mode=VideoGenerationMode.REFERENCE_IMAGE_MODE, planned_mode=None,
        video_input_intent=None, depends_on_shot_id=None, state_dependency="none", motion_dependency="none",
        required_assets=[], reason_codes=[],
        relations=SimpleNamespace(temporal="same_moment", spatial="same_space", edit="continuous_take"),
    )


def test_scene_boundary_strategy_chains_same_scene_shots_only_when_flag_on(monkeypatch) -> None:
    from app.video_plan import normalize

    shots = [_plan_item("s1", 1), _plan_item("s2", 2), _plan_item("s3", 3), _plan_item("s4", 4)]
    scenes = {"s1": "路边", "s2": "路边", "s3": "会议室", "s4": "会议室"}
    monkeypatch.setattr(normalize, "prev_frame_reference_enabled", lambda: True)
    normalize.apply_scene_boundary_strategy(shots, scene_identity_by_shot_id=scenes)
    assert [(s.depends_on_shot_id, s.state_dependency) for s in shots] == [
        (None, "none"), ("s1", "start_only"), (None, "none"), ("s3", "start_only"),
    ]
    assert "PREVIOUS_SEGMENT_FRAMES_REFERENCE" in shots[1].reason_codes
    assert all(s.mode.value == "REFERENCE_IMAGE_MODE" for s in shots)

    shots_off = [_plan_item("s1", 1), _plan_item("s2", 2)]
    monkeypatch.setattr(normalize, "prev_frame_reference_enabled", lambda: False)
    normalize.apply_scene_boundary_strategy(shots_off, scene_identity_by_shot_id=scenes)
    assert [s.depends_on_shot_id for s in shots_off] == [None, None]


def test_planned_previous_shot_id_reads_published_plan_only_when_flag_on(monkeypatch) -> None:
    from types import SimpleNamespace

    from app.video_plan import mode_attempt

    monkeypatch.setattr(mode_attempt, "get_shot_plan", lambda shot_id, conn=None: SimpleNamespace(depends_on_shot_id="s4"))
    monkeypatch.setattr(pfr, "get_setting", lambda key: "")
    assert pfr.planned_previous_shot_id(None, "s5") is None
    monkeypatch.setattr(pfr, "get_setting", lambda key: "1")
    assert pfr.planned_previous_shot_id(None, "s5") == "s4"
    monkeypatch.setattr(mode_attempt, "get_shot_plan", lambda shot_id, conn=None: None)
    assert pfr.planned_previous_shot_id(None, "s5") is None


def test_library_policy_accepts_previous_segment_frames_only_when_flag_on(monkeypatch, tmp_path) -> None:
    from app.video_modes import reference_prompt as rp

    frame = tmp_path / "01_previous_frame_x.jpg"; frame.write_bytes(b"jpg")
    portrait = tmp_path / "p.png"; portrait.write_bytes(b"png")
    meta = {
        "reference_input_policy_version": rp.REFERENCE_INPUT_POLICY_VERSION,
        "reference_images": [
            {"type": "character", "entity_type": "character", "source": "asset_library", "path": str(portrait), "selectedForSeedance": True},
            {"type": "previous_shot_frame", "source": "previous_shot", "path": str(frame), "selectedForSeedance": True},
        ],
    }
    monkeypatch.setattr(pfr, "get_setting", lambda key: "1")
    assert rp.reference_gallery_matches_library_policy(meta) is True
    monkeypatch.setattr(pfr, "get_setting", lambda key: "")
    assert rp.reference_gallery_matches_library_policy(meta) is False
