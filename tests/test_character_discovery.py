import asyncio
import json
import sqlite3

import pytest

from app import api, db, portraits
from app.schemas import (Bible, Character, EpisodeScreenplay,
                         IdentityContractEvidence, InformationItem,
                         KeyDialogueChain, KeyDialogueTurn,
                         NarrativeContinuityPlan, NarrativeIdentityContract,
                         ScriptScene, VoiceCanonical, World)


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE projects(id TEXT PRIMARY KEY, bible_json TEXT, bible_version INTEGER DEFAULT 0)")
    conn.execute("CREATE TABLE chapters(project_id TEXT, idx INTEGER, content TEXT)")
    conn.execute("CREATE TABLE episodes(project_id TEXT, episode_no INTEGER, source_chapters TEXT)")
    conn.execute(
        "CREATE TABLE character_portraits(id TEXT, project_id TEXT, character_name TEXT, ep_start INTEGER, "
        "ep_end INTEGER, appearance TEXT, prompt TEXT, image_path TEXT, base_portrait_id TEXT, "
        "bible_version INTEGER, created_at REAL)")
    return conn


def _seed_project(conn: sqlite3.Connection, chapter_content: str) -> None:
    bible = Bible(world=World(visual_style_canonical="国风"),
                  characters=[Character(name="萧炎", role="主角",
                                        appearance_canonical="黑发少年，玄色劲装，目光坚定，身形修长，腰佩火纹玉佩")])
    conn.execute("INSERT INTO projects(id, bible_json, bible_version) VALUES('p1', ?, 1)",
                 (json.dumps(bible.model_dump(), ensure_ascii=False),))
    conn.execute("INSERT INTO episodes(project_id, episode_no, source_chapters) VALUES('p1', 21, '[30]')")
    conn.execute("INSERT INTO chapters(project_id, idx, content) VALUES('p1', 30, ?)", (chapter_content,))
    conn.commit()


def _patch_settings(monkeypatch, conn) -> dict:
    settings: dict[str, str] = {}
    monkeypatch.setattr(portraits, "get_conn", lambda: conn)
    monkeypatch.setattr(portraits, "get_setting", lambda k: settings.get(k))
    monkeypatch.setattr(portraits, "set_setting", lambda k, v: settings.__setitem__(k, v))
    return settings


def test_ensure_character_card_auto_adds_prominent_character_and_portrait(monkeypatch) -> None:
    conn = _make_conn()
    _seed_project(conn, "美杜莎现身，紫色长发，妖娆冷艳。美杜莎再次出手。美杜莎统领蛇人一族。" * 3)
    _patch_settings(monkeypatch, conn)

    async def fake_assess(name, fragments, *, style, known_names, ep_label):
        assert name == "美杜莎" and "美杜莎" in fragments  # 检索到的是该角色片段
        return {"important": True, "reason": "反复出场", "role": "重要配角",
                "appearance_canonical": "紫发妖娆女子，紫色长发，金瞳蛇眸，蛇纹长裙，气场冷艳标志性蛇瞳",
                "personality": "高傲", "speech_style": "冷冽",
                "relationships": [{"to": "萧炎", "relation": "宿敌"}]}

    async def fake_portrait(project_id, name, style, appearance, *, ep_start):
        return (f"/tmp/{name}.jpg", "fake prompt")

    monkeypatch.setattr(portraits, "assess_new_character", fake_assess)
    monkeypatch.setattr(portraits, "_generate_fresh_portrait", fake_portrait)

    res = asyncio.run(portraits.ensure_character_card("p1", "美杜莎", 21))
    assert res["status"] == "added"
    assert res["has_portrait"] is True

    names = [c["name"] for c in json.loads(
        conn.execute("SELECT bible_json FROM projects WHERE id='p1'").fetchone()["bible_json"])["characters"]]
    assert "美杜莎" in names
    assert conn.execute("SELECT bible_version FROM projects WHERE id='p1'").fetchone()["bible_version"] == 2
    assert conn.execute("SELECT COUNT(*) c FROM character_portraits WHERE character_name='美杜莎'").fetchone()["c"] == 1
    queue = json.loads(conn.execute(
        "SELECT bible_auto_changes_json FROM projects WHERE id='p1'"
    ).fetchone()["bible_auto_changes_json"])
    assert queue[0]["status"] == "auto_applied"
    assert queue[0]["payload"]["character_card"]["name"] == "美杜莎"

    # 幂等：第二次识别到同名角色时不重复建卡/出图。
    res2 = asyncio.run(portraits.ensure_character_card("p1", "美杜莎", 22))
    assert res2["status"] == "exists"
    cnt = conn.execute("SELECT COUNT(*) c FROM character_portraits WHERE character_name='美杜莎'").fetchone()["c"]
    assert cnt == 1


def test_required_identity_card_accepts_complete_card_despite_importance_vote(
    monkeypatch,
) -> None:
    conn = _make_conn()
    _seed_project(conn, "丁力听令后带人巡查山门。")
    _patch_settings(monkeypatch, conn)

    async def fake_assess(*_args, **kwargs):
        assert kwargs["require_identity_card"] is True
        return {
            "important": False,
            "reason": "只出现一次",
            "role": "重要配角",
            "appearance_canonical": (
                "成年黑发男子，身穿深灰色皮甲短衫，腰间佩刀，"
                "体格壮实，左眉留有一道浅疤"
            ),
            "personality": "服从命令",
            "speech_style": "简短应答",
            "relationships": [],
        }

    monkeypatch.setattr(portraits, "assess_new_character", fake_assess)

    result = asyncio.run(portraits.ensure_character_card(
        "p1",
        "丁力",
        21,
        generate_portrait=False,
        require_identity_card=True,
    ))

    assert result["status"] == "added"
    characters = json.loads(
        conn.execute(
            "SELECT bible_json FROM projects WHERE id='p1'",
        ).fetchone()["bible_json"],
    )["characters"]
    assert any(character["name"] == "丁力" for character in characters)


def test_required_identity_card_prompt_does_not_reapply_importance_gate(
    monkeypatch,
) -> None:
    captured: list[str] = []
    call_options: list[dict] = []

    async def fake_chat(messages, **_kwargs):
        captured.append(messages[0]["content"])
        call_options.append(_kwargs)
        return json.dumps({
            "important": False,
            "reason": "只出现一次",
            "role": "重要配角",
            "appearance_canonical": (
                "成年黑发男子，身穿深灰色皮甲短衫，腰间佩刀，"
                "体格壮实，左眉留有一道浅疤"
            ),
            "personality": "服从命令",
            "speech_style": "简短应答",
            "relationships": [],
        }, ensure_ascii=False)

    monkeypatch.setattr(portraits.model_gateway, "chat", fake_chat)

    result = asyncio.run(portraits.assess_new_character(
        "丁力",
        "丁力听令后带人巡查山门。",
        style="国风",
        known_names=["萧炎"],
        ep_label="第 21 集",
        require_identity_card=True,
    ))

    assert result["important"] is False
    assert result["card_complete"] is True
    assert "本次任务不是重新判断戏份重要度" in captured[0]
    assert "不得因只出现一次而拒绝建卡" in captured[0]
    assert call_options[0]["max_tokens"] >= 4096
    assert call_options[0]["call_meta"]["expected_json"] is True


def test_mentioned_only_unknown_character_does_not_require_identity_card() -> None:
    known = {"白洁", "小晶"}

    assert portraits._candidate_requires_identity_card(
        {
            "name": "钟五",
            "identity_kind": "named",
            "kind": "mentioned",
        },
        known,
    ) is False
    assert portraits._candidate_requires_identity_card(
        {
            "name": "钟五",
            "identity_kind": "named",
            "kind": "onscreen",
        },
        known,
    ) is True
    assert portraits._candidate_requires_identity_card(
        {
            "name": "小晶",
            "identity_kind": "named",
            "kind": "onscreen",
        },
        known,
    ) is False
    assert portraits._candidate_requires_identity_card(
        {
            "name": "魂天帝",
            "kind": "onscreen",
        },
        known,
    ) is True


def test_character_card_truncation_is_reported_as_generation_error(
    monkeypatch,
) -> None:
    async def truncated_chat(*_args, **_kwargs):
        return '{"important":true,"reason":"响应被截断'

    monkeypatch.setattr(portraits.model_gateway, "chat", truncated_chat)

    with pytest.raises(
        portraits.ContentGenerationError,
        match="人物卡结构化输出不完整",
    ):
        asyncio.run(portraits.assess_new_character(
            "丁力",
            "丁力走进大厅。",
            style="国风",
            known_names=[],
            ep_label="第 1 集",
            require_identity_card=True,
        ))


def test_ensure_character_card_keeps_auto_added_card_when_portrait_fails(monkeypatch) -> None:
    conn = _make_conn()
    _seed_project(conn, "美杜莎现身，紫色长发。美杜莎再次出手。美杜莎统领蛇人一族。" * 3)
    _patch_settings(monkeypatch, conn)

    async def fake_assess(*a, **k):
        return {"important": True, "reason": "反复出场", "role": "反派",
                "appearance_canonical": "紫发妖娆女子，紫色长发，金瞳蛇眸，蛇纹长裙，气场冷艳标志性蛇瞳",
                "personality": "", "speech_style": "", "relationships": []}

    portrait_calls = 0

    async def boom(*a, **k):
        nonlocal portrait_calls
        portrait_calls += 1
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(portraits, "assess_new_character", fake_assess)
    monkeypatch.setattr(portraits, "_generate_fresh_portrait", boom)
    monkeypatch.setattr(portraits, "code_ref", lambda *_args, **_kwargs: "（测试错误）")

    res = asyncio.run(portraits.ensure_character_card("p1", "美杜莎", 21))
    assert res["status"] == "added" and res["has_portrait"] is False
    assert portrait_calls == 1
    # 供应商失败不回滚 AI 已确认的卡片；分镜前自动重试定妆资产。
    names = [c["name"] for c in json.loads(
        conn.execute("SELECT bible_json FROM projects WHERE id='p1'").fetchone()["bible_json"])["characters"]]
    assert "美杜莎" in names
    assert conn.execute("SELECT COUNT(*) c FROM character_portraits WHERE character_name='美杜莎'").fetchone()["c"] == 0
    queue = json.loads(conn.execute(
        "SELECT bible_auto_changes_json FROM projects WHERE id='p1'"
    ).fetchone()["bible_auto_changes_json"])
    assert queue[0]["status"] == "auto_applied_asset_failed"


