"""叙述向称谓归属：identity 四选一新增的 functional 值，以及 unresolved 不再
落 functional_extras 的设计变更（app.production.prep_pack.appellation_resolve）。

背景（2026-09-30，B 机隔离沙箱，第2集 ep_7623b7b0a49a 真实数据）：旧实现的
identity 三选一里，unresolved 同时装着两种判定完全不同的情形——①候选名单
之外、原文明确写到的另一个具体的人（房东、摊主，给实体是诚实的）；②看不清
到底是谁、甚至可能就是候选名单里已经登记的某个人（给实体等于编造一个人）。
真实案例：「温老师」（原文「陆一舟笑嘻嘻地说："温老师，顺路来蹭个饭的路引子。"」，
指的就是候选温念本人）、「他」「有人」都被旧实现误判成独立群演；「你俩」（指
温念+顾屿两位已登记角色）被误判成 collective，也铸了一个群演。新增 identity=
functional 专门承接情形①（落 functional_extras，证据经与具名分支同一套逐字
核验）；unresolved 现在专收情形②，不再落 functional_extras、不铸
visual_entity_id，只记进按 label 合并段号的 unresolved_appellations，供映射台
界面人工核查，见 appellation_resolve.py 模块 docstring"设计变更"一节。
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
from types import SimpleNamespace

from app.production.prep_pack import appellation_resolve as ar
from app.schemas import Bible, Character, World
from app.source_excerpt import index_source_segments


def _seg(*texts: str):
    return [SimpleNamespace(text=text) for text in texts]


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE character_portraits(id TEXT, project_id TEXT, character_name TEXT, "
        "ep_start INTEGER, ep_end INTEGER)"
    )
    conn.commit()
    return conn


def _bible(*names: str) -> Bible:
    return Bible(
        characters=[Character(name=name, role="主角", appearance_canonical="占位外观") for name in names],
        world=World(visual_style_canonical="测试画风"),
    )


# ---------------------------------------------------------------------------
# schema 契约：identity enum 必须含 functional（两侧对齐，见 CLAUDE.md
# 「模型契约两侧必须对齐」）
# ---------------------------------------------------------------------------

def test_schema_enum_includes_functional(monkeypatch):
    captured: dict = {}

    async def fake_chat_structured(*args, **kwargs):
        captured["output_schema"] = kwargs["output_schema"]
        return ar._AppellationResolutionResponse(appellations=[])

    monkeypatch.setattr(ar.model_gateway, "chat_structured", fake_chat_structured)
    asyncio.run(ar._appellation_resolution_call(
        dossier=[{"segment_index": 1, "text": "占位原文"}], candidates=["辰南"],
        episode_id="ep1", project_id="p1",
    ))
    enum = captured["output_schema"]["$defs"]["_AppellationVerdict"]["properties"]["identity"]["enum"]
    assert ar.FUNCTIONAL in enum
    assert ar.COLLECTIVE in enum
    assert ar.UNRESOLVED in enum


# ---------------------------------------------------------------------------
# _verified_verdicts：functional 的逐字证据核验（照抄具名分支纪律）
# ---------------------------------------------------------------------------

def test_functional_verdict_with_verbatim_evidence_is_kept():
    """房东：候选之外的另一个具体的人，evidence 逐字命中——保留 functional。"""
    response = ar._AppellationResolutionResponse(appellations=[
        ar._AppellationVerdict(
            raw_label="房东", identity=ar.FUNCTIONAL,
            evidence="房东站在门口收租", segment_indexes=[1],
        ),
    ])
    verified = ar._verified_verdicts(
        response, candidates={"辰南"}, source_text="",
        segments=_seg("房东站在门口收租，等着新来的房客。"), valid_segment_indexes={1},
    )
    assert len(verified) == 1
    assert verified[0].identity == ar.FUNCTIONAL
    assert verified[0].evidence == "房东站在门口收租"


def test_functional_verdict_with_non_verbatim_evidence_downgrades_to_unresolved():
    """evidence 编造/定位不到——核验不过，按 unresolved 处理（raw_label 本身
    仍逐字出现在声明段落里，条目不会被整条丢弃）。"""
    response = ar._AppellationResolutionResponse(appellations=[
        ar._AppellationVerdict(
            raw_label="小贩", identity=ar.FUNCTIONAL,
            evidence="原文根本没有这句话", segment_indexes=[1],
        ),
    ])
    verified = ar._verified_verdicts(
        response, candidates={"辰南"}, source_text="",
        segments=_seg("巷口摆摊的小贩喊了一声，声音不大。"), valid_segment_indexes={1},
    )
    assert len(verified) == 1
    assert verified[0].identity == ar.UNRESOLVED


# ---------------------------------------------------------------------------
# 端到端：resolve_narration_appellations 的路由——functional/collective 落
# functional_extras，unresolved 落 unresolved_appellations
# ---------------------------------------------------------------------------

def test_functional_lands_in_functional_extras_with_real_anchor_phrase(monkeypatch):
    conn = _make_conn()
    bible = _bible("辰南")
    source_text = "房东站在门口收租，等着新来的房客。"

    async def fake_call(*, dossier, candidates, episode_id, project_id):
        seg = dossier[0]["segment_index"]
        return ar._AppellationResolutionResponse(appellations=[
            ar._AppellationVerdict(
                raw_label="房东", identity=ar.FUNCTIONAL,
                evidence="房东站在门口收租", segment_indexes=[seg],
            ),
        ])

    monkeypatch.setattr(ar, "_appellation_resolution_call", fake_call)
    segments = index_source_segments(source_text)
    characters: dict = {}
    functional_extras: dict = {}
    unresolved: list = []
    asyncio.run(ar.resolve_narration_appellations(
        conn, project_id="p1", episode_id="ep1", episode_no=1,
        source_text=source_text, bible=bible, segments=segments,
        characters=characters, functional_extras=functional_extras,
        character_appellation_rows=[], unresolved_appellations=unresolved,
    ))
    assert characters == {}
    assert unresolved == []
    extra = functional_extras["房东"]
    assert extra["visual_entity_id"].startswith("entity:")
    assert extra["provenance"]["anchor_phrase"] == "房东站在门口收租"
    assert "collective" not in extra["provenance"]


def test_collective_still_lands_in_functional_extras(monkeypatch):
    """孩子们：候选之外的一群人，行为不变——落 functional_extras，
    provenance.collective=True（既有回归防线，本次改动不许动它）。"""
    conn = _make_conn()
    bible = _bible("辰南")
    source_text = "孩子们在院子里追逐打闹，笑声不断。"

    async def fake_call(*, dossier, candidates, episode_id, project_id):
        return ar._AppellationResolutionResponse(appellations=[
            ar._AppellationVerdict(
                raw_label="孩子们", identity=ar.COLLECTIVE,
                segment_indexes=[dossier[0]["segment_index"]],
            ),
        ])

    monkeypatch.setattr(ar, "_appellation_resolution_call", fake_call)
    segments = index_source_segments(source_text)
    characters: dict = {}
    functional_extras: dict = {}
    unresolved: list = []
    asyncio.run(ar.resolve_narration_appellations(
        conn, project_id="p1", episode_id="ep1", episode_no=1,
        source_text=source_text, bible=bible, segments=segments,
        characters=characters, functional_extras=functional_extras,
        character_appellation_rows=[], unresolved_appellations=unresolved,
    ))
    assert unresolved == []
    extra = functional_extras["孩子们"]
    assert extra["visual_entity_id"].startswith("entity:")
    assert extra["provenance"]["collective"] is True


def test_unresolved_not_in_functional_extras_recorded_separately_with_log(monkeypatch, caplog):
    """温老师真实案例复现：模型（此处用 fake_call 模拟）对候选之外看不清是谁
    的称谓申报 unresolved——不得落 functional_extras、不得铸 visual_entity_id，
    改记进 unresolved_appellations，并打固定前缀 warning 供检索。"""
    conn = _make_conn()
    bible = _bible("温念")
    source_text = "陆一舟笑嘻嘻地说：温老师，顺路来蹭个饭的路引子。"

    async def fake_call(*, dossier, candidates, episode_id, project_id):
        return ar._AppellationResolutionResponse(appellations=[
            ar._AppellationVerdict(
                raw_label="温老师", identity=ar.UNRESOLVED,
                segment_indexes=[dossier[0]["segment_index"]],
            ),
        ])

    monkeypatch.setattr(ar, "_appellation_resolution_call", fake_call)
    segments = index_source_segments(source_text)
    characters: dict = {}
    functional_extras: dict = {}
    unresolved: list = []
    with caplog.at_level(logging.WARNING, logger="app.production.prep_pack.appellation_resolve"):
        asyncio.run(ar.resolve_narration_appellations(
            conn, project_id="p1", episode_id="ep1", episode_no=1,
            source_text=source_text, bible=bible, segments=segments,
            characters=characters, functional_extras=functional_extras,
            character_appellation_rows=[], unresolved_appellations=unresolved,
        ))
    assert characters == {}
    assert functional_extras == {}
    assert unresolved == [{"label": "温老师", "segment_indexes": [1]}]
    assert any(
        "[PREP_PACK_APPELLATION_UNRESOLVED][未拦截]" in record.message and "温老师" in record.message
        for record in caplog.records
    )


def test_pair_referring_to_two_registered_candidates_stays_unresolved_not_functional_extra(monkeypatch):
    """「你俩」指两位已登记候选（温念+顾屿）：按提示词规则申报 unresolved（不是
    collective——它不是候选之外的一群人），因此同样不落 functional_extras。"""
    conn = _make_conn()
    bible = _bible("温念", "顾屿")
    source_text = "陆一舟看着两人，笑道：你俩今天倒是挺有默契。"

    async def fake_call(*, dossier, candidates, episode_id, project_id):
        return ar._AppellationResolutionResponse(appellations=[
            ar._AppellationVerdict(
                raw_label="你俩", identity=ar.UNRESOLVED,
                segment_indexes=[dossier[0]["segment_index"]],
            ),
        ])

    monkeypatch.setattr(ar, "_appellation_resolution_call", fake_call)
    segments = index_source_segments(source_text)
    characters: dict = {}
    functional_extras: dict = {}
    unresolved: list = []
    asyncio.run(ar.resolve_narration_appellations(
        conn, project_id="p1", episode_id="ep1", episode_no=1,
        source_text=source_text, bible=bible, segments=segments,
        characters=characters, functional_extras=functional_extras,
        character_appellation_rows=[], unresolved_appellations=unresolved,
    ))
    assert functional_extras == {}
    assert unresolved == [{"label": "你俩", "segment_indexes": [1]}]
