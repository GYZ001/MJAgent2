"""``app.final_edit_enhance.plan_validate``：编排计划三类判据的代码核验。全部
判据从 ``EpisodeContext``/曲库/静默窗口这些真实数据推导，不写死镜号/角色名/
曲目 ID（CLAUDE.md 禁止黑白名单）。
"""
from __future__ import annotations

from app.final_edit_enhance import plan_validate
from app.final_edit_enhance.context import EpisodeContext, ShotContext
from app.final_edit_enhance.music_library import MusicLibrary, MusicTrack
from app.final_edit_enhance.plan_schema import MonologueLineDraft, MusicCueDraft, TeaserClipDraft


def _context(source_text: str = "他站在窗边，心想：我到底该不该相信他。夜色渐深。") -> EpisodeContext:
    shots = (
        ShotContext(shot_no=1, start_s=0.0, duration_s=15.0, prompt_text="他站在窗边"),
        ShotContext(shot_no=2, start_s=15.0, duration_s=15.0, prompt_text="夜色渐深"),
    )
    return EpisodeContext(shots=shots, total_duration_s=30.0, source_text=source_text, character_roster=("顾屿", "温念"))


def _library(tmp_path) -> MusicLibrary:
    audio = tmp_path / "t1.m4a"
    audio.write_bytes(b"fake")
    return MusicLibrary(tracks=(MusicTrack("t1", "曲一", ("甜",), 120.0, audio),), dropped=())


# ---------- music cues ----------

def test_validate_music_cues_accepts_known_shot_and_track(tmp_path) -> None:
    context = _context()
    library = _library(tmp_path)
    valid, dropped = plan_validate.validate_music_cues([MusicCueDraft(shot_no=1, track_id="t1")], context=context, library=library)
    assert [c.shot_no for c in valid] == [1]
    assert dropped == []


def test_validate_music_cues_rejects_unknown_shot_no(tmp_path) -> None:
    context = _context()
    library = _library(tmp_path)
    valid, dropped = plan_validate.validate_music_cues([MusicCueDraft(shot_no=99, track_id="t1")], context=context, library=library)
    assert valid == []
    assert "99" in dropped[0]["reason"]


def test_validate_music_cues_rejects_unknown_track_id(tmp_path) -> None:
    context = _context()
    library = _library(tmp_path)
    valid, dropped = plan_validate.validate_music_cues([MusicCueDraft(shot_no=1, track_id="ghost")], context=context, library=library)
    assert valid == []
    assert "ghost" in dropped[0]["reason"]


def test_validate_music_cues_drops_duplicate_shot_no(tmp_path) -> None:
    context = _context()
    library = _library(tmp_path)
    cues = [MusicCueDraft(shot_no=1, track_id="t1"), MusicCueDraft(shot_no=1, track_id="t1")]
    valid, dropped = plan_validate.validate_music_cues(cues, context=context, library=library)
    assert len(valid) == 1
    assert len(dropped) == 1


# ---------- teaser clips ----------

def test_validate_teaser_clips_accepts_in_range_clip() -> None:
    context = _context()
    clip = TeaserClipDraft(shot_no=1, start_s=1.0, end_s=3.0, reason="心动瞬间")
    valid, dropped = plan_validate.validate_teaser_clips([clip], context=context)
    assert len(valid) == 1
    assert dropped == []


def test_validate_teaser_clips_rejects_out_of_shot_bounds() -> None:
    context = _context()
    clip = TeaserClipDraft(shot_no=1, start_s=10.0, end_s=20.0, reason="x")  # 段只有 15 秒
    valid, dropped = plan_validate.validate_teaser_clips([clip], context=context)
    assert valid == []
    assert "超出" in dropped[0]["reason"]


def test_validate_teaser_clips_rejects_clip_length_out_of_range() -> None:
    context = _context()
    too_short = TeaserClipDraft(shot_no=1, start_s=0.0, end_s=0.5, reason="x")
    too_long = TeaserClipDraft(shot_no=2, start_s=0.0, end_s=6.0, reason="x")
    valid, dropped = plan_validate.validate_teaser_clips([too_short, too_long], context=context)
    assert valid == []
    assert len(dropped) == 2


def test_teaser_total_duration_sums_valid_clips() -> None:
    clips = [TeaserClipDraft(shot_no=1, start_s=0.0, end_s=2.0, reason=""), TeaserClipDraft(shot_no=2, start_s=0.0, end_s=3.0, reason="")]
    assert plan_validate.teaser_total_duration_s(clips) == 5.0


# ---------- monologue lines ----------