def test_existing_pending_character_is_auto_applied_without_reassessment(monkeypatch) -> None:
    conn = _make_conn()
    _seed_project(conn, "葛叶陪同纳兰嫣然现身。葛叶出手阻拦萧炎。" * 4)
    _patch_settings(monkeypatch, conn)
    conn.execute("ALTER TABLE projects ADD COLUMN bible_auto_changes_json TEXT")
    card = Character(
        name="葛叶",
        role="重要配角",
        appearance_canonical="老年男性，灰白长发束起，身着云岚宗青灰长袍，面容沉稳，腰佩宗门令牌",
    )
    pending = [{
        "id": "change_old",
        "kind": "new_character",
        "status": "pending",
        "character": "葛叶",
        "ep_start": 5,
        "reason": "有具名台词与持续行动",
        "payload": {"character_card": card.model_dump(mode="json")},
    }]
    conn.execute(
        "UPDATE projects SET bible_auto_changes_json=? WHERE id='p1'",
        (json.dumps(pending, ensure_ascii=False),),
    )
    conn.commit()

    async def forbidden_assess(*_args, **_kwargs):
        raise AssertionError("已有待审卡不应重复调用 AI 评估")

    async def fake_portrait(project_id, name, style, appearance, *, ep_start):
        assert name == "葛叶" and ep_start == 5
        return ("/tmp/葛叶.jpg", "fake prompt")

    monkeypatch.setattr(portraits, "assess_new_character", forbidden_assess)
    monkeypatch.setattr(portraits, "_generate_fresh_portrait", fake_portrait)

    result = asyncio.run(portraits.ensure_character_card("p1", "葛叶", 5))

    assert result["status"] == "added"
    bible = json.loads(conn.execute(
        "SELECT bible_json FROM projects WHERE id='p1'"
    ).fetchone()["bible_json"])
    assert "葛叶" in {item["name"] for item in bible["characters"]}
    changes = json.loads(conn.execute(
        "SELECT bible_auto_changes_json FROM projects WHERE id='p1'"
    ).fetchone()["bible_auto_changes_json"])
    assert changes[0]["status"] == "auto_applied"


