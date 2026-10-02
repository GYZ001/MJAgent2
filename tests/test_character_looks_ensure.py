"""人物造型照生成编排单测（``app.video_modes.character_looks_ensure``）。

覆盖：``claim_or_get`` 的库内 CAS 四种结局（首次插入/ready 复用/running 未超时
不重复触发/running 超时或 failed 可重新抢占，均经真实的 ``db.run_write_
transaction`` 独立事务）、``character_look_prompt`` 正面陈述与服装合同剥离、
``ensure_character_looks`` 端到端（真实 sqlite 文件 + 打桩 hiagent）、生成入口
闸门三态、operation_id 随指纹变化、以及缺全身定妆照时直接 failed 不调用
``generate_image``。

``claim_or_get`` 改走 ``db.run_write_transaction``（独立连接）后不再接受调用方
传入的 ``conn``——测试改成用标准的按文件隔离的真实 sqlite（``app.db.get_conn()``
与 ``db.run_write_transaction`` 最终落在同一个 ``db.DB_PATH`` 文件上，WAL 模式下
两个连接各自提交后互相可见），不再用独立的 ``:memory:`` 连接单测这部分——那种
连接传不进 ``run_write_transaction`` 内部重新打开的独立连接里。
"""
from __future__ import annotations

import asyncio
import base64
import json

from app import db as db_mod
from app import hiagent
from app.db import get_conn
from app.video_modes import character_looks_ensure as ensure_mod
from app.video_modes.character_look_views import look_key_for_wardrobe
from app.video_modes.character_look_views_store import ensure_tables_on_connection


def _seed_look_row(**fields) -> None:
    """直接写一行 character_look_views，供 CAS 测试摆好前置状态。先确保表存在
    （该表不在 app/db.py 的 MIGRATIONS 里，标准测试 DB 模板不会自带它）。"""
    conn = get_conn()
    ensure_tables_on_connection(conn)
    columns = ", ".join(fields)
    placeholders = ", ".join("?" for _ in fields)
    conn.execute(
        f"INSERT INTO character_look_views({columns}) VALUES({placeholders})",
        list(fields.values()),
    )
    conn.commit()


# ---------- claim_or_get CAS（真实 db.run_write_transaction） ----------

def test_claim_or_get_first_time_inserts_running_row():
    row, should_generate = asyncio.run(ensure_mod.claim_or_get(
        project_id="proj_1", portrait_id="port_1", look_key="key1",
        wardrobe_text="米白色针织开衫", fingerprint="fp1",
    ))
    assert should_generate is True
    assert row["status"] == "running"
    stored = get_conn().execute("SELECT * FROM character_look_views WHERE id=?", (row["id"],)).fetchone()
    assert stored["status"] == "running"


def test_claim_or_get_reuses_ready_row_when_fingerprint_matches(tmp_path):
    path = tmp_path / "look.jpg"
    path.write_bytes(b"fake")
    _seed_look_row(
        id="look_1", project_id="proj_1", portrait_id="port_1", look_key="key1",
        wardrobe_text="w", image_path=str(path), prompt="p", status="ready",
        input_fingerprint="fp1", created_at=1.0, updated_at=1.0,
    )
    row, should_generate = asyncio.run(ensure_mod.claim_or_get(
        project_id="proj_1", portrait_id="port_1", look_key="key1",
        wardrobe_text="w", fingerprint="fp1",
    ))
    assert should_generate is False
    assert row["status"] == "ready"


def test_claim_or_get_regenerates_when_fingerprint_changed(tmp_path):
    """种子图/画风变了（指纹不同）：即便当前是 ready，也要重新生成，不能一直
    复用过期产物。"""
    path = tmp_path / "look.jpg"
    path.write_bytes(b"fake")
    _seed_look_row(
        id="look_1", project_id="proj_1", portrait_id="port_1", look_key="key1",
        wardrobe_text="w", image_path=str(path), prompt="p", status="ready",
        input_fingerprint="fp_old", created_at=1.0, updated_at=1.0,
    )
    row, should_generate = asyncio.run(ensure_mod.claim_or_get(
        project_id="proj_1", portrait_id="port_1", look_key="key1",
        wardrobe_text="w", fingerprint="fp_new",
    ))
    assert should_generate is True
    assert row["status"] == "running"


def test_claim_or_get_running_within_stale_window_is_not_retriggered():
    _seed_look_row(
        id="look_1", project_id="proj_1", portrait_id="port_1", look_key="key1",
        wardrobe_text="w", image_path="", prompt="", status="running",
        input_fingerprint="fp1", created_at=db_mod.now(), updated_at=db_mod.now(),
    )
    row, should_generate = asyncio.run(ensure_mod.claim_or_get(
        project_id="proj_1", portrait_id="port_1", look_key="key1",
        wardrobe_text="w", fingerprint="fp1",
    ))
    assert should_generate is False
    assert row["status"] == "running"


