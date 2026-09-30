"""app.portraits.neutral_identity：中性身份定妆照（默认关闭）。

覆盖：
1. ``neutral_portrait_prompt`` 提示词构成——含体貌锚点/素衣/中性表情，不含
   原始外观锚点里的服装款式或默认表情文本；体貌锚点核验失败直接 ``ValueError``。
2. ``_physical_anchor_nomination_errors``：缺失申报 + 核验不过两类都报错。
3. ``precheck_neutral_identity``：图片张数、受影响段（按 from_episode 起已有
   分镜且提到该角色的集号）、未知角色报错。
4. ``stage_neutral_identity``：过期指纹拒绝；暂存不改变当前生效行；体貌核验
   失败向上传播。
5. ``adopt_neutral_identity``：正确切分区间、项目级 costume_mode 标记翻转；
   没有待采纳候选时报错。
"""
from __future__ import annotations

import sqlite3

import pytest

from app.portraits import neutral_identity as ni

_APPEARANCE = (
    "二十四岁的年轻女性，鹅蛋脸，乌黑顺直的长发垂到胸前，身材纤细，"
    "穿米白色宽松针织开衫，内搭浅蓝色细碎花棉质长裙，神情温柔，嘴角带着浅浅的笑"
)
_PHYSICAL = "二十四岁的年轻女性，鹅蛋脸，乌黑顺直的长发垂到胸前，身材纤细"


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE projects(id TEXT PRIMARY KEY, bible_json TEXT, bible_version INTEGER DEFAULT 0, "
        "portrait_costume_mode TEXT NOT NULL DEFAULT 'baked')"
    )
    conn.execute("CREATE TABLE episodes(id TEXT PRIMARY KEY, project_id TEXT, episode_no INTEGER)")
    conn.execute("CREATE TABLE shots(id TEXT PRIMARY KEY, episode_id TEXT, shot_no INTEGER, characters TEXT)")
    conn.execute(
        "CREATE TABLE character_portraits(id TEXT PRIMARY KEY, project_id TEXT, character_name TEXT, "
        "ep_start INTEGER, ep_end INTEGER, appearance TEXT, prompt TEXT, image_path TEXT, "
        "base_portrait_id TEXT, bible_version INTEGER DEFAULT 0, pack_status TEXT, created_at REAL, "
        "UNIQUE(project_id, character_name, ep_start))"  # 同真实 schema，让唯一约束冲突在测试里也真实暴露
    )
    conn.commit()
    return conn


def _seed_project(conn, *, project_id: str = "proj1", name: str = "温念") -> None:
    bible = {"world": {"visual_style_canonical": "国漫风"}, "characters": [
        {"name": name, "appearance_canonical": _APPEARANCE},
    ]}
    import json
    conn.execute(
        "INSERT INTO projects(id, bible_json, bible_version) VALUES(?,?,1)",
        (project_id, json.dumps(bible, ensure_ascii=False)),
    )
    conn.commit()


def _seed_current_portrait(conn, tmp_path, *, project_id="proj1", name="温念", ep_start=1) -> str:
    path = tmp_path / "current.jpg"
    path.write_bytes(b"fake")
    conn.execute(
        "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end, appearance, "
        "prompt, image_path, bible_version, created_at) VALUES(?,?,?,?,?,?,?,?,1,1.0)",
        ("portrait_current", project_id, name, ep_start, None, _APPEARANCE, "旧提示词", str(path)),
    )
    conn.commit()
    return "portrait_current"


def _seed_closed_historical_portrait(
    conn, tmp_path, *, project_id="proj1", name="温念", ep_start: int, ep_end: int,
) -> str:
    """已关闭的历史分段（``ep_end`` 非空）——不是当前生效行，``_open_portrait``
    查不到它，但它的 ``ep_start`` 仍受 ``UNIQUE(project_id,character_name,
    ep_start)`` 约束。"""
    path = tmp_path / f"hist_{ep_start}.jpg"
    path.write_bytes(b"fake")
    portrait_id = f"portrait_hist_{ep_start}"
    conn.execute(
        "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end, appearance, "
        "prompt, image_path, bible_version, created_at) VALUES(?,?,?,?,?,?,?,?,1,1.0)",
        (portrait_id, project_id, name, ep_start, ep_end, _APPEARANCE, "历史提示词", str(path)),
    )
    conn.commit()
    return portrait_id


