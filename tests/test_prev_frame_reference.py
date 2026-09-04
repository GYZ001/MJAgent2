"""上一段末帧作空间参考：同场戏串接判据与开关。"""
from __future__ import annotations

from types import SimpleNamespace

from app.video_plan import prev_frame_reference as pfr


def _row(shot_id: str, shot_no: int, scene: str) -> dict:
    return {"id": shot_id, "shot_no": shot_no, "scene_name": scene}


def test_consecutive_same_scene_shots_chain_and_scene_change_breaks_the_chain() -> None:
    rows = [_row("s1", 1, "老小区路边"), _row("s2", 2, "老小区路边"), _row("s3", 3, "出租屋"),
            _row("s4", 4, "会议室"), _row("s5", 5, "会议室"), _row("s6", 6, "会议室")]
    assert pfr.scene_chain_dependencies(rows) == {"s2": "s1", "s5": "s4", "s6": "s5"}


def test_empty_scene_name_never_chains_and_order_follows_shot_no() -> None:
    rows = [_row("b", 2, ""), _row("a", 1, ""), _row("c", 3, "会议室"), _row("d", 4, "会议室")]
    assert pfr.scene_chain_dependencies(rows) == {"d": "c"}
    rows_obj = [SimpleNamespace(id="x", shot_no=1, scene_name="A"), SimpleNamespace(id="y", shot_no=2, scene_name="A")]
    assert pfr.scene_chain_dependencies(rows_obj) == {"y": "x"}


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
