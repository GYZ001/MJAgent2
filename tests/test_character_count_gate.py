"""画面人数与身份闸门：``app.media_exec.character_count_gate``（抽帧复用
``subtitle_gate.sample_frames`` + VLM 逐帧人形提名 + 代码核验人数超额/角色重复）。

判据从数据推导：允许人数 = ``resources.characters`` 里 ``visibility=visible``
的条目数 + ``resources.flashback_figures`` 条目数；段内有群演/crowd 这类无限定
人数的条目时人数上限不可判定，只留痕不拦。两条核验都要求连续两个抽样帧命中
才成立——单帧因遮挡/转场误识别是已知的 VLM 噪声模式，误拦一次就是一次视频
额度，与 ``subtitle_gate``「未判定不拦」同一取舍。
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app.media_exec import character_count_gate as gate


def _frame(index: int, matched_names: list[str]) -> dict:
    return {
        "index": index,
        "figures": [
            {"figure_no": i + 1, "desc": "一句话特征", "matched_name": name}
            for i, name in enumerate(matched_names)
        ],
    }


# ---------------------------------------------------------------------------
# parse_verdict：结构化 / 片段级兜底
# ---------------------------------------------------------------------------

def test_parse_verdict_reads_structured_frames_with_nested_figures() -> None:
    raw = json.dumps({"frames": [
        {"index": 1, "figures": [
            {"figure_no": 1, "desc": "20多岁女性，长发", "matched_name": "温念"},
            {"figure_no": 2, "desc": "20多岁男性，背影", "matched_name": "顾屿"},
        ]},
        {"index": 2, "figures": []},
    ]}, ensure_ascii=False)
    verdict = gate.parse_verdict(raw, frames_checked=2)
    assert verdict["checked"] is True and verdict["frames_checked"] == 2 and verdict["frames_reported"] == 2
    assert verdict["frames"][0]["figures"] == [
        {"figure_no": 1, "desc": "20多岁女性，长发", "matched_name": "温念"},
        {"figure_no": 2, "desc": "20多岁男性，背影", "matched_name": "顾屿"},
    ]
    assert verdict["frames"][1]["figures"] == []


@pytest.mark.parametrize("raw", ["", "没有 JSON", '{"verdict": "ok"}', '{"frames": "yes"}'])
def test_parse_verdict_rejects_unparseable_answers(raw: str) -> None:
    with pytest.raises(ValueError):
        gate.parse_verdict(raw, frames_checked=2)


def test_parse_verdict_rejects_when_structure_valid_but_no_frames_readable() -> None:
    """整体 JSON 合法但 frames 是空数组——没有任何证据，不编「无人」，判未判定。"""
    with pytest.raises(ValueError):
        gate.parse_verdict(json.dumps({"frames": []}), frames_checked=3)


def _frame_json(index: int, matched_name: str) -> str:
    return (
        f'{{"index": {index}, "figures": [{{"figure_no": 1, "desc": "", '
        f'"matched_name": "{matched_name}"}}]}}'
    )


def test_fragment_fallback_survives_corrupted_frame_with_nested_figures() -> None:
    """整体 JSON 因某一帧键名坏掉而 json.loads 失败，但其它帧（含嵌套 figures）
    各自局部合法——花括号配平扫描器应该只丢掉坏的那一帧，其余照常读出。"""
    broken = '{"\n  \t: 1, "figures": [{"matched_name": "顾屿"}]}'
    raw = "{\"frames\": [" + broken + "," + _frame_json(2, "温念") + "," + _frame_json(3, "顾屿") + "]}"
    verdict = gate.parse_verdict(raw, frames_checked=3)
    assert verdict["frames_reported"] == 2
    assert [f["index"] for f in verdict["frames"]] == [2, 3]


def test_fragment_fallback_survives_extra_closing_bracket_after_frames_array() -> None:
    raw = "{\"frames\":[" + _frame_json(1, "温念") + "," + _frame_json(2, "顾屿") + "] ]\n}"
    verdict = gate.parse_verdict(raw, frames_checked=2)
    assert verdict["frames_reported"] == 2


# ---------------------------------------------------------------------------
# evaluate_headcount_and_duplication：连续两帧才判定
# ---------------------------------------------------------------------------

def test_headcount_exceeded_not_flagged_on_single_frame() -> None:
    parsed = {"frames": [_frame(1, ["温念", "顾屿", "路人"]), _frame(2, ["温念", "顾屿"])]}
    result = gate.evaluate_headcount_and_duplication(parsed, allowed_headcount=2, roster_names=["温念", "顾屿"])
    assert result["headcount_exceeded"] is False and result["headcount_evidence"] == []


def test_headcount_exceeded_flagged_on_two_consecutive_frames() -> None:
    parsed = {"frames": [
        _frame(1, ["温念", "顾屿", "路人"]), _frame(2, ["温念", "顾屿", "路人"]), _frame(3, ["温念", "顾屿"]),
    ]}
    result = gate.evaluate_headcount_and_duplication(parsed, allowed_headcount=2, roster_names=["温念", "顾屿"])
    assert result["headcount_exceeded"] is True
    assert [e["index"] for e in result["headcount_evidence"]] == [1, 2]
    assert all(e["headcount"] == 3 for e in result["headcount_evidence"])


def test_headcount_exceeded_requires_real_index_adjacency_not_list_position() -> None:
    """index=3 缺失（模型漏报）：报出的 2、4 两帧都超额，但中间缺失一帧，不算连续。"""
    parsed = {"frames": [
        _frame(1, ["温念", "顾屿"]), _frame(2, ["温念", "顾屿", "路人"]), _frame(4, ["温念", "顾屿", "路人"]),
    ]}
    result = gate.evaluate_headcount_and_duplication(parsed, allowed_headcount=2, roster_names=["温念", "顾屿"])
    assert result["headcount_exceeded"] is False


def test_headcount_exceeded_undetermined_when_allowed_is_none() -> None:
    """crowd/extra 存在时人数上限结构上不可判定，调用方传 None，本函数不判该条。"""
    parsed = {"frames": [
        _frame(1, ["温念", "顾屿", "路人"]), _frame(2, ["温念", "顾屿", "路人"]),
    ]}
    result = gate.evaluate_headcount_and_duplication(parsed, allowed_headcount=None, roster_names=["温念", "顾屿"])
    assert result["headcount_exceeded"] is False and result["headcount_evidence"] == []


def test_character_duplicated_not_flagged_on_single_frame() -> None:
    parsed = {"frames": [_frame(1, ["顾屿", "顾屿"]), _frame(2, ["温念", "顾屿"])]}
    result = gate.evaluate_headcount_and_duplication(parsed, allowed_headcount=5, roster_names=["温念", "顾屿"])
    assert result["character_duplicated"] is False and result["duplicated_characters"] == []


def test_character_duplicated_flagged_on_two_consecutive_frames() -> None:
    parsed = {"frames": [_frame(1, ["顾屿", "顾屿"]), _frame(2, ["顾屿", "顾屿", "温念"])]}
    result = gate.evaluate_headcount_and_duplication(parsed, allowed_headcount=5, roster_names=["温念", "顾屿"])
    assert result["character_duplicated"] is True
    assert len(result["duplicated_characters"]) == 1
    entry = result["duplicated_characters"][0]
    assert entry["name"] == "顾屿"
    assert [f["index"] for f in entry["frames"]] == [1, 2]


def test_unmatched_figures_never_count_toward_duplication() -> None:
    """VLM 报「无法对应」不在角色名单里，不算重复——只信登记在场的具名角色。"""
    parsed = {"frames": [_frame(1, ["顾屿", "无法对应"]), _frame(2, ["顾屿", "无法对应"])]}
    result = gate.evaluate_headcount_and_duplication(parsed, allowed_headcount=5, roster_names=["温念", "顾屿"])
    assert result["character_duplicated"] is False


# ---------------------------------------------------------------------------
# character_roster_for_shot：允许人数推导（闪回计入、群演无限定跳过）
# ---------------------------------------------------------------------------

class _Row(dict):
    """最小 sqlite Row 替身：``row["col"]`` 取值，``None`` 行为与真实游标一致。"""


class _FakeConn:
    def __init__(self, shot_contract_json: str | None, portraits: dict[str, str] | None = None) -> None:
        self._shot_contract_json = shot_contract_json
        self._portraits = portraits or {}

    def execute(self, sql: str, params: tuple = ()):
        if "shot_contract_json" in sql:
            row = _Row(shot_contract_json=self._shot_contract_json) if self._shot_contract_json is not None else None
            return _FakeCursor(row)
        if "character_portraits" in sql:
            appearance = self._portraits.get(params[0])
            return _FakeCursor(_Row(appearance=appearance) if appearance is not None else None)
        raise AssertionError(f"unexpected sql: {sql}")


class _FakeCursor:
    def __init__(self, row) -> None:
        self._row = row

    def fetchone(self):
        return self._row


def _segment_json(characters: list[dict], flashback: list[dict] | None = None) -> str:
    return json.dumps({
        "storyboard_pack_segment": {"resources": {"characters": characters, "flashback_figures": flashback or []}},
    }, ensure_ascii=False)


def test_roster_allowed_headcount_counts_visible_characters_and_flashback_figures(monkeypatch) -> None:
    characters = [
        {"display_name": "温念", "visibility": "visible", "subject_kind": "character", "portrait_id": "p1"},
        {"display_name": "顾屿", "visibility": "visible", "subject_kind": "character", "description": "20多岁男性"},
        {"display_name": "路人", "visibility": "voice_only", "subject_kind": "character"},
    ]
    flashback = [{"label": "六岁的顾屿", "description": "……"}]
    conn = _FakeConn(_segment_json(characters, flashback), portraits={"p1": "20多岁女性，长发"})
    monkeypatch.setattr(gate, "get_conn", lambda: conn)
    result = gate.character_roster_for_shot("shot_1", "")
    assert result["allowed_headcount"] == 3  # 2 个可见角色 + 1 个闪回人物
    assert result["unlimited_reason"] == ""
    assert result["roster"] == [
        {"name": "温念", "appearance": "20多岁女性，长发"},
        {"name": "顾屿", "appearance": "20多岁男性"},
    ]


def test_roster_extra_subject_kind_counts_as_one_not_unlimited(monkeypatch) -> None:
    """extra=「独立无名人物」，人数确定是 1，照常计入；只有 crowd（复数人群）才无限定。"""
    characters = [
        {"display_name": "温念", "visibility": "visible", "subject_kind": "character", "portrait_id": "p1"},
        {"display_name": "路人甲", "visibility": "visible", "subject_kind": "extra", "description": "40多岁男性"},
    ]
    conn = _FakeConn(_segment_json(characters), portraits={"p1": "20多岁女性"})
    monkeypatch.setattr(gate, "get_conn", lambda: conn)
    result = gate.character_roster_for_shot("shot_1", "")
    assert result["allowed_headcount"] == 2
    assert result["unlimited_reason"] == ""


def test_roster_undetermined_when_crowd_subject_kind_present(monkeypatch) -> None:
    characters = [
        {"display_name": "温念", "visibility": "visible", "subject_kind": "character", "portrait_id": "p1"},
        {"display_name": "赶集人群", "visibility": "visible", "subject_kind": "crowd"},
    ]
    conn = _FakeConn(_segment_json(characters), portraits={"p1": "20多岁女性"})
    monkeypatch.setattr(gate, "get_conn", lambda: conn)
    result = gate.character_roster_for_shot("shot_1", "")
    assert result["allowed_headcount"] is None
    assert result["unlimited_reason"] and "不可判定" in result["unlimited_reason"]
    # crowd 的 display_name 是共享 label，不是具名个体，不进 roster——否则多个不同
    # 背景人形被 VLM 正确匹配到同一个 label 会被误判成 character_duplicated。
    assert result["roster"] == [{"name": "温念", "appearance": "20多岁女性"}]


def test_crowd_label_matched_twice_is_not_character_duplicated() -> None:
    """群演 label 不进 roster_names：即使 VLM 把两个不同背景人形都标成「赶集人群」
    （对群演而言是正确匹配，不是同一角色出镜两次），也不得判 character_duplicated。
    """
    parsed = {"frames": [
        _frame(1, ["温念", "赶集人群", "赶集人群"]),
        _frame(2, ["温念", "赶集人群", "赶集人群"]),
    ]}
    judged = gate.evaluate_headcount_and_duplication(parsed, allowed_headcount=None, roster_names=["温念"])
    assert judged["character_duplicated"] is False
    assert judged["duplicated_characters"] == []


def test_roster_appearance_falls_back_to_prompt_text_anchor(monkeypatch) -> None:
    characters = [{"display_name": "顾屿", "visibility": "visible", "subject_kind": "character"}]
    conn = _FakeConn(_segment_json(characters))
    monkeypatch.setattr(gate, "get_conn", lambda: conn)
    prompt_text = "镜头1：@顾屿 的外观：20多岁男性，短发，黑色外套。他推开门走进来。"
    result = gate.character_roster_for_shot("shot_1", prompt_text)
    assert result["roster"] == [{"name": "顾屿", "appearance": "20多岁男性，短发，黑色外套"}]


def test_roster_appearance_stays_empty_when_no_source_available(monkeypatch) -> None:
    """人物卡外观、段落 description、prompt_text 锚点都没有时如实留空，不编造。"""
    characters = [{"display_name": "顾屿", "visibility": "visible", "subject_kind": "character"}]
    conn = _FakeConn(_segment_json(characters))
    monkeypatch.setattr(gate, "get_conn", lambda: conn)
    result = gate.character_roster_for_shot("shot_1", "没有提到这个角色外观的文本。")
    assert result["roster"] == [{"name": "顾屿", "appearance": ""}]


# ---------------------------------------------------------------------------
# evaluate_version：永不抛出，结论落 qa_json
# ---------------------------------------------------------------------------

def test_evaluate_version_skips_when_disabled(monkeypatch) -> None:
    monkeypatch.setattr(gate, "enabled", lambda: False)

    def boom(*_a, **_k):
        raise AssertionError("闸门关闭时不该做任何事")

    monkeypatch.setattr(gate, "character_roster_for_shot", boom)
    monkeypatch.setattr(gate, "detect_character_count", boom)
    result = asyncio.run(gate.evaluate_version({"shot_id": "s"}, {"id": "v"}, "/tmp/v.mp4"))
    assert result is None


def test_evaluate_version_skips_when_no_roster_registered(monkeypatch) -> None:
    written: list[tuple[str, dict]] = []
    monkeypatch.setattr(gate, "enabled", lambda: True)
    monkeypatch.setattr(gate, "character_roster_for_shot", lambda shot_id, prompt: {"roster": [], "allowed_headcount": 0, "unlimited_reason": ""})
    monkeypatch.setattr(gate, "detect_character_count", lambda *a, **k: (_ for _ in ()).throw(AssertionError("不该调用 VLM")))
    monkeypatch.setattr(gate, "write_verdict", lambda vid, verdict: written.append((vid, verdict)))
    result = asyncio.run(gate.evaluate_version({"shot_id": "s", "project_id": "p"}, {"id": "v1"}, "/tmp/v.mp4"))
    assert result["checked"] is False and "没有登记" in result["reason"]
    assert written == [("v1", result)]


def test_evaluate_version_unchecked_when_vlm_call_fails(monkeypatch, caplog) -> None:
    written: list[tuple[str, dict]] = []
    monkeypatch.setattr(gate, "enabled", lambda: True)
    monkeypatch.setattr(
        gate, "character_roster_for_shot",
        lambda shot_id, prompt: {"roster": [{"name": "顾屿", "appearance": ""}], "allowed_headcount": 1, "unlimited_reason": ""},
    )

    async def boom(*_a, **_k):
        raise TimeoutError("VLM 读超时")

    monkeypatch.setattr(gate, "detect_character_count", boom)
    monkeypatch.setattr(gate, "write_verdict", lambda vid, verdict: written.append((vid, verdict)))
    with caplog.at_level("WARNING"):
        result = asyncio.run(gate.evaluate_version({"shot_id": "s", "project_id": "p"}, {"id": "v1"}, "/tmp/v.mp4"))
    assert result["checked"] is False and "TimeoutError" in result["error"]
    assert written == [("v1", result)]
    assert any("[VIDEO_CHARACTER_COUNT_GATE][未判定]" in rec.getMessage() for rec in caplog.records)


def test_evaluate_version_writes_headcount_exceeded_verdict(monkeypatch) -> None:
    written: list[tuple[str, dict]] = []
    monkeypatch.setattr(gate, "enabled", lambda: True)
    roster = [{"name": "温念", "appearance": ""}, {"name": "顾屿", "appearance": ""}]
    monkeypatch.setattr(
        gate, "character_roster_for_shot",
        lambda shot_id, prompt: {"roster": roster, "allowed_headcount": 2, "unlimited_reason": ""},
    )

    async def detect(video_path, roster_arg, *, call_meta):
        assert video_path == "/tmp/v1.mp4" and call_meta["version_id"] == "v1"
        return {
            "checked": True, "frames_checked": 3, "frames_reported": 3,
            "frames": [_frame(1, ["温念", "顾屿", "顾屿"]), _frame(2, ["温念", "顾屿", "顾屿"])],
        }

    monkeypatch.setattr(gate, "detect_character_count", detect)
    monkeypatch.setattr(gate, "write_verdict", lambda vid, verdict: written.append((vid, verdict)))
    result = asyncio.run(gate.evaluate_version({"shot_id": "s", "project_id": "p"}, {"id": "v1"}, "/tmp/v1.mp4"))
    assert result["headcount_exceeded"] is True
    assert result["character_duplicated"] is True
    assert result["allowed_headcount"] == 2 and result["roster_names"] == ["温念", "顾屿"]
    assert written == [("v1", result)]


def test_evaluate_version_unchecked_when_roster_lookup_raises(monkeypatch, caplog) -> None:
    """角色名单推导（读 shots.shot_contract_json）撞 DB 忙锁/脏数据时，evaluate_version
    必须仍然「永不抛出」，按未判定放行并留痕——不能让整条视频生成流水线被打断。
    """
    written: list[tuple[str, dict]] = []
    monkeypatch.setattr(gate, "enabled", lambda: True)

    def boom(shot_id, prompt):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(gate, "character_roster_for_shot", boom)
    monkeypatch.setattr(gate, "detect_character_count", lambda *a, **k: (_ for _ in ()).throw(AssertionError("不该调用 VLM")))
    monkeypatch.setattr(gate, "write_verdict", lambda vid, verdict: written.append((vid, verdict)))
    with caplog.at_level("WARNING"):
        result = asyncio.run(gate.evaluate_version({"shot_id": "s", "project_id": "p"}, {"id": "v1"}, "/tmp/v.mp4"))
    assert result["checked"] is False and "database is locked" in result["error"]
    assert written == [("v1", result)]
    assert any("[VIDEO_CHARACTER_COUNT_GATE][未判定]" in rec.getMessage() for rec in caplog.records)


def test_evaluate_version_unchecked_when_frames_truncated_and_no_violation_found(monkeypatch) -> None:
    """模型因 max_tokens 截断只报了 12 帧里的 2 帧、且这 2 帧没有触发连续命中：
    不能被当成「检查完了、没问题」，必须判未判定（同 subtitle_gate 的完整性判据）。
    """
    written: list[tuple[str, dict]] = []
    monkeypatch.setattr(gate, "enabled", lambda: True)
    roster = [{"name": "温念", "appearance": ""}, {"name": "顾屿", "appearance": ""}]
    monkeypatch.setattr(
        gate, "character_roster_for_shot",
        lambda shot_id, prompt: {"roster": roster, "allowed_headcount": 2, "unlimited_reason": ""},
    )

    async def detect(video_path, roster_arg, *, call_meta):
        return {
            "checked": True, "frames_checked": 12, "frames_reported": 2,
            "frames": [_frame(1, ["温念", "顾屿"]), _frame(2, ["温念", "顾屿"])],
        }

    monkeypatch.setattr(gate, "detect_character_count", detect)
    monkeypatch.setattr(gate, "write_verdict", lambda vid, verdict: written.append((vid, verdict)))
    result = asyncio.run(gate.evaluate_version({"shot_id": "s", "project_id": "p"}, {"id": "v1"}, "/tmp/v.mp4"))
    assert result["checked"] is False
    assert "2/12" in result["error"]
    assert written == [("v1", result)]


def test_evaluate_version_keeps_violation_even_when_frames_truncated(monkeypatch) -> None:
    """截断但已经命中连续两帧证据时，判定仍然成立——不因为漏读了别的帧就把已有
    的真实命中也一起判成未判定（同 subtitle_gate「命中即成立」的取舍）。
    """
    written: list[tuple[str, dict]] = []
    monkeypatch.setattr(gate, "enabled", lambda: True)
    roster = [{"name": "温念", "appearance": ""}, {"name": "顾屿", "appearance": ""}]
    monkeypatch.setattr(
        gate, "character_roster_for_shot",
        lambda shot_id, prompt: {"roster": roster, "allowed_headcount": 2, "unlimited_reason": ""},
    )

    async def detect(video_path, roster_arg, *, call_meta):
        return {
            "checked": True, "frames_checked": 12, "frames_reported": 2,
            "frames": [_frame(1, ["温念", "顾屿", "顾屿"]), _frame(2, ["温念", "顾屿", "顾屿"])],
        }

    monkeypatch.setattr(gate, "detect_character_count", detect)
    monkeypatch.setattr(gate, "write_verdict", lambda vid, verdict: written.append((vid, verdict)))
    result = asyncio.run(gate.evaluate_version({"shot_id": "s", "project_id": "p"}, {"id": "v1"}, "/tmp/v.mp4"))
    assert result["checked"] is True
    assert result["headcount_exceeded"] is True
    assert written == [("v1", result)]