def _seed_shot(conn, *, project_id="proj1", episode_no: int, name: str) -> None:
    import json
    ep_id = f"ep{episode_no}"
    conn.execute(
        "INSERT OR IGNORE INTO episodes(id, project_id, episode_no) VALUES(?,?,?)",
        (ep_id, project_id, episode_no),
    )
    conn.execute(
        "INSERT INTO shots(id, episode_id, shot_no, characters) VALUES(?,?,?,?)",
        (f"shot_{episode_no}", ep_id, 1, json.dumps([name], ensure_ascii=False)),
    )
    conn.commit()


# ---------- neutral_portrait_prompt ----------

def test_neutral_prompt_has_body_wardrobe_expression_not_original_clothing_or_smile() -> None:
    prompt = ni.neutral_portrait_prompt("国漫风", _APPEARANCE, _PHYSICAL)
    assert "鹅蛋脸" in prompt and "乌黑顺直的长发" in prompt
    assert ni._NEUTRAL_WARDROBE in prompt
    assert ni._NEUTRAL_EXPRESSION in prompt
    assert "针织开衫" not in prompt
    assert "浅浅的笑" not in prompt


def test_neutral_prompt_rejects_unverified_physical_description() -> None:
    with pytest.raises(ValueError):
        ni.neutral_portrait_prompt("国漫风", _APPEARANCE, "这个人很高大威猛")


def test_physical_anchor_nomination_errors_flags_missing_and_invalid() -> None:
    draft = ni._NeutralPhysicalAnchors(anchors=[
        ni._NeutralPhysicalAnchor(name="温念", physical_description="完全编造的一段话"),
    ])
    errors = ni._physical_anchor_nomination_errors(draft, {"温念": _APPEARANCE, "顾屿": "高大的青年男性"})
    assert any("温念" in e for e in errors)
    assert any("顾屿" in e for e in errors)


# ---------- precheck ----------

def test_precheck_reports_image_count_and_affected_segments() -> None:
    conn = _make_conn()
    _seed_project(conn)
    _seed_shot(conn, episode_no=3, name="温念")
    _seed_shot(conn, episode_no=5, name="温念")
    result = ni.precheck_neutral_identity(conn, "proj1", ["温念"], 2)
    assert result["image_count"] == len(ni.CHARACTER_REQUIRED_VIEWS)
    assert result["characters"][0]["affected_segments"] == [3, 5]
    assert result["fingerprint"]


def test_precheck_unknown_character_raises() -> None:
    conn = _make_conn()
    _seed_project(conn)
    with pytest.raises(ValueError):
        ni.precheck_neutral_identity(conn, "proj1", ["没有这个人"], 1)


# ---------- stage ----------

def _patch_stage_dependencies(monkeypatch, tmp_path) -> None:
    async def fake_nominate(appearance_by_name):
        return {name: _PHYSICAL for name in appearance_by_name}

    async def fake_generate(project_id, character_name, prompt, base_path):
        path = tmp_path / f"{character_name}_neutral.jpg"
        path.write_bytes(b"fake")
        return str(path)

    async def fake_pack(**kwargs):
        return {"status": "ready", "portrait_id": kwargs["portrait_id"]}

    monkeypatch.setattr(ni, "_nominate_physical_anchors", fake_nominate)
    monkeypatch.setattr(ni, "_generate_neutral_front_image", fake_generate)
    monkeypatch.setattr(ni, "ensure_character_multiview_pack", fake_pack)


@pytest.mark.asyncio
async def test_stage_does_not_touch_current_active_portrait_row(monkeypatch, tmp_path) -> None:
    conn = _make_conn()
    _seed_project(conn)
    current_id = _seed_current_portrait(conn, tmp_path)
    _patch_stage_dependencies(monkeypatch, tmp_path)
    pre = ni.precheck_neutral_identity(conn, "proj1", ["温念"], 5)

    result = await ni.stage_neutral_identity(conn, "proj1", ["温念"], 5, fingerprint=pre["fingerprint"])

    current_row = conn.execute("SELECT * FROM character_portraits WHERE id=?", (current_id,)).fetchone()
    assert current_row["ep_start"] == 1 and current_row["ep_end"] is None
    staged = conn.execute(
        "SELECT * FROM character_portraits WHERE project_id=? AND character_name=? AND ep_start=?",
        ("proj1", "温念", ni.STAGED_INITIAL_EP_START),
    ).fetchone()
    assert staged is not None and staged["costume_mode"] == "neutral"
    assert result["staged"][0]["name"] == "温念"