def test_auto_discovered_character_pack_starts_at_first_appearance(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "auto-character-pack.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()
    conn = db.get_conn()
    bible = Bible(
        world=World(visual_style_canonical="国风"),
        characters=[Character(
            name="萧炎", role="主角",
            appearance_canonical="黑发少年，玄色劲装，目光坚定，身形修长，腰佩火纹玉佩",
        )],
    )
    conn.execute(
        "INSERT INTO projects(id,name,status,bible_json,bible_version,bible_status,created_at) "
        "VALUES('p1','斗破苍穹','planned',?,1,'ready',1)",
        (bible.model_dump_json(),),
    )
    conn.execute(
        "INSERT INTO chapters(project_id,idx,title,content,char_count) VALUES(?,?,?,?,?)",
        ("p1", 5, "葛叶登场", "葛叶陪同纳兰嫣然现身，并与萧炎正面交锋。" * 8, 240),
    )
    conn.execute(
        "INSERT INTO episodes(id,project_id,episode_no,title,source_chapters,status,created_at) "
        "VALUES('e5','p1',5,'葛叶登场','[5]','planned',1)"
    )
    conn.commit()

    async def fake_assess(*_args, **_kwargs):
        return {
            "important": True, "reason": "具名对手且持续参与主线", "role": "重要配角",
            "appearance_canonical": "老年男性，灰白长发束起，身着云岚宗青灰长袍，面容沉稳，腰佩宗门令牌",
            "personality": "沉稳", "speech_style": "克制", "relationships": [],
        }

    async def fake_portrait(project_id, name, style, appearance, *, ep_start):
        path = tmp_path / f"{name}-{ep_start}.jpg"
        path.write_bytes(b"\xff\xd8\xff\xe0automatic-character")
        return str(path), "fake prompt"

    async def fake_review(*_args, **_kwargs):
        return {
            "identity_match": 1.0, "presentation_match": 1.0, "clean_frame": 1.0,
            "overall": 1.0, "issues": [], "hard_failures": [], "hard_gate_passed": True,
        }

    pack_calls = []

    async def fake_pack(**kwargs):
        pack_calls.append(kwargs)
        return {"status": "ready", "portrait_id": kwargs["portrait_id"]}

    monkeypatch.setattr(portraits, "assess_new_character", fake_assess)
    monkeypatch.setattr(portraits, "_generate_fresh_portrait", fake_portrait)
    monkeypatch.setattr(portraits, "_review_portrait_asset", fake_review)
    monkeypatch.setattr("app.multiview.ensure_character_multiview_pack", fake_pack)

    result = asyncio.run(portraits.ensure_character_card("p1", "葛叶", 5))

    assert result["status"] == "added" and result["has_portrait"] is True
    row = conn.execute(
        "SELECT ep_start,ep_end,pack_status FROM character_portraits "
        "WHERE project_id='p1' AND character_name='葛叶'"
    ).fetchone()
    assert (row["ep_start"], row["ep_end"], row["pack_status"]) == (5, None, "ready")
    assert pack_calls[0]["ep_start"] == 5


def test_minor_character_is_skipped_and_negatively_cached(monkeypatch) -> None:
    conn = _make_conn()
    _seed_project(conn, "路人甲走过。" * 6)
    _patch_settings(monkeypatch, conn)

    calls = {"assess": 0}

    async def fake_assess(*a, **k):
        calls["assess"] += 1
        return {"important": False, "reason": "路人", "role": "重要配角",
                "appearance_canonical": "", "personality": "", "speech_style": "", "relationships": []}

    monkeypatch.setattr(portraits, "assess_new_character", fake_assess)

    res = asyncio.run(portraits.ensure_character_card("p1", "路人甲", 21))
    assert res["status"] == "skipped_minor"
    assert calls["assess"] == 1
    # 21 集判过不重要 → 22 集在重判窗口内，直接命中负缓存，不再调模型
    res2 = asyncio.run(portraits.ensure_character_card("p1", "路人甲", 22))
    assert res2["status"] == "skipped_minor"
    assert calls["assess"] == 1


def test_ensure_cards_for_screenplay_blocks_unknown_names_without_building_cards(monkeypatch) -> None:
    conn = _make_conn()
    _seed_project(conn, "美杜莎现身，紫色长发。美杜莎再次出手。美杜莎统领蛇人一族。" * 3)
    _patch_settings(monkeypatch, conn)

    seen: list[tuple[str, int]] = []

    async def fake_ensure(project_id, name, episode_no):
        seen.append((name, episode_no))
        return {"status": "added", "name": name, "has_portrait": True}

    monkeypatch.setattr(portraits, "ensure_character_card", fake_ensure)

    class _Scene:
        def __init__(self, chars): self.characters = chars

    class _Screenplay:
        scene_outline = [_Scene(["萧炎", "美杜莎"]), _Scene(["美杜莎", "纳兰嫣然"])]
        beats: list = []

    bible = Bible.model_validate(json.loads(
        conn.execute("SELECT bible_json FROM projects WHERE id='p1'").fetchone()["bible_json"]))
    out = asyncio.run(portraits.ensure_cards_for_screenplay("p1", 21, _Screenplay(), bible))

    assert seen == []
    assert out["checked"] == 2 and out["added"] == []
    assert len(out["blocking_errors"]) == 2
    assert all("请回到剧本阶段" in message for message in out["blocking_errors"])


def _insert_portrait(conn, pid, name, ep_start, ep_end, appearance, image_path) -> None:
    conn.execute(
        "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end, appearance, "
        "prompt, image_path, base_portrait_id, bible_version, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (f"po_{name}_{ep_start}", pid, name, ep_start, ep_end, appearance, "p", image_path, None, 1, 0.0))
    conn.commit()


def test_ensure_cards_for_screenplay_redraws_on_appearance_drift(monkeypatch) -> None:
    conn = _make_conn()
    _seed_project(conn, "萧炎一夜白头，玄色劲装染血，左眼覆着一道狰狞刀疤。萧炎冷然出手。" * 3)
    _patch_settings(monkeypatch, conn)
    # 已有开区间定妆照（适用集 1~ 至今）
    _insert_portrait(conn, "p1", "萧炎", 1, None, "黑发少年，玄色劲装，目光坚定，身形修长", "/tmp/xiao_ep1.jpg")

    async def fake_screen(entries, ep_label):
        assert any(e["name"] == "萧炎" for e in entries) and "萧炎" in entries[0]["fragments"]
        return {"萧炎": {"new_appearance": "白发青年，玄色染血劲装，左眼狰狞刀疤，目光冷峻", "reason": "白头+刀疤"}}

    async def fake_redraw(project_id, name, style, appearance, *, base_path, ep_start):
        assert base_path == "/tmp/xiao_ep1.jpg" and ep_start == 21  # 以旧图为底、新段从本集起
        return (f"/tmp/{name}_ep{ep_start}.jpg", "redraw prompt")

    monkeypatch.setattr(portraits, "screen_appearance_changes", fake_screen)
    monkeypatch.setattr(portraits, "_redraw_portrait", fake_redraw)

    class _Scene:
        def __init__(self, chars): self.characters = chars

    class _Screenplay:
        scene_outline = [_Scene(["萧炎"])]
        beats: list = []

    bible = Bible.model_validate(json.loads(
        conn.execute("SELECT bible_json FROM projects WHERE id='p1'").fetchone()["bible_json"]))
    out = asyncio.run(portraits.ensure_cards_for_screenplay("p1", 21, _Screenplay(), bible))

    assert [r["name"] for r in out["redrawn"]] == ["萧炎"]
    rows = conn.execute(
        "SELECT ep_start, ep_end, appearance FROM character_portraits WHERE character_name='萧炎' ORDER BY ep_start"
    ).fetchall()
    # 旧段右区间关到本集-1，新开区间段从本集起
    assert (rows[0]["ep_start"], rows[0]["ep_end"]) == (1, 20)
    assert (rows[1]["ep_start"], rows[1]["ep_end"]) == (21, None)
    assert "白发" in rows[1]["appearance"]
    # bible 锚点同步成最新（供人物谱 UI 展示）
    chars = json.loads(conn.execute("SELECT bible_json FROM projects WHERE id='p1'").fetchone()["bible_json"])["characters"]
    assert "白发" in next(c for c in chars if c["name"] == "萧炎")["appearance_canonical"]


def test_no_drift_redraw_when_portrait_starts_at_or_after_this_episode(monkeypatch) -> None:
    """本集（之后）才登场的定妆照天然是最新，不应再判漂移/重绘。"""
    conn = _make_conn()
    _seed_project(conn, "萧炎一夜白头，玄色劲装染血，左眼覆着一道狰狞刀疤。" * 3)
    _patch_settings(monkeypatch, conn)
    _insert_portrait(conn, "p1", "萧炎", 21, None, "黑发少年，玄色劲装，目光坚定", "/tmp/xiao_ep21.jpg")

    calls = {"screen": 0}

    async def fake_screen(entries, ep_label):
        calls["screen"] += 1
        return {}

    monkeypatch.setattr(portraits, "screen_appearance_changes", fake_screen)

    class _Scene:
        def __init__(self, chars): self.characters = chars

    class _Screenplay:
        scene_outline = [_Scene(["萧炎"])]
        beats: list = []

    bible = Bible.model_validate(json.loads(
        conn.execute("SELECT bible_json FROM projects WHERE id='p1'").fetchone()["bible_json"]))
    out = asyncio.run(portraits.ensure_cards_for_screenplay("p1", 21, _Screenplay(), bible))
    assert out["redrawn"] == [] and calls["screen"] == 0  # ep_start>=本集 → 直接跳过，连判定都不调


def test_ensure_cards_backfills_identical_ready_future_portrait(
    monkeypatch, tmp_path,
) -> None:
    conn = _make_conn()
    _seed_project(conn, "萧炎在本集登场。")
    _patch_settings(monkeypatch, conn)
    image = tmp_path / "xiao_ep22.jpg"
    image.write_bytes(b"ready")
    appearance = "黑发少年，玄色劲装，目光坚定，身形修长，腰佩火纹玉佩"
    _insert_portrait(conn, "p1", "萧炎", 22, None, appearance, str(image))

    async def unexpected_screen(*_args, **_kwargs):
        raise AssertionError("向前扩展相同完整包后不应再判外观漂移")

    monkeypatch.setattr(portraits, "screen_appearance_changes", unexpected_screen)

    class _Scene:
        characters = ["萧炎"]

    class _Screenplay:
        scene_outline = [_Scene()]
        beats: list = []

    bible = Bible.model_validate(json.loads(
        conn.execute("SELECT bible_json FROM projects WHERE id='p1'").fetchone()["bible_json"]))
    out = asyncio.run(
        portraits.ensure_cards_for_screenplay("p1", 21, _Screenplay(), bible)
    )

    row = conn.execute(
        "SELECT ep_start,ep_end FROM character_portraits WHERE character_name='萧炎'"
    ).fetchone()
    assert (row["ep_start"], row["ep_end"]) == (21, None)
    assert out["backfilled"] == [{
        "name": "萧炎",
        "portrait_id": "po_萧炎_22",
        "ep_start": 21,
        "previous_ep_start": 22,
        "image_path": str(image),
        "pack_status": "ready",
        "reused": True,
    }]
    assert out["redrawn"] == []


def test_bible_for_episode_picks_segment_anchor(monkeypatch) -> None:
    conn = _make_conn()
    _seed_project(conn, "x")
    _patch_settings(monkeypatch, conn)
    _insert_portrait(conn, "p1", "萧炎", 1, 20, "早期：黑发少年，玄色劲装，目光坚定", "/tmp/a.jpg")
    _insert_portrait(conn, "p1", "萧炎", 21, None, "后期：白发青年，染血劲装，左眼刀疤", "/tmp/b.jpg")

    bible = Bible.model_validate(json.loads(
        conn.execute("SELECT bible_json FROM projects WHERE id='p1'").fetchone()["bible_json"]))
    original = bible.characters[0].appearance_canonical

    v10 = portraits.bible_for_episode("p1", bible, 10)
    v25 = portraits.bible_for_episode("p1", bible, 25)
    assert "黑发少年" in v10.characters[0].appearance_canonical
    assert "白发青年" in v25.characters[0].appearance_canonical
    # 取本集视图不应改动传入的原 bible
    assert bible.characters[0].appearance_canonical == original


def test_discover_character_candidates_keeps_typed_functionals_and_filters_unseen_names(monkeypatch) -> None:
    bible = Bible(
        world=World(visual_style_canonical="国风"),
        characters=[Character(
            name="萧炎",
            role="主角",
            appearance_canonical="黑发少年，玄色劲装，目光坚定，身形修长，腰佩火纹玉佩",
        )],
    )

    async def fake_chat(*_args, **kwargs):
        assert kwargs["call_meta"]["reuse_successful_operation"] is True
        return json.dumps({
            "characters": [
                {
                    "source_label": "魂天帝", "canonical_name": "魂天帝",
                    "identity_kind": "named", "kind": "onscreen",
                    "evidence": "魂天帝踏着血云现身",
                },
                {
                    "source_label": "萧炎", "canonical_name": "萧炎",
                    "identity_kind": "named", "kind": "onscreen",
                    "evidence": "萧炎迎空而起",
                },
                {
                    "source_label": "守卫", "canonical_name": "",
                    "identity_kind": "functional", "kind": "onscreen",
                    "evidence": "守卫后退",
                },
                {
                    "source_label": "不存在的人", "canonical_name": "不存在的人",
                    "identity_kind": "named", "kind": "onscreen",
                    "evidence": "模型臆造",
                },
            ],
        }, ensure_ascii=False)

    monkeypatch.setattr(portraits.model_gateway, "chat", fake_chat)
    result = asyncio.run(portraits.discover_character_candidates(
        "魂天帝踏着血云现身，萧炎迎空而起，守卫仓促后退。",
        bible,
        1926,
    ))

    assert [item["name"] for item in result] == ["魂天帝", "萧炎", "守卫"]
    assert result[-1]["identity_kind"] == "functional"


def test_discover_character_candidates_repairs_unescaped_evidence_quotes(monkeypatch) -> None:
    bible = Bible(
        world=World(visual_style_canonical="国风"),
        characters=[Character(
            name="孟浩",
            role="主角",
            appearance_canonical="黑发书生，青色长衫，目光清澈",
        )],
    )

    async def fake_chat(*_args, **_kwargs):
        return (
            '```json\n{"characters":[{"source_label":"孟浩",'
            '"canonical_name":"孟浩","identity_kind":"named","kind":"onscreen",'
            '"evidence":"原文写道"孟浩说道"。","future_evidence":""}]}\n```'
        )

    monkeypatch.setattr(portraits.model_gateway, "chat", fake_chat)
    result = asyncio.run(portraits.discover_character_candidates(
        "孟浩说道，他要去靠山宗。",
        bible,
        1,
    ))

    assert result[0]["name"] == "孟浩"
    assert result[0]["evidence"] == '原文写道"孟浩说道"。'


def test_screenplay_discovery_resolves_appearance_label_from_next_ten_chapters(monkeypatch) -> None:
    conn = _make_conn()
    _seed_project(conn, "绿袍男子拦在萧炎面前，厉声呵斥。")
    conn.execute(
        "INSERT INTO chapters(project_id,idx,content) VALUES('p1',31,?)",
        ("绿袍男子摘下斗笠，众人这才认出他正是丁力。",),
    )
    conn.execute(
        "INSERT INTO chapters(project_id,idx,content) VALUES('p1',40,?)",
        ("丁力再次现身。",),
    )
    conn.execute(
        "INSERT INTO chapters(project_id,idx,content) VALUES('p1',41,?)",
        ("超出十章的内容不得进入身份预检。",),
    )
    conn.commit()
    _patch_settings(monkeypatch, conn)

    prompts: list[str] = []

    async def fake_chat(_messages, **_kwargs):
        prompt = _messages[0]["content"]
        prompts.append(prompt)
        phase = _kwargs["call_meta"]["discovery_phase"]
        if phase == "current":
            assert "绿袍男子摘下斗笠" not in prompt
            return json.dumps({
                "characters": [{
                    "source_label": "绿袍男子",
                    "canonical_name": "",
                    "identity_kind": "functional",
                    "kind": "onscreen",
                    "evidence": "绿袍男子拦路呵斥",
                    "future_evidence": "",
                }],
            }, ensure_ascii=False)
        assert "绿袍男子摘下斗笠" in prompt
        assert "丁力再次现身" in prompt
        assert "超出十章" not in prompt
        return json.dumps({
            "characters": [{
                "source_label": "绿袍男子",
                "canonical_name": "丁力",
                "identity_kind": "named",
                "kind": "onscreen",
                "evidence": "绿袍男子拦路呵斥",
                "future_evidence": "绿袍男子摘下斗笠，众人这才认出他正是丁力。",
            }],
        }, ensure_ascii=False)

    ensured: list[str] = []

    async def fake_ensure(
        _project_id, name, _episode_no, *,
        generate_portrait=True, require_identity_card=False,
    ):
        ensured.append(name)
        assert generate_portrait is False
        assert require_identity_card is True
        return {"status": "added", "name": name, "has_portrait": False, "portrait_deferred": True}

    monkeypatch.setattr(portraits.model_gateway, "chat", fake_chat)
    monkeypatch.setattr(portraits, "ensure_character_card", fake_ensure)
    bible = Bible.model_validate(json.loads(
        conn.execute("SELECT bible_json FROM projects WHERE id='p1'").fetchone()["bible_json"]
    ))

    result = asyncio.run(portraits.ensure_cards_for_text(
        "p1", 21, "绿袍男子拦在萧炎面前，厉声呵斥。", bible,
        generate_portraits=False,
    ))

    assert ensured == ["丁力"]
    assert len(prompts) == 2
    assert result["future_context_label"] == "第 31-40 章（仅姓名消歧）"
    assert result["resolutions"] == [{
        "source_label": "绿袍男子",
        "canonical_name": "丁力",
        "resolution": "future_identity",
        "reason": "后续章节已确认该称谓的稳定真名",
        "evidence": "绿袍男子拦路呵斥",
        "future_evidence": "绿袍男子摘下斗笠，众人这才认出他正是丁力。",
        "identity_group": "current-1:绿袍男子",
        "decision_contract_version": "screenplay-future-identity.v5",
        "authority_id": "bible:丁力",
        "authority_version": "screenplay-identity-authority.v1",
    }]


def test_future_identity_model_scans_all_batches_and_named_evidence_wins(monkeypatch) -> None:
    bible = Bible(
        world=World(visual_style_canonical="国风"),
        characters=[Character(
            name="萧炎",
            role="主角",
            appearance_canonical="黑发少年，玄色劲装，目光坚定，身形修长，腰佩火纹玉佩",
        )],
    )
    future_text = (
        "前批章节暂无身份线索。"
        + "甲" * (portraits.CAST_DISCOVERY_FUTURE_CONTEXT_BUDGET * 2)
        + "青衣人摘下面具，萧炎这才认出他就是丁力。"
    )
    prompts: list[str] = []

    async def fake_chat(messages, **_kwargs):
        prompt = messages[0]["content"]
        prompts.append(prompt)
        if _kwargs["call_meta"]["discovery_phase"] == "future_identity":
            assert "前批章节暂无身份线索" not in prompt
            return json.dumps({"characters": [{
                "source_label": "青衣人",
                "canonical_name": "丁力",
                "identity_kind": "named",
                "kind": "onscreen",
                "evidence": "青衣人拦路",
                "future_evidence": "青衣人摘下面具，萧炎这才认出他就是丁力。",
            }]}, ensure_ascii=False)
        return json.dumps({"characters": [{
            "source_label": "青衣人",
            "canonical_name": "",
            "identity_kind": "functional",
            "kind": "onscreen",
            "evidence": "青衣人拦路",
            "future_evidence": "",
        }]}, ensure_ascii=False)

    monkeypatch.setattr(portraits.model_gateway, "chat", fake_chat)
    candidates = asyncio.run(portraits.discover_character_candidates(
        "青衣人拦住萧炎。",
        bible,
        21,
        future_text=future_text,
        future_label="第 31-40 章",
    ))

    assert len(prompts) == 2
    assert "他就是丁力" in prompts[-1]
    assert [(item["source_label"], item["name"], item["identity_kind"]) for item in candidates] == [
        ("青衣人", "丁力", "named"),
    ]


def test_future_identity_accepts_semantic_alias_with_verbatim_name_anchor(
    monkeypatch,
) -> None:
    bible = Bible(
        world=World(visual_style_canonical="都市漫画"),
        characters=[Character(
            name="赵振",
            role="重要配角",
            appearance_canonical="中年男子，深色西装，方脸短发，体格高大",
        )],
    )
    calls = 0

    async def fake_chat(*_args, **kwargs):
        nonlocal calls
        calls += 1
        assert kwargs["call_meta"]["discovery_phase"] == "future_identity"
        return json.dumps({"characters": [{
            "source_label": "那间学校的校长",
            "canonical_name": "赵振",
            "identity_kind": "named",
            "future_evidence": "聪慧的白洁马上反应过来是那个‘大象’赵振的主意。",
        }]}, ensure_ascii=False)

    monkeypatch.setattr(portraits.model_gateway, "chat", fake_chat)
    candidates = asyncio.run(portraits.resolve_future_identity_candidates(
        [{
            "name": "那间学校的校长",
            "source_label": "那间学校的校长",
            "identity_kind": "functional",
            "identity_group": "current:school-principal",
            "kind": "onscreen",
        }],
        source_text="那间学校的校长从楼上走下来。",
        future_text=(
            "到了酒店，原来那个男人是王申学校的校长。"
            "聪慧的白洁马上反应过来是那个“大象”赵振的主意。"
        ),
        bible=bible,
        episode_no=5,
        future_label="后续章节",
    ))

    assert calls == 1
    assert [
        (item["source_label"], item["name"], item["identity_kind"])
        for item in candidates
    ] == [("那间学校的校长", "赵振", "named")]


def test_identity_discovery_aligns_provider_expanded_source_label(monkeypatch) -> None:
    bible = Bible(
        world=World(visual_style_canonical="国风"),
        characters=[Character(
            name="李富贵",
            role="重要配角",
            appearance_canonical="圆脸胖少年，粗麻长衫，门牙醒目",
        )],
    )

    async def fake_chat(*_args, **_kwargs):
        return json.dumps({"characters": [{
            "source_label": "白白净净身较胖的少年",
            "canonical_name": "",
            "identity_kind": "functional",
            "functional_identity_key": "F1",
            "kind": "onscreen",
            "evidence": "原文中的白净胖少年",
        }]}, ensure_ascii=False)

    monkeypatch.setattr(portraits.model_gateway, "chat", fake_chat)
    candidates = asyncio.run(portraits.discover_character_candidates(
        "王有材身边另一个则是白白净净身较胖，正缩在裂缝里。",
        bible,
        1,
    ))

    assert candidates[0]["source_label"] == "白白净净身较胖"
    assert candidates[0]["model_source_label"] == "白白净净身较胖的少年"


def test_future_context_prioritizes_late_known_name_cooccurrence() -> None:
    future_text = (
        ("小胖子继续砍柴，没有报出姓名。" * 120)
        + "小胖子拍着胸口说，我李富贵认你这个朋友。"
    )

    context = portraits._future_identity_context(
        future_text,
        ["小胖子"],
        known_names=["李富贵"],
        current_text="白净胖少年被带上山。",
    )

    assert "我李富贵" in context
    assert "人物谱真名：李富贵" in context


def test_future_named_identity_upgrades_every_alias_in_same_group(monkeypatch) -> None:
    bible = Bible(
        world=World(visual_style_canonical="国风"),
        characters=[Character(
            name="许清",
            role="重要配角",
            appearance_canonical="银袍女子，面色苍白，黑发冷眸",
        )],
    )
    calls = 0

    async def fake_chat(*_args, **kwargs):
        nonlocal calls
        calls += 1
        if kwargs["call_meta"]["discovery_phase"] == "current":
            return json.dumps({"characters": [
                {
                    "source_label": "会飞的女人",
                    "canonical_name": "",
                    "identity_kind": "functional",
                    "functional_identity_key": "F1",
                    "kind": "onscreen",
                    "evidence": "银袍女子将众人卷走",
                },
                {
                    "source_label": "许师姐",
                    "canonical_name": "",
                    "identity_kind": "functional",
                    "functional_identity_key": "F1",
                    "kind": "mentioned",
                    "evidence": "同一女子被称为许师姐",
                },
            ]}, ensure_ascii=False)
        return json.dumps({"characters": [{
            "source_label": "许师姐",
            "canonical_name": "许清",
            "identity_kind": "named",
            "kind": "mentioned",
            "evidence": "许师姐是同一女子",
            "future_evidence": "许师姐转身，众人称她许清。",
        }]}, ensure_ascii=False)

    monkeypatch.setattr(portraits.model_gateway, "chat", fake_chat)
    candidates = asyncio.run(portraits.discover_character_candidates(
        "会飞的女人出现，绿袍修士称她为许师姐。",
        bible,
        1,
        future_text="许师姐转身，众人称她许清。",
        future_label="后续章节",
    ))

    assert calls == 2
    assert {
        (item["source_label"], item["name"], item["identity_kind"])
        for item in candidates
    } == {
        ("会飞的女人", "许清", "named"),
        ("许师姐", "许清", "named"),
    }


def test_structural_audit_recovers_entity_omitted_by_current_pass(monkeypatch) -> None:
    bible = Bible(
        world=World(visual_style_canonical="国风"),
        characters=[
            Character(
                name="孟浩",
                role="主角",
                appearance_canonical="黑发书生，蓝色长衫，手持葫芦",
            ),
            Character(
                name="李富贵",
                role="重要配角",
                appearance_canonical="圆脸胖少年，粗麻长衫，门牙醒目",
            ),
        ],
    )

    phases: list[str] = []

    async def fake_chat(messages, **kwargs):
        phases.append(kwargs["call_meta"]["discovery_phase"])
        if kwargs["call_meta"]["discovery_phase"] == "current":
            return json.dumps({"characters": [{
                "source_label": "孟浩",
                "canonical_name": "孟浩",
                "identity_kind": "named",
                "kind": "onscreen",
                "evidence": "当前主角",
            }]}, ensure_ascii=False)
        assert "白白净净身较胖" in messages[0]["content"]
        assert "SRC0001" in messages[0]["content"]
        assert "我李富贵" not in messages[0]["content"]
        return json.dumps({"characters": [{
            "source_label": "白白净净身较胖",
            "canonical_name": "李富贵",
            "identity_kind": "named",
            "kind": "onscreen",
            "evidence": "当前集独立出场的白净胖少年",
            "future_evidence": "后续以小胖子承接并自报李富贵",
        }]}, ensure_ascii=False)

    monkeypatch.setattr(portraits.model_gateway, "chat", fake_chat)
    candidates = asyncio.run(portraits.discover_character_candidates(
        "孟浩身边另一个则是白白净净身较胖。",
        bible,
        1,
        future_text="小胖子跟随孟浩。后来小胖子说：我李富贵认你这个朋友。",
        future_label="后续章节",
        structural_evidence=[{
            "identity_key": "unbound_person",
            "source_segment_ids": ["SRC0001"],
            "usage": "visible",
        }],
    ))

    assert any(
        item["source_label"] == "白白净净身较胖"
        and item["name"] == "李富贵"
        and item["identity_kind"] == "named"
        for item in candidates
    )
    assert phases == ["current", "coverage"]


def test_identity_discovery_does_not_run_fixed_coverage_without_structural_evidence(
    monkeypatch,
) -> None:
    bible = Bible(
        world=World(visual_style_canonical="国风"),
        characters=[],
    )
    phases: list[str] = []

    async def fake_chat(_messages, **kwargs):
        phases.append(kwargs["call_meta"]["discovery_phase"])
        return json.dumps({"characters": []}, ensure_ascii=False)

    monkeypatch.setattr(portraits.model_gateway, "chat", fake_chat)
    assert asyncio.run(portraits.discover_character_candidates(
        "孟浩身边另一个则是白白净净身较胖。",
        bible,
        1,
    )) == []
    assert phases == ["current"]


def test_stable_unique_title_is_accepted_as_named_identity(monkeypatch) -> None:
    bible = Bible(
        characters=[],
        world=World(visual_style_canonical="国风"),
    )

    async def fake_chat(*_args, **kwargs):
        if kwargs["call_meta"]["discovery_phase"] == "current":
            return json.dumps({"characters": [{
                "source_label": "靠山老祖",
                "canonical_name": "",
                "identity_kind": "functional",
                "functional_identity_key": "F1",
                "kind": "mentioned",
                "evidence": "本集提到建立宗门的老祖",
            }]}, ensure_ascii=False)
        return json.dumps({"characters": [{
            "source_label": "靠山老祖",
            "canonical_name": "靠山老祖",
            "identity_kind": "named",
            "kind": "mentioned",
            "evidence": "跨章节唯一指向建立宗门的同一位老祖",
            "future_evidence": "靠山老祖定下门规",
        }]}, ensure_ascii=False)

    monkeypatch.setattr(portraits.model_gateway, "chat", fake_chat)
    candidates = asyncio.run(portraits.discover_character_candidates(
        "靠山老祖建立宗门，靠山老祖后来失踪。",
        bible,
        2,
        future_text="靠山老祖定下门规，靠山老祖的画像仍在宗门。",
        future_label="后续章节",
    ))

    assert candidates[0]["name"] == "靠山老祖"
    assert candidates[0]["identity_kind"] == "named"


def test_future_functional_relation_label_is_not_promoted_by_text_presence(
    monkeypatch,
) -> None:
    bible = Bible(
        characters=[],
        world=World(visual_style_canonical="都市漫画"),
    )

    phases: list[str] = []

    async def fake_chat(*_args, **kwargs):
        phases.append(kwargs["call_meta"]["discovery_phase"])
        if kwargs["call_meta"]["discovery_phase"] == "current":
            return json.dumps({"characters": [{
                "source_label": "她男朋友",
                "canonical_name": "",
                "identity_kind": "functional",
                "functional_identity_key": "F1",
                "kind": "onscreen",
                "evidence": "她男朋友帮忙拎行李",
            }]}, ensure_ascii=False)
        return json.dumps({"characters": [{
            "source_label": "她男朋友",
            "canonical_name": "",
            "identity_kind": "functional",
            "functional_identity_key": "F1",
            "kind": "onscreen",
            "evidence": "她男朋友帮忙拎行李",
            "future_evidence": "",
        }]}, ensure_ascii=False)

    monkeypatch.setattr(portraits.model_gateway, "chat", fake_chat)
    candidates = asyncio.run(portraits.discover_character_candidates(
        "她男朋友帮忙拎行李。",
        bible,
        3,
        future_text="后来她男朋友又来了一次，仍未交代姓名。",
        future_label="后续章节",
    ))

    assert [
        (item["source_label"], item["name"], item["identity_kind"])
        for item in candidates
    ] == [("她男朋友", "她男朋友", "functional")]
    assert phases == ["current", "future_identity"]


def test_future_functional_enum_drift_can_use_existing_bible_identity(
    monkeypatch,
) -> None:
    bible = Bible(
        characters=[Character(
            name="李富贵",
            role="重要配角",
            appearance_canonical="圆脸胖少年，粗麻长衫，门牙醒目",
        )],
        world=World(visual_style_canonical="国风"),
    )

    async def fake_chat(*_args, **kwargs):
        if kwargs["call_meta"]["discovery_phase"] == "current":
            return json.dumps({"characters": [{
                "source_label": "小胖子",
                "canonical_name": "",
                "identity_kind": "functional",
                "functional_identity_key": "F1",
                "kind": "onscreen",
                "evidence": "小胖子跟随孟浩",
            }]}, ensure_ascii=False)
        return json.dumps({"characters": [{
            "source_label": "小胖子",
            "canonical_name": "李富贵",
            "identity_kind": "named",
            "functional_identity_key": "F1",
            "kind": "onscreen",
            "evidence": "小胖子跟随孟浩",
            "future_evidence": "小胖子拍着胸口说，我李富贵认你这个朋友。",
        }]}, ensure_ascii=False)

    monkeypatch.setattr(portraits.model_gateway, "chat", fake_chat)
    candidates = asyncio.run(portraits.discover_character_candidates(
        "小胖子跟随孟浩。",
        bible,
        2,
        future_text="小胖子拍着胸口说，我李富贵认你这个朋友。",
        future_label="后续章节",
    ))

    assert [
        (item["source_label"], item["name"], item["identity_kind"])
        for item in candidates
    ] == [("小胖子", "李富贵", "named")]


def test_character_resolutions_persist_and_future_identity_upgrades_route_fallback() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE episodes(id TEXT PRIMARY KEY, screenplay_character_resolutions TEXT NOT NULL DEFAULT '[]')"
    )
    conn.execute("INSERT INTO episodes(id) VALUES('e1')")
    first = portraits.persist_screenplay_character_resolutions(conn, "e1", [{
        "source_label": "青衣人",
        "canonical_name": "路人甲",
        "resolution": "functional_extra",
    }])
    upgraded = portraits.persist_screenplay_character_resolutions(conn, "e1", [{
        "source_label": "青衣人",
        "canonical_name": "丁力",
        "resolution": "future_identity",
    }])

    assert first[0]["canonical_name"] == "路人甲"
    assert upgraded[0]["canonical_name"] == "丁力"
    assert portraits.load_screenplay_character_resolutions(conn, "e1") == upgraded


def test_discovery_persistence_retires_only_legacy_future_identity_rows() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE episodes(id TEXT PRIMARY KEY, screenplay_character_resolutions TEXT NOT NULL DEFAULT '[]')"
    )
    conn.execute("INSERT INTO episodes(id) VALUES('e1')")
    portraits.persist_screenplay_character_resolutions(conn, "e1", [
        {
            "source_label": "旧称谓",
            "canonical_name": "旧猜测",
            "resolution": "future_identity",
        },
        {
            "source_label": "门卫",
            "canonical_name": "门卫",
            "resolution": "functional_identity",
        },
    ])

    current = portraits.persist_screenplay_character_resolutions(
        conn,
        "e1",
        [{
            "source_label": "新称谓",
            "canonical_name": "已证实名",
            "resolution": "future_identity",
            "decision_contract_version": portraits.FUTURE_IDENTITY_DECISION_VERSION,
        }],
        retire_legacy_future_identity=True,
    )

    assert {
        (item["source_label"], item["resolution"])
        for item in current
    } == {
        ("门卫", "functional_identity"),
        ("新称谓", "future_identity"),
    }


