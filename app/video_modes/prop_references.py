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
    留空当成有图。
    """
    out: list[dict[str, Any]] = []
    for entry in prop_entries or []:
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
        })
    return out


def prop_library_anchors(manifest_props: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把 ``manifest["props"]``（``resolve_segment_prop_manifest_entries`` 的
    产出）展开成与 ``app.multiview.library_anchor_assets_from_manifest`` 里
    人物/场景锚点同形状的条目，只保留真 ready 且文件存在的道具——同函数对
    人物/场景的既有判据。
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
        })
    return anchors
