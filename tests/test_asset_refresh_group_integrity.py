"""「参考资产已更新」面板的组级完整性：分组状态判据只认本分组自己的实体
差异（不被同一段里别的实体变化带偏）、同一镜头跨分组不重复查候选、以及整
组采用前"本组是否已全部处理完"的闸门。与 ``tests/test_asset_refresh_report.
py`` 分成两个文件是因为新测试文件同样受 ≤500 行上限约束（CLAUDE.md「新增
测试文件按 500 严格执行」），复用该文件已有的 fixture/helper，不重新定义。
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.domain.video_ops import asset_refresh
from app.domain.video_ops.asset_refresh_report import episode_asset_refresh_groups
import app.video_modes.prop_references as prop_references
from tests.conftest import patch_api_everywhere
from tests.test_asset_refresh_report import (
    _SEGMENT,
    _episode_row,
    _fresh_conn,
    _insert_pack_shot,
    _insert_version,
    _lookup_stub,
    _multi_lookup_stub,
    _prop_manifest,
)

_SEGMENT_TWO_PROPS = {"resources": {"characters": [], "scenes": [], "props": [
    {"label": "马克杯", "description": ""}, {"label": "围裙", "description": ""},
]}}


def _two_prop_manifest(*, mug_ready: bool, mug_rev: str | None, apron_ready: bool, apron_rev: str | None) -> dict:
    return {
        "episode_no": 1, "shot_id": "ignored", "characters": [], "scene": None, "additional_scenes": [],
        "props": [
            {"label": "马克杯", "description": "", "ready": mug_ready, "image_path": "", "resources_order": 0, "prop_revision_id": mug_rev},
            {"label": "围裙", "description": "", "ready": apron_ready, "image_path": "", "resources_order": 1, "prop_revision_id": apron_rev},
        ],
    }


def test_unrelated_entity_in_same_group_stays_latest_not_needs_regen(monkeypatch, tmp_path) -> None:
    """一段同时引用「马克杯」（未变）与「围裙」（变了）：在「马克杯」分组下这
    段必须是 latest，不能被「围裙」的变化带偏成 needs_regen（2026-10-03 复现
    的真实缺陷：旧实现按整段全量 diff 而不是本分组自己的 own_diff 判定状态，
    会把这段在马克杯分组下标成 needs_regen、理由写成跟马克杯毫无关系的"围裙
    变了"）。"""
    conn = _fresh_conn()
    patch_api_everywhere(monkeypatch, "get_conn", lambda: conn)
    image_mug = tmp_path / "mug_shared.png"
    image_mug.write_bytes(b"x")
    image_apron = tmp_path / "apron.png"
    image_apron.write_bytes(b"y")
    monkeypatch.setattr(
        prop_references, "_prop_reference_lookup",
        _multi_lookup_stub({"马克杯": (str(image_mug), "prop_mug_rev1"), "围裙": (str(image_apron), "prop_apron_rev2")}),
    )
    # s1：只引用马克杯，冻结时未 ready → 现在 ready，形成「prop:马克杯」分组。
    _insert_pack_shot(conn, shot_id="s1", shot_no=1, adopted_version_id="v1", segment=_SEGMENT)
    _insert_version(
        conn, version_id="v1", shot_id="s1", version_no=1, segment=_SEGMENT,
        frozen_manifest=_prop_manifest(ready=False, revision_id=None),
    )
    # s2：同时引用马克杯（冻结时已是 prop_mug_rev1，与当前一致，未变）与围裙
    # （冻结时未 ready，现在 ready，真的变了）。
    _insert_pack_shot(conn, shot_id="s2", shot_no=2, adopted_version_id="v2", segment=_SEGMENT_TWO_PROPS)
    _insert_version(
        conn, version_id="v2", shot_id="s2", version_no=1, segment=_SEGMENT_TWO_PROPS,
        frozen_manifest=_two_prop_manifest(mug_ready=True, mug_rev="prop_mug_rev1", apron_ready=False, apron_rev=None),
    )
    conn.commit()

    report = episode_asset_refresh_groups(conn, _episode_row(conn))
    groups = {g["entity_key"]: g for g in report["groups"]}
    assert set(groups) == {"prop:马克杯", "prop:围裙"}

    mug_members = {m["shot_id"]: m for m in groups["prop:马克杯"]["members"]}
    assert mug_members["s1"]["status"] == "needs_regen"
    assert mug_members["s2"]["status"] == "latest", mug_members["s2"]["reason"]

    apron_members = {m["shot_id"]: m for m in groups["prop:围裙"]["members"]}
    assert apron_members["s2"]["status"] == "needs_regen"
    assert "围裙" in apron_members["s2"]["reason"]


