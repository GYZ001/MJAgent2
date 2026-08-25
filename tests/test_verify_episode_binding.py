"""scripts/verify_episode_binding.py 出场判据回归测试。

背景：旧判据（character_appears_in_source）把"角色规范名/已确认别名逐字出
现在原文里"当成"角色在场"，但原文里角色经常被第三方提及而本人并不在场
（"提及 != 在场"）。2026-08-25 一轮定点验证里，这条旧判据在 EP8 许清、
EP8 李富贵、EP5 赵武刚三处把"被提及"误判成"在场"，把正确的"不出场故不绑
定"错误报成 FAIL。

新判据（character_presence_verdict）改用 screenplay_identity_discovery 产物
里 candidates[].kind 字段（onscreen/mentioned，身份判定阶段模型对"是否在画
面内出现"的直接判断，见 app/portraits.py::discover_character_candidates），
以 authority_id=="bible:{角色规范名}" 匹配；零证据时用"原文完全未提及"这个
安全方向的信号兜底判绝对缺席，否则报"unknown"（无法判定，不猜）。

本文件全部用构造数据（inline dict / 内存 sqlite），不连接 data/manju.db，
也不含任何具体人名的特判——参数化用例横跨多个虚构角色名，证明判据本身不依
赖名单。"""
from __future__ import annotations

import json
import sqlite3

import pytest

from scripts.verify_episode_binding import (
    character_appears_in_source,
    character_presence_verdict,
    load_identity_discovery_candidates,
)


def _candidate(name: str, kind: str, authority_id: str = "") -> dict:
    """构造一条 screenplay_identity_discovery candidates[] 条目，只保留判据用
    到的字段（kind / authority_id），其余字段（evidence、source_label 等）判
    据不读，测试里不需要。"""
    return {"name": name, "kind": kind, "authority_id": authority_id}


# --- character_presence_verdict：核心判据单元测试（无数据库、无网络） --------


@pytest.mark.parametrize("name", ["许清", "李富贵", "赵武刚", "随便谁"])
def test_onscreen_candidate_is_present(name: str) -> None:
    """任一条目 kind=onscreen -> present，与角色名无关（不是名单特判）。"""
    candidates = [_candidate(name, "onscreen", f"bible:{name}")]
    state, basis = character_presence_verdict(name, candidates, legacy_appears=True)
    assert state == "present"
    assert "onscreen" in basis


@pytest.mark.parametrize("name", ["许清", "李富贵", "赵武刚", "随便谁"])
def test_mentioned_only_candidate_is_absent(name: str) -> None:
    """有候选条目但全部是 mentioned -> absent，不看 legacy_appears（结构化信
    号已经给出明确判定，legacy 结果无论真假都不应改变这里的结论）。"""
    candidates = [_candidate(name, "mentioned", f"bible:{name}")]
    for legacy in (True, False):
        state, basis = character_presence_verdict(name, candidates, legacy_appears=legacy)
        assert state == "absent"
        assert "mentioned" in basis


def test_mixed_onscreen_and_mentioned_prefers_present() -> None:
    """同一 authority_id 既有 mentioned 又有 onscreen 条目时（模型对不同段落
    分别给出的候选），onscreen 优先——只要有一次被判定确实在场，就不能因为
    其它段落只是提及而否定它。"""
    candidates = [
        _candidate("许清", "mentioned", "bible:许清"),
        _candidate("许清", "onscreen", "bible:许清"),
    ]
    state, _basis = character_presence_verdict("许清", candidates, legacy_appears=True)
    assert state == "present"


def test_unrelated_authority_id_is_not_matched() -> None:
    """候选列表里有条目，但 authority_id 对不上（属于别的角色）、或是功能性
    候选（authority_id 为空串，未归入人物谱）——都不能被当成"找到了候选"，
    必须落入零证据分支。"""
    candidates = [
        _candidate("上官修", "onscreen", "bible:上官修"),
        _candidate("围观弟子", "mentioned", ""),
    ]
    state, basis = character_presence_verdict("许清", candidates, legacy_appears=False)
    assert state == "absent"
    assert "未出现" in basis or "两个独立信号" in basis


