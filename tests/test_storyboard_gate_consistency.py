"""WS8-A：分镜台资源类 [未拦截]/[拦截] 警告必须与生成台判据一致。

真实事故：三国演义_白话文版_前二十回 ep1 第 1 镜挂着 4 条 [未拦截] 警告
（3 条 STORYBOARD_PACK_RESOURCE_CHARACTER_UNKNOWN + 1 条
STORYBOARD_PACK_RESOURCE_SCENE_MANIFEST_GAP），用户读成"不影响"；生产库真实
数据（proj_ecabd38b7261/ep_9357bedfc843/shot_no=1）取自
``ssh mjb`` 只读连接，直接固化为 fixture，不是编出来的样例。三个人物都是没有
定妆照的群演/一次性人物（``portrait_id`` 全为 null），本段 ``resources.
scenes`` 为空——这正是「候选池天生为空、生成台会回退纯文本出片」的场景，是
今晚才修通的纯文本回退（``app.media_exec.reference_pool_gate``）要接住的
那一类；本测试要验证的是分镜台的告警文案与标签现在是否说清了这个后果。
"""
from __future__ import annotations

from types import SimpleNamespace

from app.media_exec.reference_pool_gate import _reference_repair_guidance
from app.multiview import PACK_STATUS_READY
from app.validators.resource_forecast import (
    OK,
    TEXT_ONLY_FALLBACK,
    WILL_BLOCK,
    blocked_consequence_text,
    forecast_shot_production,
    resource_advisories_for_segment,
    shot_resource_advisories,
)

# 生产库真实数据：proj_ecabd38b7261（三国演义_白话文版_前二十回）
# ep_9357bedfc843（ep1）shot_no=1 的 shot_contract_json.storyboard_pack_segment
# .resources，逐字段照抄（只读 ssh mjb 取数，未编造）。
REAL_TEXT_ONLY_RESOURCES = {
    "characters": [
        {
            "identity_id": "中年留三绺长须的藏青色官袍官员",
            "portrait_id": None,
            "description": "中年男性，留三绺长须，身着藏青色官袍",
        },
        {
            "identity_id": "身着粗布麻衣的年轻男子",
            "portrait_id": None,
            "description": "年轻男性，身着粗布麻衣",
        },
        {
            "identity_id": "身着粗布麻衣的中年路人",
            "portrait_id": None,
            "description": "3名中年男性，身着粗布麻衣",
        },
    ],
    "scenes": [],
    "props": [{"label": "纸质卷轴", "description": "用于张贴告示的纸质卷轴"}],
}


def _manifest_char(name: str, *, asset_required: bool, has_asset: bool = False) -> dict:
    """按 app.multiview._storyboard_pack_asset_dependencies 的真实产出形状构造。"""
    view = {"id": "v1", "image_path": "/tmp/x.png"} if has_asset else None
    return {
        "name": name,
        "asset_required": asset_required,
        "look_revision_id": "portrait_x" if has_asset else None,
        "pack_status": PACK_STATUS_READY if has_asset else None,
        "selected_view_ids": ["v1"] if has_asset else [],
        "selected_views": [view] if has_asset else [],
        "missing_required": [],
    }


def _real_text_only_manifest() -> dict:
    """对应 REAL_TEXT_ONLY_RESOURCES：三个群演都没有人物卡，has_card=False
    -> asset_required=False（app.multiview._storyboard_pack_asset_dependencies
    的真实产出规则），场景为空。"""
    return {
        "characters": [
            _manifest_char("中年留三绺长须的藏青色官袍官员", asset_required=False),
            _manifest_char("身着粗布麻衣的年轻男子", asset_required=False),
            _manifest_char("身着粗布麻衣的中年路人", asset_required=False),
        ],
        "scene": None,
        "additional_scenes": [],
    }


class TestForecastShotProduction:
    def test_missing_manifest_is_blocked_not_silently_ok(self):
        forecast = forecast_shot_production(None)
        assert forecast.verdict == WILL_BLOCK
        assert forecast.blockers == ["依赖 manifest 缺失"]

    def test_real_text_only_fallback_manifest(self):
        forecast = forecast_shot_production(_real_text_only_manifest())
        assert forecast.verdict == TEXT_ONLY_FALLBACK
        assert forecast.blockers == []

    def test_pack_status_pending_blocks_with_exact_blocker_text(self):
        manifest = {
            "characters": [_manifest_char("中年官员", asset_required=True)],
            "scene": None,
            "additional_scenes": [],
        }
        manifest["characters"][0]["look_revision_id"] = "portrait_x"
        manifest["characters"][0]["pack_status"] = "pending"
        forecast = forecast_shot_production(manifest)
        assert forecast.verdict == WILL_BLOCK
        assert forecast.blockers == ["人物「中年官员」多视角包状态为 pending"]

    def test_required_character_with_real_asset_is_ok(self):
        manifest = {
            "characters": [_manifest_char("主角", asset_required=True, has_asset=True)],
            "scene": None,
            "additional_scenes": [],
        }
        forecast = forecast_shot_production(manifest)
        assert forecast.verdict == OK
        assert forecast.blockers == []

    def test_required_scene_without_asset_blocks(self):
        manifest = {
            "characters": [],
            "scene": {"name": "客厅", "asset_required": True},
            "additional_scenes": [],
        }
        forecast = forecast_shot_production(manifest)
        assert forecast.verdict == WILL_BLOCK
        assert forecast.blockers == ["场景「客厅」缺少本集场景版本"]