def test_build_groups_caches_matching_candidates_per_shot(monkeypatch) -> None:
    """同一个 shot 同时属于两个发生变化的实体分组时，"有没有匹配当前参考的
    候选"这个查询只应该对这个 shot 算一次——答案只取决于 shot 本身（候选是否
    匹配*完整*当前 manifest），与具体触发它的是哪个实体无关，重复算是纯浪费
    （2026-10-03：本轮多张道具卡同时更新时，不少镜头同时命中多个分组，会被
    重复查询）。"""
    # 按全限定名经 sys.modules 取子模块，不用 ``from app.domain.video_ops import
    # asset_refresh_report`` 或 ``import a.b.c as x``——两者都走属性解析，而
    # ``app/domain/video_ops/__init__.py`` 把同名函数
    # ``asset_refresh.asset_refresh_report``（GET 路由）再导出成包属性后，
    # 包命名空间里的 ``asset_refresh_report`` 就是那个函数，不是子模块本身
    # （CLAUDE.md「助手遍历子模块要用 sys.modules 按全限定名解析，不要用
    # getattr」同款陷阱——``import ... as`` 一样会被坑，必须直接查 sys.modules）。
    import sys

    report_mod = sys.modules["app.domain.video_ops.asset_refresh_report"]

    calls: list[str] = []

    def fake_matching(conn, shot_id, current, *, exclude_version_id, project_id, episode_no):
        calls.append(shot_id)
        return []

    monkeypatch.setattr(report_mod, "_matching_candidates", fake_matching)
    shot = {"id": "s1", "shot_no": 1, "duration_s": 15}
    diff_a = {"entity_key": "prop:马克杯", "entity_type": "prop", "entity_name": "马克杯", "category": "added", "category_label": "新增参考图"}
    diff_b = {"entity_key": "character:温念", "entity_type": "character", "entity_name": "温念", "category": "updated", "category_label": "参考图已更新"}
    record = {
        "shot": shot,
        "current": {"characters": [{"name": "温念"}], "scene": None, "additional_scenes": [], "props": [{"label": "马克杯"}]},
        "diff": [diff_a, diff_b], "adopted": True, "adopted_version_id": "v1",
        "project_id": "p", "episode_no": 1,
    }
    groups = report_mod._build_groups(None, [record])
    assert {g["entity_key"] for g in groups} == {"prop:马克杯", "character:温念"}
    assert calls == ["s1"]