def test_no_candidate_and_no_legacy_match_is_absent() -> None:
    """零证据兜底、安全方向：authority_id 完全没有候选条目，且原文里规范
    名/已确认别名也完全没出现——两个独立信号都指向缺席，判 absent。这与旧判
    据被推翻的方向相反：旧判据的错误是"提到名字就当在场"（假阳性方向），这
    里用的是"两种独立方法都找不到任何提及，判定未出场"（假阴性风险低）。"""
    state, basis = character_presence_verdict("许清", [], legacy_appears=False)
    assert state == "absent"
    assert "两个独立信号都指向缺席" in basis


def test_no_candidate_but_legacy_match_is_unknown() -> None:
    """authority_id 没有候选条目，但原文里其实出现过规范名/别名——结构化信
    号和文本信号矛盾（识别管线没有消化这次出现），这才是真正"信号不足"，必
    须报 unknown，不能猜是 present 还是 absent。"""
    state, basis = character_presence_verdict("许清", [], legacy_appears=True)
    assert state == "unknown"
    assert "矛盾" in basis or "无法判断" in basis


def test_candidates_none_and_no_legacy_match_is_absent() -> None:
    """本集根本没有 screenplay_identity_discovery 产物（candidates=None）时，
    同样先看 legacy_appears 兜底：原文也完全没提到 -> absent。"""
    state, basis = character_presence_verdict("许清", None, legacy_appears=False)
    assert state == "absent"
    assert "无 screenplay_identity_discovery 产物" in basis


def test_candidates_none_but_legacy_match_is_unknown() -> None:
    """本集没有该产物，但原文确实提到了角色——没有结构化信号可用，报
    unknown，不回退猜测。"""
    state, basis = character_presence_verdict("许清", None, legacy_appears=True)
    assert state == "unknown"
    assert "无 screenplay_identity_discovery 产物" in basis


# --- 真实误报案例回归（用真实数据形状构造，但不连接数据库） -------------------
#
# 下面四个用例的候选数据形状抄自 2026-08-25 定点验证时从 data/manju.db 里
# screenplay_identity_discovery 产物实际读出的记录（EP5/EP8，project
# proj_3ac0b627fa46），只保留判据用到的字段。四条断言合起来就是这次修复要
# 保证的回归基线："许清"在 EP5 baseline 里对应的 authority_id=bible:许清 词
# 条含 kind=onscreen（因为身份判定阶段已经把未登记的场内称谓"许姓女子"吸收
# 归并进这个候选，即便原始别名"许师姐"本身在原文里只出现在别人的转述台词
# 里），EP8 的同名候选只有 kind=mentioned。


def test_regression_true_positive_ep5_xu_qing_present() -> None:
    """EP5 许清：确定性信号必须仍然判定在场（FAIL 分支的前提）。她本人的动
    作在原文里是以未登记的场内称谓"许姓女子"呈现的（"其旁穿着银袍的许姓女子
    自然也是一愣""许姓女子沉默片刻，从储物袋内…"），但身份判定阶段已经把这
    个称谓归并进了 authority_id=bible:许清 这个候选、并标了 kind=onscreen——
    这正是新判据要读的信号，不依赖对"许姓女子"和"许清"做任何字符串层面的猜
    测关联。"""
    candidates = [
        _candidate("上官修", "onscreen", "bible:上官修"),
        _candidate("孟浩", "onscreen", "bible:孟浩"),
        _candidate("许清", "onscreen", "bible:许清"),  # 吸收了"许姓女子"后的候选
        _candidate("赵武刚", "mentioned", "bible:赵武刚"),
    ]
    state, _basis = character_presence_verdict("许清", candidates, legacy_appears=True)
    assert state == "present"


