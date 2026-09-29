"""``app.final_edit_enhance.plan_validate``：编排计划三类判据的代码核验。全部
判据从 ``EpisodeContext``/曲库/静默窗口这些真实数据推导，不写死镜号/角色名/
曲目 ID（CLAUDE.md 禁止黑白名单）。
"""
from __future__ import annotations

from app.final_edit_enhance import plan_schema, plan_validate
from app.final_edit_enhance.context import DialogueLineContext, EpisodeContext, ShotContext
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


# ---------- music sections：换曲点当分段起点核验最短情绪段时长 ----------

def _section_context(n_shots: int = 9) -> EpisodeContext:
    shots = tuple(
        ShotContext(shot_no=i, start_s=(i - 1) * 15.0, duration_s=15.0, prompt_text=f"第{i}段")
        for i in range(1, n_shots + 1)
    )
    return EpisodeContext(shots=shots, total_duration_s=n_shots * 15.0, source_text="", character_roster=())


def test_validate_music_sections_keeps_cues_spaced_at_least_min_duration_apart() -> None:
    context = _section_context()
    cues = [MusicCueDraft(shot_no=1, track_id="t1"), MusicCueDraft(shot_no=4, track_id="t2")]  # 45s 间隔，刚好达标
    kept, dropped, first_gap = plan_validate.validate_music_sections(cues, context=context)
    assert [c.shot_no for c in kept] == [1, 4]
    assert dropped == []
    assert first_gap is None


def test_validate_music_sections_drops_cue_too_close_to_previous() -> None:
    context = _section_context()
    cues = [MusicCueDraft(shot_no=1, track_id="t1"), MusicCueDraft(shot_no=2, track_id="t2")]  # 只隔 15s
    kept, dropped, _first_gap = plan_validate.validate_music_sections(cues, context=context)
    assert [c.shot_no for c in kept] == [1]
    assert len(dropped) == 1
    assert "不足最短情绪段时长" in dropped[0]["reason"]


def test_validate_music_sections_does_not_require_min_length_for_trailing_section() -> None:
    """最后一个换曲点到全集结束这一段允许比最短时长短——收尾段可能就是全集
    本身较短，不因此被判定为「换太快」。"""
    context = _section_context(n_shots=2)  # 全集只有 30s，远小于 45s
    cues = [MusicCueDraft(shot_no=1, track_id="t1")]
    kept, dropped, _first_gap = plan_validate.validate_music_sections(cues, context=context)
    assert [c.shot_no for c in kept] == [1]
    assert dropped == []


def test_validate_music_sections_flags_first_cue_not_at_episode_start() -> None:
    context = _section_context()
    cues = [MusicCueDraft(shot_no=3, track_id="t1")]
    kept, dropped, first_gap = plan_validate.validate_music_sections(cues, context=context)
    assert [c.shot_no for c in kept] == [3]  # 不丢弃这条 cue 本身，只是提示片头有缺口
    assert dropped == []
    assert first_gap is not None
    assert "片头" in first_gap


def test_validate_music_sections_sorts_out_of_order_input_by_shot_no() -> None:
    context = _section_context()
    cues = [MusicCueDraft(shot_no=4, track_id="t2"), MusicCueDraft(shot_no=1, track_id="t1")]
    kept, dropped, _first_gap = plan_validate.validate_music_sections(cues, context=context)
    assert [c.shot_no for c in kept] == [1, 4]
    assert dropped == []


# ---------- teaser clips：模型只给 start_s，片长固定为 TEASER_CLIP_LENGTH_S ----------

def test_validate_teaser_clips_accepts_in_range_clip() -> None:
    context = _context()
    clip = TeaserClipDraft(shot_no=1, start_s=1.0, reason="心动瞬间")
    valid, dropped = plan_validate.validate_teaser_clips([clip], context=context)
    assert len(valid) == 1
    assert dropped == []


