"""供应商「输入图被拒」的结构化定位：哪一张输入图被判定为敏感。

实测（2026-09-05 我欲封天第 15 集第 16/17 镜）：Seedance 轮询返回
``{"error": {"code": "", "message": "Error code: 400 - {\\"message\\": \\"The request failed
because the input image 'content[3]' may contain sensitive information. Request id: …\\",
\\"type\\": \\"BadRequest\\", \\"code\\": \\"InputImageSensitiveContentDetected\\", …}"}}``
——外层 ``code`` 为空，供应商自己的错误体以 JSON 字符串嵌在 ``message`` 里。两镜被拒的
``content[N]`` 都对应上一段（第 15 镜成片）取出的同一张参考帧：拒的不是本镜内容，而是
串接用的锚点帧；照常按「同一镜头 3 个独立任务相同拒绝」判成模型拒绝，会把后面整条链
（每镜都挂同一个锚点）一镜一镜地全部判掉。

判据只读供应商自己的结构化字段：``code`` 以 ``InputImageSensitiveContentDetected`` 开头
（供应商发布的错误分类，闭集），再从 ``message`` 里取 ``content[N]`` 这个机器格式的位置
定位符——它不是自然语言措辞，而是请求体 ``content[]`` 数组的下标。不解析任何其它词。
与 ``hiagent_input_image_privacy`` 一样不依赖 ``app.hiagent``，避免成环。
"""
from __future__ import annotations

import json
import re

INPUT_IMAGE_REJECTION_CODE_PREFIX = "InputImageSensitiveContentDetected"
_CONTENT_LOCATOR = re.compile(r"content\[(\d+)\]")


def _embedded_error_object(text: str) -> dict | None:
    """取出文本里供应商的错误体：可能整段就是 JSON，也可能是「Error code: 400 - {…}」。"""
    raw = str(text or "").strip()
    start = raw.find("{")
    if start < 0:
        return None
    try:
        payload = json.loads(raw[start:])
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    inner = payload.get("error")
    if isinstance(inner, dict):
        # 轮询响应形态：外层 error.code 为空，真正的错误体嵌在 error.message 字符串里。
        nested = _embedded_error_object(str(inner.get("message") or ""))
        if nested is not None:
            return nested
        return inner
    return payload


def rejected_input_image_index(text: str) -> int | None:
    """供应商明确拒收了第 N 张输入（``content[N]``，0 是提示词）时返回 N，否则 None。"""
    payload = _embedded_error_object(text)
    if payload is None:
        return None
    code = str(payload.get("code") or "")
    if not code.startswith(INPUT_IMAGE_REJECTION_CODE_PREFIX):
        return None
    match = _CONTENT_LOCATOR.search(str(payload.get("message") or ""))
    return int(match.group(1)) if match else None
