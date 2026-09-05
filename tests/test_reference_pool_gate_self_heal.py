"""WS3a：参考图候选池自愈（docs/failure_triage_and_self_heal_plan_2026-09-05.md）。

``app.media_exec.reference_pool_gate.finish_reference_mode_without_assets`` 在
判定候选池「本该有资产却没有」（``_reference_pool_blockers`` 非空）时，此前
直接拦成 ``waiting_human``。现在先按 blockers 指向的人物/场景各自动触发一次
补生成（复用 ``app.refs.generate_refs``/``app.scenes.generate_scene_refs``），
成功后重新解析依赖再装配；每个镜头只自愈一次，仍缺才落回原有拦截。

打桩方式：``reference_pool_gate`` 内部函数互相以裸名调用（同一模块全局命名
空间查找，不是 ``from x import y`` 提前绑定值），``monkeypatch.setattr(rpg,
"_xxx", fake)`` 对同模块内的调用方立即生效；``video_modes.xxx(...)`` 走的是
包属性访问（``app.video_modes`` 是真实再导出包，不是 exec() 外观），同样可以
直接 ``monkeypatch.setattr(rpg.video_modes, "xxx", fake)``。
"""
from __future__ import annotations

import asyncio
import types

import pytest

from app.db import get_conn
from app.media_exec import reference_pool_gate as rpg
from app.media_exec.fences import VideoInputRepairRequired


def _job() -> dict:
    return {"shot_id": "s1", "project_id": "p1", "episode_id": "e1", "id": "j1"}


class _FakeEpisodeConn:
    """最小可用连接桩：只需要支持一次 SELECT episode_no 查询。"""

    def __init__(self, episode_no: int | None) -> None:
        self._episode_no = episode_no

    def execute(self, sql: str, params: tuple) -> "_FakeEpisodeConn":
        assert "episode_no" in sql
        return self

    def fetchone(self):
        return {"episode_no": self._episode_no} if self._episode_no is not None else None


# ---------------------------------------------------------------------------
# _blocked_entity_names：纯文本解析
# ---------------------------------------------------------------------------


def test_blocked_entity_names_parses_character_and_scene_blockers() -> None:
    blockers = [
        "人物「柳七月」缺少本集造型版本",
        "场景「靠山宗荒山丛林」没有可用的场景图",
        "人物「路人甲」多视角包状态为 pending",
        "不认识的文案格式",
    ]
    characters, scenes = rpg._blocked_entity_names(blockers)
    assert characters == ["柳七月", "路人甲"]
    assert scenes == ["靠山宗荒山丛林"]


# ---------------------------------------------------------------------------
# _self_heal_reference_pool：调用既有生成入口，单项失败不阻断其它目标
# ---------------------------------------------------------------------------


def test_self_heal_reference_pool_calls_generate_refs_and_scene_refs(monkeypatch) -> None:
    calls: list[tuple] = []

    async def fake_generate_refs(project_id, only_character=None, **_kwargs):
        calls.append(("character", project_id, only_character))

    async def fake_generate_scene_refs(project_id, only_scene=None, **_kwargs):
        calls.append(("scene", project_id, only_scene))

    monkeypatch.setattr("app.refs.generate_refs", fake_generate_refs)
    monkeypatch.setattr("app.scenes.generate_scene_refs", fake_generate_scene_refs)

    healed = asyncio.run(rpg._self_heal_reference_pool(
        project_id="p1",
        blockers=["人物「柳七月」缺少本集造型版本", "场景「荒山」没有可用的场景图"],
    ))

    assert healed is True
    assert ("character", "p1", "柳七月") in calls
    assert ("scene", "p1", "荒山") in calls


def test_self_heal_reference_pool_returns_false_when_all_targets_fail(monkeypatch) -> None:
    async def boom(*_args, **_kwargs):
        raise ValueError("角色不存在或暂不具备定妆资格：['柳七月']")

    monkeypatch.setattr("app.refs.generate_refs", boom)

    healed = asyncio.run(rpg._self_heal_reference_pool(
        project_id="p1", blockers=["人物「柳七月」缺少本集造型版本"],
    ))

    assert healed is False


def test_self_heal_reference_pool_one_target_failing_does_not_block_others(monkeypatch) -> None:
    async def boom(*_args, **_kwargs):
        raise ValueError("角色不存在")

    async def ok(*_args, **_kwargs):
        return None

    monkeypatch.setattr("app.refs.generate_refs", boom)
    monkeypatch.setattr("app.scenes.generate_scene_refs", ok)

    healed = asyncio.run(rpg._self_heal_reference_pool(
        project_id="p1",
        blockers=["人物「柳七月」缺少本集造型版本", "场景「荒山」没有可用的场景图"],
    ))

    assert healed is True


# ---------------------------------------------------------------------------
# _attempt_reference_self_heal：补生成 → 重新解析依赖装配 → 尝试完整落地
# ---------------------------------------------------------------------------


