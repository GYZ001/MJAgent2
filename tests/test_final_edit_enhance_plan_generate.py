"""``app.final_edit_enhance.plan_generate``：指纹确定性、"打回重试一次、最后
仍不满足就放行"（不整体失败）的核验节流器，以及模型草稿 -> 已核验计划的解析。
不打真实模型调用——``chat_structured`` 全部 monkeypatch。
"""
from __future__ import annotations

from app.final_edit_enhance import plan_generate, plan_validate
from app.final_edit_enhance.context import EpisodeContext, ShotContext
from app.final_edit_enhance.music_library import MusicLibrary, MusicTrack
from app.final_edit_enhance.plan_schema import EnhancementPlanDraft, MonologueLineDraft, MusicCueDraft, TeaserClipDraft
from app.harness import model_gateway


def _context() -> EpisodeContext:
    shots = (ShotContext(shot_no=1, start_s=0.0, duration_s=15.0, prompt_text="开场"),)
    return EpisodeContext(shots=shots, total_duration_s=15.0, source_text="他心想：我不会认输。", character_roster=("顾屿",))


def _library(tmp_path) -> MusicLibrary:
    audio = tmp_path / "t1.m4a"
    audio.write_bytes(b"fake")
    return MusicLibrary(tracks=(MusicTrack("t1", "曲一", (), 100.0, audio),), dropped=())


# ---------- plan_fingerprint ----------

def test_plan_fingerprint_deterministic_for_same_input(tmp_path) -> None:
    context = _context()
    library = _library(tmp_path)
    fp1 = plan_generate.plan_fingerprint(manifest_hash="m1", context=context, library=library, switches=(True, False, False))
    fp2 = plan_generate.plan_fingerprint(manifest_hash="m1", context=context, library=library, switches=(True, False, False))
    assert fp1 == fp2


def test_plan_fingerprint_changes_with_switches(tmp_path) -> None:
    context = _context()
    library = _library(tmp_path)
    fp1 = plan_generate.plan_fingerprint(manifest_hash="m1", context=context, library=library, switches=(True, False, False))
    fp2 = plan_generate.plan_fingerprint(manifest_hash="m1", context=context, library=library, switches=(True, True, False))
    assert fp1 != fp2


def test_plan_fingerprint_changes_with_manifest_hash(tmp_path) -> None:
    context = _context()
    library = _library(tmp_path)
    fp1 = plan_generate.plan_fingerprint(manifest_hash="m1", context=context, library=library, switches=(True, False, False))
    fp2 = plan_generate.plan_fingerprint(manifest_hash="m2", context=context, library=library, switches=(True, False, False))
    assert fp1 != fp2


# ---------- _amnesty_validate：打回重试一次，最后一次放行由调用方自行过滤 ----------

def test_amnesty_validate_reports_errors_on_first_attempt_then_amnesties_second() -> None:
    calls = {"n": 0}

    def always_invalid(_draft):
        calls["n"] += 1
        return ["坏了"]

    validate = plan_generate._amnesty_validate(always_invalid)
    assert validate(object()) == ["坏了"]  # 第一次：真实报错，触发重试
    assert validate(object()) == []        # 第二次（重试后）：预算已耗尽，放行
    assert calls["n"] == 2


# ---------- _semantic_errors：数量目标区间触发重试（不是硬性丢弃）----------

def test_semantic_errors_flags_teaser_clip_count_out_of_range() -> None:
    context = _context()
    draft = EnhancementPlanDraft(teaser_clips=[
        TeaserClipDraft(shot_no=1, start_s=0.0, end_s=3.0, reason="a"),
        TeaserClipDraft(shot_no=1, start_s=4.0, end_s=7.0, reason="b"),
    ])  # 只有 2 段——低于「3-5 段」目标区间
    errors = plan_generate._semantic_errors(
        draft, context=context, library=None, windows=[], switches=(False, False, False),
    )
    assert any("目标区间 3-5 段" in e for e in errors)


