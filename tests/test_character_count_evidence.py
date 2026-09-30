"""``app.evidence.character_count``：把画面人数与身份闸门的结论并进技术校验。

有人数超额/角色重复的候选与坏文件走同一条路：``passed=False`` + BLOCKER
issue，与 ``app.evidence.subtitle_overlay`` 同一形状。闸门没判定（关闭/失败/
本段无登记角色）时结果原样返回，不替它兜底。
"""
from __future__ import annotations

from app.evidence import character_count


def _technical(passed: bool = True) -> dict:
    return {"passed": passed, "issues": [], "evidence": {"duration_s": 15.0}}


def _headcount_verdict() -> dict:
    return {
        "checked": True, "headcount_exceeded": True, "character_duplicated": False,
        "duplicated_characters": [],
        "headcount_evidence": [
            {"index": 1, "seconds": 0.0, "headcount": 3},
            {"index": 2, "seconds": 1.5, "headcount": 3},
        ],
        "allowed_headcount": 2, "roster_names": ["温念", "顾屿"],
    }


def _duplicated_verdict() -> dict:
    return {
        "checked": True, "headcount_exceeded": False, "character_duplicated": True,
        "headcount_evidence": [],
        "duplicated_characters": [{
            "name": "顾屿",
            "frames": [{"index": 1, "seconds": 0.0}, {"index": 2, "seconds": 1.5}],
        }],
        "allowed_headcount": 2, "roster_names": ["温念", "顾屿"],
    }


# ---------------------------------------------------------------------------
# headcount_exceeded → BLOCKER + 中文 repair_hint
# ---------------------------------------------------------------------------

def test_headcount_exceeded_blocks_technically_valid_candidate() -> None:
    merged = character_count.technical_with_verdict(_technical(), {"character_count_gate": _headcount_verdict()})
    assert merged["passed"] is False
    assert [i.code for i in merged["issues"]] == [character_count.ISSUE_CODE_HEADCOUNT]
    issue = merged["issues"][0]
    assert "3 个人" in issue.message and "2 人" in issue.message and "温念" in issue.message and "顾屿" in issue.message
    assert "本次每个画面里出镜的人数不得超过" in issue.repair_hint
    assert issue.repairable is True
    assert merged["evidence"] == {"duration_s": 15.0}


# ---------------------------------------------------------------------------
# character_duplicated → BLOCKER + 中文 repair_hint，带秒数与角色名
# ---------------------------------------------------------------------------

def test_character_duplicated_blocks_technically_valid_candidate() -> None:
    merged = character_count.technical_with_verdict(_technical(), {"character_count_gate": _duplicated_verdict()})
    assert merged["passed"] is False
    assert [i.code for i in merged["issues"]] == [character_count.ISSUE_CODE_DUPLICATED]
    issue = merged["issues"][0]
    assert "顾屿" in issue.message and "出现了两次" in issue.message and "第 0.0 秒起" in issue.message
    assert "本次每个画面里每位角色只能出现一次" in issue.repair_hint
    assert issue.repairable is True


def test_both_conditions_produce_two_issues() -> None:
    both = {**_headcount_verdict(), **{
        "character_duplicated": True,
        "duplicated_characters": _duplicated_verdict()["duplicated_characters"],
    }}
    merged = character_count.technical_with_verdict(_technical(), {"character_count_gate": both})
    assert merged["passed"] is False
    codes = {i.code for i in merged["issues"]}
    assert codes == {character_count.ISSUE_CODE_HEADCOUNT, character_count.ISSUE_CODE_DUPLICATED}


# ---------------------------------------------------------------------------
# 未命中 / 未判定：不影响原技术校验
# ---------------------------------------------------------------------------

def _clean_verdict() -> dict:
    return {
        "checked": True, "headcount_exceeded": False, "character_duplicated": False,
        "headcount_evidence": [], "duplicated_characters": [],
        "allowed_headcount": 2, "roster_names": ["温念", "顾屿"],
    }


def test_clean_verdict_leaves_technical_untouched() -> None:
    technical = _technical()
    assert character_count.technical_with_verdict(technical, {"character_count_gate": _clean_verdict()}) is technical


def test_unchecked_verdicts_leave_technical_untouched() -> None:
    technical = _technical()
    for qa in [
        None, {}, {"character_count_gate": {"checked": False, "error": "TimeoutError: 读超时"}},
        {"character_count_gate": {"checked": False, "reason": "本段没有登记在场的可见角色，跳过画面人数与身份核验"}},
        {"character_count_gate": "garbage"},
    ]:
        assert character_count.technical_with_verdict(technical, qa) is technical


def test_headcount_undetermined_does_not_block_when_flag_is_false() -> None:
    """允许人数不可判定（群演场景）时 headcount_exceeded 本就是 False，不该被单独拦。"""
    verdict = {
        "checked": True, "headcount_exceeded": False, "character_duplicated": False,
        "headcount_evidence": [], "duplicated_characters": [],
        "allowed_headcount": None, "roster_names": ["温念"],
        "headcount_undetermined_reason": "本段登记了无限定人数的群演/crowd 条目，人数上限不可判定",
    }
    technical = _technical()
    assert character_count.technical_with_verdict(technical, {"character_count_gate": verdict}) is technical


def test_existing_technical_issues_are_preserved() -> None:
    technical = {"passed": False, "issues": ["文件损坏"], "evidence": {}}
    merged = character_count.technical_with_verdict(technical, {"character_count_gate": _headcount_verdict()})
    assert merged["issues"][0] == "文件损坏"
    assert merged["issues"][1].code == character_count.ISSUE_CODE_HEADCOUNT