def test_character_resolution_merge_preserves_distinct_scoped_authorities() -> None:
    merged = portraits.merge_screenplay_character_resolutions([], [
        {
            "source_label": "穿着绿色长袍的男",
            "canonical_name": "绿袍修士甲",
            "resolution": "functional_identity",
            "authority_id": "functional:green-a",
            "source_instance_key": "functional:green-a",
        },
        {
            "source_label": "穿着绿色长袍的男",
            "canonical_name": "绿袍修士乙",
            "resolution": "functional_identity",
            "authority_id": "functional:green-b",
            "source_instance_key": "functional:green-b",
        },
    ])

    assert [item["authority_id"] for item in merged] == [
        "functional:green-a",
        "functional:green-b",
    ]


def test_character_importance_window_remains_twenty_chapters() -> None:
    conn = _make_conn()
    _seed_project(conn, "美杜莎短暂现身。")
    conn.execute(
        "INSERT INTO chapters(project_id,idx,content) VALUES('p1',50,?)",
        ("美杜莎在二十章窗口边界再次登场。",),
    )
    fragments, label = portraits._forward_fragments(conn, "p1", "美杜莎", 21)

    assert "二十章窗口边界" in fragments
    assert "+20 章" in label


def test_unresolved_descriptive_people_keep_source_labels(monkeypatch) -> None:
    conn = _make_conn()
    _seed_project(conn, "绿袍男子与大汉守在门前。")
    _patch_settings(monkeypatch, conn)

    async def fake_chat(*_args, **_kwargs):
        return json.dumps({
            "characters": [
                {
                    "source_label": "绿袍男子", "canonical_name": "",
                    "identity_kind": "functional", "kind": "onscreen", "evidence": "绿袍男子守门",
                },
                {
                    "source_label": "大汉", "canonical_name": "",
                    "identity_kind": "functional", "kind": "onscreen", "evidence": "大汉守门",
                },
            ],
        }, ensure_ascii=False)

    async def forbidden_ensure(*_args, **_kwargs):
        raise AssertionError("过渡称谓不得建人物卡")

    monkeypatch.setattr(portraits.model_gateway, "chat", fake_chat)
    monkeypatch.setattr(portraits, "ensure_character_card", forbidden_ensure)
    bible = Bible.model_validate(json.loads(
        conn.execute("SELECT bible_json FROM projects WHERE id='p1'").fetchone()["bible_json"]
    ))

    result = asyncio.run(portraits.ensure_cards_for_text(
        "p1", 21, "绿袍男子与大汉守在门前。", bible,
        generate_portraits=False,
    ))

    assert [
        (
            item["source_label"],
            item["canonical_name"],
            item["resolution"],
        )
        for item in result["resolutions"]
    ] == [
        ("绿袍男子", "绿袍男子", "functional_identity"),
        ("大汉", "大汉", "functional_identity"),
    ]
    assert result["checked"] == 0


