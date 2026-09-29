"""app.production.storyboard_identity_contract：本段没有 portrait_id 时从全量
人物谱稳定补上当前生效的定妆照，拿不到时给准确的 degraded_capabilities 告警。

2026-09-28 真实回归（《顾念长安（第二版）》EP1 opus5.5 分镜，proj_ca86b15ab7d7/
ep_a3c61162b4ce）：第 9/15/18 段 resources.characters 里 bible:顾屿 的
portrait_id 为 null（其余段是 portrait_9c434ec58162），第 19 段 bible:温念
为 null。根因核实：``storyboard_pack._segment_relevant_assets`` 按映射阶段
登记的原文段号过滤 ``relevant_assets.characters``（``_hits``），这份覆盖有
缺口（顾屿全集 segment_indexes 缺 17-20/31-33/39-40，温念缺 41）——模型看不到
这几段的 portrait_id，只能诚实留空；但 payload["asset_manifest"]["characters"]
这份全量人物谱里，同一个 bible 角色全集只有一条记录、一个 portrait_id，一直都在。
"""
from __future__ import annotations

from app.production.storyboard_identity_contract import (
    _backfill_portrait_binding,
    canonical_segment_identities,
)

_MANIFEST = {
    "asset_manifest": {
        "characters": [
            {"identity_id": "bible:顾屿", "display_name": "顾屿", "portrait_id": "portrait_9c434ec58162"},
            {"identity_id": "bible:温念", "display_name": "温念", "portrait_id": "portrait_476f694210fa"},
            {"identity_id": "bible:待出图角色", "display_name": "待出图角色", "portrait_id": None},
        ],
        "functional_extras": [],
    },
}


def _character(identity_id: str, *, portrait_id: str | None = None, visibility: str = "visible") -> dict:
    return {"identity_id": identity_id, "display_name": "", "portrait_id": portrait_id, "visibility": visibility}


def _segment(*characters: dict) -> dict:
    return {"resources": {"characters": list(characters)}, "source_segment_indexes": [17, 18, 19], "dialogue": []}


# ---------------------------------------------------------------------------
# 红绿验证：修复前 canonical_segment_identities 只改 display_name/subject_kind，
# 不碰 portrait_id——本段拿不到就一直是 null。
# ---------------------------------------------------------------------------

def _legacy_canonicalize_one_character(entry: dict, manifest_entry: dict) -> None:
    """修复前 ``canonical_segment_identities`` 命中 ``entries`` 分支的逐字体：
    只回填 display_name/subject_kind，portrait_id 分支根本不存在。"""
    entry["display_name"] = manifest_entry.get("display_name") or entry["identity_id"]
    if entry.get("subject_kind") not in {"extra", "crowd"}:
        entry["subject_kind"] = "character"


def test_red_legacy_logic_leaves_portrait_id_null() -> None:
    entry = _character("bible:顾屿")
    _legacy_canonicalize_one_character(entry, _MANIFEST["asset_manifest"]["characters"][0])
    assert entry["portrait_id"] is None  # 本段拿不到，且从未被补上——就是真实回归的样子


def test_green_fixed_logic_backfills_portrait_id() -> None:
    entry = _character("bible:顾屿")
    _backfill_portrait_binding(entry, _MANIFEST["asset_manifest"]["characters"][0], {}, "bible:顾屿")
    assert entry["portrait_id"] == "portrait_9c434ec58162"


# ---------------------------------------------------------------------------
# _backfill_portrait_binding 单元判据
# ---------------------------------------------------------------------------

def test_existing_portrait_id_is_never_overridden() -> None:
    entry = _character("bible:顾屿", portrait_id="portrait_from_this_segment")
    _backfill_portrait_binding(entry, _MANIFEST["asset_manifest"]["characters"][0], {}, "bible:顾屿")
    assert entry["portrait_id"] == "portrait_from_this_segment"


