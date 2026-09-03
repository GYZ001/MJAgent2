"""分镜台 2.3.0 跨段连贯性备忘（用户拍板 2026-09-02，真实回归「人物白天说着
说着就变成黑夜了」驱动）。覆盖 app.production.storyboard_continuity_memo 的
纯函数（阻断式闸门、规则文案、advisory）+ storyboard_pack.py 的接线（跨段
payload 传递、chat_structured 语义重试、落库、版本号）。不覆盖真实供应商往返。
"""
from __future__ import annotations

import json

import pytest

from app import db
from app.production.storyboard_continuity_memo import (
    _AiCharacterState,
    _AiContinuityMemo,
    continuity_memo_character_advisories,
    continuity_memo_errors,
    continuity_memo_payload,
    continuity_memo_rules,
)
from app.production.storyboard_pack import (
    STORYBOARD_PACK_CONTRACT_MARKER,
    STORYBOARD_PACK_VERSION,
    StoryboardPack,
    StoryboardPackBeat,
    StoryboardPackSegment,
    _AiBeat,
    _AiBeatSheetDraft,
    _AiSegmentPlan,
    _AiStoryboardSegmentDraft,
    _generate_all_segment_prompts,
    persist_storyboard_pack,
)
from app.source_excerpt import SourceSegment, index_source_segments


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "storyboard-continuity-memo.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()


def _memo(**overrides) -> _AiContinuityMemo:
    base = dict(time_of_day="白天", time_of_day_basis="inferred")
    base.update(overrides)
    return _AiContinuityMemo(**base)


# ---------------------------------------------------------------------------
# continuity_memo_errors：阻断式闸门（判据只挂在数据上，逐条见模块 docstring）
# ---------------------------------------------------------------------------

def test_empty_time_of_day_is_rejected():
    errors = continuity_memo_errors(_memo(time_of_day=""), None, "[段1] 少年站在山顶。")
    assert any("time_of_day 不能为空" in e for e in errors)


def test_first_segment_inferred_is_accepted():
    errors = continuity_memo_errors(_memo(), None, "[段1] 少年站在山顶。")
    assert errors == []


def test_first_segment_inherited_is_rejected():
    """第一段没有上一段，不允许 inherited。"""
    errors = continuity_memo_errors(_memo(time_of_day_basis="inherited"), None, "[段1] 少年站在山顶。")
    assert any("inherited" in e and "第一段" in e for e in errors)


def test_non_first_segment_inferred_is_rejected():
    previous = _memo(time_of_day="黄昏")
    errors = continuity_memo_errors(_memo(time_of_day="正午"), previous, "[段2] 少年下山。")
    assert any("inferred" in e and "已有上一段时段" in e for e in errors)


def test_inherited_verbatim_copy_is_accepted():
    previous = _memo(time_of_day="黄昏")
    memo = _memo(time_of_day="黄昏", time_of_day_basis="inherited")
    assert continuity_memo_errors(memo, previous, "[段2] 少年继续赶路。") == []


def test_inherited_with_different_wording_is_rejected():
    """沿用必须逐字相同，「夜晚」与「深夜」视为不同——不做同义归并。"""
    previous = _memo(time_of_day="夜晚")
    memo = _memo(time_of_day="深夜", time_of_day_basis="inherited")
    errors = continuity_memo_errors(memo, previous, "[段2] 少年继续赶路。")
    assert any("逐字复制" in e and "夜晚" in e for e in errors)


def test_source_text_change_without_quote_is_rejected():
    previous = _memo(time_of_day="黄昏")
    memo = _memo(time_of_day="深夜", time_of_day_basis="source_text", time_of_day_source_quote="")
    errors = continuity_memo_errors(memo, previous, "[段2] 少年继续赶路，夜幕渐渐降临。")
    assert any("time_of_day_source_quote 为空" in e for e in errors)


def test_source_text_change_with_verbatim_quote_is_accepted():
    source_text = "[段1] 少年推门。\n[段2] 屋外月上柳梢，夜风微凉。"
    memo = _memo(
        time_of_day="深夜", time_of_day_basis="source_text",
        time_of_day_source_quote="月上柳梢，夜风微凉。",
    )
    assert continuity_memo_errors(memo, _memo(time_of_day="黄昏"), source_text) == []


def test_source_text_change_with_fabricated_quote_is_rejected():
    memo = _memo(
        time_of_day="深夜", time_of_day_basis="source_text",
        time_of_day_source_quote="乌云蔽月，狂风大作",
    )
    errors = continuity_memo_errors(memo, _memo(time_of_day="黄昏"), "[段2] 少年继续赶路。")
    assert any("找不到逐字匹配" in e for e in errors)


def test_source_text_basis_on_first_segment_with_empty_quote_is_rejected():
    memo = _memo(time_of_day="清晨", time_of_day_basis="source_text", time_of_day_source_quote="")
    errors = continuity_memo_errors(memo, None, "[段1] 少年站在山顶。")
    assert any("time_of_day_source_quote 为空" in e for e in errors)