def test_claim_or_get_stale_running_is_reclaimed():
    """进程被杀、running 行从未回写终态：超过 _RUNNING_STALE_S 视为僵死，允许
    重新抢占，否则这张造型照会永久卡在"生成中"。"""
    stale_ts = db_mod.now() - ensure_mod._RUNNING_STALE_S - 1
    _seed_look_row(
        id="look_1", project_id="proj_1", portrait_id="port_1", look_key="key1",
        wardrobe_text="w", image_path="", prompt="", status="running",
        input_fingerprint="fp1", created_at=stale_ts, updated_at=stale_ts,
    )
    row, should_generate = asyncio.run(ensure_mod.claim_or_get(
        project_id="proj_1", portrait_id="port_1", look_key="key1",
        wardrobe_text="w", fingerprint="fp1",
    ))
    assert should_generate is True


def test_claim_or_get_failed_row_is_reclaimed():
    _seed_look_row(
        id="look_1", project_id="proj_1", portrait_id="port_1", look_key="key1",
        wardrobe_text="w", image_path="", prompt="", status="failed", error="网络超时",
        input_fingerprint="fp1", created_at=1.0, updated_at=1.0,
    )
    row, should_generate = asyncio.run(ensure_mod.claim_or_get(
        project_id="proj_1", portrait_id="port_1", look_key="key1",
        wardrobe_text="w", fingerprint="fp1",
    ))
    assert should_generate is True
    assert row["status"] == "running"


# ---------- character_look_prompt ----------

def test_character_look_prompt_includes_wardrobe_and_strips_portrait_clothing_contract():
    from app.refs import _PORTRAIT_CLOTHING_CONTRACT, ensure_portrait_clothing_contract

    appearance = "二十余岁女子，鹅蛋脸，乌黑长发"
    portrait_prompt = ensure_portrait_clothing_contract(appearance)
    prompt = ensure_mod.character_look_prompt("国风写实", appearance, portrait_prompt, "米白色针织开衫")
    assert "米白色针织开衫" in prompt
    assert _PORTRAIT_CLOTHING_CONTRACT not in prompt
    assert "鹅蛋脸" in prompt


# ---------- ensure_character_looks 端到端 ----------

def _seed_episode_with_look_need(tmp_path) -> tuple[str, str]:
    from app.db import get_conn

    conn = get_conn()
    front_path = tmp_path / "front.jpg"
    front_path.write_bytes(b"fake-front")
    bible = {
        "world": {"visual_style_canonical": "国风写实"},
        "characters": [{
            "name": "温念", "role": "主角", "appearance_canonical": "二十余岁女子，鹅蛋脸",
        }],
    }
    conn.execute(
        "INSERT INTO projects(id, name, status, bible_json, created_at) VALUES(?,?, 'created', ?, 1)",
        ("proj_ensure_1", "测试项目", json.dumps(bible, ensure_ascii=False)),
    )
    conn.execute(
        "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end, "
        "appearance, prompt, image_path, pack_status, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("port_ensure_1", "proj_ensure_1", "温念", 1, None, "二十余岁女子，鹅蛋脸", "p", str(front_path), "ready", 1.0),
    )
    conn.execute(
        "INSERT INTO episodes(id, project_id, episode_no, status, created_at) VALUES(?,?,?,?,?)",
        ("ep_ensure_1", "proj_ensure_1", 1, "created", 1.0),
    )
    payload = {
        "resources": {"characters": [{
            "identity_id": "bible:温念", "display_name": "温念",
            "wardrobe_matches_default": "no", "visibility": "visible",
        }]},
        "continuity_memo": {"characters": [{"identity_id": "bible:温念", "wardrobe": "米白色针织开衫"}]},
    }
    conn.execute(
        "INSERT INTO shots(id, episode_id, shot_no, duration_s, shot_contract_json) VALUES(?,?,?,?,?)",
        ("shot_ensure_1", "ep_ensure_1", 1, 15, json.dumps({"storyboard_pack_segment": payload}, ensure_ascii=False)),
    )
    conn.commit()
    return "proj_ensure_1", "ep_ensure_1"


