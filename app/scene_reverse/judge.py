"""场景反打候选图判定：一次视觉模型调用，二值问「是否确实从相反方向拍摄同一空间」。

判定口径 2026-09-27 在 B 沙箱标定：存量 8 组假反打（同方向平移/加框 7 组、左右镜像 1 组）
全部判否——初版只问「方向是否相反」，把左右镜像当成了反打；现版写明镜像、另一个空间
都判否，并要求朝向物件露出相反的一面。代价是偏严：图像模型偶尔画出的真反打若陈设
差异大也会被判否——判严只是少用一张图，判松会把错的空间参考喂给视频模型。

送主视角图与候选反打图各一张，只问朝向，不评画质/构图美观——结论只挂
``scene_reference_views.qa_json.reverse_check``（见 ``app.scene_reverse.evidence``），
不改 ``status``。失败一律放行（``checked=False``），与 ``app.media_exec.subtitle_gate``
「未判定不拦」同一取舍。显式绑定 ``vlm:default``，不像 ``subtitle_gate`` 隐式吃
``text:default``——那是运维现状恰好两个 purpose 绑同一模型，不是契约，不应复制。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from app import hiagent

_LOGGER = logging.getLogger(__name__)

_PROMPT = (
    "图1 是某个空间的建立镜头（主视角）。图2 是一张候选反打图。判断图2 是否确实从与图1 大致相反（约180°）"
    "的方向拍摄同一个空间。依据：①同一空间——材质、陈设风格、色调一致；②方向相反——图1 画面深处的主要元素"
    "（背景墙、门窗、舞台、远处建筑或山体）在图2 里不再位于背景深处，而是在机位身后看不见、或只出现在前景边缘；"
    "图2 的背景是图1 原机位背后的那一侧；有朝向的物件（座椅、柜台、桌子、床）露出与图1 相反的一面（图1 看到椅背，"
    "图2 应看到椅面）。以下情况判否：图2 是图1 同方向的平移、推拉、加框或近似构图；图2 是图1 的左右镜像"
    "（同一批背景元素仍在画面深处，只是左右对调）；图2 看起来是另一个空间。"
    '只返回一个 JSON 对象：{"passed": true/false, "reason": "一句话说明依据"}。'
)


def _build_messages(establishing_url: str, candidate_url: str) -> list[dict[str, Any]]:
    return [
        {"role": "system", "content": "Return exactly one valid JSON object. No Markdown, no prose."},
        {"role": "user", "content": [
            {"type": "text", "text": _PROMPT},
            {"type": "image_url", "image_url": {"url": establishing_url}},
            {"type": "image_url", "image_url": {"url": candidate_url}},
        ]},
    ]


_PASSED_RE = re.compile(r'"passed"\s*:\s*(true|false)\b', re.IGNORECASE)
_REASON_RE = re.compile(r'"reason"\s*:\s*"((?:[^"\\]|\\.)*)"')


def _parse_verdict(raw: str) -> tuple[bool, str]:
    """整体 JSON 合法按结构读；不合法退到正则抓字段（与 subtitle_gate.parse_verdict 同一取舍）。"""
    text = (raw or "").strip()
    start, end = text.find("{"), text.rfind("}")
    body = text[start:end + 1] if start >= 0 and end > start else text
    try:
        data = json.loads(body)
    except ValueError:
        data = None
    if isinstance(data, dict) and "passed" in data:
        passed = data["passed"]
        if isinstance(passed, str) and passed.strip().lower() in {"true", "false"}:
            passed = passed.strip().lower() == "true"
        if not isinstance(passed, bool):  # bool("false") 为真：非布尔一律当解析失败，不判通过
            raise ValueError(f"passed 字段不是布尔值：{passed!r}")
        return passed, str(data.get("reason") or "")
    match = _PASSED_RE.search(body)
    if not match:
        raise ValueError("模型没有返回可解析的 passed 字段")
    reason_match = _REASON_RE.search(body)
    reason = reason_match.group(1).replace('\\"', '"') if reason_match else ""
    return match.group(1).lower() == "true", reason


async def judge_reverse_angle(
    *, establishing_path: str, candidate_path: str, call_meta: dict[str, Any],
) -> dict[str, Any]:
    """判定候选反打图是否确实朝向相反方向；失败返回 ``checked=False``，绝不抛出。"""
    try:
        messages = _build_messages(
            hiagent.data_url_from_file(establishing_path),
            hiagent.data_url_from_file(candidate_path),
        )
        raw = await hiagent.chat(
            messages, temperature=0, max_tokens=400,
            provider=hiagent.active_provider("vlm"),
            call_meta={"kind": "vlm_scene_reverse_judge", **call_meta},
            response_format={"type": "json_object"},
        )
        passed, reason = _parse_verdict(raw)
        return {"checked": True, "passed": passed, "reason": reason, "error": None}
    except Exception as exc:  # noqa: BLE001 未判定放行：见模块文档
        error = f"{type(exc).__name__}: {exc}"[:300]
        _LOGGER.warning("[SCENE_REVERSE_JUDGE][未判定] %s", error)
        return {"checked": False, "passed": None, "reason": "", "error": error}
