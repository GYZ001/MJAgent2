import asyncio
import json
import sqlite3

from app import api, db, portraits
from app.schemas import Bible, Character, EpisodeScreenplay, ScriptScene, World


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


def test_ensure_cards_for_screenplay_only_handles_unknown_names(monkeypatch) -> None:
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

    # 萧炎 已在人物谱 → 跳过；美杜莎/纳兰嫣然 为未知，各处理一次（美杜莎去重）
    assert {n for n, _ in seen} == {"美杜莎", "纳兰嫣然"}
    assert out["checked"] == 2 and len(out["added"]) == 2


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


def test_discover_character_candidates_filters_functional_extras_and_unseen_names(monkeypatch) -> None:
    bible = Bible(
        world=World(visual_style_canonical="国风"),
        characters=[Character(
            name="萧炎",
            role="主角",
            appearance_canonical="黑发少年，玄色劲装，目光坚定，身形修长，腰佩火纹玉佩",
        )],
    )

    async def fake_chat(*_args, **_kwargs):
        return json.dumps({
            "characters": [
                {"name": "魂天帝", "kind": "onscreen", "evidence": "魂天帝踏着血云现身"},
                {"name": "萧炎", "kind": "onscreen", "evidence": "萧炎迎空而起"},
                {"name": "守卫", "kind": "onscreen", "evidence": "守卫后退"},
                {"name": "不存在的人", "kind": "onscreen", "evidence": "模型臆造"},
            ],
        }, ensure_ascii=False)

    monkeypatch.setattr(portraits.model_gateway, "chat", fake_chat)
    result = asyncio.run(portraits.discover_character_candidates(
        "魂天帝踏着血云现身，萧炎迎空而起，守卫仓促后退。",
        bible,
        1926,
    ))

    assert [item["name"] for item in result] == ["魂天帝", "萧炎"]


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

    async def fake_candidates(source_text, current_bible, episode_no, *, draft_text=""):
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
