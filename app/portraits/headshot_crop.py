"""人物头像照：从全身定妆照裁出头颈部位，不再调用生图模型。

为什么裁切而不是生成（2026-10-02 在 B 上实测，结论已定，不要再验证）：
视频供应商 Seedance 的真人检测对图生图（image_edit）产出的人物图——无论是单张
头像近景还是曾经试过的 3×3 九宫格——一律判定 400 InputImageSensitiveContentDetected.
PrivacyInformation 拒收；同一批全身定妆照是文生图产物，放行；进一步实测锁定根因
在"图生图"这一步本身，不在头像构图：同一张全身定妆照的头颈裁切，只要保留原图
JPEG 的 APP11（0xFFEB，C2PA/JUMBF）鉴真数据段就依然放行，剥掉这一段就会被拒。
因此头像照改为纯像素裁切——不调用任何生图模型，天然不会触发这条真人检测。

为什么要原样拷贝源图的 APP11 段：裁切图只是同一张"AI 生成"全身定妆照的像素
子集，没有引入任何新像素，源图里那条"这是 AI 生成内容"的 C2PA/JUMBF 标识对
裁切后的图依然如实成立。代价是已知且接受的：C2PA 清单里按整图计算的哈希/
签名绑定裁切后会失配（遵循规范的读取器可能报"清单校验失败"而不是单纯"无
标识"）——这是已知限制，不是未发现的缺陷。

源图本就没有 APP11 段时（PNG、旧图、或任何其它原因没嵌入）的后果：裁切图也
不会有，返回的 qa 字典里 ``provenance_preserved=False`` 并记一条 warning 作为
可见信号；不做任何补偿性兜底（例如伪造一段 APP11），避免给出错误的溯源断言。

已知的裁切观感限制（2026-10-02 真实定妆照实测发现）：下边界公式在衣领本就
贴近下巴（立领、衬衫扣到最上）的定妆照上会被 ``clothing_top_y`` 这一项主导，
产出的下边距远小于预期的「头框高 25%」，裁切图几乎没有颈部、贴着下巴线。这
不违反「不得带入衣物像素」这条硬约束——衣领检测越保守，裁切就越贴脸，二者
是同一安全边界的两面，本函数不会为了好看而牺牲这条硬约束去贴近衣领——但
观感确实偏局促。不做静默接受：``qa["neck_margin_ratio"]`` 低于阈值时记一条
可见信号（warning），供后续抽查，不自动调整裁切框。
"""
from __future__ import annotations

import io
import json
import logging
import struct
from pathlib import Path
from typing import Any

from PIL import Image

from app import hiagent
from app.atomic_io import atomic_write_bytes
from app.harness import model_gateway

_LOGGER = logging.getLogger(__name__)

HEADSHOT_CROP_DESCRIPTOR = "headshot_crop_v1"

# 裁切框下边距占头框高度的比例低于此值，判定为「贴着下巴/衣领裁切」，仅用于
# 记录可见信号供后续抽查，不参与裁切决策、不触发重算或兜底。
_TIGHT_NECK_MARGIN_RATIO = 0.15

_GEOMETRY_PROMPT = (
    "这是一张人物全身定妆照，用于裁出头像照。请找出：\n"
    "①head_box——头部范围的矩形框，从头发最顶端到下巴底部，左右边界要包含头发"
    "（含刘海、蓬松发丝），不是只包住脸；\n"
    "②clothing_top_y——脖子附近衣服/衣领最高点（画面里看得到的最靠上的衣物像素）"
    "在整图高度上的位置；如果颈部以下被遮挡或画面本身没有露出衣物，按最可能的"
    "衣领/领口位置估计。\n"
    "所有坐标用归一化比例表示（0 到 1 之间，相对整张图片的宽/高），head_box 顺序"
    "为 [x0, y0, x1, y1]（左上角到右下角）。"
    '只返回一个 JSON 对象：{"head_box": [x0, y0, x1, y1], "clothing_top_y": y}。'
)


