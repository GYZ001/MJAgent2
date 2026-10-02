"""按段选参考图：人物定妆照全身照/头像照之间的选择策略（纯函数，无 I/O）。

2026-10-01 定妆照双视角改造（用户拍板：不再生成三视角，只生成正面全身照+
正面头像照，按段判断本段穿着是否就是默认造型来决定传哪张）。拆成独立模块、
不内联进 ``app.multiview._storyboard_pack_asset_dependencies``：该函数所在的
``app/multiview.py`` 在 ``app/FILE_CONVENTIONS.toml`` 的 line_count 棘轮基线上
接近顶格（见该文件头部 changelog 历次搬移先例），新增选图逻辑放这里腾行数
空间，同时让选图策略可以独立单测，不必每次都搭建完整的 manifest 依赖解析
上下文。
"""
from __future__ import annotations

from typing import Any


def pick_character_reference_view(
    *,
    wardrobe_matches_default: str,
    portrait_id: str | None,
    front_full_image_path: str,
    ready_views: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """返回本段该送哪张参考图。

    ``wardrobe_matches_default`` 取值见
    ``app.schemas.segment_identity.SegmentCharacter``：
    - ``"yes"``：本段穿着就是人物谱默认造型，送全身照（服装也一并锁定）。
    - ``"no"``/``"unsure"``/其它任何值（含旧数据缺省）：一律当"非默认"保守
      处理，送头像照（只锁长相，服装以本段文字为准）——CLAUDE.md「判据从
      数据推导」「不得兜底填充」：拿不准时错误方向只会是多锁一次长相，不会
      让参考图上的服装压过正文描述的真实穿着。

    ``front_full_image_path`` 为空（角色没有可用全身照）时返回 ``None``，交
    调用方按既有的"无图可用"口径处理（不在这里发明新的兜底）。``ready_views``
    没有 ``face_closeup``（存量角色尚未补出这张图：这条自愈不会被本函数所在
    的 2.x 分镜包主通路自动触发，只能靠「人物谱」手动补生成入口或未来的批量
    预热脚本补齐，见定妆照双视角改造设计文档第 6.3 节）时退回全身照，但文案
    仍切成"只锁长相"——好过维持现状的"硬锁服装"。
    """
    if not front_full_image_path:
        return None
    if wardrobe_matches_default == "yes":
        return {
            "id": portrait_id, "view_role": "front_full", "image_path": front_full_image_path,
            "input_fingerprint": portrait_id, "costume_mode": None,
        }
    closeup = next(
        (v for v in ready_views if v.get("view_role") == "face_closeup" and v.get("image_path")),
        None,
    )
    if closeup is not None:
        return {
            "id": closeup.get("id"), "view_role": "face_closeup", "image_path": closeup["image_path"],
            "input_fingerprint": closeup.get("input_fingerprint") or closeup.get("id"),
            "costume_mode": "neutral",
        }
    return {
        "id": portrait_id, "view_role": "front_full", "image_path": front_full_image_path,
        "input_fingerprint": portrait_id, "costume_mode": "neutral",
    }