def test_confirmed_real_name_is_not_downgraded_to_route_extra(monkeypatch) -> None:
    conn = _make_conn()
    _seed_project(conn, "青衣人拦路，后被认出是丁力。")
    _patch_settings(monkeypatch, conn)

    async def fake_candidates(*_args, **_kwargs):
        return [{
            "name": "丁力",
            "source_label": "青衣人",
            "identity_kind": "named",
            "kind": "onscreen",
            "evidence": "青衣人拦路",
            "future_evidence": "被认出是丁力",
        }]

    async def incomplete_card(*_args, **kwargs):
        assert kwargs["require_identity_card"] is True
        return {"status": "skipped_minor", "name": "丁力", "reason": "戏份少"}

    monkeypatch.setattr(portraits, "discover_character_candidates", fake_candidates)
    monkeypatch.setattr(portraits, "ensure_character_card", incomplete_card)
    bible = Bible.model_validate(json.loads(
        conn.execute("SELECT bible_json FROM projects WHERE id='p1'").fetchone()["bible_json"]
    ))

    result = asyncio.run(portraits.ensure_cards_for_text(
        "p1", 21, "青衣人拦路。", bible, generate_portraits=False,
    ))

    assert result["resolutions"] == []
    assert result["errors"] == ["丁力：真名已确认，但人物卡未完成：戏份少"]


