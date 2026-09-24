"""画幅可配置（2026-09-23）：compiler 暗雷修复 + 供应商画幅优先级 + H3 双向互换。

覆盖单元 A（compiler.py 三个函数 + compile_prompt 必传 aspect_ratio）、
单元 C（seedance.py payload["ratio"] 三档优先级、minimax_h3._output_dimensions
两个方向互换）。不调用任何真实供应商——httpx/hiagent 的网络调用一律打桩。
"""
from __future__ import annotations

import asyncio

import pytest

from app import config, hiagent, seedance
from app.compiler import (
    compile_prompt,
    ensure_source_excerpt_in_prompt,
    normalize_video_args,
    sanitize_seedance_prompt,
    _split_video_args,
)
from app.minimax_h3 import H3Connection, _output_dimensions
from app.schemas import Bible, Character, Shot, World


def _bible() -> Bible:
    return Bible(
        characters=[
            Character(name="林风", role="主角", appearance_canonical="黑发青年，青色劲装"),
        ],
        world=World(visual_style_canonical="3D国漫电影感"),
    )


def _shot(**overrides) -> Shot:
    data = dict(
        shot_no=1, duration_s=5, shot_size="中景", camera_move="固定",
        scene_setting="夜，山门前", characters=["林风"],
        action_desc="林风抬手按住山门铜环。", first_frame_desc="林风站在山门前。",
        last_frame_desc="林风按住铜环。", source_excerpt="林风站在山门前，按住铜环。",
        state_in="林风站在山门前。", primary_action="林风抬手按住山门铜环。",
        state_out="林风按住铜环。", continuity_mode="same_scene_cut",
        characters_visible=["林风"], audio_cast=[],
    )
    data.update(overrides)
    return Shot(**data)


def _h3_connection(*, width: int, height: int) -> H3Connection:
    return H3Connection(
        base_url="https://h3.example.test", api_key="k", model="m",
        width=width, height=height, acceleration="standard", turbo_profile="",
        turbo_strength=0.0, turbo_low_vram=False, video_vae="", steps=1,
        use_te_speed=False, poll_interval=1.0,
    )


# ---------------------------------------------------------------------------
# 单元 A：compiler.py 暗雷——红/绿证据
# ---------------------------------------------------------------------------

def _legacy_normalize_video_args_hardcoded_916(prompt_text: str, duration=None) -> str:
    """修复前的真实实现副本（手写，不回退线上代码）：无视调用方传入的画幅，
    永远写死 9:16——这是本次要修的暗雷，此函数只用于红/绿对照，不再被生产调用。"""
    dur = duration if duration is not None else config.DEFAULT_VIDEO_DURATION_S
    text = prompt_text.strip()
    return f"{text} --ratio 9:16 --dur {dur}"


def test_legacy_hardcoded_logic_ignores_caller_aspect_ratio_red() -> None:
    """红：手写的修复前逻辑无论怎么调用都吐 9:16，证明暗雷真实存在过。"""
    legacy = _legacy_normalize_video_args_hardcoded_916("固定远景", 15)
    assert legacy.endswith("--ratio 9:16 --dur 15")


def test_normalize_video_args_respects_explicit_aspect_ratio_green() -> None:
    """绿：修复后必须尊重调用方传入的画幅，不再被收尾逻辑吃回 9:16。"""
    fixed = normalize_video_args("固定远景", 15, aspect_ratio="16:9")
    assert fixed.endswith("--ratio 16:9 --dur 15")
    assert "9:16" not in fixed


def test_normalize_video_args_rejects_invalid_aspect_ratio() -> None:
    with pytest.raises(ValueError, match="不支持的画幅"):
        normalize_video_args("x", 5, aspect_ratio="4:3")


def test_split_video_args_round_trips_explicit_ratio() -> None:
    body, args = _split_video_args("固定远景 --ratio 9:16 --dur 5", aspect_ratio="16:9")
    assert body == "固定远景"
    assert args == " --ratio 16:9 --dur 5"


def test_sanitize_seedance_prompt_requires_aspect_ratio_and_rewrites_tail() -> None:
    out = sanitize_seedance_prompt("正文 --ratio 9:16 --dur 5", aspect_ratio="16:9")
    assert out.endswith("--ratio 16:9 --dur 5")
    with pytest.raises(TypeError):
        sanitize_seedance_prompt("正文")  # aspect_ratio 必传，漏传即 TypeError


def test_compile_prompt_aspect_ratio_has_no_default() -> None:
    with pytest.raises(TypeError):
        compile_prompt(_shot(), _bible())  # type: ignore[call-arg]


def test_compile_prompt_16_9_prompt_ends_with_correct_ratio_tag() -> None:
    """单元 1 的关键回归：compile_prompt(..., aspect_ratio="16:9") 输出末尾
    确实是 --ratio 16:9，不被 sanitize_seedance_prompt 收尾吃回 9:16。"""
    prompt = compile_prompt(_shot(), _bible(), aspect_ratio="16:9")
    assert prompt.rstrip().endswith("--ratio 16:9 --dur 5")
    assert "、16:9、" in prompt


def test_compile_prompt_9_16_still_works_unchanged() -> None:
    prompt = compile_prompt(_shot(), _bible(), aspect_ratio="9:16")
    assert prompt.rstrip().endswith("--ratio 9:16 --dur 5")