def test_validate_teaser_clips_rejects_negative_start() -> None:
    context = _context()
    clip = TeaserClipDraft(shot_no=1, start_s=-1.0, reason="x")
    valid, dropped = plan_validate.validate_teaser_clips([clip], context=context)
    assert valid == []
    assert "负" in dropped[0]["reason"]


def test_validate_teaser_clips_rejects_out_of_shot_bounds() -> None:
    context = _context()
    # 段只有 15 秒，start_s=13 + 固定片长 3 秒 = 16 秒，超出段时长。
    clip = TeaserClipDraft(shot_no=1, start_s=13.0, reason="x")
    valid, dropped = plan_validate.validate_teaser_clips([clip], context=context)
    assert valid == []
    assert "超出" in dropped[0]["reason"]


def test_teaser_clip_end_s_is_start_plus_fixed_length() -> None:
    clip = TeaserClipDraft(shot_no=1, start_s=2.0, reason="x")
    assert plan_validate.teaser_clip_end_s(clip) == 2.0 + plan_schema.TEASER_CLIP_LENGTH_S


def test_teaser_total_duration_is_clip_count_times_fixed_length() -> None:
    clips = [TeaserClipDraft(shot_no=1, start_s=0.0, reason=""), TeaserClipDraft(shot_no=2, start_s=0.0, reason="")]
    assert plan_validate.teaser_total_duration_s(clips) == 2 * plan_schema.TEASER_CLIP_LENGTH_S


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


# ---------- monologue content：不能是本集已经说出口的台词/旁白（内心独白） ----------

def _context_with_dialogue(source_text: str, *, delivered_lines: tuple[str, ...]) -> EpisodeContext:
    dialogue = tuple(
        DialogueLineContext(utterance_id=f"U{i:02d}", speaker="顾屿", text=text)
        for i, text in enumerate(delivered_lines, start=1)
    )
    shots = (ShotContext(shot_no=1, start_s=0.0, duration_s=15.0, prompt_text="开场", dialogue=dialogue),)
    return EpisodeContext(shots=shots, total_duration_s=15.0, source_text=source_text, character_roster=("顾屿",))


def test_validate_monologue_lines_rejects_line_already_delivered_as_dialogue() -> None:
    """证据（proj_ca86b15ab7d7 EP1）：模型提名的独白正文其实是本集台词
    （「我们六岁就说好了，长大要住在一起。」是顾屿已经说出口的台词），
    不是心里话——必须被拒。"""
    text = "我们六岁就说好了，长大要住在一起"
    context = _context_with_dialogue(f"他心想，{text}。", delivered_lines=(f"{text}。",))
    line = MonologueLineDraft(window_index=0, character_name="顾屿", text=text)
    valid, dropped = plan_validate.validate_monologue_lines([line], context=context, windows=[(0.0, 30.0)])
    assert valid == []
    assert "已经出现过" in dropped[0]["reason"]


def test_validate_monologue_lines_rejects_line_containing_delivered_dialogue() -> None:
    """互为子串的两个方向都要拦：独白正文比已播出台词更长，但完整包含了它。"""
    delivered = "你不是十二年前就搬去北方了吗"
    text = f"顾屿，{delivered}"
    context = _context_with_dialogue(f"温念心想：{text}。", delivered_lines=(f"{delivered}？",))
    line = MonologueLineDraft(window_index=0, character_name="顾屿", text=text)
    valid, dropped = plan_validate.validate_monologue_lines([line], context=context, windows=[(0.0, 30.0)])
    assert valid == []
    assert "已经出现过" in dropped[0]["reason"]


def test_validate_monologue_lines_accepts_inner_thought_not_delivered() -> None:
    """未在本集台词/旁白里出现过的内心独白正常通过。"""
    text = "我到底该不该相信他"
    context = _context_with_dialogue(f"他心想：{text}。", delivered_lines=("这是另一句完全不同的台词",))
    line = MonologueLineDraft(window_index=0, character_name="顾屿", text=text)
    valid, dropped = plan_validate.validate_monologue_lines([line], context=context, windows=[(0.0, 30.0)])
    assert len(valid) == 1
    assert dropped == []


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