def test_regression_false_positive_ep8_xu_qing_absent() -> None:
    """EP8 许清：8 次"许师姐"全部是缺席引用（"许师姐的余荫""许师姐之居""忌
    惮许师姐"……），她本人整集没有出场。身份判定阶段对 authority_id=bible:许
    清 只给出 kind=mentioned，新判据必须判 absent，不能像旧判据那样因为原文
    literal 命中"许师姐"就误判为在场。"""
    candidates = [
        _candidate("孟浩", "onscreen", "bible:孟浩"),
        _candidate("赵武刚", "onscreen", "bible:赵武刚"),
        _candidate("许清", "mentioned", "bible:许清"),
    ]
    state, _basis = character_presence_verdict("许清", candidates, legacy_appears=True)
    assert state == "absent"


def test_regression_false_positive_ep8_li_fugui_absent() -> None:
    """EP8 李富贵：唯一一次"小胖子"是孟浩自言自语"要找个机会在小胖子面前施
    展一下"——人不在场。"""
    candidates = [
        _candidate("孟浩", "onscreen", "bible:孟浩"),
        _candidate("李富贵", "mentioned", "bible:李富贵"),
    ]
    state, _basis = character_presence_verdict("李富贵", candidates, legacy_appears=True)
    assert state == "absent"


def test_regression_false_positive_ep5_zhao_wugang_absent() -> None:
    """EP5 赵武刚：唯一一次是路人转述往事（"被没抢到丹药的赵武刚师兄泄愤生
    生拽入公开区内砍了脑袋"）——人不在场，只是被当作反面典故提起。"""
    candidates = [
        _candidate("孟浩", "onscreen", "bible:孟浩"),
        _candidate("许清", "onscreen", "bible:许清"),
        _candidate("赵武刚", "mentioned", "bible:赵武刚"),
    ]
    state, _basis = character_presence_verdict("赵武刚", candidates, legacy_appears=True)
    assert state == "absent"


# --- character_appears_in_source：旧启发式仍作为辅助信号保留，行为不变 -------


def test_legacy_heuristic_still_reports_literal_hits_for_auxiliary_logging() -> None:
    """旧启发式函数本身没有被删除或改行为——它降级为日志里的辅助参考，新判据
    的零证据兜底也复用它的布尔结果。这里锁定它原有的行为不被意外改动。"""
    appears, basis = character_appears_in_source("许清", ["许师姐"], "许师姐的洞府……")
    assert appears is True
    assert "许师姐" in basis

    appears, basis = character_appears_in_source("许清", ["许师姐"], "全文没有提到任何人")
    assert appears is False
    assert "未出现" in basis


# --- load_identity_discovery_candidates：SQL 选取逻辑（构造内存数据库） -----


def _make_artifacts_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE artifacts (id TEXT, type TEXT, scope_type TEXT, scope_id TEXT, "
        "version INTEGER, created_at REAL, stale_reason TEXT, "
        "superseded_by_artifact_id TEXT, content_json TEXT)"
    )
    return conn


def _insert_artifact(
    conn: sqlite3.Connection, *, id: str, scope_id: str, version: int,
    created_at: float, candidates: list[dict], stale_reason: str | None = None,
    superseded_by_artifact_id: str | None = None, content_json: str | None = None,
    scope_type: str = "episode", artifact_type: str = "screenplay_identity_discovery",
) -> None:
    payload = content_json if content_json is not None else json.dumps({"candidates": candidates})
    conn.execute(
        "INSERT INTO artifacts (id, type, scope_type, scope_id, version, created_at, "
        "stale_reason, superseded_by_artifact_id, content_json) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (id, artifact_type, scope_type, scope_id, version, created_at,
         stale_reason, superseded_by_artifact_id, payload),
    )


def test_load_returns_none_when_no_rows() -> None:
    conn = _make_artifacts_db()
    candidates, note = load_identity_discovery_candidates(conn, "ep_missing")
    assert candidates is None
    assert "无" in note


def test_load_ignores_other_episodes_and_other_artifact_types() -> None:
    conn = _make_artifacts_db()
    _insert_artifact(
        conn, id="a1", scope_id="ep_other", version=1, created_at=1.0,
        candidates=[_candidate("张三", "onscreen", "bible:张三")],
    )
    _insert_artifact(
        conn, id="a2", scope_id="ep_target", version=1, created_at=1.0,
        candidates=[_candidate("李四", "onscreen", "bible:李四")],
        artifact_type="episode_prep_pack",
    )
    candidates, _note = load_identity_discovery_candidates(conn, "ep_target")
    assert candidates is None


