"""道具图人脸判定：供拼图装箱排除含人脸的道具图（见 ``prop_composite_pack.py``）。

Seedance 对"非原始来源"的图生图/像素合成图一律按真人隐私拒收（已实测：定妆照
头部裁切若剥掉原图 APP11/C2PA 段即被拒，见 ``app.portraits.headshot_crop`` 模块
docstring）；拼图是本地像素合成，必然丢失 C2PA——任何一格含真人脸都会让整张
拼图被供应商拒收。因此每张候选道具图入拼图前先判一次"图中是否有人脸（含照片/
画像里的人，不止真人实拍）"，命中就排除在拼图之外，保持单张参与正常优先级
竞争（真实案例：《顾念长安》"旧照片"道具卡本身是两个孩童的合影）。

二值判定，一次视觉模型调用（走 ``app.harness.model_gateway``，与
``app.portraits.headshot_crop``/``app.scene_reverse.judge`` 等既有视觉判定同一
入口）；按图片内容 sha256 缓存（``prop_composite_face_store``），同一张图不
重复判定。判定失败（异常/返回不可解析）一律按"有人脸"处理——fail closed，
宁可一张本来安全的道具图留在单张队列里按原有优先级竞争（可能因此被挤掉），
也不让一张未判定的图悄悄混进拼图触发供应商整段拒收。
"""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

from app import hiagent
from app.harness import model_gateway

from .prop_composite_face_store import get_cached_has_face, set_cached_has_face

_LOGGER = logging.getLogger(__name__)

_PROMPT = (
    "这是一张道具参考图。判断画面里是否出现任何人脸——不论是真人实拍、照片/画像里的人物、"
    "还是雕像/海报上的人脸，只要画面中能看到人脸都算。"
    '只返回一个 JSON 对象：{"has_face": true 或 false}。'
)


def _content_hash(path: str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _parse_has_face(raw: str) -> bool:
    text = (raw or "").strip()
    start, end = text.find("{"), text.rfind("}")
    body = text[start:end + 1] if start >= 0 and end > start else text
    data = json.loads(body)
    value = data["has_face"]
    if not isinstance(value, bool):
        raise ValueError(f"has_face 字段不是布尔值：{value!r}")
    return value


async def _judge_has_face(path: str, *, call_meta: dict[str, Any]) -> bool:
    messages = [
        {"role": "system", "content": "Return exactly one valid JSON object. No Markdown, no prose."},
        {"role": "user", "content": [
            {"type": "text", "text": _PROMPT},
            {"type": "image_url", "image_url": {"url": hiagent.data_url_from_file(path)}},
        ]},
    ]
    raw = await model_gateway.chat(
        messages, temperature=0, max_tokens=100,
        provider=hiagent.active_provider("vlm"),
        call_meta={"kind": "vlm_prop_composite_face_check", **call_meta},
        response_format={"type": "json_object"},
    )
    return _parse_has_face(raw)


async def prop_image_has_face(path: str, *, call_meta: dict[str, Any] | None = None) -> bool:
    """给定一张道具图文件路径，返回"是否含人脸"；判定失败 fail-closed=True
    （见模块文档）。"""
    content_hash = _content_hash(path)
    cached = get_cached_has_face(content_hash)
    if cached is not None:
        return cached
    try:
        has_face = await _judge_has_face(path, call_meta=call_meta or {})
    except Exception as exc:  # noqa: BLE001 fail closed：见模块文档
        _LOGGER.warning("[PROP_COMPOSITE_FACE_CHECK][未判定，按有人脸处理] %s", exc)
        return True
    set_cached_has_face(content_hash, has_face)
    return has_face