def test_ensure_character_looks_generates_missing_look_and_marks_ready(tmp_path, monkeypatch):
    project_id, episode_id = _seed_episode_with_look_need(tmp_path)

    async def fake_generate_image(prompt, *, size, image_inputs=None, call_meta=None):
        assert "米白色针织开衫" in prompt
        return {"b64_json": base64.b64encode(b"generated-look-bytes").decode("ascii")}

    monkeypatch.setattr(hiagent, "generate_image", fake_generate_image)

    result = asyncio.run(ensure_mod.ensure_character_looks(project_id=project_id, episode_id=episode_id))

    assert result["summary"]["ready"] == 1
    assert len(result["items"]) == 1
    assert result["items"][0]["status"] == "ready"

    from app.db import get_conn

    row = get_conn().execute(
        "SELECT * FROM character_look_views WHERE portrait_id='port_ensure_1'",
    ).fetchone()
    assert row["status"] == "ready"
    assert row["image_path"]


def test_ensure_character_looks_marks_failed_on_provider_error(tmp_path, monkeypatch):
    project_id, episode_id = _seed_episode_with_look_need(tmp_path)

    async def failing_generate_image(prompt, *, size, image_inputs=None, call_meta=None):
        raise hiagent.ProviderError("供应商超时")

    monkeypatch.setattr(hiagent, "generate_image", failing_generate_image)

    result = asyncio.run(ensure_mod.ensure_character_looks(project_id=project_id, episode_id=episode_id))

    assert result["summary"]["failed"] == 1
    assert result["items"][0]["status"] == "failed"
    assert "供应商超时" in result["items"][0]["error"]


def test_ensure_character_looks_is_idempotent_on_second_call(tmp_path, monkeypatch):
    """已经 ready 的造型照第二次 ensure 不应该再调用供应商。"""
    project_id, episode_id = _seed_episode_with_look_need(tmp_path)
    calls = {"count": 0}

    async def fake_generate_image(prompt, *, size, image_inputs=None, call_meta=None):
        calls["count"] += 1
        return {"b64_json": base64.b64encode(b"generated-look-bytes").decode("ascii")}

    monkeypatch.setattr(hiagent, "generate_image", fake_generate_image)

    asyncio.run(ensure_mod.ensure_character_looks(project_id=project_id, episode_id=episode_id))
    asyncio.run(ensure_mod.ensure_character_looks(project_id=project_id, episode_id=episode_id))

    assert calls["count"] == 1


# ---------- operation_id 随指纹变化 ----------

def test_look_operation_id_changes_with_fingerprint():
    """定妆照换图/画风变了会算出新指纹；operation_id 必须跟着变，否则
    ``reuse_successful_operation`` 会把过期的旧图原样复用回来。"""
    a = ensure_mod._look_operation_id("port_1", "key1", "fp_a")
    b = ensure_mod._look_operation_id("port_1", "key1", "fp_b")
    assert a != b


# ---------- 缺全身定妆照：直接 failed，不调用供应商 ----------

def test_generate_one_look_fails_without_seed_image_and_skips_provider_call(monkeypatch):
    calls = {"n": 0}

    async def fake_generate_image(*args, **kwargs):
        calls["n"] += 1
        return {}

    monkeypatch.setattr(hiagent, "generate_image", fake_generate_image)
    spec = {
        "portrait_id": "port_missing_seed", "look_key": "keyX", "wardrobe_text": "w",
        "front_full_image_path": "/tmp/does-not-exist-character-looks-xyz.jpg",
        "appearance": "a", "portrait_prompt": None, "character_name": "温念",
    }
    asyncio.run(ensure_mod._generate_one_look(project_id="proj_x", visual_style="国风", spec=spec))

    assert calls["n"] == 0
    row = get_conn().execute(
        "SELECT * FROM character_look_views WHERE portrait_id='port_missing_seed' AND look_key='keyX'",
    ).fetchone()
    assert row["status"] == "failed"
    assert "定妆照全身图缺失" in row["error"]


# ---------- 生成入口闸门三态 ----------

def test_pending_character_looks_gate_missing_returns_message_and_launches_once(tmp_path, monkeypatch):
    project_id, episode_id = _seed_episode_with_look_need(tmp_path)
    launched = []
    monkeypatch.setattr(ensure_mod, "launch_background_ensure", lambda **kw: launched.append(kw))

    message = asyncio.run(ensure_mod.pending_character_looks_gate(project_id, episode_id, None))

    assert message is not None
    assert "人物造型照" in message
    assert "本集" in message
    assert len(launched) == 1


def test_pending_character_looks_gate_shot_scope_uses_segment_wording(tmp_path, monkeypatch):
    project_id, episode_id = _seed_episode_with_look_need(tmp_path)
    monkeypatch.setattr(ensure_mod, "launch_background_ensure", lambda **kw: None)

    message = asyncio.run(ensure_mod.pending_character_looks_gate(project_id, episode_id, ["shot_ensure_1"]))

    assert message is not None
    assert "本段" in message


