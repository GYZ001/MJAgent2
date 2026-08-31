"""POST /projects/{project_id}/characters/nominate（用户提名一个原文称呼）。

覆盖 app/domain/bible_ops/nominate.py 的三态路由：
- 命中已有角色（owner）：不新建卡，报出归属者；核验路径复用
  app.portraits.card_aliases.new_card_aliases 的共现证据判据，别名核验通过时
  真的把新称呼写进那张卡的 aliases（不是摆设）。
- 冲突（conflict）：fail closed，列出全部命中者，不建卡。
- 都没命中（none）：走 ensure_character_card(require_identity_card=True)，
  被拒时四种真实原因（skipped_minor/card_incomplete/skipped_not_person/error）
  原样透出，不糊成一句"提名失败"。

以及端点异常经命令总线统一转成 409（不是裸奔成 500）。
"""
from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest
from fastapi import HTTPException

from app.domain.bible_ops.nominate import (
    _register_alias_if_verified,
    nominate_character,
)
from app.schemas import Bible, Character, CharacterAlias, World
from tests.conftest import patch_api_everywhere, patch_portraits_everywhere


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE projects(id TEXT PRIMARY KEY, bible_json TEXT, bible_version INTEGER DEFAULT 0)"
    )
    conn.execute("CREATE TABLE chapters(project_id TEXT, idx INTEGER, content TEXT)")
    conn.execute("CREATE TABLE episodes(project_id TEXT, episode_no INTEGER, source_chapters TEXT)")
    conn.execute(
        "CREATE TABLE character_portraits(id TEXT, project_id TEXT, character_name TEXT, ep_start INTEGER, "
        "ep_end INTEGER, appearance TEXT, prompt TEXT, image_path TEXT, base_portrait_id TEXT, "
        "bible_version INTEGER, created_at REAL)"
    )
    return conn


def _seed_bible(conn: sqlite3.Connection, bible: Bible, *, version: int = 1) -> None:
    conn.execute(
        "INSERT INTO projects(id, bible_json, bible_version) VALUES('p1', ?, ?)",
        (json.dumps(bible.model_dump(), ensure_ascii=False), version),
    )
    conn.commit()


def _patch_project(monkeypatch, conn: sqlite3.Connection) -> None:
    """让 nominate.py 自身以及它调用的 app.portraits 各子模块都读同一个内存连接。"""
    patch_api_everywhere(monkeypatch, "get_conn", lambda: conn)
    patch_portraits_everywhere(monkeypatch, "get_conn", lambda: conn)
    patch_api_everywhere(
        monkeypatch, "_project_or_404",
        lambda pid: dict(conn.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()),
    )


def _patch_settings(monkeypatch) -> dict:
    """ensure_character_card 的负缓存走 get_setting/set_setting；用内存字典
    代替真实 settings 表，避免测试库还要额外建表（同 test_character_discovery.py
    的 _patch_settings 手法）。"""
    settings: dict[str, str] = {}
    patch_portraits_everywhere(monkeypatch, "get_setting", lambda k: settings.get(k, ""))
    patch_portraits_everywhere(monkeypatch, "set_setting", lambda k, v: settings.__setitem__(k, v))
    return settings


def _characters(conn: sqlite3.Connection) -> list[dict]:
    row = conn.execute("SELECT bible_json FROM projects WHERE id='p1'").fetchone()
    return json.loads(row["bible_json"])["characters"]


# ---------------------------------------------------------------------------
# 1) 命中已有角色的别名：不新建卡，且核验路径真的能把新称呼登记成别名
# ---------------------------------------------------------------------------

def test_nominate_existing_alias_hits_owner_without_new_card(monkeypatch) -> None:
    conn = _make_conn()
    _seed_bible(conn, Bible(
        world=World(visual_style_canonical="国风"),
        characters=[Character(
            name="李富贵", role="重要配角", appearance_canonical="圆脸胖身，锦袍加身",
            aliases=[CharacterAlias(
                text="小胖子", name_kind="honorific",
                evidence_chapter_index=1, evidence_quote="众人都唤他小胖子",
            )],
        )],
    ))
    _patch_project(monkeypatch, conn)
    _patch_settings(monkeypatch)

    result = asyncio.run(nominate_character("p1", {"label": "小胖子", "from_episode_no": 1}))

    assert result["status"] == "exists"
    assert result["owner"] == "李富贵"
    assert result["alias_registered"] is False
    assert result["alias_reason"] == "该称呼已经登记过"
    assert "李富贵" in result["message"]
    assert len(_characters(conn)) == 1  # 没有建出第二张卡


