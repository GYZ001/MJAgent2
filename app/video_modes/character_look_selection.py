"""按段选参考图：人物定妆照全身照/本段造型照之间的选择策略（纯函数，无 I/O）。

2026-10-01 定妆照双视角改造（用户拍板：不再生成三视角，只生成正面全身照+
正面头像照，按段判断本段穿着是否就是默认造型来决定传哪张）。拆成独立模块、
不内联进 ``app.multiview._storyboard_pack_asset_dependencies``：该函数所在的
``app/multiview.py`` 在 ``app/FILE_CONVENTIONS.toml`` 的 line_count 棘轮基线上
接近顶格（见该文件头部 changelog 历次搬移先例），新增选图逻辑放这里腾行数
空间，同时让选图策略可以独立单测，不必每次都搭建完整的 manifest 依赖解析
上下文。

2026-10-02（人物造型照）：头像九宫格（``face_closeup``）改用于人物谱展示，
**不再**作为视频参考图发送——Seedance 对单张大头近景与头像九宫格都判定
``InputImageSensitiveContentDetected.PrivacyInformation``（真人隐私疑似）
拒收，正面全身照（脸占比小）此前 142 次全部放行。非默认造型段改为优先发送
``app.video_modes.character_look_views`` 生成的「本段造型照」（同一张脸、
本段这身衣服的正面全身照）；尚未生成好时退回全身定妆照 + 服装中性化文案，
与此前对 face_closeup 缺失的退路同构，只是不再有九宫格这一级中间退路。
"""
from __future__ import annotations

from typing import Any


def pick_character_reference_view(
    *,
    wardrobe_matches_default: str,
    portrait_id: str | None,
    front_full_image_path: str,
    look_view: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """返回本段该送哪张参考图。

    ``wardrobe_matches_default`` 取值见
    ``app.schemas.segment_identity.SegmentCharacter``：
    - ``"yes"``：本段穿着就是人物谱默认造型，送全身照（服装也一并锁定）。
    - ``"no"``/``"unsure"``/其它任何值（含旧数据缺省）：一律当"非默认"保守
      处理——CLAUDE.md「判据从数据推导」「不得兜底填充」：拿不准时唯一不会
      让参考图上的服装压过正文真实穿着的做法，是不要假定参考图上的服装就是
      对的。

    ``front_full_image_path`` 为空（角色没有可用全身照）时返回 ``None``，交
    调用方按既有的"无图可用"口径处理（不在这里发明新的兜底）。``look_view``
    是调用方按本段 wardrobe 文本查到的 ready 造型照（``None`` 表示尚未生成好
    或本段没有可判断的 wardrobe 文本）：有则优先发送——同一张脸、本段这身
    衣服，脸与服装都以它为准（``costume_mode=None``）；没有则退回全身定妆照，
    文案切成"只锁长相，服装以本段文字为准"（``costume_mode="neutral"``）——
    调用方据此判断是否需要给用户留一条"造型照未生成，已退回定妆照"的可见提示。
    """
    if not front_full_image_path:
        return None
    if wardrobe_matches_default == "yes":
        return {
            "id": portrait_id, "view_role": "front_full", "image_path": front_full_image_path,
            "input_fingerprint": portrait_id, "costume_mode": None,
        }
    if look_view is not None and look_view.get("image_path"):
        return {
            "id": look_view.get("id"), "view_role": "look", "image_path": look_view["image_path"],
            "input_fingerprint": look_view.get("input_fingerprint") or look_view.get("id"),
            "costume_mode": None,
        }
    return {
        "id": portrait_id, "view_role": "front_full", "image_path": front_full_image_path,
        "input_fingerprint": portrait_id, "costume_mode": "neutral",
    }