def test_semantic_errors_flags_monologue_line_count_out_of_range() -> None:
    context = EpisodeContext(
        shots=(ShotContext(shot_no=1, start_s=0.0, duration_s=15.0, prompt_text="开场"),),
        total_duration_s=15.0, source_text="我不会认输。", character_roster=("顾屿",),
    )
    draft = EnhancementPlanDraft(monologue_lines=[MonologueLineDraft(window_index=0, character_name="顾屿", text="我不会认输")])
    errors = plan_generate._semantic_errors(
        draft, context=context, library=None, windows=[(0.0, 20.0)], switches=(False, False, False),
    )
    assert any("目标区间 3-8 句" in e for e in errors)


# ---------- _semantic_errors：开关开启却零个可用条目触发重试（空集合不等于无需检查）----------

def test_semantic_errors_empty_music_with_switch_on_is_error(tmp_path) -> None:
    context = _context()
    library = _library(tmp_path)
    draft = EnhancementPlanDraft()  # 三项全空
    errors = plan_generate._semantic_errors(
        draft, context=context, library=library, windows=[], switches=(True, False, False),
    )
    assert any("配乐" in e and "换曲点" in e for e in errors)


def test_semantic_errors_empty_music_with_switch_off_is_not_error(tmp_path) -> None:
    context = _context()
    library = _library(tmp_path)
    draft = EnhancementPlanDraft()
    errors = plan_generate._semantic_errors(
        draft, context=context, library=library, windows=[], switches=(False, False, False),
    )
    assert not any("配乐" in e for e in errors)


def test_semantic_errors_empty_teaser_with_switch_on_is_error() -> None:
    context = _context()
    draft = EnhancementPlanDraft()
    errors = plan_generate._semantic_errors(
        draft, context=context, library=None, windows=[], switches=(False, True, False),
    )
    assert any("预告" in e and "3-5 段" in e for e in errors)


def test_semantic_errors_empty_teaser_with_switch_off_is_not_error() -> None:
    context = _context()
    draft = EnhancementPlanDraft()
    errors = plan_generate._semantic_errors(
        draft, context=context, library=None, windows=[], switches=(False, False, False),
    )
    assert not any("预告" in e for e in errors)


def test_semantic_errors_empty_monologue_with_switch_on_is_error() -> None:
    context = _context()
    draft = EnhancementPlanDraft()
    errors = plan_generate._semantic_errors(
        draft, context=context, library=None, windows=[(0.0, 20.0)], switches=(False, False, True),
    )
    assert any("独白" in e and "3-8 句" in e for e in errors)


def test_semantic_errors_empty_monologue_with_switch_off_is_not_error() -> None:
    context = _context()
    draft = EnhancementPlanDraft()
    errors = plan_generate._semantic_errors(
        draft, context=context, library=None, windows=[(0.0, 20.0)], switches=(False, False, False),
    )
    assert not any("独白" in e for e in errors)


# ---------- 配乐"换曲点"语义：提示词与 Schema 字段描述必须一致（同一份意图两处落地）----------

def test_system_prompt_states_cue_continues_until_next_cue() -> None:
    assert "一直连续播放到" in plan_generate._SYSTEM_PROMPT
    assert "下一条 cue 的 shot_no" in plan_generate._SYSTEM_PROMPT
    assert "45 秒" in plan_generate._SYSTEM_PROMPT


def test_music_cue_draft_field_descriptions_match_prompt_semantics() -> None:
    shot_no_desc = MusicCueDraft.model_fields["shot_no"].description or ""
    assert "一直连续播放到下一条" in shot_no_desc
    track_id_desc = MusicCueDraft.model_fields["track_id"].description or ""
    assert "mood_tags" in track_id_desc


# ---------- 配乐分段核验：一段一换（生产实测失败模式）触发重试并在放行后按最短
# 情绪段时长丢弃，合理的分段计划直接通过 ----------

def _music_section_context(n_shots: int = 23) -> EpisodeContext:
    shots = tuple(
        ShotContext(shot_no=i, start_s=(i - 1) * 15.0, duration_s=15.0, prompt_text=f"第{i}段")
        for i in range(1, n_shots + 1)
    )
    return EpisodeContext(shots=shots, total_duration_s=n_shots * 15.0, source_text="", character_roster=())


