"""首尾帧模式静态边界帧的种子图说明回归（2026-10-02 代码评审发现）。

face_closeup 改成同一角色的 3×3 头像九宫格后，``app.multiview.keyframe_seed_paths``
在该角色本段只被选中 face_closeup 一张图时，仍会把这张九宫格原样当成"强身份
图"喂给 ``app.media_exec.input_first_frame_last._prepare_first_last_mode_inputs``
→ ``app.video_modes.reference_generate._generate_one_reference`` 这条首尾帧静态
边界帧生成路径。该路径唯一的整体说明文案 ``_SEED_USAGE_NOTE`` 若不显式声明
"这张图可能是同一人的九宫格"，模型可能把九格误读成九个人，或把网格画进输出帧
——与 ``app.video_modes.seedance_reference_notes`` 那条已经修好的 REFERENCE_IMAGE_
MODE 说明文案是两条独立通路，必须分别补。
"""
from __future__ import annotations

from app.multiview import keyframe_seed_paths
from app.video_modes.reference_generate import _SEED_USAGE_NOTE


def test_seed_usage_note_explains_headshot_grid_is_one_identity() -> None:
    assert "3x3 grid" in _SEED_USAGE_NOTE
    assert "not nine different people" in _SEED_USAGE_NOTE
    assert "never draw any grid lines" in _SEED_USAGE_NOTE
    # 既有的"每张图是一个独立具名身份"条款必须保留，新句子只是补充条件分支，
    # 不能替换掉原条款（原条款仍对 front_full 等非九宫格种子图成立）。
    assert "never merge, swap, omit, or duplicate identities" in _SEED_USAGE_NOTE


def test_keyframe_seed_paths_selects_sole_face_closeup_view_when_that_is_all_segment_picked(
    tmp_path,
) -> None:
    """角色本段只被选中 face_closeup（头像九宫格）一张图时，keyframe_seed_paths
    仍会把它当该角色唯一可用的"强身份图"选中——这正是首尾帧模式会把九宫格喂进
    静态边界帧生成的真实触发路径，不是假设。"""
    grid_image = tmp_path / "headshot_grid.jpg"
    grid_image.write_bytes(b"fake-nine-grid-bytes")
    manifest = {
        "characters": [{
            "role_kind": "storyboard_pack",
            "selected_views": [{
                "view_role": "face_closeup",
                "image_path": str(grid_image),
            }],
        }],
        "scene": {},
    }

    paths = keyframe_seed_paths(manifest)

    assert paths == [str(grid_image)]