def _build_geometry_messages(image_url: str) -> list[dict[str, Any]]:
    return [
        {"role": "system", "content": "Return exactly one valid JSON object. No Markdown, no prose."},
        {"role": "user", "content": [
            {"type": "text", "text": _GEOMETRY_PROMPT},
            {"type": "image_url", "image_url": {"url": image_url}},
        ]},
    ]


def _parse_geometry_json(raw: str) -> dict[str, Any]:
    text = (raw or "").strip()
    start, end = text.find("{"), text.rfind("}")
    body = text[start:end + 1] if start >= 0 and end > start else text
    try:
        data = json.loads(body)
    except ValueError as exc:
        raise ValueError(f"模型没有返回合法 JSON：{exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("模型返回的不是 JSON 对象")
    return data


def _validate_geometry(data: dict[str, Any], *, img_w: int, img_h: int) -> dict[str, Any]:
    """代码核验头部几何：坐标范围、头框比例、衣领位置相对关系。

    不合格一律抛 ValueError，交调用方决定是否重问；绝不把越界坐标悄悄夹回合法
    范围——那是兜底，会用一张几何错误的裁切图冒充"核验通过"。
    """
    head_box = data.get("head_box")
    if not (isinstance(head_box, (list, tuple)) and len(head_box) == 4):
        raise ValueError(f"head_box 字段缺失或不是 4 个数：{head_box!r}")
    try:
        x0, y0, x1, y1 = (float(v) for v in head_box)
        clothing_top_y = float(data.get("clothing_top_y"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"head_box/clothing_top_y 不是数字：{data!r}") from exc
    for name, value in (
        ("x0", x0), ("y0", y0), ("x1", x1), ("y1", y1), ("clothing_top_y", clothing_top_y),
    ):
        if not (0.0 <= value <= 1.0):
            raise ValueError(f"{name}={value} 不在 [0,1] 归一化范围内")
    if not (x0 < x1 and y0 < y1):
        raise ValueError(f"head_box 不是合法矩形：x0={x0} x1={x1} y0={y0} y1={y1}")
    height_ratio = y1 - y0
    if not (0.03 <= height_ratio <= 0.40):
        raise ValueError(f"头框高度占整图比例 {height_ratio:.3f} 超出 3%-40% 合理范围")
    head_w_px, head_h_px = (x1 - x0) * img_w, (y1 - y0) * img_h
    aspect = head_w_px / head_h_px if head_h_px else 0.0
    if not (0.45 <= aspect <= 1.4):
        raise ValueError(f"头框像素宽高比 {aspect:.2f} 超出 0.45-1.4 合理范围")
    if not (clothing_top_y > y0):
        raise ValueError(f"clothing_top_y={clothing_top_y} 不在头框顶部 y0={y0} 以下")
    return {"head_box": [x0, y0, x1, y1], "clothing_top_y": clothing_top_y}


async def _detect_head_geometry(
    source_path: str, *, img_w: int, img_h: int, call_meta: dict[str, Any],
) -> dict[str, Any]:
    """一次 VLM 调用定位头部几何；不合格重问一次，仍不合格抛异常（不兜底）。"""
    image_url = hiagent.data_url_from_file(source_path)
    messages = _build_geometry_messages(image_url)
    last_error: Exception | None = None
    for attempt in (1, 2):
        # 走 model_gateway（app/portraits 下的唯一模型入口，见 scripts/check_contract_surface.py 的 FORBIDDEN）：带追踪元数据与网关重试
        raw = await model_gateway.chat(
            messages, temperature=0, max_tokens=300,
            provider=hiagent.active_provider("vlm"),
            call_meta={"kind": "vlm_headshot_crop_geometry", "attempt": attempt, **call_meta},
            response_format={"type": "json_object"},
        )
        try:
            return _validate_geometry(_parse_geometry_json(raw), img_w=img_w, img_h=img_h)
        except ValueError as exc:
            last_error = exc
            _LOGGER.warning("[HEADSHOT_CROP][第 %d 次几何核验未通过] %s", attempt, exc)
    raise ValueError(
        f"头像裁切几何核验连续两次未通过，无法从该定妆照裁出头像照：{last_error}"
    ) from last_error


def _compute_crop_box_px(
    head_box: list[float], clothing_top_y: float, *, img_w: int, img_h: int,
) -> tuple[int, int, int, int]:
    """裁切框：左右各外扩头框宽 25%、上外扩头框高 10%；下边界取"下巴+头框高
    25%"与"clothing_top_y-头框高2%"中更靠上的一个，但不高于头框 80% 高度处；
    全部夹在图内；宽度不足高度 0.8 倍时左右对称加宽（同样夹在图内）。

    底线是头框 80% 处而不是下巴（2026-10-02 B 上顾屿实测）：立领衬衫的领尖比
    视觉模型估的下巴还高（模型给的头框下沿偏松），按「不高于下巴」会把灰领尖
    带进头像照，视频里就可能把衬衫领画到别的衣服里。衣领位置是更直接的证据，
    以它为准；80% 底线保证衣领估得过高时最多裁到下巴附近，不伤五官。"""
    x0, y0, x1, y1 = head_box
    px0, py0, px1, py1 = x0 * img_w, y0 * img_h, x1 * img_w, y1 * img_h
    head_w, head_h = px1 - px0, py1 - py0
    chin_y = py1
    left = px0 - head_w * 0.25
    right = px1 + head_w * 0.25
    top = py0 - head_h * 0.10
    bottom = min(chin_y + head_h * 0.25, clothing_top_y * img_h - head_h * 0.02)
    bottom = max(bottom, py0 + head_h * 0.8)
    left, top = max(0.0, left), max(0.0, top)
    right, bottom = min(float(img_w), right), min(float(img_h), bottom)
    width, height = right - left, bottom - top
    if height > 0 and width < height * 0.8:
        deficit = height * 0.8 - width
        left -= deficit / 2
        right += deficit / 2
        if left < 0:
            right += -left
            left = 0.0
        if right > img_w:
            left -= right - img_w
            right = float(img_w)
        left, right = max(0.0, left), min(float(img_w), right)
    return (round(left), round(top), round(right), round(bottom))


def _neck_margin_ratio(
    head_box: list[float], box: tuple[int, int, int, int], *, img_h: int,
) -> float:
    """裁切框下边距相对头框高度的占比：越小说明越贴着下巴/衣领裁切。纯只读
    指标，不反过来影响 `_compute_crop_box_px` 的裁切决策。"""
    _, y0, _, y1 = head_box
    head_h_px = (y1 - y0) * img_h
    if head_h_px <= 0:
        return 0.0
    chin_y_px = y1 * img_h
    return (box[3] - chin_y_px) / head_h_px


def _render_crop(image: Image.Image, box: tuple[int, int, int, int]) -> Image.Image:
    cropped = image.crop(box)
    long_side = max(cropped.size)
    if 0 < long_side < 768:
        scale = 768 / long_side
        new_size = (max(1, round(cropped.width * scale)), max(1, round(cropped.height * scale)))
        cropped = cropped.resize(new_size, Image.Resampling.LANCZOS)
    return cropped


# ---------- JPEG APP11（C2PA/JUMBF）段的纯字节搬运 ----------
# 只处理 PIL/主流编码器会产生的标准 marker 结构（SOI 后紧跟不含填充字节的
# marker 序列），不处理 0xFF 填充字节这类生产中不会出现的边界。

_SOI = b"\xff\xd8"
_MARKER_SOS = 0xDA
_MARKER_APP0 = 0xE0
_MARKER_APP11 = 0xEB


def _iter_jpeg_segments(data: bytes):
    pos = 2
    size = len(data)
    while pos + 4 <= size:
        if data[pos] != 0xFF:
            break
        marker = data[pos + 1]
        if marker in (0xD8, 0xD9, 0x01) or 0xD0 <= marker <= 0xD7:
            pos += 2
            continue
        if marker == _MARKER_SOS:
            break
        length = struct.unpack(">H", data[pos + 2:pos + 4])[0]
        end = pos + 2 + length
        if end > size:
            break
        yield marker, data[pos:end]
        pos = end


def _extract_app11_segments(source_bytes: bytes) -> list[bytes]:
    if source_bytes[:2] != _SOI:
        return []
    return [segment for marker, segment in _iter_jpeg_segments(source_bytes) if marker == _MARKER_APP11]


def _insert_app11_segments(jpeg_bytes: bytes, app11_segments: list[bytes]) -> bytes:
    """把 APP11 段原样插到输出 JPEG 的 SOI/APP0 之后：纯字节拼接，不重新编码
    既有 segment，不触碰压缩扫描数据。"""
    if not app11_segments:
        return jpeg_bytes
    if jpeg_bytes[:2] != _SOI:
        raise ValueError("输出不是合法 JPEG（缺少 SOI）")
    pos = 2
    if jpeg_bytes[pos:pos + 2] == bytes((0xFF, _MARKER_APP0)) and pos + 4 <= len(jpeg_bytes):
        length = struct.unpack(">H", jpeg_bytes[pos + 2:pos + 4])[0]
        pos += 2 + length
    return jpeg_bytes[:pos] + b"".join(app11_segments) + jpeg_bytes[pos:]


async def crop_headshot_from_portrait(
    source_path: str, *, dest_path: str, call_meta: dict[str, Any],
) -> dict[str, Any]:
    """从全身定妆照裁出头颈部位头像照；不调用任何生图模型。

    返回 qa 字典：``provenance_preserved``、``head_box``、``clothing_top_y``、
    ``crop_box_px``、``output_size``、``source_path``、``neck_margin_ratio``，
    供调用方写进 ``character_portrait_views.qa_json``。
    """
    source_bytes = Path(source_path).read_bytes()
    with Image.open(source_path) as opened:
        opened.load()
        image = opened.convert("RGB")
    img_w, img_h = image.size
    geometry = await _detect_head_geometry(source_path, img_w=img_w, img_h=img_h, call_meta=call_meta)
    box = _compute_crop_box_px(geometry["head_box"], geometry["clothing_top_y"], img_w=img_w, img_h=img_h)
    cropped = _render_crop(image, box)
    buffer = io.BytesIO()
    cropped.save(buffer, format="JPEG", quality=95)
    app11_segments = _extract_app11_segments(source_bytes)
    output_bytes = _insert_app11_segments(buffer.getvalue(), app11_segments)
    with Image.open(io.BytesIO(output_bytes)) as verify:
        verify.load()
    atomic_write_bytes(dest_path, output_bytes)
    provenance_preserved = bool(app11_segments)
    if not provenance_preserved:
        _LOGGER.warning(
            "[HEADSHOT_CROP][无 C2PA 溯源] 源图 %s 没有 APP11 段，裁切图无法继承溯源标识",
            source_path,
        )
    neck_margin_ratio = _neck_margin_ratio(geometry["head_box"], box, img_h=img_h)
    if neck_margin_ratio < _TIGHT_NECK_MARGIN_RATIO:
        _LOGGER.warning(
            "[HEADSHOT_CROP][颈部余量偏紧] 源图 %s 下边距仅占头框高 %.1f%%（阈值 "
            "%.0f%%），衣领位置靠近下巴导致裁切贴脸，建议后续抽查",
            source_path, neck_margin_ratio * 100, _TIGHT_NECK_MARGIN_RATIO * 100,
        )
    return {
        "provenance_preserved": provenance_preserved,
        "head_box": geometry["head_box"],
        "clothing_top_y": geometry["clothing_top_y"],
        "crop_box_px": list(box),
        "output_size": list(cropped.size),
        "source_path": source_path,
        "neck_margin_ratio": round(neck_margin_ratio, 3),
    }
