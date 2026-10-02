"""改选图/排序逻辑却没 bump 版本常量的防回归护栏。

RCA（2026-10-01，第 1 集第 22 段真实故障，详见
``tests/test_prop_reference_manifest_staleness.py`` 模块 docstring）：
77232c94 把 ``resources_order`` 接入 ``select_library_references``/
``app.multiview.ref_pack_priority`` 这两道选图/排序闸，改变了"参考图超限时
留谁、组内先后顺序"这个可观测行为，却没有 bump
``REFERENCE_INPUT_POLICY_VERSION``（见 ``app.video_modes.reference_prompt.
reference_gallery_matches_library_policy``，它是"旧冻结画廊还能不能继续当
输入用"的唯一版本闸）。结果是旧冻结画廊在这道闸上畅通无阻，继续被判定为
"仍然符合库资产策略"。

本文件不复刻那次具体故障（那是 ``test_prop_reference_manifest_staleness.py``
的职责），而是守住"下一次类似的事不再发生"：对这两个函数的选取/排序行为
在一组代表性输入上取确定性指纹，记录在案；指纹一旦变化，就必须同时看到
``REFERENCE_INPUT_POLICY_VERSION`` 也变了——这不是说版本号是本次故障选中
的修法（本次选的是让 ``app.multiview.manifest_revisions_match`` 直接比较
``manifest["props"]`` 的选取结果/修订，见该文件），而是因为
``REFERENCE_INPUT_POLICY_VERSION`` 本来就是这两个函数行为变化时唯一会被
检查、用来让旧画廊整体失效的全局闸门，往后谁动了这两个函数的选取/排序
算法，都应该被逼着回答"旧画廊还能不能继续用"这个问题，而不是像这次一样
从未被问到。

判据不是维护一份"哪些历史故障用例"的硬编码名单，而是直接调用这两个函数
在一组覆盖人物去重、场景多选、道具 ``resources_order``/旧数据缺省/超限裁剪
的代表性输入上的真实产出，取 sha256 摘要——指纹变了就是"行为变了"的数据
证据，不靠猜或靠记哪次改动触碰了哪几行源码。
"""
from __future__ import annotations

import hashlib

from app.multiview import ref_pack_priority
from app.video_modes.mode_selection import REFERENCE_INPUT_POLICY_VERSION, ReferenceImageAsset
from app.video_modes.reference_assemble import select_library_references


def _asset(entity_type: str, name: str, **kwargs) -> ReferenceImageAsset:
    return ReferenceImageAsset(
        id=f"{entity_type}-{name}", url="", type=entity_type, source="asset_library",
        path=f"/tmp/{entity_type}-{name}.jpg", entity_type=entity_type, entity_name=name,
        relatedCharacterIds=[name], **kwargs,
    )


def _representative_assets() -> list[ReferenceImageAsset]:
    """覆盖：同名人物去重、多场景各自保留、道具按 resources_order 排序、
    旧数据缺 resources_order 时的回退顺序——与
    ``tests/test_prop_reference_assets.py`` 已有单元测试覆盖的分支一致，
    只是这里把它们合在一次装配里取指纹。"""
    return [
        _asset("character", "A", view_role="front_full"),
        _asset("character", "A", view_role="profile"),  # 同名重复，装配后应去重
        _asset("character", "B", view_role="three_quarter"),
        _asset("scene", "S1", view_role="establishing"),
        _asset("scene", "S2", view_role="establishing"),
        _asset("prop", "prop-zzz", resources_order=0),
        _asset("prop", "prop-aaa", resources_order=1),
        _asset("prop", "prop-legacy-no-order"),  # 旧数据无 resources_order
        _asset("prop", "prop-mmm", resources_order=2),
    ]


def _representative_ref_dicts() -> list[dict[str, object]]:
    return [
        {"id": "prop-zzz", "type": "prop", "source": "asset_library", "resources_order": 0},
        {"id": "prop-aaa", "type": "prop", "source": "asset_library", "resources_order": 1},
        {"id": "prop-legacy", "type": "prop", "source": "asset_library"},
        {"id": "char-1", "type": "character", "source": "asset_library"},
        {"id": "scene-1", "type": "scene", "source": "asset_library"},
    ]


def _behavior_fingerprint() -> str:
    selected = select_library_references(_representative_assets(), ["A", "B"], max_images=4)
    selected_repr = tuple((a.entity_type, a.entity_name, a.resources_order) for a in selected)

    priority_repr = tuple(
        (r["type"], r["id"], r.get("resources_order"))
        for r in sorted(_representative_ref_dicts(), key=ref_pack_priority)
    )

    payload = repr((selected_repr, priority_repr))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# 2026-10-01 记录值：77232c94 落地 resources_order 排序闸之后的真实产出指纹，
# 与当时的 REFERENCE_INPUT_POLICY_VERSION 一起入账。这不是"指纹不能变"，是
# "指纹变了，这个版本常量必须也变"（见下面测试函数）。
_RECORDED_FINGERPRINT = "c7f0dd4495266c3c8011651df518d9b30d9d07cc02ce01867afa79aebf9ee1af"
_RECORDED_POLICY_VERSION = "library_assets_only_v1"


def test_selection_behavior_change_requires_policy_version_bump() -> None:
    current_fingerprint = _behavior_fingerprint()
    version_moved = REFERENCE_INPUT_POLICY_VERSION != _RECORDED_POLICY_VERSION
    if current_fingerprint == _RECORDED_FINGERPRINT:
        return
    assert version_moved, (
        "select_library_references/ref_pack_priority 的选取或排序行为变了"
        f"（指纹从 {_RECORDED_FINGERPRINT} 变成 {current_fingerprint}），但 "
        "REFERENCE_INPUT_POLICY_VERSION 还是旧值 "
        f"{_RECORDED_POLICY_VERSION!r}——这意味着冻结在旧版本号下的历史参考图"
        "清单会继续被 reference_gallery_matches_library_policy 判定为仍然可用"
        "（2026-10-01 第 1 集第 22 段真实故障同款根因）。请 bump "
        "REFERENCE_INPUT_POLICY_VERSION（app/video_modes/mode_selection.py），"
        "并评估是否需要让 app.multiview.manifest_revisions_match 也感知这次"
        "行为变化，再把本测试的 _RECORDED_FINGERPRINT/_RECORDED_POLICY_VERSION "
        "更新成新值。"
    )
