"""存量定妆照肤色局部色块核验：缓存存取原语（``portrait_skin_audit_store``）
与业务编排（``portrait_skin_blush_audit.audit_project_portraits``）。用真实
``app.db.get_conn()``（conftest 的 per-test 隔离库），不手搭 schema 副本。
"""
from __future__ import annotations

import json

from app.db import get_conn
from app.portraits import portrait_skin_audit_store as store
from app.portraits import portrait_skin_blush_audit as audit
from app.portraits.portrait_skin_blush import PORTRAIT_SKIN_BLUSH_RULE_VERSION

_PHOTOGRAPHIC_STYLE = "真人实拍电影质感，照片级写实人像，原创虚构人物，自然光影，皮肤肌理清晰，全程实拍写实渲染，不出现卡通、动画或CG质感。"
_NON_PHOTOGRAPHIC_STYLE = "国漫3D动画电影质感，明确虚构数字角色、非真人照片，精致光影，统一电影画面。"


def _seed_portrait(project_id: str, *, character_name: str, image_path, style: str) -> None:
    conn = get_conn()
    bible_json = json.dumps({"world": {"visual_style_canonical": style}})
    conn.execute("INSERT INTO projects(id, name, bible_json, created_at) VALUES(?,?,?,0)", (
        project_id, "P", bible_json,
    ))
    conn.execute(
        "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end, "
        "appearance, prompt, image_path, bible_version, created_at) "
        "VALUES(?,?,?,?,?,?,?,?,1,0)",
        ("portrait1", project_id, character_name, 1, None, "外观", "prompt", str(image_path)),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# portrait_skin_audit_store
# ---------------------------------------------------------------------------


def test_ensure_tables_on_connection_is_idempotent():
    conn = get_conn()
    store.ensure_tables_on_connection(conn)
    store.ensure_tables_on_connection(conn)
    conn.execute("SELECT 1 FROM portrait_skin_blush_audits").fetchall()


def test_cache_roundtrip():
    assert store.get_cached_audit("hash1", "v1") is None
    store.set_cached_audit(
        content_hash="hash1", rule_version="v1", project_id="p1", character_name="甲",
        portrait_id="portrait1", image_path="/x.jpg", has_local_color=True, reason="有色块",
    )
    cached = store.get_cached_audit("hash1", "v1")
    assert cached["has_local_color"] is True
    assert cached["reason"] == "有色块"


def test_cache_is_scoped_by_rule_version():
    store.set_cached_audit(
        content_hash="hash1", rule_version="v1", project_id="p1", character_name="甲",
        portrait_id="portrait1", image_path="/x.jpg", has_local_color=True, reason="r",
    )
    assert store.get_cached_audit("hash1", "v2") is None


# ---------------------------------------------------------------------------
# audit_project_portraits
# ---------------------------------------------------------------------------


async def test_non_photographic_project_returns_empty_list(tmp_path):
    image = tmp_path / "front.jpg"
    image.write_bytes(b"x")
    _seed_portrait("p_anim", character_name="甲", image_path=image, style=_NON_PHOTOGRAPHIC_STYLE)
    conn = get_conn()
    project_row = conn.execute("SELECT * FROM projects WHERE id=?", ("p_anim",)).fetchone()

    results = await audit.audit_project_portraits(conn, "p_anim", project_row)
    assert results == []


async def test_photographic_project_judges_and_caches(monkeypatch, tmp_path):
    image = tmp_path / "front.jpg"
    image.write_bytes(b"real bytes")
    _seed_portrait("p_real", character_name="温念", image_path=image, style=_PHOTOGRAPHIC_STYLE)
    conn = get_conn()
    project_row = conn.execute("SELECT * FROM projects WHERE id=?", ("p_real",)).fetchone()

    judge_calls = {"n": 0}

    async def fake_judge(_path, *, call_meta):
        judge_calls["n"] += 1
        return {
            "checked": True, "has_local_color": True, "reason": "双颊有粉色块",
            "rule_version": PORTRAIT_SKIN_BLUSH_RULE_VERSION,
        }

    monkeypatch.setattr(audit, "judge_face_local_color", fake_judge)

    first = await audit.audit_project_portraits(conn, "p_real", project_row)
    assert len(first) == 1
    assert first[0]["character_name"] == "温念"
    assert first[0]["has_local_color"] is True
    assert first[0]["cached"] is False
    assert judge_calls["n"] == 1

    # 第二次：同一张图内容不变，命中缓存，不再真实调用模型。
    second = await audit.audit_project_portraits(conn, "p_real", project_row)
    assert second[0]["cached"] is True
    assert second[0]["has_local_color"] is True
    assert judge_calls["n"] == 1


async def test_unchecked_judgement_is_not_cached(monkeypatch, tmp_path):
    image = tmp_path / "front.jpg"
    image.write_bytes(b"real bytes 2")
    _seed_portrait("p_real2", character_name="温念", image_path=image, style=_PHOTOGRAPHIC_STYLE)
    conn = get_conn()
    project_row = conn.execute("SELECT * FROM projects WHERE id=?", ("p_real2",)).fetchone()

    async def unverified(_path, *, call_meta):
        return {"checked": False, "has_local_color": None, "reason": "", "rule_version": "v1"}

    monkeypatch.setattr(audit, "judge_face_local_color", unverified)

    results = await audit.audit_project_portraits(conn, "p_real2", project_row)
    assert results[0]["checked"] is False
    content_hash = store.content_sha256(str(image))
    assert store.get_cached_audit(content_hash, PORTRAIT_SKIN_BLUSH_RULE_VERSION) is None


async def test_only_character_filters_other_characters(monkeypatch, tmp_path):
    image_a = tmp_path / "a.jpg"; image_a.write_bytes(b"a")
    image_b = tmp_path / "b.jpg"; image_b.write_bytes(b"b")
    conn = get_conn()
    bible_json = json.dumps({"world": {"visual_style_canonical": _PHOTOGRAPHIC_STYLE}})
    conn.execute("INSERT INTO projects(id, name, bible_json, created_at) VALUES(?,?,?,0)", (
        "p_multi", "P", bible_json,
    ))
    for name, path, pid in (("甲", image_a, "portrait_a"), ("乙", image_b, "portrait_b")):
        conn.execute(
            "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end, "
            "appearance, prompt, image_path, bible_version, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,1,0)",
            (pid, "p_multi", name, 1, None, "外观", "prompt", str(path)),
        )
    conn.commit()
    project_row = conn.execute("SELECT * FROM projects WHERE id=?", ("p_multi",)).fetchone()

    async def fake_judge(_path, *, call_meta):
        return {"checked": True, "has_local_color": False, "reason": "", "rule_version": "v1"}

    monkeypatch.setattr(audit, "judge_face_local_color", fake_judge)

    results = await audit.audit_project_portraits(conn, "p_multi", project_row, only_character="甲")
    assert [r["character_name"] for r in results] == ["甲"]