def test_load_picks_highest_version() -> None:
    """一集有多条候选产物（同一次生成内部多轮判定）时取 version 最大的一
    批，与 EP5 现存数据的真实形状一致（version=1/2，后者是修订版）。"""
    conn = _make_artifacts_db()
    _insert_artifact(
        conn, id="a_v1", scope_id="ep_x", version=1, created_at=100.0,
        candidates=[_candidate("许清", "mentioned", "bible:许清")],
    )
    _insert_artifact(
        conn, id="a_v2", scope_id="ep_x", version=2, created_at=200.0,
        candidates=[_candidate("许清", "onscreen", "bible:许清")],
    )
    candidates, note = load_identity_discovery_candidates(conn, "ep_x")
    assert candidates == [_candidate("许清", "onscreen", "bible:许清")]
    assert "a_v2" in note
    assert "a_v1" not in note


def test_load_excludes_stale_and_superseded_rows() -> None:
    conn = _make_artifacts_db()
    _insert_artifact(
        conn, id="a_stale", scope_id="ep_x", version=2, created_at=200.0,
        candidates=[_candidate("许清", "onscreen", "bible:许清")],
        stale_reason="regenerated",
    )
    _insert_artifact(
        conn, id="a_superseded", scope_id="ep_x", version=1, created_at=150.0,
        candidates=[_candidate("许清", "onscreen", "bible:许清")],
        superseded_by_artifact_id="a_stale",
    )
    _insert_artifact(
        conn, id="a_fresh", scope_id="ep_x", version=1, created_at=100.0,
        candidates=[_candidate("许清", "mentioned", "bible:许清")],
    )
    candidates, note = load_identity_discovery_candidates(conn, "ep_x")
    # 排除 stale/superseded 后只剩 a_fresh 一条，即便它的 version 比被排除的
    # 两条都小——"过期标记"优先于"版本号更大"。
    assert candidates == [_candidate("许清", "mentioned", "bible:许清")]
    assert "a_fresh" in note


def test_load_falls_back_to_full_pool_when_all_rows_are_stale() -> None:
    """极端情况：该集所有候选产物都被标记过期（理论上不该发生，但函数不应
    因此返回"无数据"去制造无谓的 unknown——退回未过滤前的全集，按 version 继
    续选。"""
    conn = _make_artifacts_db()
    _insert_artifact(
        conn, id="a_only", scope_id="ep_x", version=1, created_at=100.0,
        candidates=[_candidate("许清", "onscreen", "bible:许清")],
        stale_reason="regenerated",
    )
    candidates, _note = load_identity_discovery_candidates(conn, "ep_x")
    assert candidates == [_candidate("许清", "onscreen", "bible:许清")]


def test_load_skips_unparseable_json_but_keeps_others() -> None:
    conn = _make_artifacts_db()
    _insert_artifact(
        conn, id="a_bad", scope_id="ep_x", version=1, created_at=100.0,
        candidates=[], content_json="{not valid json",
    )
    _insert_artifact(
        conn, id="a_good", scope_id="ep_x", version=1, created_at=100.0,
        candidates=[_candidate("许清", "onscreen", "bible:许清")],
    )
    candidates, note = load_identity_discovery_candidates(conn, "ep_x")
    assert candidates == [_candidate("许清", "onscreen", "bible:许清")]
    assert "a_good" in note


def test_load_returns_none_when_all_json_unparseable() -> None:
    conn = _make_artifacts_db()
    _insert_artifact(
        conn, id="a_bad", scope_id="ep_x", version=1, created_at=100.0,
        candidates=[], content_json="{not valid json",
    )
    candidates, note = load_identity_discovery_candidates(conn, "ep_x")
    assert candidates is None
    assert "解析失败" in note