def test_pending_character_looks_gate_running_blocks_and_still_launches_ensure(tmp_path, monkeypatch):
    """running 也要启动 ensure：僵死的 running（进程重启时正在生成）只有 ensure 里的
    claim_or_get 能回收；只在 missing 时启动会让僵死行永远没人收、闸门永久拦截。
    未超时的 running 由 claim_or_get 跳过，不会重复出图（见下方 ensure 层两条用例）。"""
    project_id, episode_id = _seed_episode_with_look_need(tmp_path)
    look_key = look_key_for_wardrobe("米白色针织开衫")
    _seed_look_row(
        id="look_running", project_id=project_id, portrait_id="port_ensure_1", look_key=look_key,
        wardrobe_text="米白色针织开衫", image_path="", prompt="", status="running",
        input_fingerprint="fp", created_at=db_mod.now(), updated_at=db_mod.now(),
    )
    launched = []
    monkeypatch.setattr(ensure_mod, "launch_background_ensure", lambda **kw: launched.append(kw))

    message = asyncio.run(ensure_mod.pending_character_looks_gate(project_id, episode_id, None))

    assert message is not None
    assert len(launched) == 1


def _count_generate_calls(monkeypatch) -> dict[str, int]:
    calls = {"count": 0}

    async def fake_generate_image(prompt, *, size, image_inputs=None, call_meta=None):
        calls["count"] += 1
        return {"b64_json": base64.b64encode(b"generated-look-bytes").decode("ascii")}

    monkeypatch.setattr(hiagent, "generate_image", fake_generate_image)
    return calls


def test_ensure_reclaims_stale_running_look(tmp_path, monkeypatch):
    project_id, episode_id = _seed_episode_with_look_need(tmp_path)
    stale_ts = db_mod.now() - ensure_mod._RUNNING_STALE_S - 1
    _seed_look_row(
        id="look_stale", project_id=project_id, portrait_id="port_ensure_1",
        look_key=look_key_for_wardrobe("米白色针织开衫"), wardrobe_text="米白色针织开衫",
        image_path="", prompt="", status="running", input_fingerprint="fp",
        created_at=stale_ts, updated_at=stale_ts,
    )
    calls = _count_generate_calls(monkeypatch)

    result = asyncio.run(ensure_mod.ensure_character_looks(project_id=project_id, episode_id=episode_id))

    assert calls["count"] == 1
    assert result["items"][0]["status"] == "ready"


def test_ensure_skips_fresh_running_look(tmp_path, monkeypatch):
    project_id, episode_id = _seed_episode_with_look_need(tmp_path)
    _seed_look_row(
        id="look_fresh", project_id=project_id, portrait_id="port_ensure_1",
        look_key=look_key_for_wardrobe("米白色针织开衫"), wardrobe_text="米白色针织开衫",
        image_path="", prompt="", status="running", input_fingerprint="fp",
        created_at=db_mod.now(), updated_at=db_mod.now(),
    )
    calls = _count_generate_calls(monkeypatch)

    result = asyncio.run(ensure_mod.ensure_character_looks(project_id=project_id, episode_id=episode_id))

    assert calls["count"] == 0
    assert result["items"][0]["status"] == "running"


def test_pending_character_looks_gate_ready_passes_through(tmp_path, monkeypatch):
    project_id, episode_id = _seed_episode_with_look_need(tmp_path)
    look_key = look_key_for_wardrobe("米白色针织开衫")
    look_path = tmp_path / "look_ready.jpg"
    look_path.write_bytes(b"fake")
    _seed_look_row(
        id="look_ready", project_id=project_id, portrait_id="port_ensure_1", look_key=look_key,
        wardrobe_text="米白色针织开衫", image_path=str(look_path), prompt="p", status="ready",
        input_fingerprint="fp", created_at=1.0, updated_at=1.0,
    )
    launched = []
    monkeypatch.setattr(ensure_mod, "launch_background_ensure", lambda **kw: launched.append(kw))

    message = asyncio.run(ensure_mod.pending_character_looks_gate(project_id, episode_id, None))

    assert message is None
    assert launched == []


def test_pending_character_looks_gate_failed_passes_through(tmp_path):
    """failed 不拦——照常生成，选图会退回定妆照并留可见提示，不是生成入口要
    解决的问题。"""
    project_id, episode_id = _seed_episode_with_look_need(tmp_path)
    look_key = look_key_for_wardrobe("米白色针织开衫")
    _seed_look_row(
        id="look_failed", project_id=project_id, portrait_id="port_ensure_1", look_key=look_key,
        wardrobe_text="米白色针织开衫", image_path="", prompt="", status="failed", error="供应商超时",
        input_fingerprint="fp", created_at=1.0, updated_at=1.0,
    )

    message = asyncio.run(ensure_mod.pending_character_looks_gate(project_id, episode_id, None))

    assert message is None
