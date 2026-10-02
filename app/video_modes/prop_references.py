"""道具参考图接入分镜参考图池（P2：道具形态漂移修复的消费侧，视频侧一半）。

与 ``app.production.storyboard_prop_assets``（分镜阶段二素材清单那一半）配套：
那边把道具外观/参考图写进 ``asset_manifest.props``/segment ``resources.props``；
本模块把已经落到 ``app.multiview`` reference manifest 里的道具条目
（``manifest["props"]``，见 ``app.multiview._storyboard_pack_asset_dependencies``）
展开成 ``app.video_modes`` 参考图装配管线认识的锚点/候选形状，供
``app.video_modes.reference_assemble`` 挑进最终参考图池。

``app.props``（WS-P1 并行落地的世界书物件库）没到位前，``_prop_reference_
lookup`` 惰性 import + ImportError 兜底返回 None——道具没有参考图时按"没有
可用参考图"处理，不阻断分镜/视频生成。测试直接 monkeypatch 本模块的
``_prop_reference_lookup`` 验证装配逻辑。

2026-10-01（``resources_order``，第 1 集第 19/20 段真实成片复查）：
``app.multiview.ref_pack_priority`` 超过参考图张数上限（``max_reference_
images()``，9 张）时按道具分数+id 取舍，分数对库资产道具恒为 0（没有 QA
分数），实际落到 ``ref.id``——一串与本段画面无关的随机串，哪件道具被舍弃
因此是随机的（真实故障：第 20 段 resources.props 列了 8 项，加上人物/场景
超过 9 张，行李箱参考图被随机丢弃，成片行李箱颜色与卡片不符）。修法：本段
``resources.props`` 的声明顺序就是模型给出的重要性排序（见
``app.production.storyboard_prop_visibility`` 新增的排序正面陈述），
``resolve_segment_prop_manifest_entries`` 原样保留这个顺序、按下标打上
``resources_order``，``prop_library_anchors`` 透传给锚点字典，一路经
``app.video_modes.asset_lookup._asset_from_path``/``ReferenceImageAsset.
resources_order`` 字段带到最终参考图 dict，供 ``ref_pack_priority`` 的
道具档把它当第一级排序键——超限时优先保留声明顺序靠前的道具，不再看
与排序无关的随机 id。没有这个字段的旧数据（``resources_order is None``）
排在有序号的道具之后，组内仍按原有的 ``-quality, id`` 排序，行为不变。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any


def _prop_reference_lookup(conn, project_id: str, name: str, episode_no: int) -> Any:
    """与 ``app.production.storyboard_prop_assets._prop_reference_lookup`` 同一
    惰性 import 手法，两处各自持有一份（都只有几行胶水代码，不值得为此新增
    跨包耦合）——见该函数 docstring 的完整理由。
    """
    try:
        from app.props import prop_reference_for_episode
    except ImportError:
        return None
    return prop_reference_for_episode(conn, project_id, name, episode_no)


def resolve_segment_prop_manifest_entries(
    prop_entries: list[dict[str, Any]], *, conn, project_id: str, episode_no: int,
) -> list[dict[str, Any]]:
    """把分镜段 ``resources.props``（``_AiResourceProp``: label/description）
    逐条接上 ``app.props`` 的 ready 参考图，供
    ``app.multiview._storyboard_pack_asset_dependencies`` 写进 reference
    manifest（``manifest["props"]``）。ready 判据同
    ``app.multiview.scene_row_for_episode`` 一路的既有用法（``status==
    "ready"`` 且文件真实存在）；查不到/未 ready 时只带 label/description，
    ``ready`` 显式为 False——下游据此判定"这个道具没有可用参考图"，不是
    留空当成有图。``resources_order``（2026-10-01）是这条在 ``prop_entries``
    里的下标（模型声明的重要性顺序，见模块 docstring），原样带出供
    ``prop_library_anchors`` 透传——不重新排序、不去重，逐字保留输入顺序。

    ``prop_revision_id``（2026-10-01，第 1 集第 22 段真实故障追加）是命中行的
    ``prop_references.id``——``app.props.store.upsert_prop_reference`` 每次都是
    先删后插（见其 docstring「覆盖式」），同一道具重新登记外观卡必然拿到新
    id，供 ``app.multiview.manifest_revisions_match`` 据此判定冻结参考图清单
    是否因为外观卡换图而过期；没查到行（``row`` 为 None）时为 None，与
    ``ready=False`` 同义。
    """
    out: list[dict[str, Any]] = []
    for index, entry in enumerate(prop_entries or []):
        label = str(entry.get("label") or "").strip()
        row = _prop_reference_lookup(conn, project_id, label, episode_no) if label else None
        ready = False
        image_path = ""
        if row and str(row["status"] or "") == "ready":
            candidate = str(row["image_path"] or "").strip()
            if candidate and Path(candidate).is_file():
                ready, image_path = True, candidate
        out.append({
            "label": label,
            "description": str(entry.get("description") or ""),
            "ready": ready,
            "image_path": image_path,
            "resources_order": index,
            "prop_revision_id": str(row["id"]) if row else None,
        })
    return out


def manifest_props_signature(manifest: dict[str, Any] | None) -> dict[str, tuple[Any, Any, bool]]:
    """按 label 提取道具条目的选取/版本签名，供
    ``app.multiview.manifest_revisions_match`` 判定冻结参考图清单是否过期。

    ``prop_revision_id`` 换了说明外观卡被重新登记过（旧图已不是当前外观）；
    ``resources_order`` 换了说明超限裁剪/选取顺序会不同；``ready`` 换了说明
    "有没有可用参考图"这件事本身变了。三者任一不同，旧冻结清单在道具这一维
    度上就不再代表当前状态（2026-10-01，第 1 集第 22 段真实故障，见
    ``app.multiview.manifest_revisions_match`` 调用处的完整背景）。"""
    return {
        str(prop.get("label") or ""): (
            prop.get("prop_revision_id"), prop.get("resources_order"), bool(prop.get("ready")),
        )
        for prop in (manifest or {}).get("props") or []
        if isinstance(prop, dict) and prop.get("label")
    }


def prop_library_anchors(manifest_props: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把 ``manifest["props"]``（``resolve_segment_prop_manifest_entries`` 的
    产出）展开成与 ``app.multiview.library_anchor_assets_from_manifest`` 里
    人物/场景锚点同形状的条目，只保留真 ready 且文件存在的道具——同函数对
    人物/场景的既有判据。``resources_order`` 原样透传给锚点字典，供
    ``app.video_modes.asset_lookup._asset_from_path`` 继续带进
    ``ReferenceImageAsset``（见模块 docstring）。
    """
    anchors: list[dict[str, Any]] = []
    for prop in manifest_props or []:
        path = str(prop.get("image_path") or "")
        if not prop.get("ready") or not path or not Path(path).is_file():
            continue
        label = str(prop.get("label") or "")
        anchors.append({
            "entity_type": "prop", "entity_name": label,
            "image_path": path, "purposes": ["qa_anchor", "keyframe_seed"],
            "type": "prop", "source": "asset_library",
            "resources_order": prop.get("resources_order"),
        })
    return anchors