def test_accurate_advisory_when_manifest_also_has_no_portrait() -> None:
    """人物谱本身也没有定妆照（角色确实还没出图）：不假装补上了，给准确告警。"""
    entry = _character("bible:待出图角色", visibility="visible")
    result: dict = {}
    _backfill_portrait_binding(entry, _MANIFEST["asset_manifest"]["characters"][2], result, "bible:待出图角色")
    assert entry["portrait_id"] is None
    advisories = result["degraded_capabilities"]
    assert len(advisories) == 1
    assert "STORYBOARD_PACK_PORTRAIT_MISSING" in advisories[0]
    assert "待出图角色" in advisories[0]
    assert "本段在场" in advisories[0]  # 与数据一致：不是「不在本段内」这类矛盾措辞


def test_no_advisory_when_character_not_visible_this_segment() -> None:
    entry = _character("bible:待出图角色", visibility="voice_only")
    result: dict = {}
    _backfill_portrait_binding(entry, _MANIFEST["asset_manifest"]["characters"][2], result, "bible:待出图角色")
    assert result.get("degraded_capabilities", []) == []


def test_missing_portrait_advisory_is_idempotent_across_repeated_calls() -> None:
    """评审确认（2026-09-28）：真实流水线对同一份数据至少调用 canonical_segment_
    identities 两到三次（生成期 finalize_generated_identity、落库期
    persist_storyboard_pack、身份工作台 prepare_identity_candidate 编辑保存），
    每次都从上一次已带告警的产物继续处理；portrait_id 回填分支本就幂等，告警
    分支之前没有同等保护，人物谱本身也没出图时会逐次线性堆叠重复告警。"""
    entry = _character("bible:待出图角色", visibility="visible")
    manifest_entry = _MANIFEST["asset_manifest"]["characters"][2]
    result: dict = {}
    _backfill_portrait_binding(entry, manifest_entry, result, "bible:待出图角色")
    _backfill_portrait_binding(entry, manifest_entry, result, "bible:待出图角色")
    _backfill_portrait_binding(entry, manifest_entry, result, "bible:待出图角色")
    assert len(result["degraded_capabilities"]) == 1


def test_canonical_segment_identities_does_not_duplicate_advisory_on_replay() -> None:
    """通过公开入口模拟真实流水线的多次调用（生成期 + 落库期）：同一个人物
    真的还没出图时，degraded_capabilities 里这条告警只应出现一次，不随重复
    canonicalize 无限增长。"""
    segment = _segment(_character("bible:待出图角色"))
    first_pass = canonical_segment_identities(segment, _MANIFEST)
    second_pass = canonical_segment_identities(first_pass, _MANIFEST)
    third_pass = canonical_segment_identities(second_pass, _MANIFEST)
    portrait_missing_notes = [n for n in third_pass["degraded_capabilities"] if "STORYBOARD_PACK_PORTRAIT_MISSING" in n]
    assert len(portrait_missing_notes) == 1


# ---------------------------------------------------------------------------
# 通过公开入口 canonical_segment_identities 集成验证（真实调用路径）
# ---------------------------------------------------------------------------

def test_canonical_segment_identities_backfills_portrait_for_gap_segment() -> None:
    """第 9 段的形状：顾屿在场但本段 relevant_assets 过滤把他漏掉，portrait_id 为空。"""
    segment = _segment(_character("bible:顾屿"), _character("bible:温念", portrait_id="portrait_476f694210fa"))
    normalized = canonical_segment_identities(segment, _MANIFEST)
    by_id = {c["identity_id"]: c for c in normalized["resources"]["characters"]}
    assert by_id["bible:顾屿"]["portrait_id"] == "portrait_9c434ec58162"
    assert by_id["bible:温念"]["portrait_id"] == "portrait_476f694210fa"
    assert normalized.get("degraded_capabilities", []) == []


def test_canonical_segment_identities_reports_when_truly_no_portrait() -> None:
    segment = _segment(_character("bible:待出图角色"))
    normalized = canonical_segment_identities(segment, _MANIFEST)
    assert any("STORYBOARD_PACK_PORTRAIT_MISSING" in note for note in normalized["degraded_capabilities"])
