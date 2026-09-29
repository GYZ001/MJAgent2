"""``app.final_edit_enhance.plan_generate``：指纹确定性、"打回重试一次、最后
仍不满足就放行"（不整体失败）的核验节流器，以及模型草稿 -> 已核验计划的解析。
不打真实模型调用——``chat_structured`` 全部 monkeypatch。
"""
from __future__ import annotations

from app.final_edit_enhance import plan_generate
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
    errors = plan_generate._semantic_errors(draft, context=context, library=None, windows=[])
    assert any("目标区间 3-5 段" in e for e in errors)


def test_semantic_errors_flags_monologue_line_count_out_of_range() -> None:
    context = EpisodeContext(
        shots=(ShotContext(shot_no=1, start_s=0.0, duration_s=15.0, prompt_text="开场"),),
        total_duration_s=15.0, source_text="我不会认输。", character_roster=("顾屿",),
    )
    draft = EnhancementPlanDraft(monologue_lines=[MonologueLineDraft(window_index=0, character_name="顾屿", text="我不会认输")])
    errors = plan_generate._semantic_errors(draft, context=context, library=None, windows=[(0.0, 20.0)])
    assert any("目标区间 3-8 句" in e for e in errors)


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
    )
    assert plan.music_cues == ()
    assert not any(d["feature"] == "music_bed" for d in plan.dropped)
