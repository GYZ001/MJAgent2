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

实测（2026-10-02 ERR-20261002-de0b34）：供应商可以一次点名多张——``'content[2]'
'content[3]' may contain real person``。``rejected_input_image_index`` 只取第一个，
对"要不要去掉上一段锚点重试"这一个判断够用；但要把人物参考图标签原样展示给用户时
不能漏掉后面那几张，于是加 ``rejected_input_image_indices``（全部、按首次出现顺序、
去重）与 ``rejected_reference_labels``（配合
``shot_versions.image_inputs._seedance_image_input_labels`` 把下标翻成人看得懂的标签，
``content[N]`` 的 0 是提示词，图片从 ``content[1]`` 起按标签顺序排列，故取
``labels[N-1]``）。
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


def rejected_input_image_indices(text: str) -> list[int]:
    """供应商明确拒收了哪几张输入（``content[N]``，0 是提示词）：按首次出现顺序、
    去重返回全部下标；不满足结构判据（code 前缀不符/取不到错误体）时返回空列表。"""
    payload = _embedded_error_object(text)
    if payload is None:
        return []
    code = str(payload.get("code") or "")
    if not code.startswith(INPUT_IMAGE_REJECTION_CODE_PREFIX):
        return []
    indices: list[int] = []
    for match in _CONTENT_LOCATOR.finditer(str(payload.get("message") or "")):
        value = int(match.group(1))
        if value not in indices:
            indices.append(value)
    return indices


def rejected_input_image_index(text: str) -> int | None:
    """供应商明确拒收了第 N 张输入（``content[N]``，0 是提示词）时返回 N（取供应商
    报文里第一个出现的下标），否则 None。"""
    indices = rejected_input_image_indices(text)
    return indices[0] if indices else None


def rejected_reference_labels(text: str, labels: list[dict]) -> list[dict]:
    """把 ``rejected_input_image_indices`` 取到的下标翻译成
    ``_seedance_image_input_labels`` 里对应的标签项，按出现顺序、跳过越界下标
    （旧数据没有标签，或下标超出当前列表长度）。不满足判据/没有可对应的标签时
    返回空列表——调用方据此判断"供应商点名了但我们对不上"，不编造标签。"""
    out: list[dict] = []
    for index in rejected_input_image_indices(text):
        position = index - 1  # content[0] 是提示词，图片从 content[1] 起按标签顺序排列
        if 0 <= position < len(labels):
            out.append(labels[position])
    return out