def test_baseline_audit_uses_model_to_classify_arbitrary_descriptive_identity(monkeypatch) -> None:
    bible = Bible(
        world=World(visual_style_canonical="国风"),
        characters=[Character(
            name="萧炎", role="主角",
            appearance_canonical="黑发少年，玄色劲装，目光坚定，身形修长，腰佩火纹玉佩",
        )],
    )

    async def fake_chat(*_args, **_kwargs):
        return json.dumps({"characters": [{
            "source_label": "紫甲女子",
            "canonical_name": "",
            "identity_kind": "functional",
            "kind": "onscreen",
            "evidence": "紫甲女子拦路",
            "future_evidence": "",
        }]}, ensure_ascii=False)

    monkeypatch.setattr(portraits.model_gateway, "chat", fake_chat)
    draft = EpisodeScreenplay(
        episode_no=21,
        scene_outline=[ScriptScene(
            scene_no=1,
            scene_heading="【场1】日 / 山门",
            story_function="触发拦路冲突",
            characters=["萧炎", "紫甲女子"],
            summary="紫甲女子在山门前拦住萧炎，双方的冲突随即升级。",
        )],
    ).model_dump_json()

    candidates = asyncio.run(portraits.discover_character_candidates(
        "萧炎来到山门。", bible, 21, draft_text=draft,
    ))

    assert [(item["source_label"], item["identity_kind"]) for item in candidates] == [
        ("紫甲女子", "functional"),
    ]


def test_baseline_audit_sends_typed_identity_projection_only(monkeypatch) -> None:
    bible = Bible(
        world=World(visual_style_canonical="国风"),
        characters=[Character(
            name="萧炎", role="主角",
            appearance_canonical="黑发少年，玄色劲装，目光坚定，身形修长，腰佩火纹玉佩",
        )],
    )
    prompts: list[str] = []

    async def fake_chat(messages, **_kwargs):
        prompt = messages[0]["content"]
        prompts.append(prompt)
        assert _kwargs["call_meta"]["discovery_phase"] == "current"
        assert "SOURCE_BODY_MARKER" not in prompt
        assert "SCRIPT_ACTION_MARKER" not in prompt
        assert "紫甲女子" in prompt
        return json.dumps({"characters": [{
            "source_label": "紫甲女子",
            "canonical_name": "",
            "identity_kind": "functional",
            "kind": "onscreen",
            "evidence": "类型合同中的场次人物",
            "future_evidence": "",
        }]}, ensure_ascii=False)

    monkeypatch.setattr(portraits.model_gateway, "chat", fake_chat)
    draft = EpisodeScreenplay(
        episode_no=21,
        scene_outline=[ScriptScene(
            scene_no=1,
            scene_heading="【场1】日 / 山门",
            story_function="SCRIPT_ACTION_MARKER",
            characters=["萧炎", "紫甲女子"],
            summary="SCRIPT_ACTION_MARKER",
        )],
        full_script_text="【场1】日 / 山门\nSCRIPT_ACTION_MARKER",
    ).model_dump_json()

    candidates = asyncio.run(portraits.discover_character_candidates(
        "SOURCE_BODY_MARKER", bible, 21, draft_text=draft,
    ))

    assert len(prompts) == 1
    assert [(item["source_label"], item["identity_kind"]) for item in candidates] == [
        ("紫甲女子", "functional"),
    ]


def test_draft_identity_projection_keeps_structured_annotated_speaker() -> None:
    script = EpisodeScreenplay(
        episode_no=5,
        full_script_text=(
            "【场1】夜 / 室内\n"
            "路人乙（小晶的声音）：我在信里说明经过。"
        ),
        dialogue_chains=[KeyDialogueChain(
            chain_id="DC1",
            turns=[KeyDialogueTurn(
                speaker="路人乙（小晶的声音）",
                line="我在信里说明经过。",
                source_text="我在信里说明经过。",
            )],
        )],
    )

    projection = json.loads(
        portraits._draft_identity_projection(script.model_dump_json())
    )
    values = [item["value"] for item in projection["identity_mentions"]]

    assert "路人乙（小晶的声音）" in values
    assert "路人乙" not in values


def test_identity_annotation_normalization_requires_authoritative_base() -> None:
    bible = Bible(
        world=World(visual_style_canonical="国风"),
        characters=[Character(
            name="小晶",
            role="配角",
            appearance_canonical="黑色长发，浅色衬衫，神情克制",
        )],
    )
    script = EpisodeScreenplay(
        episode_no=5,
        scene_outline=[ScriptScene(
            scene_no=1,
            scene_heading="【场1】夜 / 室内",
            story_function="读信",
            characters=["小晶（画外音）", "井下回声（画外）"],
            summary="小晶的信件内容被读出。",
        )],
        full_script_text=(
            "【场1】夜 / 室内\n"
            "小晶（画外音）：这是信的内容。\n"
            "路人乙（小晶的声音）：这是错误的说话人标签。"
        ),
        dialogue_chains=[KeyDialogueChain(
            chain_id="DC1",
            turns=[
                KeyDialogueTurn(
                    speaker="小晶（画外音）",
                    line="这是信的内容。",
                    source_text="这是信的内容。",
                ),
                KeyDialogueTurn(
                    speaker="路人乙（小晶的声音）",
                    line="这是错误的说话人标签。",
                    source_text="这是错误的说话人标签。",
                ),
            ],
        )],
        narrative_plan=NarrativeContinuityPlan(
            scope_id="episode-5",
            identity_contracts=[NarrativeIdentityContract(
                identity_id="voice-well",
                display_name="井下回声",
                kind="画外声源",
                visual_policy="offscreen_only",
                asset_requirement="forbidden",
                voice_ids=["井下回声"],
                evidence=IdentityContractEvidence(
                    proposition_ids=["P1"],
                    rationale="来源只定义声音，不定义可见实体",
                ),
            )],
        ),
        voice_bible=[
            VoiceCanonical(
                speaker_id="小晶",
                voice_canonical="克制的年轻声音",
            ),
            VoiceCanonical(
                speaker_id="井下回声",
                voice_canonical="遥远的回声",
                role_type="offscreen_speaker",
            ),
        ],
    )

    changes = portraits.normalize_screenplay_identity_annotations(script, bible)

    assert changes == [{
        "source_label": "小晶（画外音）",
        "canonical_name": "小晶",
        "resolution": "authority_annotation",
    }]
    assert script.scene_outline[0].characters == ["小晶", "井下回声（画外）"]
    assert script.dialogue_chains[0].turns[0].speaker == "小晶"
    assert script.dialogue_chains[0].turns[1].speaker == "路人乙（小晶的声音）"
    assert "小晶：这是信的内容。" in script.full_script_text
    assert "路人乙（小晶的声音）" in script.full_script_text


