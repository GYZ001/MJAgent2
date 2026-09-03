"""app.portraits.timeline_anchors 的锚点提取/核验/推导/落库核验。

覆盖：
1. 逐字核验（evidence 必须是章节原文子串，value/subject 必须是 evidence 子串）
   ——核验不过的单条候选丢弃，不影响同批其它候选、不重试整批。
2. era 推导只由已核验的锚点拼出（年份优先取首尾区间，其次退到 era 表述），
   没有可用锚点时保持空——不是模型/代码臆造。
3. ``apply_world_era`` 写回 bible_json.world.era：写入、幂等（值相同不重复写）、
   推不出 era 时不覆盖已有值。
"""
from __future__ import annotations

import json
import sqlite3

from app.portraits import timeline_anchors as ta
from app.portraits.timeline_anchors import (
    TimelineAnchor,
    _TimelineAnchorBatchResponse,
    _TimelineAnchorCandidate,
    apply_world_era,
    derive_world_era,
    extract_chapter_timeline_anchors,
    extract_project_timeline_anchors,
)


def _fake_chat_structured(anchors: list[dict]):
    async def fake(*args, **kwargs):
        assert kwargs["model_type"] is _TimelineAnchorBatchResponse
        return _TimelineAnchorBatchResponse(
            anchors=[_TimelineAnchorCandidate(**a) for a in anchors]
        )
    return fake


async def test_literal_anchor_is_kept(monkeypatch) -> None:
    chapter = {"idx": 3, "title": "第三章", "content": "那一年是2004年，里奥17岁首次代表巴萨出场。"}
    monkeypatch.setattr(
        ta.model_gateway, "chat_structured",
        _fake_chat_structured([
            {"kind": "year", "value": "2004年", "subject": "", "evidence": "那一年是2004年"},
            {"kind": "age", "value": "17岁", "subject": "里奥", "evidence": "里奥17岁首次代表巴萨出场"},
        ]),
    )
    anchors = await extract_chapter_timeline_anchors(chapter)
    assert len(anchors) == 2
    assert anchors[0] == TimelineAnchor(
        kind="year", value="2004年", subject="", evidence="那一年是2004年", chapter_index=3,
    )
    assert anchors[1].subject == "里奥"
    assert anchors[1].chapter_index == 3


async def test_non_literal_evidence_is_dropped(monkeypatch) -> None:
    """evidence 不是原文子串（模型编造/意译）时该条丢弃，不影响同批其它条目。"""
    chapter = {"idx": 1, "title": "第一章", "content": "那一年是2004年，一切才刚刚开始。"}
    monkeypatch.setattr(
        ta.model_gateway, "chat_structured",
        _fake_chat_structured([
            {"kind": "year", "value": "2004年", "subject": "", "evidence": "那一年是2004年"},
            # 原文没有这句话——评审应丢弃
            {"kind": "era", "value": "黄金年代", "subject": "", "evidence": "那是一个黄金年代"},
        ]),
    )
    anchors = await extract_chapter_timeline_anchors(chapter)
    assert len(anchors) == 1
    assert anchors[0].value == "2004年"


async def test_age_anchor_without_literal_subject_is_dropped(monkeypatch) -> None:
    """age 锚点的 subject 必须逐字出现在 evidence 里，否则丢弃这一条。"""
    chapter = {"idx": 1, "title": "第一章", "content": "他八岁那年第一次走进土场。"}
    monkeypatch.setattr(
        ta.model_gateway, "chat_structured",
        _fake_chat_structured([
            # subject "里奥" 没有出现在 evidence 原文里
            {"kind": "age", "value": "八岁", "subject": "里奥", "evidence": "他八岁那年第一次走进土场"},
        ]),
    )
    anchors = await extract_chapter_timeline_anchors(chapter)
    assert anchors == []