def test_validate_monologue_lines_accepts_exact_quote_in_roster_and_window() -> None:
    context = _context("他站在窗边，心想：我到底该不该相信他。夜色渐深。")
    windows = [(0.0, 30.0)]
    line = MonologueLineDraft(window_index=0, character_name="顾屿", text="我到底该不该相信他")
    valid, dropped = plan_validate.validate_monologue_lines([line], context=context, windows=windows)
    assert len(valid) == 1
    assert dropped == []


def test_validate_monologue_lines_rejects_text_not_verbatim_in_source() -> None:
    context = _context("他站在窗边，心想：我到底该不该相信他。")
    windows = [(0.0, 30.0)]
    line = MonologueLineDraft(window_index=0, character_name="顾屿", text="我已经决定不再相信任何人了")
    valid, dropped = plan_validate.validate_monologue_lines([line], context=context, windows=windows)
    assert valid == []
    assert "逐字子串" in dropped[0]["reason"]


def test_validate_monologue_lines_rejects_unknown_character() -> None:
    context = _context("我到底该不该相信他。")
    windows = [(0.0, 30.0)]
    line = MonologueLineDraft(window_index=0, character_name="路人甲", text="我到底该不该相信他")
    valid, dropped = plan_validate.validate_monologue_lines([line], context=context, windows=windows)
    assert valid == []
    assert "人物谱" in dropped[0]["reason"]


def test_validate_monologue_lines_rejects_window_index_out_of_range() -> None:
    context = _context("我到底该不该相信他。")
    line = MonologueLineDraft(window_index=3, character_name="顾屿", text="我到底该不该相信他")
    valid, dropped = plan_validate.validate_monologue_lines([line], context=context, windows=[(0.0, 30.0)])
    assert valid == []
    assert "越界" in dropped[0]["reason"]


def test_validate_monologue_lines_rejects_when_window_capacity_exhausted() -> None:
    # 窗口只有 3 秒，按 4.5 字/秒 + 首尾各 1 秒余量估算，一句 10 个字根本放不下。
    context = _context("我到底该不该相信他到底该不该相信")
    windows = [(0.0, 3.0)]
    line = MonologueLineDraft(window_index=0, character_name="顾屿", text="我到底该不该相信他到底该不该相信")
    valid, dropped = plan_validate.validate_monologue_lines([line], context=context, windows=windows)
    assert valid == []
    assert "容量不足" in dropped[0]["reason"]


# ---------- allocate_monologue_placements ----------

def test_allocate_monologue_placements_two_lines_in_same_window_do_not_overlap() -> None:
    """``validate_monologue_lines`` 只负责"要不要收"；已经收下的多句独白必须
    由 ``allocate_monologue_placements`` 重新分配出互不重叠的实际播放区间，
    不能都回退到整段候选窗口的原始边界。"""
    text_a, text_b = "我到底该不该相信他", "夜色渐深无人回应"
    windows = [(0.0, 20.0)]
    lines = [
        MonologueLineDraft(window_index=0, character_name="顾屿", text=text_a),
        MonologueLineDraft(window_index=0, character_name="顾屿", text=text_b),
    ]
    (start_a, end_a), (start_b, end_b) = plan_validate.allocate_monologue_placements(lines, windows)
    assert start_a == 0.0
    assert start_b == end_a
    assert end_b <= windows[0][1] + 1e-6


def test_allocate_monologue_placements_empty_input_returns_empty() -> None:
    assert plan_validate.allocate_monologue_placements([], [(0.0, 20.0)]) == []


def test_validate_monologue_lines_second_line_in_same_window_respects_remaining_capacity() -> None:
    """两句独白共用同一个静默窗口时，第二句只能用第一句消耗后剩下的容量——
    不是"每句各自对整窗判定"，否则会在真实混音时互相重叠。"""
    text_a, text_b = "我到底该不该相信他", "夜色渐深无人回应"
    context = _context(f"{text_a}。{text_b}。")
    # 窗口 6 秒：text_a（9 字）按 4.5 字/秒+2 秒余量 ≈ 4 秒，恰好放下；
    # text_b 再需要 ≈4 秒，剩余容量不足，应被丢弃。
    windows = [(0.0, 6.0)]
    lines = [
        MonologueLineDraft(window_index=0, character_name="顾屿", text=text_a),
        MonologueLineDraft(window_index=0, character_name="顾屿", text=text_b),
    ]
    valid, dropped = plan_validate.validate_monologue_lines(lines, context=context, windows=windows)
    assert [line.text for line in valid] == [text_a]
    assert len(dropped) == 1
    assert "容量不足" in dropped[0]["reason"]