def test_register_alias_if_verified_writes_new_alias_with_cooccurrence_evidence(monkeypatch) -> None:
    """直接核实核验通道本身：给一个真正尚未登记的称呼 + 原文共现证据，必须真的
    写进那张卡的 aliases（不是摆设的死代码），且证据锚点字段如实记录。"""
    conn = _make_conn()
    _seed_bible(conn, Bible(
        world=World(visual_style_canonical="国风"),
        characters=[Character(name="李富贵", role="重要配角", appearance_canonical="圆脸胖身，锦袍加身")],
    ))
    conn.execute("INSERT INTO episodes(project_id, episode_no, source_chapters) VALUES('p1', 1, '[1]')")
    conn.execute(
        "INSERT INTO chapters(project_id, idx, content) VALUES('p1', 1, ?)",
        ("李富贵在集市上摆摊，众人都唤他胖爷。",),
    )
    conn.commit()
    _patch_project(monkeypatch, conn)

    outcome = asyncio.run(_register_alias_if_verified("p1", "李富贵", "胖爷", 1))

    assert outcome["alias_registered"] is True
    characters = _characters(conn)
    assert len(characters) == 1
    alias_texts = {a["text"]: a for a in characters[0]["aliases"]}
    assert "胖爷" in alias_texts
    assert alias_texts["胖爷"]["is_exclusive"] is False
    assert alias_texts["胖爷"]["evidence_quote"]

    # 重复登记同一个称呼：已经在册，直接短路，不重复追加、不报错。
    outcome2 = asyncio.run(_register_alias_if_verified("p1", "李富贵", "胖爷", 1))
    assert outcome2["alias_registered"] is False
    assert outcome2["alias_reason"] == "该称呼已经登记过"
    assert len(_characters(conn)[0]["aliases"]) == 1


# ---------------------------------------------------------------------------
# 2) 命中多个角色：fail closed，不猜测归属
# ---------------------------------------------------------------------------

def test_nominate_ambiguous_label_fails_closed_as_conflict(monkeypatch) -> None:
    conn = _make_conn()
    _seed_bible(conn, Bible(
        world=World(visual_style_canonical="国风"),
        characters=[
            Character(
                name="曹阳", role="重要配角", appearance_canonical="虬髯壮汉，皮甲",
                aliases=[CharacterAlias(
                    text="大汉", name_kind="referential", is_exclusive=False,
                    evidence_chapter_index=1, evidence_quote="那大汉正是曹阳",
                )],
            ),
            Character(
                name="虎爷", role="重要配角", appearance_canonical="络腮胡壮汉，皮甲",
                aliases=[CharacterAlias(
                    text="大汉", name_kind="referential", is_exclusive=False,
                    evidence_chapter_index=2, evidence_quote="虎爷这大汉一声怒吼",
                )],
            ),
        ],
    ))
    _patch_project(monkeypatch, conn)
    _patch_settings(monkeypatch)

    result = asyncio.run(nominate_character("p1", {"label": "大汉", "from_episode_no": 1}))

    assert result["status"] == "conflict"
    assert sorted(result["owners"]) == ["曹阳", "虎爷"]
    assert "曹阳" in result["message"] and "虎爷" in result["message"]
    assert len(_characters(conn)) == 2  # 没有猜一个，也没有建第三张卡


# ---------------------------------------------------------------------------
# 3) 原文里查不到：真实原因，不是笼统的"建卡失败"
# ---------------------------------------------------------------------------

def test_nominate_label_absent_from_source_reports_real_reason(monkeypatch) -> None:
    conn = _make_conn()
    _seed_bible(conn, Bible(
        world=World(visual_style_canonical="国风"),
        characters=[Character(name="甲一", role="主角", appearance_canonical="黑发少年，玄色劲装")],
    ))
    conn.execute("INSERT INTO episodes(project_id, episode_no, source_chapters) VALUES('p1', 5, '[9]')")
    conn.execute(
        "INSERT INTO chapters(project_id, idx, content) VALUES('p1', 9, ?)",
        ("甲一走入山门，四下无人。",),
    )
    conn.commit()
    _patch_project(monkeypatch, conn)
    _patch_settings(monkeypatch)

    result = asyncio.run(nominate_character("p1", {"label": "无名散修", "from_episode_no": 5}))

    assert result["status"] == "error"
    assert "原文" in result["reason"]  # 真实原因（无可核验片段），不是笼统的"建卡失败"
    assert "建卡失败" not in result["message"] or "原文" in result["message"]
    assert not any(c["name"] == "无名散修" for c in _characters(conn))


# ---------------------------------------------------------------------------
# 4) 非人（宗门/器物）：skipped_not_person，理由如实
# ---------------------------------------------------------------------------