def test_existing_bible_name_that_looks_generic_keeps_its_canonical_identity(monkeypatch) -> None:
    bible = Bible(
        world=World(visual_style_canonical="国风"),
        characters=[Character(
            name="少年", role="主角",
            appearance_canonical="十六岁黑发少年，蓝色长衫，身形清瘦，目光坚定，衣着朴素整洁",
        )],
    )

    async def fake_chat(*_args, **_kwargs):
        return json.dumps({"characters": [{
            "source_label": "少年", "canonical_name": "少年",
            "identity_kind": "named", "kind": "onscreen", "evidence": "少年转身迎战",
        }]}, ensure_ascii=False)

    monkeypatch.setattr(portraits.model_gateway, "chat", fake_chat)
    candidates = asyncio.run(portraits.discover_character_candidates(
        "少年转身迎战。", bible, 21,
    ))

    assert candidates[0]["name"] == "少年"
    assert candidates[0]["identity_kind"] == "named"


def test_screenplay_resolution_is_applied_before_publish_and_keeps_source_evidence() -> None:
    script = EpisodeScreenplay(
        episode_no=21,
        scene_outline=[ScriptScene(
            scene_no=1,
            scene_heading="【场1】日 / 山门",
            story_function="绿袍男子拦路并触发冲突",
            characters=["萧炎", "绿袍男子"],
            summary="绿袍男子站到萧炎面前，厉声阻止他继续前行。",
            source_basis="原文写绿袍男子拦在山门前。",
        )],
        full_script_text="【场1】日 / 山门\n绿袍男子拦住萧炎。\n绿袍男子：止步！\n萧炎：让开。",
        dialogue_chains=[KeyDialogueChain(
            chain_id="DC1",
            turns=[KeyDialogueTurn(
                speaker="绿袍男子",
                line="止步！",
                source_text="绿袍男子厉声道：止步！",
            )],
        )],
        information_ledger=[InformationItem(
            info_id="I1", content="绿袍男子拦住萧炎", speaker_id="绿袍男子",
        )],
        voice_bible=[VoiceCanonical(
            speaker_id="绿袍男子", voice_canonical="低沉粗粝",
        )],
    )
    resolutions = [{
        "source_label": "绿袍男子",
        "canonical_name": "路人甲",
        "resolution": "functional_extra",
    }]

    assert portraits.screenplay_character_resolution_errors(script, resolutions)
    changes = portraits.apply_screenplay_character_resolutions(script, resolutions)

    assert changes == [{
        "source_label": "绿袍男子", "canonical_name": "路人甲",
        "resolution": "functional_extra",
    }]
    assert script.scene_outline[0].characters == ["萧炎", "路人甲"]
    assert "路人甲拦住萧炎" in script.full_script_text
    assert "路人甲：止步！" in script.full_script_text
    assert script.dialogue_chains[0].turns[0].speaker == "路人甲"
    assert script.dialogue_chains[0].turns[0].source_text == "绿袍男子厉声道：止步！"
    assert script.scene_outline[0].source_basis == "原文写绿袍男子拦在山门前。"
    assert script.voice_bible[0].speaker_id == "路人甲"
    assert script.voice_bible[0].role_type == "functional_character"
    assert portraits.screenplay_character_resolution_errors(script, resolutions) == []


def test_resolution_does_not_turn_non_dialogue_prefix_into_speaker() -> None:
    script = EpisodeScreenplay(
        episode_no=1,
        full_script_text=(
            "【场1】夜 / 歌厅\n"
            "并行画面：王申和同事在歌厅唱歌。\n"
            "王申：我先回去了。"
        ),
        dialogue_chains=[KeyDialogueChain(
            chain_id="DC1",
            turns=[KeyDialogueTurn(
                speaker="王申",
                line="我先回去了。",
                source_text="我先回去了。",
            )],
        )],
    )

    portraits.apply_screenplay_character_resolutions(script, [{
        "source_label": "并行画面",
        "canonical_name": "路人11",
        "resolution": "functional_extra",
    }])

    assert "并行画面：王申和同事在歌厅唱歌。" in script.full_script_text
    assert "路人11：" not in script.full_script_text


def test_dialogue_normalization_demotes_unowned_colon_line_to_action() -> None:
    from app.validators import normalize_screenplay_dialogue_chains

    script = EpisodeScreenplay(
        episode_no=1,
        full_script_text=(
            "【场1】夜 / 歌厅\n"
            "路人11：王申和同事在歌厅唱歌。\n"
            "王申：我先回去了。"
        ),
        dialogue_chains=[KeyDialogueChain(
            chain_id="DC1",
            turns=[KeyDialogueTurn(
                speaker="王申",
                line="我先回去了。",
                source_text="我先回去了。",
            )],
        )],
    )

    normalize_screenplay_dialogue_chains(script)

    assert "路人11，王申和同事在歌厅唱歌。" in script.full_script_text
    assert "王申：我先回去了。" in script.full_script_text


def test_identity_gate_uses_shared_speaker_parser_and_allows_narrator() -> None:
    bible = Bible(
        world=World(visual_style_canonical="国风"),
        characters=[Character(
            name="萧炎", role="主角",
            appearance_canonical="黑发少年，玄色劲装，目光坚定，身形修长，腰佩火纹玉佩",
        )],
    )
    script = EpisodeScreenplay(
        episode_no=1,
        scene_outline=[ScriptScene(
            scene_no=1,
            scene_heading="【场1】日 / 山门",
            story_function="交代山门对峙",
            characters=["萧炎"],
            summary="山门骤然安静，萧炎站到众人面前准备迎战。",
        )],
        full_script_text="【场1】日 / 山门\n旁白：山门骤然安静。\n萧炎：我来应战。",
        information_ledger=[InformationItem(
            info_id="I1", content="山门骤然安静", delivery_owner="narration", speaker_id="旁白",
        )],
        voice_bible=[VoiceCanonical(
            speaker_id="旁白", voice_canonical="沉稳克制", role_type="narrator",
        )],
    )

    assert portraits.screenplay_unknown_identity_errors(script, bible) == []

    script.full_script_text += "\n青衣人：此路不通。"
    errors = portraits.screenplay_unknown_identity_errors(script, bible)
    assert len(errors) == 1
    assert "青衣人" in errors[0]


def test_voice_alias_is_normalized_only_from_unambiguous_ledger_identity() -> None:
    bible = Bible(
        world=World(visual_style_canonical="国风"),
        characters=[
            Character(
                name="孟浩",
                role="主角",
                appearance_canonical="黑发书生，青色长衫，身形清瘦，背着旧书箱",
            ),
            Character(
                name="王有材",
                role="重要配角",
                appearance_canonical="圆脸少年，粗布短衣，身形敦实，神态慌张",
            ),
        ],
    )
    script = EpisodeScreenplay(
        episode_no=1,
        narrative_plan=NarrativeContinuityPlan(scope_id="episode-1"),
        scene_outline=[ScriptScene(
            scene_no=1,
            scene_heading="【场1】日 / 山顶",
            story_function="孟浩决定离开山顶寻找出路",
            characters=["孟浩", "王有材"],
            summary="孟浩听见王有材求救，转身寻找声音来源。",
        )],
        full_script_text=(
            "【场1】日 / 山顶\n"
            "孟浩：又落榜了。\n"
            "王有材：救命！"
        ),
        dialogue_chains=[KeyDialogueChain(
            chain_id="DC1",
            turns=[
                KeyDialogueTurn(
                    speaker="孟浩",
                    line="又落榜了。",
                    source_text="又落榜了。",
                ),
                KeyDialogueTurn(
                    speaker="王有材",
                    line="救命！",
                    source_text="救命！",
                ),
            ],
        )],
        information_ledger=[
            InformationItem(
                info_id="I1",
                content="孟浩连续三年科举落榜",
                speaker_id="V-MH",
            ),
            InformationItem(
                info_id="I2",
                content="孟浩听见王有材在山崖下求救",
                exact_text="救命！",
                speaker_id="V-WYC",
            ),
            InformationItem(
                info_id="I3",
                content="山崖下同时传来孟浩与王有材的声音",
                speaker_id="V-AMBIGUOUS",
            ),
        ],
        voice_bible=[
            VoiceCanonical(
                speaker_id="V-MH",
                voice_canonical="清瘦书生的年轻嗓音",
            ),
            VoiceCanonical(
                speaker_id="V-WYC",
                voice_canonical="慌张的少年嗓音",
            ),
            VoiceCanonical(
                speaker_id="V-AMBIGUOUS",
                voice_canonical="少年嗓音",
            ),
        ],
    )

    changes = portraits.normalize_screenplay_voice_ids(script, bible)

    assert changes == [{
        "source_label": "V-MH",
        "canonical_name": "孟浩",
        "resolution": "voice_alias_from_ledger",
    }, {
        "source_label": "V-WYC",
        "canonical_name": "王有材",
        "resolution": "voice_alias_from_ledger",
    }, {
        "source_label": "V-AMBIGUOUS",
        "canonical_name": "",
        "resolution": "non_voice_carrier_removed",
    }]
    assert script.voice_bible[0].speaker_id == "孟浩"
    assert script.information_ledger[0].speaker_id == "孟浩"
    assert script.voice_bible[1].speaker_id == "王有材"
    assert script.information_ledger[1].speaker_id == "王有材"
    assert len(script.voice_bible) == 2
    assert script.information_ledger[2].speaker_id is None