def test_ensure_source_excerpt_in_prompt_requires_and_propagates_aspect_ratio() -> None:
    shot = _shot()
    scrubbed = ensure_source_excerpt_in_prompt(
        "镜头正文，无原文重合。 --ratio 9:16 --dur 5", shot, aspect_ratio="16:9",
    )
    assert scrubbed.endswith("--ratio 16:9 --dur 5")
    with pytest.raises(TypeError):
        ensure_source_excerpt_in_prompt("x", shot)  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# 单元 C：seedance.py payload["ratio"] 三档优先级
# ---------------------------------------------------------------------------

def _patch_seedance_network(monkeypatch, captured: dict) -> None:
    async def fake_post_json(_client, _url, payload, **_kwargs):
        captured["payload"] = payload
        return {"id": "task-aspect-ratio"}

    monkeypatch.setattr(hiagent, "active_model", lambda *_a, **_k: "test-video-model")
    monkeypatch.setattr(hiagent, "_model_connection", lambda *_a, **_k: ("https://example.test", {}))
    monkeypatch.setattr(hiagent, "_latest_provider_operation_request", lambda *_a, **_k: None)
    monkeypatch.setattr(hiagent, "_post_json", fake_post_json)


def test_seedance_ratio_prefers_call_meta_over_prompt_tail(monkeypatch) -> None:
    captured: dict = {}
    _patch_seedance_network(monkeypatch, captured)

    asyncio.run(
        seedance.SeedanceAdapter().create_video_task(
            "正文 --ratio 9:16 --dur 5",
            call_meta={"aspect_ratio": "16:9"},
        )
    )

    assert captured["payload"]["ratio"] == "16:9"


def test_seedance_ratio_falls_back_to_prompt_tail_without_call_meta(monkeypatch) -> None:
    """2.x 段本身没有 --ratio 尾巴时这条不适用；这里验证「call_meta 没给、
    prompt 尾部有」的兼容路径（例如走过 compiler 的旧版路径）。"""
    captured: dict = {}
    _patch_seedance_network(monkeypatch, captured)

    asyncio.run(
        seedance.SeedanceAdapter().create_video_task("正文 --ratio 16:9 --dur 5")
    )

    assert captured["payload"]["ratio"] == "16:9"


def test_seedance_ratio_defaults_to_9_16_for_legacy_tasks_without_snapshot(monkeypatch) -> None:
    """老任务兼容：meta 里没有 aspect_ratio 快照、prompt 也没有 --ratio 尾巴
    （典型的分镜台 2.0.0 段原文）时，落到 9:16——不是新数据的兜底，只兼容
    本次上线前已入队的历史任务。"""
    captured: dict = {}
    _patch_seedance_network(monkeypatch, captured)

    asyncio.run(
        seedance.SeedanceAdapter().create_video_task(
            "镜头1：固定远景，无对白。", call_meta={"duration_s": 15},
        )
    )

    assert captured["payload"]["ratio"] == "9:16"


def test_seedance_ratio_call_meta_wins_even_when_prompt_tail_disagrees(monkeypatch) -> None:
    captured: dict = {}
    _patch_seedance_network(monkeypatch, captured)

    asyncio.run(
        seedance.SeedanceAdapter().create_video_task(
            "正文 --ratio 9:16 --dur 5", call_meta={"aspect_ratio": "16:9"},
        )
    )

    assert captured["payload"]["ratio"] == "16:9"


# ---------------------------------------------------------------------------
# 单元 C：minimax_h3._output_dimensions 两个方向都要互换（风险 4）
# ---------------------------------------------------------------------------

def test_h3_dimensions_call_meta_landscape_swaps_portrait_baseline() -> None:
    conn = _h3_connection(width=576, height=1024)  # 竖屏基线
    width, height = _output_dimensions("正文", conn, call_meta={"aspect_ratio": "16:9"})
    assert (width, height) == (1024, 576)


def test_h3_dimensions_call_meta_portrait_swaps_landscape_baseline() -> None:
    """风险 4：连接本身被模型库配置成横屏基线时，请求竖屏也必须换回来，
    不能只处理「请求横屏、基线竖屏」单向。"""
    conn = _h3_connection(width=1024, height=576)  # 横屏基线
    width, height = _output_dimensions("正文", conn, call_meta={"aspect_ratio": "9:16"})
    assert (width, height) == (576, 1024)


def test_h3_dimensions_call_meta_matches_baseline_no_swap() -> None:
    conn = _h3_connection(width=576, height=1024)
    width, height = _output_dimensions("正文", conn, call_meta={"aspect_ratio": "9:16"})
    assert (width, height) == (576, 1024)


def test_h3_dimensions_falls_back_to_prompt_tail_without_call_meta() -> None:
    conn = _h3_connection(width=576, height=1024)
    width, height = _output_dimensions("正文 --ratio 16:9 --dur 5", conn, call_meta=None)
    assert (width, height) == (1024, 576)


def test_h3_dimensions_call_meta_overrides_disagreeing_prompt_tail() -> None:
    conn = _h3_connection(width=576, height=1024)
    width, height = _output_dimensions(
        "正文 --ratio 16:9 --dur 5", conn, call_meta={"aspect_ratio": "9:16"},
    )
    assert (width, height) == (576, 1024)


def test_h3_dimensions_no_hint_keeps_connection_baseline() -> None:
    conn = _h3_connection(width=576, height=1024)
    assert _output_dimensions("正文无参数", conn, call_meta=None) == (576, 1024)