@pytest.mark.asyncio
async def test_stage_rejects_stale_fingerprint(monkeypatch, tmp_path) -> None:
    conn = _make_conn()
    _seed_project(conn)
    _patch_stage_dependencies(monkeypatch, tmp_path)
    with pytest.raises(ValueError):
        await ni.stage_neutral_identity(conn, "proj1", ["温念"], 5, fingerprint="不是真的指纹")


@pytest.mark.asyncio
async def test_stage_propagates_physical_anchor_verification_failure(monkeypatch, tmp_path) -> None:
    conn = _make_conn()
    _seed_project(conn)
    _patch_stage_dependencies(monkeypatch, tmp_path)

    async def bad_nominate(appearance_by_name):
        return {name: "完全编造的一段话" for name in appearance_by_name}

    monkeypatch.setattr(ni, "_nominate_physical_anchors", bad_nominate)
    pre = ni.precheck_neutral_identity(conn, "proj1", ["温念"], 5)
    with pytest.raises(ValueError):
        await ni.stage_neutral_identity(conn, "proj1", ["温念"], 5, fingerprint=pre["fingerprint"])


# ---------- adopt ----------

@pytest.mark.asyncio
async def test_adopt_splits_segment_and_marks_project_neutral(monkeypatch, tmp_path) -> None:
    conn = _make_conn()
    _seed_project(conn)
    current_id = _seed_current_portrait(conn, tmp_path, ep_start=1)
    _patch_stage_dependencies(monkeypatch, tmp_path)
    pre = ni.precheck_neutral_identity(conn, "proj1", ["温念"], 5)
    await ni.stage_neutral_identity(conn, "proj1", ["温念"], 5, fingerprint=pre["fingerprint"])

    result = ni.adopt_neutral_identity(conn, "proj1", "温念", 5)

    old_row = conn.execute("SELECT * FROM character_portraits WHERE id=?", (current_id,)).fetchone()
    assert old_row["ep_start"] == 1 and old_row["ep_end"] == 4
    new_row = conn.execute("SELECT * FROM character_portraits WHERE id=?", (result["portrait_id"],)).fetchone()
    assert new_row["ep_start"] == 5 and new_row["ep_end"] is None and new_row["costume_mode"] == "neutral"
    project_row = conn.execute("SELECT portrait_costume_mode FROM projects WHERE id='proj1'").fetchone()
    assert project_row["portrait_costume_mode"] == "neutral"


@pytest.mark.asyncio
async def test_adopt_from_same_episode_as_current_start_deletes_old_row(monkeypatch, tmp_path) -> None:
    """``from_episode`` 与当前行 ``ep_start`` 相同（或更早）时，收窄会把 ep_end
    压到比 ep_start 还小、且与新行撞上唯一约束；旧行改为整段删除（见
    ``adopt_neutral_identity`` 内对 ``_delete_portrait_segment`` 的调用）。"""
    conn = _make_conn()
    _seed_project(conn)
    current_id = _seed_current_portrait(conn, tmp_path, ep_start=5)
    _patch_stage_dependencies(monkeypatch, tmp_path)
    pre = ni.precheck_neutral_identity(conn, "proj1", ["温念"], 5)
    await ni.stage_neutral_identity(conn, "proj1", ["温念"], 5, fingerprint=pre["fingerprint"])

    result = ni.adopt_neutral_identity(conn, "proj1", "温念", 5)

    assert conn.execute("SELECT 1 FROM character_portraits WHERE id=?", (current_id,)).fetchone() is None
    new_row = conn.execute("SELECT * FROM character_portraits WHERE id=?", (result["portrait_id"],)).fetchone()
    assert new_row["ep_start"] == 5 and new_row["ep_end"] is None