def test_attempt_self_heal_returns_none_when_nothing_healed(monkeypatch) -> None:
    async def fake_self_heal(**_kwargs):
        return False

    def boom_build(**_kwargs):
        raise AssertionError("没补成功就不该重新装配")

    monkeypatch.setattr(rpg, "_self_heal_reference_pool", fake_self_heal)
    monkeypatch.setattr(rpg.video_modes, "build_reference_assets", boom_build)

    result = asyncio.run(rpg._attempt_reference_self_heal(
        conn=None, job=_job(), meta={}, prompt_text="p", version={"id": "v1"},
        shot_model=None, bible=None, screenplay=None, decision=None,
        blockers=["人物「柳七月」缺少本集造型版本"],
    ))

    assert result is None


def test_attempt_self_heal_returns_none_when_reassembly_still_empty(monkeypatch) -> None:
    async def fake_self_heal(**_kwargs):
        return True

    async def fake_build(**_kwargs):
        return []

    def boom_complete(**_kwargs):
        raise AssertionError("没有资产不该走完整落地")

    monkeypatch.setattr(rpg, "_self_heal_reference_pool", fake_self_heal)
    monkeypatch.setattr(rpg.video_modes, "build_reference_assets", fake_build)
    monkeypatch.setattr(rpg, "_complete_reference_mode_with_healed_assets", boom_complete)

    result = asyncio.run(rpg._attempt_reference_self_heal(
        conn=_FakeEpisodeConn(None), job=_job(), meta={}, prompt_text="p",
        version={"id": "v1"}, shot_model=None, bible=None, screenplay=None,
        decision=None, blockers=["人物「柳七月」缺少本集造型版本"],
    ))

    assert result is None


def test_attempt_self_heal_success_delegates_to_completion(monkeypatch) -> None:
    fake_assets = [object()]
    meta_obj: dict = {}

    async def fake_self_heal(**_kwargs):
        return True

    async def fake_build(**kwargs):
        assert kwargs["existing_meta"] is meta_obj
        assert kwargs["episode_no"] == 7
        return fake_assets

    async def fake_complete(**kwargs):
        assert kwargs["assets"] is fake_assets
        return ({"ok": True}, "换路后的提示词")

    monkeypatch.setattr(rpg, "_self_heal_reference_pool", fake_self_heal)
    monkeypatch.setattr(rpg.video_modes, "build_reference_assets", fake_build)
    monkeypatch.setattr(rpg, "_complete_reference_mode_with_healed_assets", fake_complete)

    result = asyncio.run(rpg._attempt_reference_self_heal(
        conn=_FakeEpisodeConn(7), job=_job(), meta=meta_obj, prompt_text="p",
        version={"id": "v1"}, shot_model=None, bible=None, screenplay=None,
        decision=None, blockers=["场景「荒山」没有可用的场景图"],
    ))

    assert result == ({"ok": True}, "换路后的提示词")


# ---------------------------------------------------------------------------
# _complete_reference_mode_with_healed_assets：策略门禁与状态落盘
# ---------------------------------------------------------------------------


def test_complete_with_healed_assets_returns_none_when_gate_fails(monkeypatch) -> None:
    class _Asset:
        def public_dict(self):
            return {}

    monkeypatch.setattr(rpg.video_modes, "reference_gallery_matches_library_policy", lambda _meta: False)

    result = asyncio.run(rpg._complete_reference_mode_with_healed_assets(
        conn=None, job=_job(), meta={}, prompt_text="p", version={"id": "v1"},
        shot_model=None, decision=None, assets=[_Asset()],
    ))

    assert result is None


def test_complete_with_healed_assets_writes_ready_state(monkeypatch) -> None:
    class _Asset:
        def public_dict(self):
            return {"path": "/tmp/x.png", "type": "character"}

    stage_calls: list[tuple] = []
    monkeypatch.setattr(rpg.video_modes, "reference_gallery_matches_library_policy", lambda _meta: True)
    monkeypatch.setattr(rpg.video_modes, "decision_to_dict", lambda _decision: {"decision": "x"})
    monkeypatch.setattr(rpg.video_modes, "dedupe_reference_dicts", lambda items: items)
    monkeypatch.setattr(
        rpg.video_modes, "append_reference_prompt_notes",
        lambda prompt, _assets, *, duration_s, required_identity_names: prompt + " [NOTES]",
    )
    monkeypatch.setattr(
        "app.media_pipeline.stage_state.set_pipeline_stage",
        lambda *args, **kwargs: stage_calls.append((args, kwargs)),
    )
    monkeypatch.setattr(
        "app.media_pipeline.reference_store.upsert_reference_set_from_meta",
        lambda **_kwargs: None,
    )

    conn = get_conn()
    meta: dict = {}
    shot_model = types.SimpleNamespace(duration_s=5)

    result = asyncio.run(rpg._complete_reference_mode_with_healed_assets(
        conn=conn, job=_job(), meta=meta, prompt_text="原文", version={"id": "v-not-seeded"},
        shot_model=shot_model, decision=None, assets=[_Asset()],
    ))

    assert result is not None
    result_meta, result_prompt = result
    assert result_meta["reference_generation_complete"] is True
    assert result_meta["reference_static_ready"] is True
    assert result_meta["reference_group_gate_passed"] is True
    assert result_meta["video_input_manifest_frozen"] is True
    assert result_prompt == "原文 [NOTES]"
    assert stage_calls, "必须推进到 STAGE_VIDEO_READY"


