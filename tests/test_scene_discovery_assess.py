"""``app.production.scene_discovery_assess.assess_new_scene``：场景卡/道具卡
边界执行——scene_canonical 一旦复述了本次映射同时产出的道具卡名称/别名，补一次
纠正重试（至多两次模型调用），仍未清干净就放行但留下可见日志信号。

真实缺陷（《顾念长安（第二版）》proj_ca86b15ab7d7 EP1）：「顾屿家客房」
scene_canonical 把道具「小木星星」的外观细节写了进去，场景定场图因此把单颗星星
画成一整串花环，而道具卡自己的参考图是正确的单颗星。
"""
from __future__ import annotations

import json
import logging

import pytest

from app.harness import model_gateway
from app.production.scene_discovery_assess import assess_new_scene


def _verdict_payload(scene_canonical: str) -> str:
    return json.dumps({
        "important": True, "role": "anchor", "name": "顾屿家客房",
        "scene_canonical": scene_canonical, "location_kind": "室内",
        "location_key": "顾屿家客房", "era_anchor": "", "anchor_phrase": "",
        "existing_scene_name": "", "reason": "新场景",
    }, ensure_ascii=False)


LEAKING_CANONICAL = "室内空间，暖黄色灯光照明，床头柜摆放红绳穿的摩挲发亮的小木星星，整体温暖写实"
CLEAN_CANONICAL = "室内空间，暖黄色灯光照明，床头柜整洁，环境温暖写实，木质家具质感细腻"


async def test_red_without_leak_guard_the_leaking_canonical_would_pass_through() -> None:
    """红灯（手写一份修复前逻辑的临时副本，不回退线上代码）：旧版 assess_new_scene
    只发一次模型调用、不做道具串场核验，直接把模型第一次的产出当成最终结果。"""
    async def fake_chat_pre_fix(_messages, **_kwargs):
        return _verdict_payload(LEAKING_CANONICAL)

    from app.schemas import extract_json
    from app.production.scene_granularity import resolve_scene_granularity_verdict

    async def pre_fix_assess_new_scene(label, spatial_context, *, style, known_scenes, ep_label):
        raw = await fake_chat_pre_fix([{"role": "user", "content": "x"}])
        verdict = resolve_scene_granularity_verdict(
            extract_json(raw), label=label, spatial_context=spatial_context,
            canonical_min=30, canonical_max=80,
        )
        return verdict.as_dict()

    result = await pre_fix_assess_new_scene(
        "顾屿家客房", "客房床头柜上摆着一枚用红绳穿着的小木星星",
        style="真人实拍风", known_scenes=[], ep_label="第 1 集",
    )
    assert "小木星星" in result["scene_canonical"], "前提校验：红灯必须先复现道具串场"


async def test_assess_new_scene_retries_once_when_scene_canonical_leaks_known_prop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def fake_chat(messages, **_kwargs):
        calls.append(messages[0]["content"])
        if len(calls) == 1:
            return _verdict_payload(LEAKING_CANONICAL)
        return _verdict_payload(CLEAN_CANONICAL)

    monkeypatch.setattr(model_gateway, "chat", fake_chat)

    result = await assess_new_scene(
        "顾屿家客房", "客房床头柜上摆着一枚用红绳穿着的小木星星",
        style="真人实拍风", known_scenes=[], ep_label="第 1 集",
        known_prop_labels=["小木星星", "深灰色围巾"],
    )

    assert len(calls) == 2, "命中道具串场必须补一次纠正重试，不多不少"
    assert "小木星星" not in result["scene_canonical"]
    assert "小木星星" in calls[1], "重试提示词必须把命中的道具名称带回给模型"


async def test_assess_new_scene_single_call_when_no_leak(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    async def fake_chat(messages, **_kwargs):
        calls.append(messages[0]["content"])
        return _verdict_payload(CLEAN_CANONICAL)

    monkeypatch.setattr(model_gateway, "chat", fake_chat)

    result = await assess_new_scene(
        "顾屿家客房", "客房床头柜整洁", style="真人实拍风", known_scenes=[],
        ep_label="第 1 集", known_prop_labels=["小木星星"],
    )

    assert len(calls) == 1, "没有道具串场就不需要重试"
    assert result["scene_canonical"] == CLEAN_CANONICAL


async def test_assess_new_scene_logs_warning_and_still_returns_when_retry_still_leaks(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    async def fake_chat(_messages, **_kwargs):
        return _verdict_payload(LEAKING_CANONICAL)

    monkeypatch.setattr(model_gateway, "chat", fake_chat)

    with caplog.at_level(logging.WARNING):
        result = await assess_new_scene(
            "顾屿家客房", "客房床头柜上摆着一枚用红绳穿着的小木星星",
            style="真人实拍风", known_scenes=[], ep_label="第 1 集",
            known_prop_labels=["小木星星"],
        )

    assert "小木星星" in result["scene_canonical"], "至多重试一次；仍未清干净时按软检查口径放行，不做无界重试"
    assert any("小木星星" in record.message for record in caplog.records), "放行必须留下可见日志信号，不能静默"


async def test_assess_new_scene_no_prop_labels_never_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    async def fake_chat(messages, **_kwargs):
        calls.append(messages[0]["content"])
        return _verdict_payload(LEAKING_CANONICAL)

    monkeypatch.setattr(model_gateway, "chat", fake_chat)

    result = await assess_new_scene(
        "顾屿家客房", "客房床头柜上摆着一枚用红绳穿着的小木星星",
        style="真人实拍风", known_scenes=[], ep_label="第 1 集",
    )

    assert len(calls) == 1, "known_prop_labels 为空时没有可核对的道具名单，不该触发重试"
    assert result["scene_canonical"] == LEAKING_CANONICAL


# ---------------------------------------------------------------------------
# app.scenes.ensure_scenes_for_labels 的接线：known_prop_labels 必须来自
# bible.props（名称 + 别名），供 assess_new_scene 做边界核验（见上方各用例）。
# ---------------------------------------------------------------------------


async def test_ensure_scenes_for_labels_passes_known_prop_labels_from_bible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.db import get_conn
    from app import scenes

    bible = {
        "characters": [], "scenes": [],
        "props": [{"name": "小木星星", "appearance_canonical": "锚点", "aliases": ["星星挂饰"]}],
        "world": {"era": "", "genre": "", "visual_style_canonical": "测试画风"},
    }
    conn = get_conn()
    conn.execute(
        "INSERT INTO projects(id, name, bible_json, bible_version, created_at) VALUES(?,?,?,0,0)",
        ("proj-boundary-test", "测试项目", json.dumps(bible, ensure_ascii=False)),
    )
    conn.commit()

    captured: dict = {}

    async def fake_assess_new_scene(label, spatial_context, *, style, known_scenes, ep_label, known_prop_labels=()):
        captured["known_prop_labels"] = list(known_prop_labels)
        return {
            "important": False, "existing_scene_name": "", "reason": "",
            "name": "", "scene_canonical": "", "location_kind": "",
            "role": "transitional",
        }

    monkeypatch.setattr(scenes, "assess_new_scene", fake_assess_new_scene)

    await scenes.ensure_scenes_for_labels("proj-boundary-test", 1, ["顾屿家客房"])

    assert set(captured["known_prop_labels"]) == {"小木星星", "星星挂饰"}