def test_nominate_non_person_reports_skipped_not_person(monkeypatch) -> None:
    conn = _make_conn()
    _seed_bible(conn, Bible(
        world=World(visual_style_canonical="国风"),
        characters=[Character(name="甲一", role="主角", appearance_canonical="黑发少年，玄色劲装")],
    ))
    conn.execute("INSERT INTO episodes(project_id, episode_no, source_chapters) VALUES('p1', 3, '[7]')")
    conn.execute(
        "INSERT INTO chapters(project_id, idx, content) VALUES('p1', 7, ?)",
        ("靠山宗乃本地第一大宗门，门下弟子数千。靠山宗历代宗主皆出自嫡传。" * 2,),
    )
    conn.commit()
    _patch_project(monkeypatch, conn)
    _patch_settings(monkeypatch)

    async def fake_assess(name, fragments, *, style, known_names, ep_label, **_kwargs):
        assert name == "靠山宗"
        return {
            "subject_kind": "organization",
            "important": True,
            "reason": "属于独立的组织类出场单元，需单独建卡保证一致性",
            "role": "重要配角",
            "appearance_canonical": "",
            "personality": "", "speech_style": "", "relationships": [],
        }

    patch_portraits_everywhere(monkeypatch, "assess_new_character", fake_assess)

    result = asyncio.run(nominate_character("p1", {"label": "靠山宗", "from_episode_no": 3}))

    assert result["status"] == "skipped_not_person"
    assert result["subject_kind"] == "organization"
    assert "组织" in result["reason"]
    assert "组织" in result["message"] or "organization" in result["message"]
    assert not any(c["name"] == "靠山宗" for c in _characters(conn))


# ---------------------------------------------------------------------------
# 4b) 外观长度越界等格式问题：nominate 恒传 require_identity_card=True，这条
#     路径下 unimportant_verdict_result 在 card_incomplete 判据之前先命中
#     `if require_identity_card: return error` 分支（见 app/portraits/
#     card_verdict.py），所以真实 status 是 "error" 而不是 "card_incomplete"
#     ——先验证这一点再断言，不要顺着"四个状态字都各自独立可达"的假设走（那个
#     假设在 require_identity_card=False 的路径下才成立）。真正要保住的是
#     reason 里模型给的具体越界数值必须原样透出，不能被吞成"人物卡模型未返回
#     完整稳定卡片"这句通用文案本身。
# ---------------------------------------------------------------------------

def test_nominate_incomplete_card_reports_real_reason_not_generic_error(monkeypatch) -> None:
    conn = _make_conn()
    _seed_bible(conn, Bible(
        world=World(visual_style_canonical="国风"),
        characters=[Character(name="甲一", role="主角", appearance_canonical="黑发少年，玄色劲装")],
    ))
    conn.execute("INSERT INTO episodes(project_id, episode_no, source_chapters) VALUES('p1', 2, '[4]')")
    conn.execute(
        "INSERT INTO chapters(project_id, idx, content) VALUES('p1', 4, ?)",
        ("丁力听令后带人巡查山门。丁力沉默寡言。" * 2,),
    )
    conn.commit()
    _patch_project(monkeypatch, conn)
    _patch_settings(monkeypatch)

    async def fake_assess(name, fragments, *, style, known_names, ep_label, **_kwargs):
        # important 被格式校验强制降级，但 model_important 如实记录模型本来的判断——
        # 这正是 card_incomplete 与 skipped_minor 的区分判据（见 card_verdict.py）。
        return {
            "subject_kind": "person",
            "important": False,
            "model_important": True,
            "incomplete_reason": "appearance_canonical 长度 3 字，要求 20~80 字",
            "reason": "值得建卡", "role": "重要配角",
            "appearance_canonical": "一个人",  # 明显短于 APPEARANCE_MIN=20
            "personality": "", "speech_style": "", "relationships": [],
        }

    patch_portraits_everywhere(monkeypatch, "assess_new_character", fake_assess)

    result = asyncio.run(nominate_character("p1", {"label": "丁力", "from_episode_no": 2}))

    assert result["status"] == "error"
    assert "20~80" in result["reason"]  # 模型给的实际越界数值，不是被吞掉的通用文案
    assert "20~80" in result["message"]
    assert not any(c["name"] == "丁力" for c in _characters(conn))


# ---------------------------------------------------------------------------
# 5) 端点异常：命令总线统一转 409，不是裸奔成 500
# ---------------------------------------------------------------------------

def test_nominate_without_bible_raises_409_via_command_bus(monkeypatch) -> None:
    """项目还没生成人物谱时提名，领域函数抛的是裸 ValueError；直接调用
    nominate_character（未经 handler 上下文，走真实 ui_route -> dispatch ->
    command bus -> handler -> call_guarded 全链路）必须落地成 409，不是 500——
    这正是 call_guarded 把 ValueError 转 invalid_state(409) 的既定契约。"""
    conn = _make_conn()
    conn.execute("INSERT INTO projects(id, bible_json, bible_version) VALUES('p1', NULL, 0)")
    conn.commit()
    _patch_project(monkeypatch, conn)
    _patch_settings(monkeypatch)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(nominate_character("p1", {"label": "某人", "from_episode_no": 1}))

    assert exc_info.value.status_code == 409