def test_adopt_core_rejects_when_group_still_has_needs_regen_shot(monkeypatch, tmp_path) -> None:
    """本组（prop:马克杯）里 s2 还是 needs_regen（还没重生成出匹配候选），
    调用方只传了 s1 的 versions——必须整组拒绝，不能只换 s1（否则跟用户亲自
    拒绝的「只换第 10 段」是同一类跨段不一致，只是从「被用户拦住」变成了
    「产品允许发生」，2026-10-03 复现）。"""
    conn = _fresh_conn()
    patch_api_everywhere(monkeypatch, "get_conn", lambda: conn)
    image = tmp_path / "mug_gate.png"
    image.write_bytes(b"x")
    video_candidate = tmp_path / "candidate_gate.mp4"
    video_candidate.write_bytes(b"v")
    monkeypatch.setattr(prop_references, "_prop_reference_lookup", _lookup_stub(str(image), "prop_mug_rev2"))
    # s1：has_candidate——已有按最新参考生成的成功候选 v2。
    _insert_pack_shot(conn, shot_id="s1", shot_no=1, adopted_version_id="v1")
    _insert_version(conn, version_id="v1", shot_id="s1", version_no=1, frozen_manifest=_prop_manifest(ready=True, revision_id="prop_mug_rev1"))
    _insert_version(
        conn, version_id="v2", shot_id="s1", version_no=2,
        frozen_manifest=_prop_manifest(ready=True, revision_id="prop_mug_rev2"), video_path=str(video_candidate),
    )
    # s2：needs_regen——还没有任何匹配当前参考的候选。
    _insert_pack_shot(conn, shot_id="s2", shot_no=2, adopted_version_id="v3")
    _insert_version(conn, version_id="v3", shot_id="s2", version_no=1, frozen_manifest=_prop_manifest(ready=False, revision_id=None))
    conn.commit()
    pre_adopted = conn.execute("SELECT adopted_version_id FROM shots WHERE id='s1'").fetchone()["adopted_version_id"]

    with pytest.raises(HTTPException) as exc_info:
        asset_refresh._asset_refresh_adopt_core("e", {
            "entity_key": "prop:马克杯", "reason": "按新道具卡统一采用",
            "versions": {"s1": "v2"},
        })
    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "ASSET_REFRESH_ADOPT_INCOMPLETE"
    assert exc_info.value.detail["needs_regen_shot_ids"] == ["s2"]
    # 整组拒绝，s1 也没被动——不是"先换能换的，剩下的再说"。
    assert conn.execute("SELECT adopted_version_id FROM shots WHERE id='s1'").fetchone()["adopted_version_id"] == pre_adopted


def test_adopt_core_rejects_when_versions_miss_a_has_candidate_shot(monkeypatch, tmp_path) -> None:
    """本组两段都是 has_candidate，调用方只给了其中一段的 versions——同样整
    组拒绝：整组采用必须覆盖本组全部待切换段落，不能漏掉。"""
    conn = _fresh_conn()
    patch_api_everywhere(monkeypatch, "get_conn", lambda: conn)
    image = tmp_path / "mug_gate2.png"
    image.write_bytes(b"x")
    video1 = tmp_path / "candidate1.mp4"; video1.write_bytes(b"v")
    video2 = tmp_path / "candidate2.mp4"; video2.write_bytes(b"v")
    monkeypatch.setattr(prop_references, "_prop_reference_lookup", _lookup_stub(str(image), "prop_mug_rev2"))
    _insert_pack_shot(conn, shot_id="s1", shot_no=1, adopted_version_id="v1")
    _insert_version(conn, version_id="v1", shot_id="s1", version_no=1, frozen_manifest=_prop_manifest(ready=True, revision_id="prop_mug_rev1"))
    _insert_version(
        conn, version_id="v2", shot_id="s1", version_no=2,
        frozen_manifest=_prop_manifest(ready=True, revision_id="prop_mug_rev2"), video_path=str(video1),
    )
    _insert_pack_shot(conn, shot_id="s2", shot_no=2, adopted_version_id="v3")
    _insert_version(conn, version_id="v3", shot_id="s2", version_no=1, frozen_manifest=_prop_manifest(ready=True, revision_id="prop_mug_rev1"))
    _insert_version(
        conn, version_id="v4", shot_id="s2", version_no=2,
        frozen_manifest=_prop_manifest(ready=True, revision_id="prop_mug_rev2"), video_path=str(video2),
    )
    conn.commit()

    with pytest.raises(HTTPException) as exc_info:
        asset_refresh._asset_refresh_adopt_core("e", {
            "entity_key": "prop:马克杯", "reason": "按新道具卡统一采用",
            "versions": {"s1": "v2"},
        })
    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "ASSET_REFRESH_ADOPT_INCOMPLETE"
    assert exc_info.value.detail["missing_shot_ids"] == ["s2"]
