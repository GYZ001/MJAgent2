"""构造数据断言 scripts/audit_character_discovery.py 的四类判据各自能被检出。

覆盖：
  - 问题 1：ep_start<0 的历史定妆槽位必须被排除，不能算成"多张卡"。
  - 问题 2：称谓形态候选能被找到，已在人物谱/别名里的词必须被排除。
  - 问题 3：别名跨角色碰撞、name 撞 alias 两种确定性重复形态；紧邻共现弱信号
    在真正紧邻拼接时触发、在两个正常互动角色（低比例）时不触发。
  - 问题 4：分集发现曲线按 ep_start 正确分桶累计；assess_new_character 调用
    计数只认 meta.stage 命中的那一部分。
  - 只读连接：readonly_connection 打开的连接确实拒绝写入。
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import audit_character_discovery as audit  # noqa: E402


_SCHEMA = """
CREATE TABLE projects (
    id TEXT PRIMARY KEY, name TEXT, bible_json TEXT, deleted_at REAL
);
CREATE TABLE chapters (
    id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL,
    idx INTEGER NOT NULL, content TEXT NOT NULL
);
CREATE TABLE character_portraits (
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL, character_name TEXT NOT NULL,
    ep_start INTEGER NOT NULL, ep_end INTEGER, pack_status TEXT NOT NULL DEFAULT 'ready'
);
CREATE TABLE episodes (
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL, episode_no INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'planned'
);
CREATE TABLE provider_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT, meta TEXT
);
"""


def _make_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "fixture.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(_SCHEMA)
    conn.commit()
    conn.close()
    return db_path


def _writer(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _character(name: str, aliases: list[str] | None = None) -> dict:
    return {"name": name, "aliases": [{"text": a} for a in (aliases or [])]}


# ---------------------------------------------------------------------------
# 问题 1：人物谱 + 定妆盘点
# ---------------------------------------------------------------------------

def test_negative_ep_start_slot_excluded_from_version_count(tmp_path):
    db_path = _make_db(tmp_path)
    conn = _writer(db_path)
    conn.execute(
        "INSERT INTO character_portraits VALUES ('p1','proj_a','井田',-1,0,'ready')"
    )
    conn.execute(
        "INSERT INTO character_portraits VALUES ('p2','proj_a','井田',1,NULL,'ready')"
    )
    conn.commit()

    roster = audit.build_roster(conn, "proj_a", [_character("井田")], asof_ep=1)
    conn.close()

    assert len(roster) == 1
    assert roster[0].version_count == 1  # 只数 ep_start>=0 的那一条
    assert roster[0].legacy_slot_count == 1  # 已作废历史槽位单独计数，不混进上面
    assert roster[0].current_portrait_status == "ready"


# ---------------------------------------------------------------------------
# 问题 2：谁可能被漏了
# ---------------------------------------------------------------------------

def test_missed_candidate_found_when_absent_from_bible(tmp_path):
    chapters = [
        {"idx": 1, "content": '他推门而入。"我来了。”王大锤淡淡道，转身离去。'},
        {"idx": 2, "content": '"你在做什么？”王大锤问道，众人侧目。'},
        {"idx": 3, "content": '"住手！”王大锤怒道，场面一度紧张。'},
    ]
    candidates = audit.find_missed_candidates(
        chapters, characters=[], min_freq=3, min_chapters=2, top_n=10,
    )
    tokens = {c.token for c in candidates}
    assert "王大锤" in tokens  # 三章都出现在对话引导语位置，且没被截断成"王大"


def test_missed_candidate_excludes_known_bible_name(tmp_path):
    chapters = [
        {"idx": 1, "content": '"我来了。”孟浩淡淡道，转身离去。'},
        {"idx": 2, "content": '"你在做什么？”孟浩问道，众人侧目。'},
        {"idx": 3, "content": '"住手！”孟浩怒道，场面一度紧张。'},
    ]
    candidates = audit.find_missed_candidates(
        chapters, characters=[_character("孟浩")], min_freq=3, min_chapters=2, top_n=10,
    )
    assert candidates == []  # 已在人物谱里，不该被当成"漏了"


# ---------------------------------------------------------------------------
# 问题 3：有没有同一个人两张卡
# ---------------------------------------------------------------------------

def test_alias_collision_across_two_characters():
    characters = [_character("甲", ["老者"]), _character("乙", ["老者"])]
    issues = audit.find_alias_collisions(characters)
    assert len(issues) == 1
    assert "老者" in issues[0] and "甲" in issues[0] and "乙" in issues[0]


def test_name_equals_other_characters_alias():
    characters = [_character("金袍老者"), _character("上官修", ["金袍老者"])]
    issues = audit.find_name_alias_overlap(characters)
    assert len(issues) == 1
    assert "上官修" in issues[0] and "金袍老者" in issues[0]


def test_cooccurrence_suspect_fires_on_tight_apposition():
    # "小明（大明）" 反复紧邻拼接，模拟同位语式重复称呼同一人。
    chapter_text = "。".join(f"小明（大明）第{i}次现身" for i in range(10))
    chapters = [{"idx": 1, "content": chapter_text}]
    characters = [_character("小明"), _character("大明")]
    issues = audit.find_cooccurrence_suspects(
        chapters, characters, window=6, ratio_threshold=0.4, min_count=5,
    )
    assert any("小明" in i and "大明" in i for i in issues)


def test_cooccurrence_suspect_silent_on_normal_interacting_pair():
    # 两个角色频繁互动、但从不紧邻拼接（中间夹着大段叙述），比例应远低于阈值。
    filler = "又发生了一些与他人无关的事情，风吹过山谷，天色渐暗，无人在意。"
    chapter_text = ("孟浩说了几句话。" + filler) * 30 + ("许清也说了几句话。" + filler) * 30
    chapters = [{"idx": 1, "content": chapter_text}]
    characters = [_character("孟浩"), _character("许清")]
    issues = audit.find_cooccurrence_suspects(
        chapters, characters, window=6, ratio_threshold=0.4, min_count=5,
    )
    assert issues == []


def test_cooccurrence_suspect_skips_already_linked_pairs():
    # 即使紧邻拼接得很像，只要已经是同一张卡的别名，就不该被当成"疑似同人"。
    chapter_text = "。".join(f"小胖子（李富贵）第{i}次现身" for i in range(10))
    chapters = [{"idx": 1, "content": chapter_text}]
    characters = [_character("小胖子", ["李富贵"])]
    issues = audit.find_cooccurrence_suspects(
        chapters, characters, window=6, ratio_threshold=0.4, min_count=5,
    )
    assert issues == []


# ---------------------------------------------------------------------------
# 问题 4：分集发现曲线
# ---------------------------------------------------------------------------

def test_growth_curve_buckets_by_ep_start_and_accumulates(tmp_path):
    db_path = _make_db(tmp_path)
    conn = _writer(db_path)
    rows = [
        ("p1", "proj_a", "甲", 1), ("p2", "proj_a", "乙", 1),
        ("p3", "proj_a", "丙", 3), ("p4", "proj_a", "丁", 5),
        ("p5", "proj_a", "戊", -1),  # 历史槽位，不该计入任何一桶
    ]
    for pid, proj, name, ep in rows:
        conn.execute(
            "INSERT INTO character_portraits (id, project_id, character_name, ep_start, ep_end) "
            "VALUES (?,?,?,?,NULL)", (pid, proj, name, ep),
        )
    conn.commit()

    curve = audit.build_growth_curve(conn, "proj_a", episode_count=5)
    conn.close()

    assert len(curve) == 5  # 5 集 -> 5 个桶，每桶 1 集
    new_counts = [c[2] for c in curve]
    assert new_counts == [2, 0, 1, 0, 1]  # 集1新增2、集3新增1、集5新增1
    assert curve[-1][3] == 4  # 累计 4（戊的历史槽位不计入）
    assert all(a <= b for a, b in zip([c[3] for c in curve], [c[3] for c in curve][1:]))


def test_discovery_call_count_only_matches_target_stage(tmp_path):
    db_path = _make_db(tmp_path)
    conn = _writer(db_path)
    metas = [
        {"stage": "assess_new_character"},
        {"stage": "assess_new_character"},
        {"stage": "assess_new_scene"},
        {"stage": None},
    ]
    for meta in metas:
        conn.execute(
            "INSERT INTO provider_calls (project_id, meta) VALUES (?, ?)",
            ("proj_a", json.dumps(meta)),
        )
    conn.commit()

    assert audit.count_discovery_calls(conn, "proj_a") == 2
    conn.close()


# ---------------------------------------------------------------------------
# 只读约束
# ---------------------------------------------------------------------------

def test_readonly_connection_rejects_write(tmp_path):
    db_path = _make_db(tmp_path)
    ro = audit.readonly_connection(db_path)
    with pytest.raises(sqlite3.OperationalError):
        ro.execute("INSERT INTO projects (id, name) VALUES ('x', 'y')")
        ro.commit()
    ro.close()


# ---------------------------------------------------------------------------
# 端到端冒烟：main() 能跑通、覆盖四个问题小标题
# ---------------------------------------------------------------------------

def test_main_smoke_prints_all_four_sections(tmp_path, capsys):
    db_path = _make_db(tmp_path)
    conn = _writer(db_path)
    conn.execute(
        "INSERT INTO projects (id, name, bible_json) VALUES (?,?,?)",
        ("proj_a", "测试项目", json.dumps({"characters": [_character("孟浩")]})),
    )
    conn.execute("INSERT INTO chapters (project_id, idx, content) VALUES ('proj_a', 1, '正文')")
    conn.execute(
        "INSERT INTO character_portraits (id, project_id, character_name, ep_start, ep_end) "
        "VALUES ('p1','proj_a','孟浩',1,NULL)"
    )
    conn.execute("INSERT INTO episodes (id, project_id, episode_no) VALUES ('e1','proj_a',1)")
    conn.commit()
    conn.close()

    rc = audit.main(["--db", str(db_path), "--project", "proj_a"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "问题 1" in out and "问题 2" in out and "问题 3" in out and "问题 4" in out
    assert "测试项目" in out


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