def test_one_cue_per_segment_plan_triggers_retry_error(tmp_path) -> None:
    """生产实测失败模式：模型把曲库里的 7 首曲子逐一提名给段 1-7，一段一换。"""
    context = _music_section_context()
    library = _library(tmp_path)
    cues = [MusicCueDraft(shot_no=i, track_id="t1") for i in range(1, 8)]
    draft = EnhancementPlanDraft(music_cues=cues)
    errors = plan_generate._semantic_errors(
        draft, context=context, library=library, windows=[], switches=(True, False, False),
    )
    assert any("不足最短情绪段时长" in e for e in errors)


def test_one_cue_per_segment_plan_dropped_after_retry_leaves_min_length_sections(tmp_path) -> None:
    context = _music_section_context()
    library = _library(tmp_path)
    cues = [MusicCueDraft(shot_no=i, track_id="t1") for i in range(1, 8)]
    draft = EnhancementPlanDraft(music_cues=cues)
    plan = plan_generate._resolve(draft, context=context, library=library, windows=[])
    kept = [c.shot_no for c in plan.music_cues]
    assert kept == [1, 4, 7]  # 45s 一段：0/45/90s 起点存活，中间的都因太短被丢
    starts = {s.shot_no: s.start_s for s in context.shots}
    for a, b in zip(kept, kept[1:]):
        assert starts[b] - starts[a] >= plan_validate.MUSIC_SECTION_MIN_DURATION_S - 1e-6
    assert any(d["feature"] == "music_bed" and "不足最短情绪段时长" in d["reason"] for d in plan.dropped)


def test_sane_three_section_music_plan_passes_without_errors(tmp_path) -> None:
    context = _music_section_context()
    library = _library(tmp_path)
    cues = [
        MusicCueDraft(shot_no=1, track_id="t1"),
        MusicCueDraft(shot_no=8, track_id="t1"),
        MusicCueDraft(shot_no=16, track_id="t1"),
    ]
    draft = EnhancementPlanDraft(music_cues=cues)
    errors = plan_generate._semantic_errors(
        draft, context=context, library=library, windows=[], switches=(True, False, False),
    )
    assert errors == []
    plan = plan_generate._resolve(draft, context=context, library=library, windows=[])
    assert [c.shot_no for c in plan.music_cues] == [1, 8, 16]
    assert plan.dropped == ()


def test_first_cue_not_at_episode_start_triggers_retry_but_is_not_dropped(tmp_path) -> None:
    """首条换曲点没有落在片头段——触发重试（生成阶段），但重试预算耗尽后不
    强行编造一条覆盖片头的 cue（不兜底填充）：这条 cue 本身依然保留，片头到
    它之间保持诚实的静音，由 ``app.final_edit_enhance.apply`` 的展开逻辑体现。
    """
    context = _music_section_context()
    library = _library(tmp_path)
    draft = EnhancementPlanDraft(music_cues=[MusicCueDraft(shot_no=4, track_id="t1")])
    errors = plan_generate._semantic_errors(
        draft, context=context, library=library, windows=[], switches=(True, False, False),
    )
    assert any("片头" in e for e in errors)
    plan = plan_generate._resolve(draft, context=context, library=library, windows=[])
    assert [c.shot_no for c in plan.music_cues] == [4]  # 没有被丢弃，也没有被编造成从 1 开始
    assert plan.dropped == ()


# ---------- plan_fingerprint：规则版本号变化必须让指纹变化（否则旧规则缓存会被继续复用）----------

def test_plan_fingerprint_changes_when_rules_version_bumps(monkeypatch, tmp_path) -> None:
    context = _context()
    library = _library(tmp_path)
    fp1 = plan_generate.plan_fingerprint(manifest_hash="m1", context=context, library=library, switches=(True, False, False))
    monkeypatch.setattr(plan_generate, "_PLAN_RULES_VERSION", plan_generate._PLAN_RULES_VERSION + 1)
    fp2 = plan_generate.plan_fingerprint(manifest_hash="m1", context=context, library=library, switches=(True, False, False))
    assert fp1 != fp2


# ---------- generate_plan：解析、丢弃、记录原因 ----------