async def test_empty_chapter_content_short_circuits(monkeypatch) -> None:
    called = False

    async def fail_if_called(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("空章节不应发起模型调用")

    monkeypatch.setattr(ta.model_gateway, "chat_structured", fail_if_called)
    anchors = await extract_chapter_timeline_anchors({"idx": 1, "title": "空章", "content": "  "})
    assert anchors == []
    assert called is False


def _anchor(kind: str, value: str, subject: str = "", chapter_index: int = 1) -> TimelineAnchor:
    return TimelineAnchor(kind=kind, value=value, subject=subject, evidence=value, chapter_index=chapter_index)


def test_derive_world_era_from_year_anchors_takes_earliest_to_latest() -> None:
    anchors = [
        _anchor("year", "2004年"), _anchor("year", "2022年"), _anchor("year", "2009年"),
        _anchor("age", "17岁", subject="里奥"),
    ]
    assert derive_world_era(anchors) == "2004年～2022年"


def test_derive_world_era_single_year_anchor() -> None:
    assert derive_world_era([_anchor("year", "2004年")]) == "2004年"


def test_derive_world_era_year_values_with_month_day_sort_by_year_not_concatenated_digits() -> None:
    """真实 B 库样本（proj_ce9fcf749b23《跑不快的孩子》）：year 锚点常带月日，如
    "2000 年 9 月"/"2004 年 10 月 16 日"。按整串数字拼接排序会把 "2000 年 9 月"
    错误地排到 "2022 年" 之后（20009 > 2022），必须只取开头的年份数字排序。"""
    anchors = [
        _anchor("year", "2000 年 9 月"),
        _anchor("year", "2004 年 10 月 16 日"),
        _anchor("year", "2022 年"),
        _anchor("year", "1986 年"),
    ]
    assert derive_world_era(anchors) == "1986 年～2022 年"


def test_derive_world_era_ignores_month_day_fragments_mistagged_as_year() -> None:
    """真实 B 库样本：模型把承接上文年份的"11 月 22 日"（本身不含年份数字，
    指代 2022 年世界杯决赛）也标成了 kind="year"，必须过滤掉，不能参与排序
    （否则"11"被当成比"2022"更早的年份，拼出不成立的区间）。"""
    anchors = [
        _anchor("year", "2022年"), _anchor("year", "11 月 22 日"), _anchor("year", "12 月 18 日"),
    ]
    assert derive_world_era(anchors) == "2022年"


def test_derive_world_era_falls_back_to_era_phrases_when_no_year() -> None:
    anchors = [_anchor("era", "东汉末年"), _anchor("era", "黄巾起义"), _anchor("relative", "三年后")]
    assert derive_world_era(anchors) == "东汉末年、黄巾起义"


def test_derive_world_era_empty_without_any_usable_anchor() -> None:
    assert derive_world_era([]) == ""
    assert derive_world_era([_anchor("relative", "三年后"), _anchor("age", "八岁", subject="里奥")]) == ""


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE projects(id TEXT PRIMARY KEY, bible_json TEXT, bible_version INTEGER DEFAULT 0)"
    )
    return conn


def test_apply_world_era_writes_derived_value() -> None:
    conn = _make_conn()
    conn.execute(
        "INSERT INTO projects(id, bible_json, bible_version) VALUES(?,?,0)",
        ("p1", json.dumps({"world": {"era": "", "genre": "", "visual_style_canonical": "国风"}})),
    )
    conn.commit()
    written = apply_world_era(conn, "p1", [_anchor("year", "2004年"), _anchor("year", "2022年")])
    assert written is True
    data = json.loads(conn.execute("SELECT bible_json FROM projects WHERE id='p1'").fetchone()["bible_json"])
    assert data["world"]["era"] == "2004年～2022年"


def test_apply_world_era_no_anchors_does_not_touch_existing_value() -> None:
    conn = _make_conn()
    conn.execute(
        "INSERT INTO projects(id, bible_json, bible_version) VALUES(?,?,0)",
        ("p1", json.dumps({"world": {"era": "既有值", "genre": "", "visual_style_canonical": "国风"}})),
    )
    conn.commit()
    written = apply_world_era(conn, "p1", [])
    assert written is False
    data = json.loads(conn.execute("SELECT bible_json FROM projects WHERE id='p1'").fetchone()["bible_json"])
    assert data["world"]["era"] == "既有值"


def test_apply_world_era_idempotent_when_value_unchanged() -> None:
    conn = _make_conn()
    conn.execute(
        "INSERT INTO projects(id, bible_json, bible_version) VALUES(?,?,0)",
        ("p1", json.dumps({"world": {"era": "2004年～2022年", "genre": "", "visual_style_canonical": "国风"}})),
    )
    conn.commit()
    written = apply_world_era(conn, "p1", [_anchor("year", "2004年"), _anchor("year", "2022年")])
    assert written is False


# ==================== WS10-B：地名/门派名误标 era ====================
#
# 生产事故（我欲封天）：era 推导退化成地名拼接——「赵国」「靠山宗」是地点/门派
# 专名，被模型标成了 kind="era"。提示词已改成正面陈述只接受真正的时代/朝代/
# 纪年表述；这里另加一道结构性复核：value 若逐字等于本项目场景库名/人物谱名/
# 已判非角色名（数据里实际存在的集合，不是词表），直接拒绝这一条。