# ---------------------------------------------------------------------------
# finish_reference_mode_without_assets：整体编排与「只自愈一次」
# ---------------------------------------------------------------------------


def test_finish_without_assets_self_heals_once_and_returns_healed_result(monkeypatch) -> None:
    monkeypatch.setattr(
        rpg, "_reference_pool_blockers", lambda _manifest: ["人物「柳七月」缺少本集造型版本"],
    )
    calls = {"self_heal": 0}

    async def fake_attempt(**_kwargs):
        calls["self_heal"] += 1
        return ({"healed": True}, "healed prompt")

    def boom_repair(**_kwargs):
        raise AssertionError("自愈已经成功，不该走到待人工分支")

    monkeypatch.setattr(rpg, "_attempt_reference_self_heal", fake_attempt)
    monkeypatch.setattr(rpg, "_raise_reference_mode_repair_required", boom_repair)

    meta: dict = {"reference_manifest": {}}
    result = asyncio.run(rpg.finish_reference_mode_without_assets(
        conn=None, job=_job(), meta=meta, prompt_text="p", version={"id": "v1"},
        shot_model=None, bible=None, screenplay=None, decision=None,
        rejection_details=[], rejected_assets=[], lease_owner=None,
    ))

    assert result == ({"healed": True}, "healed prompt")
    assert calls["self_heal"] == 1
    assert meta["reference_self_heal_attempted"] is True


def test_finish_without_assets_skips_self_heal_when_already_attempted(monkeypatch) -> None:
    monkeypatch.setattr(
        rpg, "_reference_pool_blockers", lambda _manifest: ["人物「柳七月」缺少本集造型版本"],
    )

    def boom_self_heal(**_kwargs):
        raise AssertionError("已经自愈过一次，不应再次触发")

    captured: dict = {}

    def fake_repair(**kwargs):
        captured.update(kwargs)
        raise VideoInputRepairRequired("测试用拦截")

    monkeypatch.setattr(rpg, "_attempt_reference_self_heal", boom_self_heal)
    monkeypatch.setattr(rpg, "_raise_reference_mode_repair_required", fake_repair)

    meta = {"reference_manifest": {}, "reference_self_heal_attempted": True}
    with pytest.raises(VideoInputRepairRequired):
        asyncio.run(rpg.finish_reference_mode_without_assets(
            conn=None, job=_job(), meta=meta, prompt_text="p", version={"id": "v1"},
            shot_model=None, bible=None, screenplay=None, decision=None,
            rejection_details=[], rejected_assets=[], lease_owner=None,
        ))

    assert captured["blockers"] == ["人物「柳七月」缺少本集造型版本"]


def test_finish_without_assets_falls_back_to_repair_when_self_heal_fails(monkeypatch) -> None:
    monkeypatch.setattr(
        rpg, "_reference_pool_blockers", lambda _manifest: ["场景「荒山」没有可用的场景图"],
    )

    async def fake_attempt(**_kwargs):
        return None

    def fake_repair(**_kwargs):
        raise VideoInputRepairRequired("测试用拦截")

    monkeypatch.setattr(rpg, "_attempt_reference_self_heal", fake_attempt)
    monkeypatch.setattr(rpg, "_raise_reference_mode_repair_required", fake_repair)

    meta: dict = {"reference_manifest": {}}
    with pytest.raises(VideoInputRepairRequired):
        asyncio.run(rpg.finish_reference_mode_without_assets(
            conn=None, job=_job(), meta=meta, prompt_text="p", version={"id": "v1"},
            shot_model=None, bible=None, screenplay=None, decision=None,
            rejection_details=[], rejected_assets=[], lease_owner=None,
        ))

    assert meta["reference_self_heal_attempted"] is True


def test_finish_without_assets_skips_self_heal_when_no_blockers(monkeypatch) -> None:
    monkeypatch.setattr(rpg, "_reference_pool_blockers", lambda _manifest: [])

    def boom_self_heal(**_kwargs):
        raise AssertionError("候选池本来就是空的，不该触发自愈")

    async def fake_text_only(**_kwargs):
        return ({"text_only": True}, "text prompt")

    monkeypatch.setattr(rpg, "_attempt_reference_self_heal", boom_self_heal)
    monkeypatch.setattr(rpg, "_complete_reference_mode_as_text_only", fake_text_only)

    meta: dict = {"reference_manifest": {}}
    result = asyncio.run(rpg.finish_reference_mode_without_assets(
        conn=None, job=_job(), meta=meta, prompt_text="p", version={"id": "v1"},
        shot_model=None, bible=None, screenplay=None, decision=None,
        rejection_details=[], rejected_assets=[], lease_owner=None,
    ))

    assert result == ({"text_only": True}, "text prompt")
    assert "reference_self_heal_attempted" not in meta
