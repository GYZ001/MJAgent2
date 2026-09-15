"""称谓归属的证据用引文定位原语核验（2026-09-14 第 13 集「大汉→曹阳」被跨段换行+自补引号打成 unresolved）。
从 test_identity_appellation_resolution.py 拆出：那个文件已到 500 行上限。
"""
from __future__ import annotations

from app.production.prep_pack import appellation_resolve as ar


def test_evidence_crossing_a_paragraph_break_with_self_closed_quote_still_passes():
    """2026-09-14 第 13 集：模型判「大汉→曹阳」，证据跨了一个段落换行并自补了收尾引号——
    引用格式差异不是改写，按 _prep_pack_locate_phrase 定位通过，落库 evidence 取原文真实形态。"""
    from types import SimpleNamespace
    seg1 = "许师姐是一张很大的虎皮。看到山下有一个大汉，正迈步临近公开区。"
    seg2 = "“是曹阳……此人凝气二层巅峰，半只脚迈入三层。”"
    segments = [SimpleNamespace(text=seg1), SimpleNamespace(text=seg2)]
    response = ar._AppellationResolutionResponse(appellations=[
        ar._AppellationVerdict(
            raw_label="大汉", identity="曹阳",
            evidence="看到山下有一个大汉，正迈步临近公开区。“是曹阳……”", segment_indexes=[1],
        ),
        ar._AppellationVerdict(raw_label="那人", identity="曹阳", evidence="原文没有这一句", segment_indexes=[1]),
    ])
    verified = ar._verified_verdicts(
        response, candidates={"曹阳"}, source_text=seg1 + "\n" + seg2,
        valid_segment_indexes={1, 2}, segments=segments,
    )
    assert [v.identity for v in verified] == ["曹阳", ar.UNRESOLVED]
    assert verified[0].evidence == "看到山下有一个大汉，正迈步临近公开区。“是曹阳"


def test_stage_direction_context_plus_dialogue_with_ellipsis_is_located() -> None:
    """2026-09-15《龙猫出爪》：模型引「（小李走过来…桌角。）」时丢了舞台提示括号，又以「……」收尾；
    拆句后不能掉出只剩「…」的碎片，否则整条被拒、「周医生」落成群演。"""
    from types import SimpleNamespace
    from app.production.prep_pack.provenance import _prep_pack_locate_phrase

    segments = [
        SimpleNamespace(text="（前台只开一盏台灯。周晚坐在桌前。）"),
        SimpleNamespace(text="（小李走过来，把一张叠好的纸放在桌角。）\n小李：周医生，我下个月……\n周晚：嗯？"),
    ]
    located, phrase = _prep_pack_locate_phrase(segments, "小李走过来，把一张叠好的纸放在桌角。小李：周医生，我下个月……")
    assert located == [2]
    assert phrase in segments[1].text
