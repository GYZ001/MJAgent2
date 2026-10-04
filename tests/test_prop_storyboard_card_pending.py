"""分镜阶段补卡（``app.props.card_pending_scan``/``card_pending_store``/
``card_pending_ensure``）：判据、CAS 抢占、建卡/别名两条路径、生成闸门、
对已采纳视频交付的零影响。背景见本次派单（2026-10-03，proj_ca86b15ab7d7
实测：35 段里 42 条道具条目查无卡，其中 40 条是世界书根本没有这张卡）。
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from app.db import get_conn, now
from app.props import card_pending_ensure as ensure_mod
from app.props import card_pending_scan as scan_mod
from app.props.card_pending_store import ensure_tables_on_connection, get_pending
from app.capabilities import inputs as I
from app.capabilities.handlers import video as video_handlers
from app.capabilities.handlers import video_gates


def _seed_project(project_id: str, *, props_list: list[dict] | None = None, style: str = "国漫电影风") -> None:
    bible = {
        "characters": [], "scenes": [], "props": props_list or [],
        "world": {"era": "", "genre": "", "visual_style_canonical": style},
    }
    conn = get_conn()
    conn.execute(
        "INSERT INTO projects(id, name, bible_json, bible_version, created_at) VALUES(?,?,?,0,?)",
        (project_id, "测试项目", json.dumps(bible, ensure_ascii=False), now()),
    )
    conn.commit()


def _seed_episode(
    project_id: str, episode_id: str, episode_no: int, *, chapter_text: str = "",
) -> None:
    conn = get_conn()
    conn.execute(
        "INSERT INTO chapters(project_id, idx, title, content) VALUES(?,?,?,?)",
        (project_id, 1, "第一章", chapter_text or "正文占位。"),
    )
    conn.execute(
        "INSERT INTO episodes(id, project_id, episode_no, status, source_chapters, created_at) "
        "VALUES(?,?,?,?,?,?)",
        (episode_id, project_id, episode_no, "scripted", json.dumps([1]), now()),
    )
    conn.commit()


def _seed_shot(episode_id: str, shot_no: int, props: list[dict]) -> None:
    segment = {"resources": {"props": props}}
    conn = get_conn()
    conn.execute(
        "INSERT INTO shots(id, episode_id, shot_no, duration_s, shot_contract_json) VALUES(?,?,?,?,?)",
        (f"{episode_id}_s{shot_no}", episode_id, shot_no, 10,
         json.dumps({"storyboard_pack_segment": segment}, ensure_ascii=False)),
    )
    conn.commit()


_FAKE_PROP_APPEARANCE = "白色陶瓷材质、圆口、带配套茶托"


async def _fake_chat_structured(_messages, **kwargs):
    # 触发点①同步复核：每条子句须显式"不删除"，空列表会判定成失败重试而非"全部保留"。
    if (kwargs.get("call_meta") or {}).get("stage") == "audit_prop_card":
        n = len(_FAKE_PROP_APPEARANCE.split("、"))
        return SimpleNamespace(clauses=[{"index": i + 1, "remove": False} for i in range(n)], aliases=[])
    return SimpleNamespace(appearance_canonical=_FAKE_PROP_APPEARANCE, aliases=[])


async def _explode_chat_structured(*_a, **_k):
    raise AssertionError("命中既有卡只该走别名登记，不该再发模型调用")


async def _fake_generate_image(project_id, name, _prompt):
    return f"/fake/{project_id}/{name}.png"


# ---------------------------------------------------------------------------
# 1) 判据：card_pending_scan
# ---------------------------------------------------------------------------

def test_candidate_requires_at_least_two_shots() -> None:
    _seed_project("proj_scan_1")
    _seed_episode("proj_scan_1", "ep_scan_1", 1)
    _seed_shot("ep_scan_1", 1, [{"label": "白色陶瓷杯", "description": "圆口"}])
    conn = get_conn()
    shot_rows = scan_mod.load_episode_shot_rows(conn, "ep_scan_1")
    candidates = scan_mod.candidate_labels_without_card(conn, "proj_scan_1", 1, shot_rows)
    assert "白色陶瓷杯" not in candidates


def test_candidate_qualifies_with_two_shots_and_no_card() -> None:
    _seed_project("proj_scan_2")
    _seed_episode("proj_scan_2", "ep_scan_2", 1)
    _seed_shot("ep_scan_2", 1, [{"label": "白色陶瓷杯", "description": "圆口，带把手"}])
    _seed_shot("ep_scan_2", 2, [{"label": "白色陶瓷杯", "description": "杯口有缺口"}])
    conn = get_conn()
    shot_rows = scan_mod.load_episode_shot_rows(conn, "ep_scan_2")
    candidates = scan_mod.candidate_labels_without_card(conn, "proj_scan_2", 1, shot_rows)
    assert set(candidates["白色陶瓷杯"]["shot_nos"]) == {1, 2}
    assert candidates["白色陶瓷杯"]["descriptions"] == ["圆口，带把手", "杯口有缺口"]


def test_candidate_excluded_once_ready_card_exists(tmp_path) -> None:
    img = tmp_path / "cup.png"
    img.write_bytes(b"x")
    _seed_project("proj_scan_3", props_list=[{
        "name": "白色陶瓷杯", "appearance_canonical": "白瓷杯", "aliases": [],
        "ref_image_path": str(img), "first_episode_no": 1,
    }])
    _seed_episode("proj_scan_3", "ep_scan_3", 1)
    _seed_shot("ep_scan_3", 1, [{"label": "白色陶瓷杯", "description": "a"}])
    _seed_shot("ep_scan_3", 2, [{"label": "白色陶瓷杯", "description": "b"}])
    conn = get_conn()
    from app.props.store import upsert_prop_reference
    upsert_prop_reference(
        conn, "proj_scan_3", "白色陶瓷杯", 1, appearance="白瓷杯", image_path=str(img),
        prompt="p", status="ready", qa={},
    )
    conn.commit()
    shot_rows = scan_mod.load_episode_shot_rows(conn, "ep_scan_3")
    candidates = scan_mod.candidate_labels_without_card(conn, "proj_scan_3", 1, shot_rows)
    assert "白色陶瓷杯" not in candidates


# ---------------------------------------------------------------------------
# 2) claim_or_get CAS（真实 db.run_write_transaction）
# ---------------------------------------------------------------------------

def test_claim_or_get_first_time_inserts_running() -> None:
    row, should_build = asyncio.run(ensure_mod.claim_or_get(project_id="proj_cas", label="白色陶瓷杯"))
    assert should_build is True
    assert row["status"] == "running"
    assert row["attempts"] == 1


def test_claim_or_get_reuses_ready_row() -> None:
    conn = get_conn()
    ensure_tables_on_connection(conn)
    conn.execute(
        "INSERT INTO prop_storyboard_card_pending(id,project_id,label,status,resolved_name,attempts,"
        "created_at,updated_at) VALUES('x1','proj_cas2','白色陶瓷杯','ready','白色陶瓷杯',1,1,1)",
    )
    conn.commit()
    row, should_build = asyncio.run(ensure_mod.claim_or_get(project_id="proj_cas2", label="白色陶瓷杯"))
    assert should_build is False
    assert row["status"] == "ready"


def test_claim_or_get_blocks_on_fresh_running() -> None:
    conn = get_conn()
    ensure_tables_on_connection(conn)
    conn.execute(
        "INSERT INTO prop_storyboard_card_pending(id,project_id,label,status,attempts,created_at,updated_at) "
        "VALUES('x2','proj_cas3','白色陶瓷杯','running',1,?,?)", (now(), now()),
    )
    conn.commit()
    row, should_build = asyncio.run(ensure_mod.claim_or_get(project_id="proj_cas3", label="白色陶瓷杯"))
    assert should_build is False
    assert row["status"] == "running"


def test_claim_or_get_reclaims_stale_running() -> None:
    conn = get_conn()
    ensure_tables_on_connection(conn)
    conn.execute(
        "INSERT INTO prop_storyboard_card_pending(id,project_id,label,status,attempts,created_at,updated_at) "
        "VALUES('x3','proj_cas4','白色陶瓷杯','running',1,0,0)",
    )
    conn.commit()
    row, should_build = asyncio.run(ensure_mod.claim_or_get(project_id="proj_cas4", label="白色陶瓷杯"))
    assert should_build is True
    assert row["attempts"] == 2


def test_claim_or_get_retries_failed_under_quota() -> None:
    conn = get_conn()
    ensure_tables_on_connection(conn)
    conn.execute(
        "INSERT INTO prop_storyboard_card_pending(id,project_id,label,status,attempts,created_at,updated_at) "
        "VALUES('x4','proj_cas5','白色陶瓷杯','failed',1,0,0)",
    )
    conn.commit()
    row, should_build = asyncio.run(ensure_mod.claim_or_get(project_id="proj_cas5", label="白色陶瓷杯"))
    assert should_build is True
    assert row["attempts"] == 2


def test_claim_or_get_exhausted_failed_does_not_retry() -> None:
    conn = get_conn()
    ensure_tables_on_connection(conn)
    conn.execute(
        "INSERT INTO prop_storyboard_card_pending(id,project_id,label,status,attempts,created_at,updated_at) "
        "VALUES('x5','proj_cas6','白色陶瓷杯','failed',?,0,0)", (ensure_mod.MAX_BUILD_ATTEMPTS,),
    )
    conn.commit()
    row, should_build = asyncio.run(ensure_mod.claim_or_get(project_id="proj_cas6", label="白色陶瓷杯"))
    assert should_build is False
    assert row["status"] == "failed"


# ---------------------------------------------------------------------------
# 3) ensure_storyboard_prop_cards 端到端：新建卡 / 命中既有卡只建别名
# ---------------------------------------------------------------------------

def test_ensure_builds_new_card_for_qualifying_label(monkeypatch) -> None:
    from app.props import judge, service

    monkeypatch.setattr(judge.model_gateway, "chat_structured", _fake_chat_structured)
    monkeypatch.setattr(service, "generate_prop_reference_image", _fake_generate_image)
    _seed_project("proj_build_1")
    _seed_episode("proj_build_1", "ep_build_1", 1, chapter_text="桌上那只没喝几口的白色陶瓷杯还温着。")
    _seed_shot("ep_build_1", 1, [{"label": "白色陶瓷杯", "description": "圆口，带把手"}])
    _seed_shot("ep_build_1", 2, [{"label": "白色陶瓷杯", "description": "杯口有缺口"}])

    result = asyncio.run(ensure_mod.ensure_storyboard_prop_cards(
        project_id="proj_build_1", episode_id="ep_build_1",
    ))

    assert "白色陶瓷杯" in result["attempted"]
    conn = get_conn()
    row = get_pending(conn, project_id="proj_build_1", label="白色陶瓷杯")
    assert row["status"] == "ready"
    bible = json.loads(conn.execute(
        "SELECT bible_json FROM projects WHERE id='proj_build_1'",
    ).fetchone()[0])
    names = {p["name"] for p in bible["props"]}
    assert "白色陶瓷杯" in names


def test_ensure_binds_alias_when_card_match_hits_existing_card(monkeypatch) -> None:
    from app.props import judge, service

    # 命中既有卡就该走别名登记、不再发模型调用——用会报错的桩钉住这条路径。
    monkeypatch.setattr(judge.model_gateway, "chat_structured", _explode_chat_structured)
    monkeypatch.setattr(service, "generate_prop_reference_image", _explode_chat_structured)
    _seed_project("proj_build_2", props_list=[{
        "name": "行李箱", "appearance_canonical": "灰色硬壳行李箱", "aliases": [],
    }])
    _seed_episode("proj_build_2", "ep_build_2", 1)
    _seed_shot("ep_build_2", 1, [{"label": "旧行李箱", "description": "行李箱边角有磨损"}])
    _seed_shot("ep_build_2", 2, [{"label": "旧行李箱", "description": "行李箱轮子掉了一个"}])

    result = asyncio.run(ensure_mod.ensure_storyboard_prop_cards(
        project_id="proj_build_2", episode_id="ep_build_2",
    ))

    assert "旧行李箱" in result["attempted"]
    conn = get_conn()
    row = get_pending(conn, project_id="proj_build_2", label="旧行李箱")
    assert row["status"] == "ready"
    assert row["resolved_name"] == "行李箱"
    bible = json.loads(conn.execute(
        "SELECT bible_json FROM projects WHERE id='proj_build_2'",
    ).fetchone()[0])
    prop = next(p for p in bible["props"] if p["name"] == "行李箱")
    assert "旧行李箱" in prop["aliases"]
    assert len(bible["props"]) == 1  # 没有新建第二张卡


def test_ensure_build_quota_one_attempt_marks_failed_and_stops(monkeypatch) -> None:
    async def _fail_chat_structured(_messages, **_kwargs):
        raise RuntimeError("供应商超时")

    from app.props import judge

    monkeypatch.setattr(judge.model_gateway, "chat_structured", _fail_chat_structured)
    _seed_project("proj_build_3")
    _seed_episode("proj_build_3", "ep_build_3", 1)
    _seed_shot("ep_build_3", 1, [{"label": "米白色平底单鞋", "description": "鞋头圆润"}])
    _seed_shot("ep_build_3", 2, [{"label": "米白色平底单鞋", "description": "鞋带已松脱"}])

    asyncio.run(ensure_mod.ensure_storyboard_prop_cards(project_id="proj_build_3", episode_id="ep_build_3"))

    row = get_pending(get_conn(), project_id="proj_build_3", label="米白色平底单鞋")
    assert row["status"] == "failed"
    assert row["attempts"] == 1
    assert "供应商超时" in row["error"]


# ---------------------------------------------------------------------------
# 4) 生成闸门
# ---------------------------------------------------------------------------

def test_pending_gate_blocks_until_ready_then_releases(monkeypatch) -> None:
    from app.props import judge, service

    monkeypatch.setattr(judge.model_gateway, "chat_structured", _fake_chat_structured)
    monkeypatch.setattr(service, "generate_prop_reference_image", _fake_generate_image)
    _seed_project("proj_gate_1")
    _seed_episode("proj_gate_1", "ep_gate_1", 1)
    _seed_shot("ep_gate_1", 1, [{"label": "白色陶瓷杯", "description": "a"}])
    _seed_shot("ep_gate_1", 2, [{"label": "白色陶瓷杯", "description": "b"}])

    message = asyncio.run(ensure_mod.pending_prop_card_gate("proj_gate_1", "ep_gate_1"))
    assert message is not None and "道具" in message

    # 闸门内部已经 launch_background_ensure；这里直接跑一次同步的 ensure 模拟它
    # 跑完，验证第二次调用闸门不再拦。
    asyncio.run(ensure_mod.ensure_storyboard_prop_cards(project_id="proj_gate_1", episode_id="ep_gate_1"))
    message2 = asyncio.run(ensure_mod.pending_prop_card_gate("proj_gate_1", "ep_gate_1"))
    assert message2 is None


def test_pending_gate_does_not_block_forever_after_quota_exhausted() -> None:
    _seed_project("proj_gate_2")
    _seed_episode("proj_gate_2", "ep_gate_2", 1)
    _seed_shot("ep_gate_2", 1, [{"label": "白色陶瓷杯", "description": "a"}])
    _seed_shot("ep_gate_2", 2, [{"label": "白色陶瓷杯", "description": "b"}])
    conn = get_conn()
    ensure_tables_on_connection(conn)
    conn.execute(
        "INSERT INTO prop_storyboard_card_pending(id,project_id,label,status,attempts,created_at,updated_at) "
        "VALUES('xx','proj_gate_2','白色陶瓷杯','failed',?,0,0)", (ensure_mod.MAX_BUILD_ATTEMPTS,),
    )
    conn.commit()
    message = asyncio.run(ensure_mod.pending_prop_card_gate("proj_gate_2", "ep_gate_2"))
    assert message is None, "重试额度用尽后不得永久阻塞生成（退回无卡的既有降级）"


def _seed_gate_project_with_running_row(project_id: str, episode_id: str, *, stamp: float) -> None:
    _seed_project(project_id)
    _seed_episode(project_id, episode_id, 1)
    _seed_shot(episode_id, 1, [{"label": "白色陶瓷杯", "description": "a"}])
    _seed_shot(episode_id, 2, [{"label": "白色陶瓷杯", "description": "b"}])
    conn = get_conn()
    ensure_tables_on_connection(conn)
    conn.execute(
        "INSERT INTO prop_storyboard_card_pending(id,project_id,label,status,attempts,created_at,updated_at) "
        "VALUES(?,?,'白色陶瓷杯','running',1,?,?)", (f"row_{project_id}", project_id, stamp, stamp),
    )
    conn.commit()


def test_pending_gate_launch_dedup_fresh_vs_stale_running(monkeypatch) -> None:
    """代码评审修复：某 label 已处于"running 且未超时"（已有后台任务在处理）
    时不该再 launch 一个新的 ``ensure_storyboard_prop_cards`` 后台任务（用户
    连点生成会累积重复任务；底层 ``claim_or_get`` 的 CAS 不会重复建卡，但重
    复加载 episode/bible/扫描整集是可避免的浪费）；running 行已超时（僵死）
    则必须重新 launch 才能真正重新抢占。"""
    calls = {"n": 0}

    def _counting_launch(**_kwargs):
        calls["n"] += 1

    monkeypatch.setattr(ensure_mod, "launch_background_ensure", _counting_launch)

    _seed_gate_project_with_running_row("proj_gate_3", "ep_gate_3", stamp=now())
    message = asyncio.run(ensure_mod.pending_prop_card_gate("proj_gate_3", "ep_gate_3"))
    assert message is not None and "道具" in message  # 仍然要拦住并提示
    assert calls["n"] == 0  # 但不该再起一个新的后台任务

    stale_stamp = now() - ensure_mod._RUNNING_STALE_S - 1.0
    _seed_gate_project_with_running_row("proj_gate_4", "ep_gate_4", stamp=stale_stamp)
    asyncio.run(ensure_mod.pending_prop_card_gate("proj_gate_4", "ep_gate_4"))
    assert calls["n"] == 1


def test_ensure_return_keys_consistent_across_early_return_and_success(monkeypatch) -> None:
    """代码评审修复：早退（分集不存在/世界书未初始化）与成功路径的返回字典
    键名必须一致（``candidates``/``attempted``），不能一个用 ``built``/
    ``pending``、另一个用别的键名，否则读返回值的调用方在早退路径会 KeyError。"""
    missing_episode = asyncio.run(ensure_mod.ensure_storyboard_prop_cards(
        project_id="proj_missing", episode_id="ep_does_not_exist",
    ))
    _seed_project("proj_no_bible")
    conn = get_conn()
    conn.execute("UPDATE projects SET bible_json='' WHERE id='proj_no_bible'")
    conn.commit()
    _seed_episode("proj_no_bible", "ep_no_bible", 1)
    missing_bible = asyncio.run(ensure_mod.ensure_storyboard_prop_cards(
        project_id="proj_no_bible", episode_id="ep_no_bible",
    ))

    from app.props import judge, service

    monkeypatch.setattr(judge.model_gateway, "chat_structured", _fake_chat_structured)
    monkeypatch.setattr(service, "generate_prop_reference_image", _fake_generate_image)
    _seed_project("proj_success_keys")
    _seed_episode("proj_success_keys", "ep_success_keys", 1)
    _seed_shot("ep_success_keys", 1, [{"label": "白色陶瓷杯", "description": "a"}])
    _seed_shot("ep_success_keys", 2, [{"label": "白色陶瓷杯", "description": "b"}])
    success = asyncio.run(ensure_mod.ensure_storyboard_prop_cards(
        project_id="proj_success_keys", episode_id="ep_success_keys",
    ))
    for result in (missing_episode, missing_bible, success):
        assert set(result) == {"candidates", "attempted"}


def test_video_generate_shot_handler_returns_409_when_prop_pending(monkeypatch) -> None:
    async def fake_gate(_project_id, _episode_id, _shot_ids):
        return "本段涉及的 1 件道具正在补建参考图", "prop_card_pending"

    monkeypatch.setattr(video_gates, "pending_video_dispatch_gate", fake_gate)
    monkeypatch.setattr(
        "app.video_modes.scene_state_ensure.resolve_shot_scope",
        lambda _shot_id: ("proj_x", "ep_x"),
    )

    from app.capabilities.schemas import CommandStatus

    result = asyncio.run(video_handlers.generate_shot(
        I.VideoGenerateShotInput(shot_id="shot_x"),
    ))

    assert result.status == CommandStatus.FAILED
    assert result.error_code == "prop_card_pending"


# ---------------------------------------------------------------------------
# 5) 不编造证据
# ---------------------------------------------------------------------------

def test_combined_evidence_has_no_source_sentence_when_absent() -> None:
    text, description = ensure_mod._combined_evidence(
        "白色陶瓷杯", {"descriptions": ["圆口"]}, source_text="与该道具完全无关的一段原文。",
    )
    assert "原文：" not in text
    assert "圆口" in description


def test_combined_evidence_includes_matching_source_sentence() -> None:
    text, _description = ensure_mod._combined_evidence(
        "白色陶瓷杯", {"descriptions": ["圆口"]},
        source_text="桌上那只白色陶瓷杯还温着。窗外下着雨。",
    )
    assert "原文：桌上那只白色陶瓷杯还温着" in text


# ---------------------------------------------------------------------------
# 6) 对已采纳视频交付零影响
# ---------------------------------------------------------------------------

def _commit_shot_video_artifact(conn, *, shot_id: str, version_id: str, path) -> str:
    """提交一条最简 shot_video artifact，供交付清单测试使用。不借用
    ``tests/test_identity_revision_retention.py`` 的同名函数——该文件是另一
    个并行工作流的在途改动范围（见本次派单「并发」约束），其签名/位置随时
    可能变化；本测试文件就近自带一份独立副本。"""
    from app.evidence import repository
    from app.harness.types import Evaluation, EvidenceArtifact

    artifact = repository.create_artifact(EvidenceArtifact(
        type="shot_video", scope_type="shot", scope_id=shot_id,
        content={"kind": "shot_video", "version_id": version_id},
        file_path=str(path), status="validated", trust_level="T2",
        contract_version="video-2.0.0",
    ), conn=conn)
    artifact = repository.commit_artifact(None, artifact["id"], [Evaluation(
        evaluator_type="file", evaluator_name="video_technical_validator",
        evaluator_version="1", status="passed", hard_gate_passed=True, score=100,
    )])
    conn.execute(
        "UPDATE shot_versions SET artifact_id=?,technical_validation_json=? WHERE id=?",
        (artifact["id"], json.dumps({"passed": True, "issues": []}), version_id),
    )
    return artifact["id"]


def test_registering_new_prop_card_does_not_change_adopted_video_delivery_manifest(tmp_path, monkeypatch) -> None:
    from app import downstream_authority
    from app.evidence import repository
    from tests.test_episode_partial_concat import _database, _version

    conn = _database(shot_nos=(1,))
    monkeypatch.setattr(repository, "get_conn", lambda: conn)
    video_path = tmp_path / "shot1.mp4"
    video_path.write_bytes(b"video-bytes")
    _version(conn, shot_no=1, path=video_path, adopted=True)
    _commit_shot_video_artifact(conn, shot_id="s1", version_id="v1", path=video_path)
    conn.execute(
        "INSERT INTO projects(id,name,bible_json,created_at) VALUES('proj_deliver','P',?,0) "
        "ON CONFLICT(id) DO NOTHING",
        (json.dumps({"characters": [], "scenes": [], "props": [],
                     "world": {"visual_style_canonical": "写实"}}, ensure_ascii=False),),
    )
    conn.commit()

    before = downstream_authority.current_adopted_video_delivery_manifest("e", conn=conn)
    before_partial = downstream_authority.current_partial_adopted_video_delivery_manifest("e", conn=conn)

    from app.props.service import bind_existing_prop_alias

    def mutate(data: dict) -> bool:
        data["props"].append({
            "name": "新建道具卡", "appearance_canonical": "补卡测试用外观锚点字符串",
            "aliases": [], "ref_image_path": None, "first_episode_no": 1,
        })
        return True
    from app.bible_store import mutate_bible_json
    mutate_bible_json(conn, "proj_deliver", mutate)
    bind_existing_prop_alias(conn, "proj_deliver", "新建道具卡", "补卡别名")
    conn.commit()

    after = downstream_authority.current_adopted_video_delivery_manifest("e", conn=conn)
    after_partial = downstream_authority.current_partial_adopted_video_delivery_manifest("e", conn=conn)

    assert after["manifest_hash"] == before["manifest_hash"]
    assert after_partial["manifest_hash"] == before_partial["manifest_hash"]
