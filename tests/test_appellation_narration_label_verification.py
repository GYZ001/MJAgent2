"""叙述向称谓归属：unresolved/collective 分支的 raw_label 逐字核验
（app.production.prep_pack.appellation_resolve._verbatim_segments_for_label /
_verified_verdicts）。从 test_identity_appellation_resolution.py 拆出新文件
（那个文件已经在 500 行上限附近，见其文件末尾的拆分说明）。

背景（生产第2集真实缺陷）：identity 命中具名人物谱角色时，``_verified_verdicts``
一直对 evidence 做逐字定位核验；但 identity 落在 unresolved/collective 时完全
不核验，模型声称「「他」在第5段」，而第5段原文里根本没有一个独立的「他」字
（anchor_phrase=""），这条没有任何依据的声明照样原样进了 functional_extras。
本文件覆盖三种结果：逐字找得到的保留、逐字找不到的整条剔除、部分段号找不到的
只剔除那几段。
"""
from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

from app.production.prep_pack import appellation_resolve as ar


def _seg(*texts: str):
    return [SimpleNamespace(text=text) for text in texts]


# ---------------------------------------------------------------------------
# _verified_verdicts 单元测试：unresolved/collective 的 raw_label 逐字核验
# ---------------------------------------------------------------------------

def test_unresolved_verdict_kept_when_raw_label_verbatim_in_its_segment():
    """逐字找得到——保留，不受影响（既有行为不能因为新增核验被误伤）。"""
    response = ar._AppellationResolutionResponse(appellations=[
        ar._AppellationVerdict(raw_label="老人", identity=ar.UNRESOLVED, segment_indexes=[1]),
    ])
    verified = ar._verified_verdicts(
        response, candidates={"辰南"}, source_text="",
        segments=_seg("一个老人站在原地，没有人知道他是谁。"), valid_segment_indexes={1},
    )
    assert len(verified) == 1
    assert verified[0].identity == ar.UNRESOLVED
    assert verified[0].segment_indexes == [1]


def test_unresolved_verdict_dropped_when_raw_label_not_in_its_declared_segment(caplog):
    """生产第2集真实缺陷复现：raw_label「他」声明落在第1段，但第1段原文里没有
    一个独立的「他」字——逐字核验找不到依据，整条不发布，且要留可见信号
    （log.warning），不是静默丢弃。"""
    response = ar._AppellationResolutionResponse(appellations=[
        ar._AppellationVerdict(raw_label="他", identity=ar.UNRESOLVED, segment_indexes=[1]),
    ])
    with caplog.at_level(logging.WARNING, logger="app.production.prep_pack.appellation_resolve"):
        verified = ar._verified_verdicts(
            response, candidates={"辰南"}, source_text="",
            segments=_seg("远处传来脚步声，没有人回答。"), valid_segment_indexes={1},
        )
    assert verified == []
    assert any("他" in record.message for record in caplog.records)


def test_unresolved_verdict_dropped_when_single_char_label_only_hits_plural_compound(caplog):
    """单字代词复数后缀复现：段落原文只有「他们」「我们」，没有一次独立出现的
    「他」或「我」——纯子串包含会被"他们"里的"他"打穿，必须剔除，不能因为
    子串命中就判定"逐字核验通过"。"""
    response = ar._AppellationResolutionResponse(appellations=[
        ar._AppellationVerdict(raw_label="他", identity=ar.UNRESOLVED, segment_indexes=[1]),
        ar._AppellationVerdict(raw_label="我", identity=ar.UNRESOLVED, segment_indexes=[1]),
    ])
    with caplog.at_level(logging.WARNING, logger="app.production.prep_pack.appellation_resolve"):
        verified = ar._verified_verdicts(
            response, candidates=set(), source_text="",
            segments=_seg("他们在村口说笑，我们则在屋里等待，谁都没提起那件事。"),
            valid_segment_indexes={1},
        )
    assert verified == []


def test_unresolved_verdict_kept_when_single_char_label_independently_present():
    """同一段里既有"他们"又有独立的"他"："李四推了推他"——独立出现的那次必须
    仍然通过，复数后缀过滤不能误伤真实的单字代词指代。"""
    response = ar._AppellationResolutionResponse(appellations=[
        ar._AppellationVerdict(raw_label="他", identity=ar.UNRESOLVED, segment_indexes=[1]),
    ])
    verified = ar._verified_verdicts(
        response, candidates=set(), source_text="",
        segments=_seg("他们说要走了，李四却拉住了他。"), valid_segment_indexes={1},
    )
    assert len(verified) == 1
    assert verified[0].segment_indexes == [1]


