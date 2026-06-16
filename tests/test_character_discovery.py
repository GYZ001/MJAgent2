import asyncio
import json
import sqlite3

from app import portraits
from app.schemas import Bible, Character, World


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


def test_ensure_character_card_adds_prominent_new_character(monkeypatch) -> None:
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
    row = conn.execute("SELECT * FROM character_portraits WHERE character_name='美杜莎'").fetchone()
    assert row["ep_start"] == 21 and row["ep_end"] is None

    # 幂等：第二次直接 exists，不重复建卡/出图
    res2 = asyncio.run(portraits.ensure_character_card("p1", "美杜莎", 22))
    assert res2["status"] == "exists"
    cnt = conn.execute("SELECT COUNT(*) c FROM character_portraits WHERE character_name='美杜莎'").fetchone()["c"]
    assert cnt == 1


def test_ensure_character_card_still_adds_when_portrait_fails(monkeypatch) -> None:
    conn = _make_conn()
    _seed_project(conn, "美杜莎现身，紫色长发。美杜莎再次出手。美杜莎统领蛇人一族。" * 3)
    _patch_settings(monkeypatch, conn)

    async def fake_assess(*a, **k):
        return {"important": True, "reason": "反复出场", "role": "反派",
                "appearance_canonical": "紫发妖娆女子，紫色长发，金瞳蛇眸，蛇纹长裙，气场冷艳标志性蛇瞳",
                "personality": "", "speech_style": "", "relationships": []}

    async def boom(*a, **k):
        raise RuntimeError("seedream down")

    monkeypatch.setattr(portraits, "assess_new_character", fake_assess)
    monkeypatch.setattr(portraits, "_generate_fresh_portrait", boom)

    res = asyncio.run(portraits.ensure_character_card("p1", "美杜莎", 21))
    assert res["status"] == "added" and res["has_portrait"] is False
    # 仍补进人物谱，但没有定妆照行
    names = [c["name"] for c in json.loads(
        conn.execute("SELECT bible_json FROM projects WHERE id='p1'").fetchone()["bible_json"])["characters"]]
    assert "美杜莎" in names
    assert conn.execute("SELECT COUNT(*) c FROM character_portraits WHERE character_name='美杜莎'").fetchone()["c"] == 0


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