# ---------------------------------------------------------------------------
# continuity_memo_rules：正面陈述文案（有/无上一段两种情况）
# ---------------------------------------------------------------------------

def test_rules_with_previous_mentions_time_of_day_and_verbatim_copy():
    rules = continuity_memo_rules(_memo(time_of_day="黄昏"))
    assert any("黄昏" in r and "逐字复制" in r for r in rules)


def test_rules_without_previous_describes_first_segment_case():
    rules = continuity_memo_rules(None)
    assert any("本集第一段" in r and "inferred" in r for r in rules)


# ---------------------------------------------------------------------------
# continuity_memo_character_advisories：只做 advisory，不阻断
# ---------------------------------------------------------------------------

def test_character_advisory_flags_unknown_identity_without_blocking():
    memo = _memo(characters=[_AiCharacterState(identity_id="id_ghost")])
    advisories = continuity_memo_character_advisories(memo, {"id_a"})
    assert len(advisories) == 1
    assert "[未拦截]" in advisories[0] and "id_ghost" in advisories[0]


def test_character_advisory_silent_when_identity_known():
    memo = _memo(characters=[_AiCharacterState(identity_id="id_a")])
    assert continuity_memo_character_advisories(memo, {"id_a"}) == []


# ---------------------------------------------------------------------------
# continuity_memo_payload：task_payload["previous_continuity_memo"] 的取值
# ---------------------------------------------------------------------------

def test_payload_is_none_without_previous_memo():
    assert continuity_memo_payload(None) is None


def test_payload_dumps_dict_for_existing_memo():
    assert continuity_memo_payload(_memo(time_of_day="黄昏"))["time_of_day"] == "黄昏"


# ---------------------------------------------------------------------------
# _generate_all_segment_prompts 端到端：跨调用传递 + 语义重试接线
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_generate_passes_previous_continuity_memo_into_next_segment_payload(monkeypatch):
    """第 2 段收到的 previous_continuity_memo 必须等于第 1 段返回的备忘；
    本集第一段收到的必须诚实地是 None（没有上一段可言）。"""
    import app.production.storyboard_pack as storyboard_pack_module

    calls: list[dict] = []

    async def fake_chat_structured(messages, **kwargs):
        payload = json.loads(messages[1]["content"])
        calls.append(payload)
        segment_no = payload["segment_no"]
        basis = "inferred" if segment_no == 1 else "inherited"
        memo = _AiContinuityMemo(time_of_day="黄昏", time_of_day_basis=basis)
        return _AiStoryboardSegmentDraft(prompt_text=f"提示词-段{segment_no}", shot_count=3, continuity_memo=memo)

    monkeypatch.setattr(storyboard_pack_module.model_gateway, "chat_structured", fake_chat_structured)
    monkeypatch.setattr(storyboard_pack_module, "_ensure_segment_prompt_budget", lambda: None)

    beat_draft = _AiBeatSheetDraft(
        beat_sheet=[_AiBeat(beat_id="B1", summary="x", segment_indexes=[1])],
        segments=[
            _AiSegmentPlan(segment_no=1, synopsis="a", source_segment_indexes=[1], beat_ids=["B1"]),
            _AiSegmentPlan(segment_no=2, synopsis="b", source_segment_indexes=[1], beat_ids=["B1"]),
        ],
    )
    source = [SourceSegment(segment_id="s1", text="少年站在山顶。", start_offset=0, end_offset=7)]

    await _generate_all_segment_prompts(
        episode_id="ep-memo-chain", episode_no=1, beat_draft=beat_draft, segments=source,
        payload={}, target_video_model="hiagent", bible=None, conn=None, project_id="",
        required_dialogue_by_segment_no={},
    )

    assert calls[0]["previous_continuity_memo"] is None
    assert calls[1]["previous_continuity_memo"] == _AiContinuityMemo(
        time_of_day="黄昏", time_of_day_basis="inferred",
    ).model_dump(mode="json")


