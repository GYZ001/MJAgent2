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
