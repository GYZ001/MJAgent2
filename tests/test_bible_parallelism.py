from __future__ import annotations

import asyncio

import pytest

from app import config, stages
from app.schemas import Character, character_is_portrait_eligible
from tests.conftest import patch_stages_everywhere as _patch_stages

# 旧点名与身份归并管线（roster_*.py 共 7 个模块）已于 2026-09-01 整体退场：
# generate_bible 不再点名角色，这条管线除测试外零生产调用方，7 个模块彼此
# 连通、构成一条完整链路，随本轮一并删除。本文件原有的约 40 个用例里，绝大
# 多数直接单元测试这条管线（点名主函数、候选模型、归并/归一化辅助函数等），
# 已同步删除；本文件只保留测试仍存活符号的 4 个用例。


def test_character_is_portrait_eligible_defaults_and_gates() -> None:
    old = Character(
        name="甲一",
        role="主角",
        appearance_canonical="黑发少年，青色长衫，身形修长，目光坚定，腰系布带",
    )
    assert character_is_portrait_eligible(old) is True
    assert character_is_portrait_eligible({
        "name": "甲一",
        "appearance_canonical": "黑发少年",
    }) is True
    assert character_is_portrait_eligible({
        "name": "孟浩",
        "portrait_eligible": False,
        "appearance_status": "insufficient_evidence",
    }) is False
    assert character_is_portrait_eligible({
        "name": "王腾飞",
        "portrait_eligible": True,
        "appearance_status": "grounded",
        "presence_status": "mentioned_only",
    }) is True


def test_sanitize_character_detail_drops_aliases_without_chapter_index() -> None:
    payload = {
        "appearance_canonical": "黑色短发，青色长衫，身形修长，腰系深色布带，脚穿布靴",
        "period_costume_canonical": "青布长衫布靴，束发挽髻，禁用现代面料拉链",
        "personality": "沉稳",
        "speech_style": "句式简短，语气平稳，少用修饰",
        "relationships": [],
        "aliases": [
            {
                "text": "孟才子",
                "name_kind": "honorific",
                "evidence_chapter_index": None,
                "evidence_quote": "孟才子救我",
            },
            {
                "text": "孟兄",
                "name_kind": "honorific",
                "evidence_chapter_index": 1,
                "evidence_quote": "孟兄来了",
            },
        ],
        "source_evidence": [
            {"evidence_chapter_index": None, "evidence_quote": "无效"},
            {"evidence_chapter_index": 1, "evidence_quote": "孟浩拔剑"},
        ],
    }
    cleaned = stages._sanitize_character_detail_payload(payload)
    assert [item["text"] for item in cleaned["aliases"]] == ["孟兄"]
    assert cleaned["source_evidence"] == [
        {"evidence_chapter_index": 1, "evidence_quote": "孟浩拔剑"},
    ]
    detail = stages._CharacterDetail.model_validate(cleaned)
    assert [item.text for item in detail.aliases] == ["孟兄"]


def test_bible_short_json_call_meta_keeps_explicit_first_token_timeout() -> None:
    """显式传入的 first_token_timeout_s 必须原样保留，不被默认值覆盖；缺省时
    退回跨阶段统一的 BIBLE_FIRST_TOKEN_TIMEOUT_S。

    原用例断言用的是 `BIBLE_DETAIL_FIRST_TOKEN_TIMEOUT_S`（单角色详情生成专用
    超时常量，随 bible_paratext.py 于 2026-09-01 一并退场——生产零调用方）；
    这里改用字面量验证同一条"显式值不被覆盖"的行为，不依赖已删除的常量。
    """
    meta = stages._bible_short_json_call_meta({
        "stage_key": "character_bible_detail",
        "first_token_timeout_s": 45.0,
    })
    assert meta["first_token_timeout_s"] == 45.0
    defaulted = stages._bible_short_json_call_meta({"stage_key": "character_roll_call"})
    assert defaulted["first_token_timeout_s"] == stages.BIBLE_FIRST_TOKEN_TIMEOUT_S
    # run_59d372954c0e：成功点名首字最慢 19.4s，20s 上限把仍在排队的流误杀。
    assert stages.BIBLE_FIRST_TOKEN_TIMEOUT_S == float(config.TIMEOUT_CHAT_FIRST_TOKEN_S)
    assert stages.BIBLE_FIRST_TOKEN_TIMEOUT_S >= 60.0


@pytest.mark.asyncio
async def test_alias_verification_runs_per_character_in_parallel(monkeypatch) -> None:
    from app.schemas import Bible, Character, CharacterAlias, World

    active = 0
    peak = 0

    async def fake_resolution(*_args, **_kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.05)
        active -= 1
        return {"accepted": False, "chapter_idx": None, "quote": "", "reason": "no"}

    _patch_stages(monkeypatch, "_alias_evidence_resolution", fake_resolution)
    appearance = "黑色短发，青色长衫，身形修长，腰系深色布带，脚穿布靴"
    bible = Bible(
        world=World(visual_style_canonical="国漫三维动画电影质感，统一自然光影与细腻材质"),
        characters=[
            Character(
                name="甲", role="主角", appearance_canonical=appearance,
                aliases=[CharacterAlias(
                    text="甲兄", name_kind="honorific",
                    evidence_chapter_index=1, evidence_quote="甲兄来了",
                )],
            ),
            Character(
                name="乙", role="重要配角", appearance_canonical=appearance,
                aliases=[CharacterAlias(
                    text="乙兄", name_kind="honorific",
                    evidence_chapter_index=1, evidence_quote="乙兄来了",
                )],
            ),
        ],
    )
    await stages._verify_character_aliases_for_subset(
        bible, bible.characters, {1: "甲兄来了。乙兄来了。"}, project_id="p1",
    )
    assert peak > 1