@pytest.mark.asyncio
async def test_generate_retries_when_segment_changes_time_of_day_without_quote(monkeypatch):
    """第 2 段第一次擅自把时段从「白天」改成「黑夜」且不给引用：validate 必须
    拦下并把错误喂回去；模拟 chat_structured 自己的语义重试后改正才通过——
    fake 内部自己调用 kwargs["validate"]，照抄 test_storyboard_pack.py 里
    端到端用例学到的 fake 写法，只是这里额外验证重试判据本身。"""
    import app.production.storyboard_pack as storyboard_pack_module

    seen_errors: list[list[str]] = []
    attempts: dict[int, int] = {}

    async def fake_chat_structured(messages, **kwargs):
        payload = json.loads(messages[1]["content"])
        segment_no = payload["segment_no"]
        attempts[segment_no] = attempts.get(segment_no, 0) + 1
        if segment_no == 1:
            memo = _AiContinuityMemo(time_of_day="白天", time_of_day_basis="inferred")
        elif attempts[segment_no] == 1:
            memo = _AiContinuityMemo(time_of_day="黑夜", time_of_day_basis="inferred")  # 擅自改时段
        else:
            memo = _AiContinuityMemo(time_of_day="白天", time_of_day_basis="inherited")  # 重试后改正
        draft = _AiStoryboardSegmentDraft(prompt_text=f"提示词-段{segment_no}", shot_count=3, continuity_memo=memo)
        errors = kwargs["validate"](draft)
        seen_errors.append(errors)
        if errors:
            return await fake_chat_structured(messages, **kwargs)
        return draft

    monkeypatch.setattr(storyboard_pack_module.model_gateway, "chat_structured", fake_chat_structured)
    monkeypatch.setattr(storyboard_pack_module, "_ensure_segment_prompt_budget", lambda: None)

    beat_draft = _AiBeatSheetDraft(
        beat_sheet=[_AiBeat(beat_id="B1", summary="x", segment_indexes=[1])],
        segments=[
            _AiSegmentPlan(segment_no=1, synopsis="a", source_segment_indexes=[1], beat_ids=["B1"]),
            _AiSegmentPlan(segment_no=2, synopsis="b", source_segment_indexes=[1], beat_ids=["B1"]),
        ],
    )
    source = [SourceSegment(segment_id="s1", text="少年站在山顶。", start_offset=0, end_offset=7)]

    result = await _generate_all_segment_prompts(
        episode_id="ep-memo-retry", episode_no=1, beat_draft=beat_draft, segments=source,
        payload={}, target_video_model="hiagent", bible=None, conn=None, project_id="",
        required_dialogue_by_segment_no={},
    )

    assert attempts[2] == 2, "第 2 段第一次因擅自改时段被拦，重试后才通过"
    assert any("time_of_day" in e for e in seen_errors[1]), "错误文本必须进入重试"
    assert result[2].continuity_memo.time_of_day == "白天"


# ---------------------------------------------------------------------------
# 落库 + 版本号
# ---------------------------------------------------------------------------

def _seed_memo_episode(conn, episode_id: str) -> None:
    conn.execute(
        "INSERT INTO projects(id,name,bible_json,created_at) VALUES(?,?,?,?)",
        ("proj-memo", "continuity memo fixture", "{}", db.now()),
    )
    conn.execute(
        "INSERT INTO chapters(project_id, idx, title, content) VALUES(?,?,?,?)",
        ("proj-memo", 1, "第一章", "少年站在山顶。\n\n他扔掉了葫芦。"),
    )
    conn.execute(
        """INSERT INTO episodes(
               id,project_id,episode_no,title,source_chapters,target_duration_s,
               status,screenplay_status,target_video_model,
               screenplay_character_resolutions,created_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (episode_id, "proj-memo", 1, "Fixture", "[1]", 1800, "scripted", "ready", "hiagent", "[]", db.now()),
    )
    conn.commit()


def test_persist_storyboard_pack_writes_continuity_memo_per_segment():
    from app.domain.common import _episode_source_text

    conn = db.get_conn()
    episode_id = "ep-memo-persist"
    _seed_memo_episode(conn, episode_id)
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    segments = index_source_segments(_episode_source_text(conn, ep))

    memo_dict = _AiContinuityMemo(time_of_day="黄昏", time_of_day_basis="inferred").model_dump(mode="json")
    pack = StoryboardPack(
        episode_no=1, target_model="seedance_2",
        beat_sheet=[StoryboardPackBeat(beat_id="B1", summary="x", segment_indexes=[1])],
        segments=[
            StoryboardPackSegment(
                segment_no=1, synopsis="x", source_segment_indexes=[1], beat_ids=["B1"],
                prompt_text="提示词", shot_count=3, dialogue=[],
                resources={"characters": [], "scenes": [], "props": []},
                degraded_capabilities=[], continuity_memo=memo_dict,
            ),
        ],
    )
    persist_storyboard_pack(conn, episode_id, ep, {}, pack, segments=segments)

    row = conn.execute("SELECT shot_contract_json FROM shots WHERE episode_id=?", (episode_id,)).fetchone()
    contract = json.loads(row["shot_contract_json"])
    assert contract["storyboard_pack_segment"]["continuity_memo"]["time_of_day"] == "黄昏"
    assert contract["prompt_contract_version"] == STORYBOARD_PACK_CONTRACT_MARKER


def test_version_and_marker_stay_in_sync():
    """版本号本身随后续改造继续前进（当前 2.4.0，见 storyboard_pack 模块
    docstring 的完整 changelog），这条测试只锁"marker 必须跟版本号同步"这个
    不变式，不锁具体版本字符串——那会在每次版本前进时制造无关的红。"""
    assert STORYBOARD_PACK_CONTRACT_MARKER == f"storyboard_pack/{STORYBOARD_PACK_VERSION}"
