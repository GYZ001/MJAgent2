"""道具参考图超出张数上限时的拼图合成（纯像素，PIL，无文字/编号/边框标注）。

背景：参考图总数硬顶 ``max_reference_images()``（Seedance 2.0，见
``app.video_modes.mode_selection``），超限时道具永远排最后被丢
（``app.multiview.ref_pack_priority``）。与其静默丢弃已有参考图的道具，把
"本应丢弃的道具"与"最后一个放得下的单张道具"合成一张拼图占用它原来的那一个
槽位，让本段列出的全部 ready 道具都能送达——见
``app.video_modes.prop_composite_pack`` 的装箱编排。

本模块只负责"给定一组已排好序的 (label, 图片路径)，拼出一张 PNG"这一件事：
布局、留白、内容寻址缓存、落盘。不做人脸判定（见 ``prop_composite_face_
check.py``）、不做装箱/超限判定（见 ``prop_composite_pack.py``）——三者各自
都在 CLAUDE.md「单函数 ≤50 代码行」的预算内，单一职责。

布局固定取 2×2 / 2×3 / 3×3 三档（按成员数取够用的最小一档，封顶 9 件——3×3
是本模块支持的最大布局，再多需要新增 4×3 等布局，不在本次范围内，超出部分
由调用方按 ``ref_pack_priority`` 丢弃）。每格统一 480×854（9:16，与
``app.config.REF_IMAGE_SIZE`` 单图宽高比一致；480px 短边取"标准单图宽度
1440px 的三分之一"——3×3 最密布局下仍清楚可辨材质/颜色，同时控制住最终画布
与文件体积），格间留白 24px，画布背景纯白——不画任何文字、编号、边框或分割
线（视频模型会把画面上出现的文字原样画进视频，本仓已有字幕/画面文字闸门拦
这类问题，拼图不能主动制造一个新的违规来源）。成员图按"contain"方式等比缩
放后居中放入各自格子，不裁切、不变形。

内容寻址：输出文件名取"各成员文件内容 sha256（已排好最终呈现顺序）+ 布局
参数"的 sha256，同一组成员、同一顺序、同一天总是复用同一张文件，不重复合
成、不重复占盘；成员集合或顺序变化则是不同的文件名，旧文件不清理（与
``app.props.store`` 的道具参考图一样，磁盘占用随道具库增长，不在本次范围内
新增 GC）。
"""
from __future__ import annotations

import hashlib
import io
from pathlib import Path
from typing import Sequence

from PIL import Image

from app.atomic_io import atomic_write_bytes

CELL_W = 480
CELL_H = 854  # 480 * 16 / 9（取整），与 REF_IMAGE_SIZE 的 9:16 单图比例一致
GRID_GAP = 24
CANVAS_BACKGROUND = (255, 255, 255)
# 只支持这三档布局（派单冻结）；超过 9 件由调用方在拼图之前按优先级丢弃。
MAX_PROP_COMPOSITE_MEMBERS = 9


def composite_grid_dims(member_count: int) -> tuple[int, int]:
    """成员数 -> (rows, cols)；只在 2×2/2×3/3×3 三档中取够用的最小一档。"""
    if member_count <= 4:
        return 2, 2
    if member_count <= 6:
        return 2, 3
    return 3, 3


def member_fingerprint(path: str) -> str:
    """单个成员源图的内容指纹（sha256）；拼图自身的内容寻址 key 由
    ``composite_fingerprint`` 在此基础上再叠加布局参数算出。"""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def composite_fingerprint(member_fingerprints: Sequence[str]) -> str:
    """成员指纹 + 布局参数的内容寻址 key；成员集合/顺序不同则 key 不同。"""
    rows, cols = composite_grid_dims(len(member_fingerprints))
    material = "|".join([
        *member_fingerprints, f"{rows}x{cols}", f"{CELL_W}x{CELL_H}", str(GRID_GAP),
    ])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _composite_path(project_id: str, fingerprint: str) -> Path:
    from app.props.store import prop_ref_dir  # 只有本函数用到，不提到模块顶层常驻

    d = prop_ref_dir(project_id) / "composites"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{fingerprint[:32]}.png"


def _paste_contained(canvas: Image.Image, member_path: str, *, x: int, y: int) -> None:
    with Image.open(member_path) as src:
        img = src.convert("RGB")
        ratio = min(CELL_W / img.width, CELL_H / img.height)
        size = (max(1, int(img.width * ratio)), max(1, int(img.height * ratio)))
        resized = img.resize(size, Image.LANCZOS)
    offset = (x + (CELL_W - resized.width) // 2, y + (CELL_H - resized.height) // 2)
    canvas.paste(resized, offset)


def build_prop_composite_image(
    project_id: str, members: list[tuple[str, str]],
) -> tuple[str, list[str]]:
    """``members``：已按最终呈现顺序排好的 ``(label, image_path)``，``label``
    只用于调用方记录（冻结清单、参考说明），本函数不把文字画进图里。返回
    ``(拼图文件路径, 按同一顺序的成员内容指纹列表)``。

    内容寻址：已存在同指纹文件直接复用，不重复合成（见模块 docstring）。
    """
    fingerprints = [member_fingerprint(path) for _, path in members]
    fingerprint = composite_fingerprint(fingerprints)
    out_path = _composite_path(project_id, fingerprint)
    if out_path.is_file():
        return str(out_path), fingerprints
    rows, cols = composite_grid_dims(len(members))
    canvas_w = cols * CELL_W + (cols + 1) * GRID_GAP
    canvas_h = rows * CELL_H + (rows + 1) * GRID_GAP
    canvas = Image.new("RGB", (canvas_w, canvas_h), CANVAS_BACKGROUND)
    for index, (_, path) in enumerate(members):
        row, col = divmod(index, cols)
        x = GRID_GAP + col * (CELL_W + GRID_GAP)
        y = GRID_GAP + row * (CELL_H + GRID_GAP)
        _paste_contained(canvas, path, x=x, y=y)
    buffer = io.BytesIO()
    canvas.save(buffer, format="PNG")
    atomic_write_bytes(out_path, buffer.getvalue())
    return str(out_path), fingerprints