def test_single_char_label_prefix_compound_is_a_known_gap():
    """已知局限，不是本次修复范围：单字代词作为前缀复合词一部分出现（"其他"
    里的"他"、"自我"里的"我"）时，逐字核验仍可能假阳性通过——排除这类需要
    真正的分词，本仓库未引入分词依赖（见 _label_occurs_standalone 文档字符
    串）。这条测试把这个缺口钉在这里，防止未来有人误以为"逐字核验"已经把
    单字代词的误判全部堵死。"""
    response = ar._AppellationResolutionResponse(appellations=[
        ar._AppellationVerdict(raw_label="他", identity=ar.UNRESOLVED, segment_indexes=[1]),
    ])
    verified = ar._verified_verdicts(
        response, candidates=set(), source_text="",
        segments=_seg("大家各忙各的，其他人都没搭话。"), valid_segment_indexes={1},
    )
    # 已知局限：这里"应该"是 []（原文没有独立的"他"），但当前实现会把
    # "其他"里的"他"误判通过——记录现状，不是期望行为。
    assert verified != []


def test_collective_verdict_dropped_when_raw_label_not_verbatim():
    """collective 分支同样要核验——众猴」若没有逐字出现在它声明的段落里，
    不能因为是「集体称谓」就免检。"""
    response = ar._AppellationResolutionResponse(appellations=[
        ar._AppellationVerdict(raw_label="众猴", identity=ar.COLLECTIVE, segment_indexes=[1]),
    ])
    verified = ar._verified_verdicts(
        response, candidates=set(), source_text="",
        segments=_seg("山中一片寂静，草木不生。"), valid_segment_indexes={1},
    )
    assert verified == []


def test_unresolved_verdict_partial_segments_kept_when_only_some_segments_miss_label():
    """部分段号找不到只剔除那几段：raw_label「有人」在第1段出现、第2段没有，
    只剔除第2段，第1段保留——不因为一段没依据就把整条一起丢掉。"""
    response = ar._AppellationResolutionResponse(appellations=[
        ar._AppellationVerdict(raw_label="有人", identity=ar.UNRESOLVED, segment_indexes=[1, 2]),
    ])
    verified = ar._verified_verdicts(
        response, candidates=set(), source_text="",
        segments=_seg("有人在门口敲门。", "屋里静悄悄的，没有回应。"),
        valid_segment_indexes={1, 2},
    )
    assert len(verified) == 1
    assert verified[0].segment_indexes == [1]


def test_named_identity_downgraded_to_unresolved_is_not_double_checked():
    """identity 原本命中候选人名（走的是「具名分支」，已经用 evidence 逐字核验
    过一次），只是 evidence 定位失败才被降级为 unresolved——这条不重复套用
    unresolved/collective 分支新增的 raw_label 逐字核验（那是只管「identity
    原始声明就落在候选人名之外」的独立一支，核验口径不重复加码，见
    _verified_verdicts 文档字符串）：即使 raw_label 没有逐字出现在声明的段落
    里，条目仍然保留、只是 identity 降级。"""
    response = ar._AppellationResolutionResponse(appellations=[
        ar._AppellationVerdict(
            raw_label="你俩", identity="辰南", evidence="原文根本没有这句话",
            segment_indexes=[1],
        ),
    ])
    verified = ar._verified_verdicts(
        response, candidates={"辰南"}, source_text="",
        segments=_seg("天色渐暗，四周无人说话。"), valid_segment_indexes={1},
    )
    assert len(verified) == 1
    assert verified[0].identity == ar.UNRESOLVED
    assert verified[0].segment_indexes == [1]


# ---------------------------------------------------------------------------
# 端到端：resolve_narration_appellations 合并后 functional_extras 不含
# 无依据的条目
# ---------------------------------------------------------------------------

def _make_conn():
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE character_portraits(id TEXT, project_id TEXT, character_name TEXT, "
        "ep_start INTEGER, ep_end INTEGER)"
    )
    conn.commit()
    return conn


def test_resolve_narration_appellations_drops_unfounded_pronoun_claim(monkeypatch):
    """生产第2集缺陷端到端复现：模型对「我」「你俩」「有人」这类代词/称谓申报了
    unresolved，但声明的段落原文里根本没有这个字面——合并后 functional_extras
    不应该出现这些条目。"""
    from app.schemas import Bible, Character, World
    from app.source_excerpt import index_source_segments

    conn = _make_conn()
    bible = Bible(
        characters=[Character(name="辰南", role="主角", appearance_canonical="占位外观")],
        world=World(visual_style_canonical="测试画风"),
    )
    source_text = (
        "山道上尘土飞扬，商队缓缓前行。\n\n"
        "天色渐暗，众人停下歇息，四周一片寂静。\n\n"
        "远处山峦起伏，看不出有任何异样。"
    )

    async def fake_call(*, dossier, candidates, episode_id, project_id):
        seg = dossier[0]["segment_index"]
        return ar._AppellationResolutionResponse(appellations=[
            ar._AppellationVerdict(raw_label="我", identity=ar.UNRESOLVED, segment_indexes=[seg]),
        ])

    monkeypatch.setattr(ar, "_appellation_resolution_call", fake_call)
    segments = index_source_segments(source_text)
    characters: dict = {}
    functional_extras: dict = {}
    rows: list = []
    asyncio.run(ar.resolve_narration_appellations(
        conn, project_id="p1", episode_id="ep1", episode_no=1,
        source_text=source_text, bible=bible, segments=segments,
        characters=characters, functional_extras=functional_extras, character_appellation_rows=rows,
    ))
    assert characters == {}
    assert functional_extras == {}