async def test_generate_plan_resolves_valid_and_drops_invalid_items(monkeypatch, tmp_path) -> None:
    context = _context()
    library = _library(tmp_path)
    draft = EnhancementPlanDraft(
        music_cues=[MusicCueDraft(shot_no=1, track_id="t1"), MusicCueDraft(shot_no=99, track_id="t1")],
        teaser_clips=[
            TeaserClipDraft(shot_no=1, start_s=0.0, end_s=3.0, reason="开场"),
            TeaserClipDraft(shot_no=1, start_s=4.0, end_s=7.0, reason="转折"),
            TeaserClipDraft(shot_no=1, start_s=8.0, end_s=11.0, reason="高潮"),
        ],
        monologue_lines=[MonologueLineDraft(window_index=0, character_name="顾屿", text="我不会认输")],
    )

    async def fake_chat_structured(*_args, **_kwargs):
        return draft

    monkeypatch.setattr(model_gateway, "chat_structured", fake_chat_structured)

    plan = await plan_generate.generate_plan(
        context=context, library=library, windows=[(0.0, 15.0)], episode_id="ep-1", fingerprint="fp-1",
        switches=(True, True, True),
    )
    assert [c.shot_no for c in plan.music_cues] == [1]
    assert [c.shot_no for c in plan.teaser_clips] == [1, 1, 1]
    assert plan.teaser_total_duration_s == 9.0
    assert [m.text for m in plan.monologue_lines] == ["我不会认输"]
    assert any(d["feature"] == "music_bed" and "99" in d["reason"] for d in plan.dropped)


# ---------- _resolve：总长越界整批丢弃 / 同窗口多句独白不重叠（2026-09-28 评审）----------

def test_resolve_drops_entire_teaser_batch_when_total_duration_below_min() -> None:
    """单段各自合法（1.5-4s、落在段时长内）但总长低于 8s 目标区间下限时，不能
    当作"个体合法就都保留"——必须整批丢弃并在 ``dropped`` 里写明原因，不能
    产出一个明显偏离目标区间的预告片却仍标记为已应用。"""
    context = _context()
    draft = EnhancementPlanDraft(teaser_clips=[TeaserClipDraft(shot_no=1, start_s=0.0, end_s=2.0, reason="开场")])
    plan = plan_generate._resolve(draft, context=context, library=None, windows=[])
    assert plan.teaser_clips == ()
    assert plan.teaser_total_duration_s == 0.0
    assert any(d["feature"] == "teaser" and "超出" in d["reason"] for d in plan.dropped)


def test_resolve_monologue_lines_sharing_one_window_do_not_overlap() -> None:
    """两句独白共用同一个静默窗口且都通过校验时，最终解析出的播放区间必须
    首尾相接、互不重叠——不能都回退到整段候选窗口的原始边界（2026-09-28
    评审发现的回归：曾导致同窗口多句独白在混音/字幕上完全重叠）。"""
    context = EpisodeContext(
        shots=(ShotContext(shot_no=1, start_s=0.0, duration_s=15.0, prompt_text="开场"),),
        total_duration_s=15.0, source_text="我到底该不该相信他。夜色渐深无人回应。",
        character_roster=("顾屿",),
    )
    draft = EnhancementPlanDraft(monologue_lines=[
        MonologueLineDraft(window_index=0, character_name="顾屿", text="我到底该不该相信他"),
        MonologueLineDraft(window_index=0, character_name="顾屿", text="夜色渐深无人回应"),
    ])
    plan = plan_generate._resolve(draft, context=context, library=None, windows=[(0.0, 20.0)])
    assert len(plan.monologue_lines) == 2
    first, second = plan.monologue_lines
    assert second.start_s >= first.end_s
    assert plan.dropped == ()


async def test_generate_plan_without_library_yields_no_music_cues_and_no_music_errors(monkeypatch) -> None:
    context = _context()
    draft = EnhancementPlanDraft(music_cues=[MusicCueDraft(shot_no=1, track_id="whatever")])

    async def fake_chat_structured(*_args, **_kwargs):
        return draft

    monkeypatch.setattr(model_gateway, "chat_structured", fake_chat_structured)

    plan = await plan_generate.generate_plan(
        context=context, library=None, windows=[], episode_id="ep-1", fingerprint="fp-1",
        switches=(True, False, False),
    )
    assert plan.music_cues == ()
    assert not any(d["feature"] == "music_bed" for d in plan.dropped)