def test_voice_normalization_removes_only_unreferenced_unbound_entries() -> None:
    bible = Bible(
        world=World(visual_style_canonical="国风"),
        characters=[Character(
            name="孟浩",
            role="主角",
            appearance_canonical="清瘦书生，青色长衫，目光坚定",
        )],
    )
    script = EpisodeScreenplay(
        episode_no=1,
        narrative_plan=NarrativeContinuityPlan(scope_id="episode-1"),
        dialogue_chains=[KeyDialogueChain(
            chain_id="DC1",
            topic="门外来客",
            turns=[KeyDialogueTurn(
                speaker="门外来客",
                line="请开门。",
                source_text="请开门。",
            )],
        )],
        voice_bible=[
            VoiceCanonical(
                speaker_id="门外来客",
                voice_canonical="门外传来的低沉人声",
            ),
            VoiceCanonical(
                speaker_id="未引用声源",
                voice_canonical="短促的非语言声响",
                role_type="sound_effect",
            ),
        ],
    )

    changes = portraits.normalize_screenplay_voice_ids(script, bible)

    assert [voice.speaker_id for voice in script.voice_bible] == ["门外来客"]
    assert changes == [{
        "source_label": "未引用声源",
        "canonical_name": "",
        "resolution": "unreferenced_voice_removed",
    }]


def test_voice_normalization_projects_non_voice_delivery_out_of_speaker_fields() -> None:
    bible = Bible(
        world=World(visual_style_canonical="国风"),
        characters=[Character(
            name="孟浩",
            role="主角",
            appearance_canonical="清瘦书生，青色长衫，目光坚定",
        )],
    )
    script = EpisodeScreenplay(
        episode_no=1,
        narrative_plan=NarrativeContinuityPlan(scope_id="episode-1"),
        full_script_text="未绑定声源：当——\n门外来客：请开门。",
        key_lines=["未绑定声源：当——", "门外来客：请开门。"],
        information_ledger=[InformationItem(
            info_id="I1",
            content="门外传来一声钟响。",
            delivery_owner="ambient_sound",
            speaker_id="未绑定声源",
            exact_text="当——",
        )],
        dialogue_chains=[KeyDialogueChain(
            chain_id="DC1",
            topic="门外动静",
            turns=[
                KeyDialogueTurn(
                    speaker="未绑定声源",
                    line="当——",
                    source_text="当——",
                ),
                KeyDialogueTurn(
                    speaker="门外来客",
                    line="请开门。",
                    source_text="请开门。",
                ),
            ],
        )],
        voice_bible=[
            VoiceCanonical(
                speaker_id="未绑定声源",
                voice_canonical="短促的非语言声响",
                role_type="sound_effect",
            ),
            VoiceCanonical(
                speaker_id="门外来客",
                voice_canonical="门外传来的低沉人声",
            ),
        ],
    )

    changes = portraits.normalize_screenplay_voice_ids(script, bible)

    assert script.information_ledger[0].speaker_id is None
    assert [turn.speaker for turn in script.dialogue_chains[0].turns] == ["门外来客"]
    assert script.key_lines == ["门外来客：请开门。"]
    assert script.full_script_text == "【当——】\n门外来客：请开门。"
    assert [voice.speaker_id for voice in script.voice_bible] == ["门外来客"]
    assert changes == [{
        "source_label": "未绑定声源",
        "canonical_name": "",
        "resolution": "non_voice_carrier_removed",
    }]


def test_source_identity_contexts_cover_complete_long_source() -> None:
    source = "甲" * 19 + "\n\n" + "乙" * 17

    chunks = portraits._source_identity_contexts(source, budget=10)

    assert len(chunks) == 4
    assert "".join(chunks) == source.replace("\n", "")


def test_future_identity_keeps_current_display_label() -> None:
    script = EpisodeScreenplay(
        episode_no=1,
        scene_outline=[ScriptScene(
            scene_no=1,
            scene_heading="【场1】夜 / 山门",
            story_function="神秘来客阻路",
            characters=["青衣人"],
            summary="青衣人挡在门前，没有公开姓名。",
            source_basis="原文只称青衣人。",
        )],
        full_script_text="【场1】夜 / 山门\n青衣人挡在门前。\n青衣人：止步。",
        dialogue_chains=[KeyDialogueChain(
            chain_id="DC1",
            topic="阻路",
            turns=[KeyDialogueTurn(
                speaker="青衣人",
                line="止步。",
                source_text="止步。",
            )],
        )],
    )

    portraits.apply_screenplay_character_resolutions(script, [{
        "source_label": "青衣人",
        "canonical_name": "丁力",
        "resolution": "future_identity",
    }])

    assert script.scene_outline[0].characters == ["丁力"]
    assert script.dialogue_chains[0].turns[0].speaker == "丁力"
    assert "青衣人挡在门前" in script.full_script_text
    assert "丁力：止步" in script.full_script_text
    assert "丁力" not in script.scene_outline[0].summary


def test_late_episode_screenplay_auto_adds_character_and_defers_portrait_generation(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "late-episode-character.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()
    conn = db.get_conn()
    bible = Bible(
        world=World(visual_style_canonical="国风"),
        characters=[Character(
            name="萧炎",
            role="主角",
            appearance_canonical="黑发少年，玄色劲装，目光坚定，身形修长，腰佩火纹玉佩",
        )],
    )
    conn.execute(
        "INSERT INTO projects(id,name,status,bible_json,bible_version,bible_status,created_at) "
        "VALUES('p1','斗破苍穹','planned',?,1,'ready',1)",
        (bible.model_dump_json(),),
    )
    conn.execute(
        "INSERT INTO chapters(project_id,idx,title,content,char_count) VALUES(?,?,?,?,?)",
        ("p1", 1926, "第一千六百二十二章 双帝之战",
         "魂天帝踏着血云现身。魂天帝与萧炎在中州上空连续交锋。" * 8, 240),
    )
    conn.execute(
        """INSERT INTO episodes(
            id,project_id,episode_no,title,hook,cliffhanger,synopsis,source_chapters,
            target_duration_s,screenplay_status,status,created_at
        ) VALUES('e1926','p1',1926,'双帝之战','','','魂天帝现身','[1926]',50,'running','planned',1)"""
    )
    conn.commit()

    async def fake_candidates(source_text, current_bible, episode_no, *, draft_text="", **_kwargs):
        assert episode_no == 1926
        assert "魂天帝" in source_text
        return [{"name": "魂天帝", "kind": "onscreen", "evidence": "魂天帝踏着血云现身"}]

    async def fake_assess(*_args, **_kwargs):
        return {
            "important": True,
            "reason": "本章核心反派并反复出场",
            "role": "反派",
            "appearance_canonical": "中年男性，黑色长发披肩，暗红帝袍覆身，血色双瞳冷漠，周身缠绕血云",
            "personality": "冷酷",
            "speech_style": "低沉威压",
            "relationships": [{"to": "萧炎", "relation": "决战对手"}],
        }

    async def portrait_failure(*_args, **_kwargs):
        raise AssertionError("剧本阶段不应调用定妆图 Provider")

    generated_with: list[set[str]] = []

    async def fake_generate(ep_data, source_text, current_bible, prev_ending=""):
        names = {character.name for character in current_bible.characters}
        generated_with.append(names)
        assert "魂天帝" in names
        scenes = [
            ScriptScene(
                scene_no=index,
                scene_heading=f"【场{index}】日 / 中州天际",
                story_function="推进双帝决战并交接下一场冲突",
                characters=["萧炎", "魂天帝"],
                summary="萧炎与魂天帝在中州天际正面交锋，帝境力量持续碰撞。",
                conflict="双方争夺天地存亡的最终胜负",
                turn="帝境交锋进一步升级",
                source_basis="保留魂天帝现身并与萧炎连续交锋的原文事件",
            )
            for index in range(1, 4)
        ]
        return EpisodeScreenplay(
            episode_no=ep_data["episode_no"],
            title="双帝之战",
            scene_outline=scenes,
            full_script_text="【场1】魂天帝：今日便结束一切。\n萧炎：那就一战。",
        )

    async def fake_production(*, episode_id, episode, source_text, bible, **_kwargs):
        # 生产链仍应看到 preflight 已追加的 source-backed 角色
        script = await fake_generate(episode, source_text, bible)
        conn.execute(
            "UPDATE episodes SET screenplay_json=?, screenplay_status='ready', "
            "screenplay_error=NULL, screenplay_updated_at=? WHERE id=?",
            (script.model_dump_json(), db.now(), episode_id),
        )
        conn.commit()
        return script

    monkeypatch.setattr(portraits, "discover_character_candidates", fake_candidates)
    monkeypatch.setattr(portraits, "assess_new_character", fake_assess)
    monkeypatch.setattr(portraits, "_generate_fresh_portrait", portrait_failure)
    monkeypatch.setattr(
        "app.production.screenplay_repair.run_screenplay_production",
        fake_production,
    )

    result = asyncio.run(api._screenplay_task("e1926"))

    project = conn.execute(
        "SELECT bible_json,bible_version FROM projects WHERE id='p1'"
    ).fetchone()
    names = {
        item["name"] for item in json.loads(project["bible_json"])["characters"]
    }
    episode = conn.execute(
        "SELECT screenplay_status,screenplay_json,screenplay_error FROM episodes WHERE id='e1926'"
    ).fetchone()
    assert result is not None
    assert result.title == "双帝之战"
    assert names == {"萧炎", "魂天帝"}
    assert project["bible_version"] == 2
    assert generated_with == [{"萧炎", "魂天帝"}]
    assert episode["screenplay_status"] == "ready"
    assert episode["screenplay_json"] is not None
    assert episode["screenplay_error"] is None
    queue = json.loads(conn.execute(
        "SELECT bible_auto_changes_json FROM projects WHERE id='p1'"
    ).fetchone()["bible_auto_changes_json"])
    assert queue[0]["character"] == "魂天帝"
    assert queue[0]["status"] == "auto_applied_asset_pending"