class TestResourceAdvisoriesForSegment:
    def test_real_shot_produces_four_advisories_matching_incident(self):
        """三国 ep1 shot1 真实数据：3 个群演 + 1 条"无场景资源"，且全部
        [未拦截]（不是 [拦截]）——因为这一镜确实会回退纯文本出片，不是被拦。"""
        advisories = resource_advisories_for_segment(
            resources=REAL_TEXT_ONLY_RESOURCES, manifest=_real_text_only_manifest(),
        )
        assert len(advisories) == 4
        assert all("[未拦截]" in line for line in advisories)
        assert all("[拦截]" not in line for line in advisories)
        character_lines = [l for l in advisories if "CHARACTER_UNKNOWN" in l]
        assert len(character_lines) == 3
        for line in character_lines:
            assert "生成台将按纯文本出片" in line
            assert "外观只由分镜文字决定" in line
        scene_lines = [l for l in advisories if "SCENE_MANIFEST_GAP" in l]
        assert len(scene_lines) == 1
        assert "生成台将按纯文本出片" in scene_lines[0]

    def test_blocked_character_gets_blocked_tag_and_consequence_text(self):
        resources = {
            "characters": [{"identity_id": "bible:中年官员", "portrait_id": None, "description": ""}],
            "scenes": [],
        }
        manifest = {
            "characters": [{
                "name": "中年官员", "asset_required": True,
                "look_revision_id": "portrait_x", "pack_status": "pending",
                "selected_view_ids": [], "selected_views": [],
            }],
            "scene": None, "additional_scenes": [],
        }
        advisories = resource_advisories_for_segment(resources=resources, manifest=manifest)
        assert len(advisories) == 1
        line = advisories[0]
        assert "[拦截]" in line
        assert "[未拦截]" not in line
        assert "CHARACTER_BLOCKED" in line
        assert "本镜会被生成台拦下" in line
        assert "人物「中年官员」多视角包状态为 pending" in line

    def test_character_with_real_asset_produces_no_advisory(self):
        resources = {
            "characters": [{"identity_id": "主角", "portrait_id": "p1", "description": ""}],
            "scenes": [],
        }
        manifest = {
            "characters": [_manifest_char("主角", asset_required=True, has_asset=True)],
            "scene": None, "additional_scenes": [],
        }
        assert resource_advisories_for_segment(resources=resources, manifest=manifest) == []

    def test_no_scene_advisory_when_verdict_is_ok(self):
        """resources.scenes 为空，但有一个人物已经能拿到真实参考图（verdict=OK）
        时，不额外报"无场景资源"——没有场景不构成这一镜的额外后果。"""
        resources = {
            "characters": [{"identity_id": "主角", "portrait_id": "p1", "description": ""}],
            "scenes": [],
        }
        manifest = {
            "characters": [_manifest_char("主角", asset_required=True, has_asset=True)],
            "scene": None, "additional_scenes": [],
        }
        advisories = resource_advisories_for_segment(resources=resources, manifest=manifest)
        assert advisories == []

    def test_empty_manifest_is_not_silently_ok(self):
        """CLAUDE.md「空集合不等于无需检查」：manifest=None 时 blockers 非空
        （"依赖 manifest 缺失"），必须落在 [拦截] 分支，不能假装 OK。"""
        resources = {
            "characters": [{"identity_id": "某人", "portrait_id": None, "description": ""}],
            "scenes": [],
        }
        advisories = resource_advisories_for_segment(resources=resources, manifest=None)
        assert len(advisories) == 1
        assert "[拦截]" in advisories[0]


class TestShotResourceAdvisories:
    def test_legacy_shot_without_segment_returns_empty(self):
        shot = SimpleNamespace(storyboard_pack_segment=None)
        assert shot_resource_advisories(shot, manifest=None) == []

    def test_pack_shot_delegates_to_segment_resources(self):
        shot = SimpleNamespace(
            storyboard_pack_segment={"resources": REAL_TEXT_ONLY_RESOURCES},
        )
        advisories = shot_resource_advisories(shot, manifest=_real_text_only_manifest())
        assert len(advisories) == 4


class TestReferencePoolGateSharesGuidanceText:
    """A3：reference_pool_gate 的修复引导与分镜台的 [拦截] 文案必须来自同一
    份 blocked_consequence_text，不许两处各说各话。"""

    def test_guidance_matches_shared_formatter(self):
        blockers = ["人物「中年官员」多视角包状态为 pending"]
        assert _reference_repair_guidance(blockers) == blocked_consequence_text(blockers)
        assert "生成台会拦下本镜" in _reference_repair_guidance(blockers)
        assert "人物谱" in _reference_repair_guidance(blockers)

    def test_no_blockers_keeps_regenerate_guidance_untouched(self):
        """blockers 为空但仍然走到修复引导（真实资产被 VLM 判不合格的场景）
        时，文案维持"重新生成"而不是"补齐"——两种缺口的出路不一样，不能被
        共用文案覆盖掉。"""
        assert _reference_repair_guidance([]) == "请到「人物谱」或「场景库」重新生成对应定妆照/场景图后重试。"
