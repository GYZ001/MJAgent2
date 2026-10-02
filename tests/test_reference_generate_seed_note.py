"""首尾帧模式静态边界帧的种子图说明回归（2026-10-02）。

face_closeup 已经从"图生图（曾经试过 3×3 头像九宫格）"改成从全身定妆照纯像素
裁切（``app.portraits.headshot_crop``），不再是网格图，``_SEED_USAGE_NOTE`` 不
应再残留九宫格措辞。``keyframe_seed_paths`` 选中单张 face_closeup 视角作为角色
"强身份图"喂给首尾帧模式静态边界帧生成这条行为本身不受影响，继续覆盖。
"""
from __future__ import annotations

from app.multiview import keyframe_seed_paths
from app.video_modes.reference_generate import _SEED_USAGE_NOTE


def test_seed_usage_note_has_no_leftover_grid_wording() -> None:
    assert "3x3 grid" not in _SEED_USAGE_NOTE
    assert "nine" not in _SEED_USAGE_NOTE
    # 既有的"每张图是一个独立具名身份"条款必须保留，它对任何种子图（含头像
    # 裁切图）都成立，不是只为九宫格服务。
    assert "never merge, swap, omit, or duplicate identities" in _SEED_USAGE_NOTE


def test_keyframe_seed_paths_selects_sole_face_closeup_view_when_that_is_all_segment_picked(
    tmp_path,
) -> None:
    """角色本段只被选中 face_closeup（定妆照头部裁切）一张图时，
    keyframe_seed_paths 仍会把它当该角色唯一可用的"强身份图"选中。"""
    crop_image = tmp_path / "headshot_crop.jpg"
    crop_image.write_bytes(b"fake-headshot-crop-bytes")
    manifest = {
        "characters": [{
            "role_kind": "storyboard_pack",
            "selected_views": [{
                "view_role": "face_closeup",
                "image_path": str(crop_image),
            }],
        }],
        "scene": {},
    }

    paths = keyframe_seed_paths(manifest)

    assert paths == [str(crop_image)]