async def test_era_candidate_matching_known_scene_name_is_rejected(monkeypatch) -> None:
    chapter = {"idx": 1, "title": "第一章", "content": "少年从赵国出发，一路向北。"}
    monkeypatch.setattr(
        ta.model_gateway, "chat_structured",
        _fake_chat_structured([
            {"kind": "era", "value": "赵国", "subject": "", "evidence": "少年从赵国出发"},
        ]),
    )
    anchors = await extract_chapter_timeline_anchors(
        chapter, rejected_era_values=frozenset({"赵国"}),
    )
    assert anchors == []


async def test_era_candidate_without_project_context_is_kept(monkeypatch) -> None:
    """不传 ``rejected_era_values``（默认空集合）时行为与改动前一致——这是一层
    结构性复核（对照本次输入实际存在的数据），不是新增一张地名/门派词表；
    没有项目上下文可比对时，逐字核验通过的候选原样保留。"""
    chapter = {"idx": 1, "title": "第一章", "content": "少年从赵国出发，一路向北。"}
    monkeypatch.setattr(
        ta.model_gateway, "chat_structured",
        _fake_chat_structured([
            {"kind": "era", "value": "赵国", "subject": "", "evidence": "少年从赵国出发"},
        ]),
    )
    anchors = await extract_chapter_timeline_anchors(chapter)
    assert len(anchors) == 1


async def test_era_candidate_only_rejects_era_kind_not_other_kinds(monkeypatch) -> None:
    """``rejected_era_values`` 只影响 ``kind=="era"``——同名文本若被模型（错误地）
    标成其它类别，不受这道复核影响，问题留给各自既有的逐字核验处理。"""
    chapter = {"idx": 1, "title": "第一章", "content": "赵国二十年，风调雨顺。"}
    monkeypatch.setattr(
        ta.model_gateway, "chat_structured",
        _fake_chat_structured([
            {"kind": "relative", "value": "赵国", "subject": "", "evidence": "赵国二十年，风调雨顺"},
        ]),
    )
    anchors = await extract_chapter_timeline_anchors(
        chapter, rejected_era_values=frozenset({"赵国"}),
    )
    assert len(anchors) == 1


def test_project_known_non_era_names_from_scenes_characters_and_settings() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE projects(id TEXT PRIMARY KEY, bible_json TEXT)")
    conn.execute("CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT)")
    bible = {
        "world": {"visual_style_canonical": "国风", "scenes": [{"name": "赵国", "scene_canonical": "边境城池"}]},
        "characters": [{"name": "楚天", "role": "主角", "appearance_canonical": "少年剑客"}],
    }
    conn.execute(
        "INSERT INTO projects(id, bible_json) VALUES(?,?)",
        ("p1", json.dumps(bible, ensure_ascii=False)),
    )
    conn.execute(
        "INSERT INTO settings(key, value) VALUES(?, '1')", ("char_not_character:p1:靠山宗",),
    )
    conn.commit()

    names = ta._project_known_non_era_names(conn, "p1")
    assert names == frozenset({"赵国", "楚天", "靠山宗"})


def test_project_known_non_era_names_empty_without_bible() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE projects(id TEXT PRIMARY KEY, bible_json TEXT)")
    conn.execute("CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT)")
    conn.commit()
    assert ta._project_known_non_era_names(conn, "does-not-exist") == frozenset()


async def test_extract_project_timeline_anchors_filters_known_scene_name(monkeypatch) -> None:
    """端到端：场景库里已有「靠山宗」时，同名 era 候选在整本提取流程里被拒绝。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE projects(id TEXT PRIMARY KEY, bible_json TEXT)")
    conn.execute("CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT)")
    bible = {
        "world": {"visual_style_canonical": "国风", "scenes": [{"name": "靠山宗", "scene_canonical": "山门"}]},
        "characters": [],
    }
    conn.execute(
        "INSERT INTO projects(id, bible_json) VALUES(?,?)",
        ("p1", json.dumps(bible, ensure_ascii=False)),
    )
    conn.commit()
    monkeypatch.setattr(ta, "get_conn", lambda: conn)
    monkeypatch.setattr(ta, "_persist_timeline_anchors_artifact", lambda *_a, **_k: {"id": "art1"})
    monkeypatch.setattr(
        ta.model_gateway, "chat_structured",
        _fake_chat_structured([
            {"kind": "era", "value": "靠山宗", "subject": "", "evidence": "他自幼长在靠山宗"},
        ]),
    )
    chapters = [{"idx": 1, "title": "第一章", "content": "他自幼长在靠山宗，从未离开。"}]

    anchors = await extract_project_timeline_anchors("p1", chapters)

    assert anchors == []