@pytest.mark.asyncio
async def test_adopt_when_from_episode_matches_historical_segment_ep_start(monkeypatch, tmp_path) -> None:
    """评审确认的 blocking 缺陷复现：``from_episode`` 撞上的是一段已关闭的历史
    分段（而不是当前开区间行）的 ``ep_start``——旧实现只检查 ``_open_portrait``
    返回的当前行，历史行会在最后的 UPDATE 里撞
    ``UNIQUE(project_id,character_name,ep_start)`` 抛出未捕获的
    ``sqlite3.IntegrityError``。角色有历史分段 ep_start=1~3（已关闭）与当前
    开区间行 ep_start=4；from_episode=1 同时撞历史行的 ep_start 与需要删除
    当前行（ep_start=4>=from_episode=1）。修复后两行都应被物理删除且不抛错。
    """
    conn = _make_conn()
    _seed_project(conn)
    hist_id = _seed_closed_historical_portrait(conn, tmp_path, ep_start=1, ep_end=3)
    current_id = _seed_current_portrait(conn, tmp_path, ep_start=4)
    _patch_stage_dependencies(monkeypatch, tmp_path)
    pre = ni.precheck_neutral_identity(conn, "proj1", ["温念"], 1)
    await ni.stage_neutral_identity(conn, "proj1", ["温念"], 1, fingerprint=pre["fingerprint"])

    result = ni.adopt_neutral_identity(conn, "proj1", "温念", 1)

    assert conn.execute("SELECT 1 FROM character_portraits WHERE id=?", (hist_id,)).fetchone() is None
    assert conn.execute("SELECT 1 FROM character_portraits WHERE id=?", (current_id,)).fetchone() is None
    new_row = conn.execute("SELECT * FROM character_portraits WHERE id=?", (result["portrait_id"],)).fetchone()
    assert new_row["ep_start"] == 1 and new_row["ep_end"] is None and new_row["costume_mode"] == "neutral"


@pytest.mark.asyncio
async def test_adopt_when_from_episode_matches_earlier_unrelated_historical_segment(monkeypatch, tmp_path) -> None:
    """历史分段 ep_start 与 from_episode 撞上、但当前行不需要被取代（当前行
    ep_start 早于 from_episode，走既有的收窄分支）——两条分支各自独立生效，
    互不影响。"""
    conn = _make_conn()
    _seed_project(conn)
    hist_id = _seed_closed_historical_portrait(conn, tmp_path, ep_start=5, ep_end=6)
    current_id = _seed_current_portrait(conn, tmp_path, ep_start=1)
    _patch_stage_dependencies(monkeypatch, tmp_path)
    pre = ni.precheck_neutral_identity(conn, "proj1", ["温念"], 5)
    await ni.stage_neutral_identity(conn, "proj1", ["温念"], 5, fingerprint=pre["fingerprint"])

    result = ni.adopt_neutral_identity(conn, "proj1", "温念", 5)

    assert conn.execute("SELECT 1 FROM character_portraits WHERE id=?", (hist_id,)).fetchone() is None
    current_row = conn.execute("SELECT * FROM character_portraits WHERE id=?", (current_id,)).fetchone()
    assert current_row["ep_start"] == 1 and current_row["ep_end"] == 4
    new_row = conn.execute("SELECT * FROM character_portraits WHERE id=?", (result["portrait_id"],)).fetchone()
    assert new_row["ep_start"] == 5 and new_row["ep_end"] is None


def test_delete_row_superseded_by_open_start_rejects_row_starting_after_from_episode() -> None:
    """内部不变量守卫：调用方若传入一行 ``ep_start<from_episode``，说明它不会
    被新行（从 ``from_episode`` 起开区间）整段覆盖，直接报错，不静默物理删除
    （CLAUDE.md「不得兜底填充」的另一面——这里是不兜底删除）。"""
    conn = _make_conn()
    row = {"id": "x", "ep_start": 1, "ep_end": 3}
    with pytest.raises(ValueError):
        ni._delete_row_superseded_by_open_start(
            conn, character_name="温念", row=row, from_episode=5, reason="test",
        )


def test_adopt_without_staged_candidate_raises() -> None:
    conn = _make_conn()
    _seed_project(conn)
    with pytest.raises(ValueError):
        ni.adopt_neutral_identity(conn, "proj1", "温念", 5)
